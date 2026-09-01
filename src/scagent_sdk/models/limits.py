"""Discover context limits from the model endpoint instead of assuming one global ceiling."""

from __future__ import annotations

import importlib
import io
import json
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import Any, Protocol

from scagent_sdk.models.profile import ModelProfile

_CONTEXT_KEYS = (
    "max_model_len",
    "context_window",
    "context_length",
    "max_context_length",
    "max_input_tokens",
    "max_position_embeddings",
    "max_tokens",
)
_OUTPUT_KEYS = ("max_output_tokens", "max_completion_tokens")


class _HTTPResponse(Protocol):
    status: int
    body: bytes
    url: str


class ModelLimitHTTPTransport(Protocol):
    def request(
        self,
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> _HTTPResponse: ...


@dataclass(frozen=True)
class _UrllibResponse:
    status: int
    body: bytes
    url: str


class UrllibModelLimitTransport:
    def request(
        self,
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> _UrllibResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _UrllibResponse(
                    status=int(response.status),
                    body=response.read(),
                    url=response.geturl(),
                )
        except urllib.error.HTTPError as exc:
            return _UrllibResponse(status=exc.code, body=exc.read(), url=url)


@dataclass(frozen=True)
class ModelLimits:
    context_window_tokens: int | None
    max_output_tokens: int | None
    source: str
    advertised_model: str | None = None
    endpoint: str | None = None

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "context_window_tokens": self.context_window_tokens,
            "max_output_tokens": self.max_output_tokens,
            "source": self.source,
            "advertised_model": self.advertised_model,
            "endpoint": self.endpoint,
        }


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _first_limit(mapping: Any, keys: tuple[str, ...]) -> int | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = _positive_int(mapping.get(key))
        if value is not None:
            return value
    for nested_key in ("model_info", "metadata", "capabilities"):
        value = _first_limit(mapping.get(nested_key), keys)
        if value is not None:
            return value
    return None


def _model_names(value: str) -> set[str]:
    names = {value, value.rsplit("/", 1)[-1]}
    if ":" in value:
        names.add(value.rsplit(":", 1)[-1])
    return {name.casefold() for name in names if name}


def _matching_model(items: Any, wanted: str) -> dict[str, Any] | None:
    if not isinstance(items, list):
        return None
    wanted_names = _model_names(wanted)
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = item.get("id") or item.get("model_name") or item.get("model")
        if isinstance(candidate, str) and _model_names(candidate) & wanted_names:
            return item
    return None


class ModelLimitResolver:
    """Resolve a profile alias through LiteLLM and, when needed, its upstream server."""

    def __init__(
        self,
        profile: ModelProfile,
        *,
        transport: ModelLimitHTTPTransport | None = None,
        timeout: float = 5.0,
        environ: dict[str, str] | None = None,
    ):
        self.profile = profile
        self.transport = transport or UrllibModelLimitTransport()
        self.timeout = timeout
        self.environ = environ

    def _headers(self) -> dict[str, str]:
        key = self.profile.resolve_api_key(self.environ)
        return {
            "authorization": f"Bearer {key}",
            "x-api-key": key,
            "content-type": "application/json",
        }

    def _get(self, url: str, *, authenticated: bool = True) -> dict[str, Any] | None:
        try:
            response = self.transport.request(
                url=url,
                method="GET",
                headers=self._headers() if authenticated else {"content-type": "application/json"},
                body=None,
                timeout=self.timeout,
            )
        except (OSError, ValueError):
            return None
        if not 200 <= response.status < 300:
            return None
        try:
            value = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _limits_from_item(
        item: dict[str, Any],
        *,
        source: str,
        endpoint: str,
        fallback_output: int | None,
    ) -> ModelLimits | None:
        context = _first_limit(item, _CONTEXT_KEYS)
        output = _first_limit(item, _OUTPUT_KEYS) or fallback_output
        if context is None:
            return None
        advertised = item.get("id") or item.get("model_name") or item.get("model")
        # Prefer the concrete upstream checkpoint over the routing alias: LiteLLM's
        # /model/info reports the alias as `model_name` but carries the real model in
        # litellm_params.model (e.g. "openai/Qwen3.8-27B"). Strip the "provider/" prefix
        # so it reads like the raw id an OpenAI /models endpoint advertises.
        params = item.get("litellm_params")
        if isinstance(params, dict):
            concrete = params.get("model")
            if isinstance(concrete, str) and concrete:
                advertised = concrete.split("/", 1)[-1] if "/" in concrete else concrete
        return ModelLimits(
            context_window_tokens=context,
            max_output_tokens=output,
            source=source,
            advertised_model=advertised if isinstance(advertised, str) else None,
            endpoint=endpoint,
        )

    def resolve(self) -> ModelLimits:
        if self.profile.base_url:
            base = self.profile.base_url.rstrip("/")
            info_url = base + "/model/info"
            info_payload = self._get(info_url)
            info_item = (
                _matching_model(info_payload.get("data"), self.profile.model)
                if info_payload is not None
                else None
            )
            if info_item is not None:
                direct = self._limits_from_item(
                    info_item,
                    source="litellm:model_info",
                    endpoint=info_url,
                    fallback_output=self.profile.max_output_tokens,
                )
                if direct is not None:
                    return direct
                params = info_item.get("litellm_params")
                if isinstance(params, dict):
                    upstream = params.get("api_base")
                    concrete = params.get("model")
                    if isinstance(upstream, str) and isinstance(concrete, str):
                        models_url = upstream.rstrip("/") + "/models"
                        payload = self._get(models_url, authenticated=False)
                        item = (
                            _matching_model(payload.get("data"), concrete)
                            if payload is not None
                            else None
                        )
                        if item is not None:
                            resolved = self._limits_from_item(
                                item,
                                source="upstream:models",
                                endpoint=models_url,
                                fallback_output=self.profile.max_output_tokens,
                            )
                            if resolved is not None:
                                return resolved

            model_urls = (
                (base + "/models",)
                if base.endswith("/v1")
                else (base + "/v1/models", base + "/models")
            )
            for models_url in model_urls:
                payload = self._get(models_url)
                item = (
                    _matching_model(payload.get("data"), self.profile.model)
                    if payload is not None
                    else None
                )
                if item is not None:
                    resolved = self._limits_from_item(
                        item,
                        source="provider:models",
                        endpoint=models_url,
                        fallback_output=self.profile.max_output_tokens,
                    )
                    if resolved is not None:
                        return resolved

        # Most hosted model-list APIs identify models but do not publish context metadata.
        # LiteLLM's installed provider registry is a model-specific (not global) fallback for
        # those cloud APIs. It is intentionally below live endpoint discovery.
        try:
            litellm = importlib.import_module("litellm")
            # Some LiteLLM versions print provider-help banners for an unknown model. Discovery
            # is best-effort and runs behind the terminal status line, so keep that library noise
            # out of the user's resume UI.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                model_info = litellm.get_model_info(self.profile.model)
        except Exception:
            model_info = None
        if isinstance(model_info, dict):
            context = _first_limit(model_info, _CONTEXT_KEYS)
            output = _first_limit(model_info, _OUTPUT_KEYS) or self.profile.max_output_tokens
            if context is not None:
                return ModelLimits(
                    context_window_tokens=context,
                    max_output_tokens=output,
                    source="provider:model_registry",
                    advertised_model=self.profile.model,
                )

        return ModelLimits(
            context_window_tokens=self.profile.context_window_fallback,
            max_output_tokens=self.profile.max_output_tokens,
            source=(
                "profile:fallback"
                if self.profile.context_window_fallback is not None
                else "runtime:unknown"
            ),
            advertised_model=self.profile.model,
        )
