from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from scagent_sdk.doctor.model import HTTPResponse
from scagent_sdk.models.limits import ModelLimitResolver
from scagent_sdk.models.profile import ModelProfile


class FakeTransport:
    def __init__(self, responses: dict[str, tuple[int, object]]):
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def request(self, **kwargs) -> HTTPResponse:
        self.requests.append(kwargs)
        status, payload = self.responses.get(kwargs["url"], (404, {}))
        return HTTPResponse(
            status=status,
            body=json.dumps(payload).encode(),
            url=str(kwargs["url"]),
        )


def _profile(tmp_path: Path, **overrides) -> ModelProfile:
    prompt = tmp_path / "system.md"
    prompt.write_text("system", encoding="utf-8")
    values = {
        "name": "iris",
        "runtime": "claude-agent-sdk",
        "transport": "litellm",
        "model": "scagent-primary",
        "system_prompt": str(prompt),
        "base_url": "http://127.0.0.1:4000",
        "allow_noauth": True,
    }
    values.update(overrides)
    return ModelProfile(**values)


def test_resolves_litellm_alias_through_upstream_vllm_advertisement(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            "http://127.0.0.1:4000/model/info": (
                200,
                {
                    "data": [
                        {
                            "model_name": "scagent-primary",
                            "model_info": {"max_tokens": None},
                            "litellm_params": {
                                "model": "openai/Qwen3.6-27B",
                                "api_base": "http://iscp001:8000/v1",
                            },
                        }
                    ]
                },
            ),
            "http://iscp001:8000/v1/models": (
                200,
                {"data": [{"id": "Qwen3.6-27B", "max_model_len": 262144}]},
            ),
        }
    )

    limits = ModelLimitResolver(_profile(tmp_path), transport=transport).resolve()

    assert limits.context_window_tokens == 262_144
    assert limits.source == "upstream:models"
    assert limits.advertised_model == "Qwen3.6-27B"
    upstream = transport.requests[1]
    assert upstream["headers"] == {"content-type": "application/json"}


def test_prefers_limits_published_directly_by_litellm_model_info(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            "http://127.0.0.1:4000/model/info": (
                200,
                {
                    "data": [
                        {
                            "model_name": "scagent-primary",
                            "model_info": {
                                "max_input_tokens": 128000,
                                "max_output_tokens": 16000,
                            },
                        }
                    ]
                },
            )
        }
    )

    limits = ModelLimitResolver(_profile(tmp_path), transport=transport).resolve()

    assert limits.context_window_tokens == 128_000
    assert limits.max_output_tokens == 16_000
    assert limits.source == "litellm:model_info"
    assert len(transport.requests) == 1


def test_advertises_concrete_upstream_model_not_routing_alias(tmp_path: Path) -> None:
    # When LiteLLM publishes limits directly (early return), advertised_model must still
    # report the concrete upstream checkpoint from litellm_params.model, not the alias.
    transport = FakeTransport(
        {
            "http://127.0.0.1:4000/model/info": (
                200,
                {
                    "data": [
                        {
                            "model_name": "scagent-primary",
                            "model_info": {"max_input_tokens": 262144},
                            "litellm_params": {
                                "model": "openai/Qwen3.8-27B",
                                "api_base": "http://iscp001:8000/v1",
                            },
                        }
                    ]
                },
            )
        }
    )

    limits = ModelLimitResolver(_profile(tmp_path), transport=transport).resolve()

    assert limits.source == "litellm:model_info"
    assert limits.advertised_model == "Qwen3.8-27B"


def test_uses_profile_value_only_as_fallback_when_endpoints_do_not_advertise(
    tmp_path: Path,
) -> None:
    profile = _profile(
        tmp_path,
        context_window_fallback=200_000,
        max_output_tokens=12_000,
    )

    limits = ModelLimitResolver(profile, transport=FakeTransport({})).resolve()

    assert limits.context_window_tokens == 200_000
    assert limits.max_output_tokens == 12_000
    assert limits.source == "profile:fallback"


def test_hosted_api_uses_model_specific_provider_registry(
    tmp_path: Path, monkeypatch
) -> None:
    profile = _profile(
        tmp_path,
        transport="openai",
        model="openai/gpt-cloud",
        base_url=None,
    )
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            get_model_info=lambda model: {
                "max_input_tokens": 400_000,
                "max_output_tokens": 100_000,
            }
        ),
    )

    limits = ModelLimitResolver(profile).resolve()

    assert limits.context_window_tokens == 400_000
    assert limits.max_output_tokens == 100_000
    assert limits.source == "provider:model_registry"
