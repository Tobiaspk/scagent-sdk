"""Claude Agent SDK implementation of the provider-neutral runtime protocol."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from scagent_sdk.errors import ContextRolloverRequired, RuntimeExecutionError
from scagent_sdk.runtime.claude_store import ScientificSessionTranscriptStore
from scagent_sdk.runtime.compaction import (
    CHARS_PER_TOKEN,
    DEFAULT_IMAGE_TOKEN_ESTIMATE,
    DEFAULT_KEEP_TAIL_ENTRIES,
    DEFAULT_MAX_CONTEXT_IMAGES,
    CompactionConfig,
    CompactionStats,
)
from scagent_sdk.runtime.observer import NullRuntimeObserver, RuntimeObserver
from scagent_sdk.runtime.protocol import (
    RuntimeMessage,
    RuntimeRequest,
    RuntimeResponse,
)

# Headroom over the capability layer's per-result media budget (see
# scagent_sdk.capabilities.results.MODEL_MEDIA_TOTAL_BYTES), which is the limit that should
# actually bind. Base64 inflates payloads by 4/3, and the CLI replays tool results in its own
# frames, so this leaves room for several image-bearing results in one turn.
MAX_RUNTIME_MESSAGE_BYTES = 64 * 1024 * 1024
DEFAULT_OUTPUT_RESERVE_TOKENS = 32_000
MIN_CONTEXT_SAFETY_MARGIN_TOKENS = 4_096
# Conservative allowance for the MCP tool schemas the CLI injects alongside the system prompt.
# The calibration ratchet corrects any residual drift, so a rough constant is sufficient.
DEFAULT_TOOL_SCHEMA_OVERHEAD_TOKENS = 8_000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class ClaudeRuntimeExtensions:
    """Explicit SDK capabilities assembled outside the model backend.

    The default is intentionally empty. Capability discovery can later build one
    of these from installed skill packages without teaching this adapter any
    single-cell science or granting Claude Code's built-in tools implicitly.
    """

    mcp_servers: dict[str, Any] = field(default_factory=dict)
    hooks: dict[str, Any] = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    skills: tuple[str, ...] | None = None
    # Local plugin roots the CLI loads skills from. Skills reach the model this way rather than
    # through project setting sources, which would also pull this repository's CLAUDE.md/AGENTS.md
    # — coding-agent instructions — into a scientific session's context.
    plugins: tuple[dict[str, str], ...] = ()
    include_hook_events: bool = False
    # Host facts the profile prompt cannot know statically, appended to it verbatim so the
    # model knows what it can run without probing the filesystem for evidence.
    system_prompt_suffix: str = ""


class ClaudeAgentSDKBackend:
    runtime_name = "claude-agent-sdk"

    def __init__(
        self,
        *,
        sdk_module: Any | None = None,
        extensions: ClaudeRuntimeExtensions | None = None,
        observer: RuntimeObserver | None = None,
    ):
        self._sdk_module = sdk_module
        self.extensions = extensions or ClaudeRuntimeExtensions()
        self.observer = observer or NullRuntimeObserver()
        self._client: Any | None = None
        self._transcript_store: ScientificSessionTranscriptStore | None = None
        self._interrupt_requested = False
        self._context_rollover_requested: ContextRolloverRequired | None = None
        # Last runtime session ID seen on the wire. A turn stopped before its ResultMessage
        # still has a resumable model conversation; keeping the ID is what preserves exact
        # resume instead of silently downgrading the next turn to a reconstructed one.
        self.last_runtime_session_id: str | None = None

    def _sdk(self) -> Any:
        if self._sdk_module is not None:
            return self._sdk_module
        try:
            return importlib.import_module("claude_agent_sdk")
        except ImportError as exc:
            raise RuntimeExecutionError(
                "Claude runtime is not installed; install the project with .[runtime]"
            ) from exc

    @staticmethod
    def _sdk_stderr(line: str) -> None:
        if "connectors are disabled because" in line:
            return
        sys.stderr.write(line if line.endswith("\n") else line + "\n")

    @staticmethod
    def _compaction_config(request: RuntimeRequest, system_prompt: str) -> CompactionConfig:
        """Budget the replayed transcript against the model window, minus fixed overhead.

        Disabled (a pure no-op) when the context window is unknown or when
        ``SCAGENT_COMPACTION_DISABLED`` is set, in which case the existing preflight/rollover
        path remains the sole backstop. Image/tail knobs are env-overridable so the policy can
        follow the model in use without a code change.
        """

        disabled = os.environ.get("SCAGENT_COMPACTION_DISABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        overhead = len(system_prompt) // CHARS_PER_TOKEN + DEFAULT_TOOL_SCHEMA_OVERHEAD_TOKENS
        return CompactionConfig(
            context_limit=request.context_window_tokens,
            output_reserve=request.max_output_tokens or DEFAULT_OUTPUT_RESERVE_TOKENS,
            overhead_tokens=overhead,
            image_token_estimate=_env_int(
                "SCAGENT_IMAGE_TOKEN_ESTIMATE", DEFAULT_IMAGE_TOKEN_ESTIMATE
            ),
            keep_tail_entries=_env_int("SCAGENT_KEEP_TAIL_ENTRIES", DEFAULT_KEEP_TAIL_ENTRIES),
            max_context_images=_env_int(
                "SCAGENT_MAX_CONTEXT_IMAGES", DEFAULT_MAX_CONTEXT_IMAGES
            ),
            enabled=not disabled,
        )

    def _options(self, request: RuntimeRequest, sdk: Any) -> Any:
        profile = request.profile
        extensions = self.extensions
        system_prompt = profile.read_system_prompt()
        if extensions.system_prompt_suffix.strip():
            system_prompt = f"{system_prompt}\n\n{extensions.system_prompt_suffix.strip()}"
        self._transcript_store = ScientificSessionTranscriptStore(
            request.scientific_session_dir,
            compaction=self._compaction_config(request, system_prompt),
        )
        values: dict[str, Any] = {
            "model": profile.model,
            "system_prompt": system_prompt,
            "max_turns": profile.max_turns,
            "max_budget_usd": profile.max_budget_usd,
            "cwd": request.cwd,
            "tools": list(extensions.tools),
            "allowed_tools": list(extensions.allowed_tools),
            "disallowed_tools": list(extensions.disallowed_tools),
            "mcp_servers": dict(extensions.mcp_servers),
            "strict_mcp_config": True,
            "permission_mode": "dontAsk",
            "setting_sources": [],
            "skills": (
                list(extensions.skills) if extensions.skills is not None else list(profile.skills)
            ),
            "hooks": dict(extensions.hooks) or None,
            "plugins": [dict(plugin) for plugin in extensions.plugins],
            "include_hook_events": extensions.include_hook_events,
            "env": profile.runtime_environment(),
            "resume": request.resume_session_id,
            "fork_session": request.fork_session,
            "session_store": self._transcript_store,
            # Tool results mutate the durable scientific session before they are returned to the
            # model. Mirror each runtime frame eagerly as well, so a context rollover at that
            # committed boundary never depends on a later terminal ResultMessage to preserve the
            # conversation prefix.
            "session_store_flush": "eager",
            "stderr": self._sdk_stderr,
            # Scientific turns carry figure pixels back to the model. The SDK's default 1 MiB
            # stdout frame limit is a transport detail that a few normal-sized plots exceed, and
            # overflowing it kills the whole turn rather than one tool call. Size it above the
            # capability layer's own media budget so the enforced limit is the scientific one.
            "max_buffer_size": MAX_RUNTIME_MESSAGE_BYTES,
        }
        # Reasoning generation is profile-controlled; "native" injects nothing.
        values.update(profile.thinking.to_claude_options())
        return sdk.ClaudeAgentOptions(**values)

    @staticmethod
    def _assistant_blocks(message: Any, sdk: Any) -> list[RuntimeMessage]:
        output: list[RuntimeMessage] = []
        for block in message.content:
            if isinstance(block, sdk.TextBlock):
                output.append(RuntimeMessage("text", block.text))
            elif isinstance(block, sdk.ThinkingBlock):
                output.append(RuntimeMessage("thinking", block.thinking))
            elif isinstance(block, sdk.ToolUseBlock):
                output.append(
                    RuntimeMessage(
                        "tool_use",
                        {"id": block.id, "name": block.name, "input": block.input},
                    )
                )
            elif isinstance(block, sdk.ToolResultBlock):
                output.append(
                    RuntimeMessage(
                        "tool_result",
                        {
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        },
                    )
                )
        return output

    async def interrupt(self) -> bool:
        """Ask the running turn to stop cleanly. Returns whether a turn was in flight.

        This is the graceful stop: the CLI ends the turn itself, so the transcript is
        flushed and the runtime session stays resumable. Cancelling the execute() task is
        the forcible fallback when this does not land.
        """

        self._interrupt_requested = True
        client = self._client
        if client is None:
            return False
        with suppress(Exception):
            await client.interrupt()
        return True

    @staticmethod
    def _contains_tool_result(message: Any, sdk: Any) -> bool:
        """Return whether a user-side runtime frame completed a tool invocation."""

        content = getattr(message, "content", None)
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            if isinstance(block, getattr(sdk, "ToolResultBlock", ())):
                return True
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return True
        return False

    def _note_session_id(self, message: Any) -> None:
        session_id = getattr(message, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            data = getattr(message, "data", None)
            session_id = data.get("session_id") if isinstance(data, dict) else None
        if isinstance(session_id, str) and session_id:
            self.last_runtime_session_id = session_id

    def _interrupted_response(
        self, messages: list[RuntimeMessage], result: Any | None
    ) -> RuntimeResponse:
        text_blocks = [
            str(message.content)
            for message in messages
            if message.kind == "text" and str(message.content).strip()
        ]
        session_id = getattr(result, "session_id", None) or self.last_runtime_session_id or ""
        return RuntimeResponse(
            runtime_session_id=session_id,
            messages=tuple(messages),
            final_text=getattr(result, "result", None) or "\n".join(text_blocks),
            stop_reason=getattr(result, "stop_reason", None) or "interrupted",
            is_error=False,
            subtype="interrupted",
            usage=dict(getattr(result, "usage", None) or {}),
            model_usage=dict(getattr(result, "model_usage", None) or {}),
            total_cost_usd=getattr(result, "total_cost_usd", None),
            interrupted=True,
        )

    @staticmethod
    async def _preflight_context(client: Any, request: RuntimeRequest) -> None:
        """Refuse an exact query before the provider rejects its overfull transcript.

        The deployment's advertised limit wins. When endpoint discovery is unavailable,
        Claude Agent SDK's runtime-reported raw maximum provides a provider-neutral fallback.
        A failed usage probe is non-fatal because the service also recognizes an actual
        provider context error and can roll over safely.
        """

        if not request.resume_session_id or not hasattr(client, "get_context_usage"):
            return
        try:
            usage = await client.get_context_usage()
        except Exception:
            return
        if not isinstance(usage, dict):
            return
        total = usage.get("totalTokens")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            return
        runtime_limit = usage.get("rawMaxTokens") or usage.get("maxTokens")
        if not isinstance(runtime_limit, int) or isinstance(runtime_limit, bool):
            runtime_limit = None
        limit = request.context_window_tokens or runtime_limit
        if limit is None or limit < 1:
            return
        output_reserve = request.max_output_tokens or DEFAULT_OUTPUT_RESERVE_TOKENS
        safety_margin = max(MIN_CONTEXT_SAFETY_MARGIN_TOKENS, int(limit * 0.03))
        if total + output_reserve + safety_margin < limit:
            return
        source = request.context_limit_source or (
            "claude-agent-sdk:rawMaxTokens" if runtime_limit else "runtime:unknown"
        )
        raise ContextRolloverRequired(
            "the exact model conversation needs a context rollover before another turn "
            f"({total:,} used + {output_reserve:,} output reserve + "
            f"{safety_margin:,} safety margin >= {limit:,} token window)",
            total_tokens=total,
            context_window_tokens=limit,
            output_reserve_tokens=output_reserve,
            safety_margin_tokens=safety_margin,
            source=source,
        )

    @staticmethod
    async def _current_context_usage(
        client: Any,
        request: RuntimeRequest,
    ) -> dict[str, Any]:
        if not hasattr(client, "get_context_usage"):
            return {}
        try:
            usage = await client.get_context_usage()
        except Exception:
            return {}
        if not isinstance(usage, dict):
            return {}
        total = usage.get("totalTokens")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            return {}
        sdk_raw_limit = usage.get("rawMaxTokens")
        effective_limit = usage.get("maxTokens")
        if (
            not isinstance(sdk_raw_limit, int)
            or isinstance(sdk_raw_limit, bool)
            or sdk_raw_limit < 1
        ):
            sdk_raw_limit = None
        # Live model/deployment discovery is authoritative. Claude SDK can report its own
        # autocompact ceiling (200K on the 262K Iris deployment) as rawMaxTokens, so using it as
        # the denominator makes a healthy context appear over 100%.
        context_limit = request.context_window_tokens or sdk_raw_limit
        if (
            not isinstance(effective_limit, int)
            or isinstance(effective_limit, bool)
            or effective_limit < 1
        ):
            effective_limit = None
        percentage = (100.0 * total / context_limit) if context_limit else None
        return {
            "total_tokens": total,
            "context_window_tokens": context_limit,
            "sdk_raw_context_window_tokens": sdk_raw_limit,
            "effective_limit_tokens": effective_limit,
            "percentage": percentage,
            "model": usage.get("model"),
            "auto_compact_enabled": usage.get("isAutoCompactEnabled"),
            "source": "claude-agent-sdk:get_context_usage",
            "context_window_source": (
                request.context_limit_source
                if request.context_window_tokens
                else "claude-agent-sdk:rawMaxTokens"
            ),
        }

    @staticmethod
    def _rollover_for_usage(
        usage: Mapping[str, Any], request: RuntimeRequest
    ) -> ContextRolloverRequired | None:
        total = usage.get("total_tokens")
        limit = usage.get("context_window_tokens")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
        ):
            return None
        output_reserve = request.max_output_tokens or DEFAULT_OUTPUT_RESERVE_TOKENS
        safety_margin = max(MIN_CONTEXT_SAFETY_MARGIN_TOKENS, int(limit * 0.03))
        if total + output_reserve + safety_margin < limit:
            return None
        return ContextRolloverRequired(
            "the live model conversation reached its context reserve after a committed "
            "scientific tool result "
            f"({total:,} used + {output_reserve:,} output reserve + "
            f"{safety_margin:,} safety margin >= {limit:,} token window)",
            total_tokens=total,
            context_window_tokens=limit,
            output_reserve_tokens=output_reserve,
            safety_margin_tokens=safety_margin,
            source=str(usage.get("context_window_source") or usage.get("source") or "runtime"),
        )

    @staticmethod
    def _compaction_summary(stats: CompactionStats) -> str:
        parts: list[str] = []
        if stats.images_evicted:
            parts.append(f"{stats.images_evicted} figure(s) aged out")
        if stats.results_trimmed:
            parts.append(f"{stats.results_trimmed} result(s) trimmed")
        if stats.args_trimmed:
            parts.append(f"{stats.args_trimmed} tool input(s) trimmed")
        if stats.assistant_truncated:
            parts.append(f"{stats.assistant_truncated} note(s) shortened")
        summary = ", ".join(parts) or "history trimmed"
        return f"emergency compaction — {summary}" if stats.emergency else summary

    def _report_compaction(self) -> None:
        store = self._transcript_store
        if store is None:
            return
        stats = store.take_last_compaction()
        if stats is None or not stats.triggered:
            return
        self.observer.on_context_compacted(
            summary=self._compaction_summary(stats),
            tokens_before=stats.tokens_before,
            tokens_after=stats.tokens_after,
        )

    @staticmethod
    def _prompt_tokens(usage: Any) -> int | None:
        if not isinstance(usage, dict):
            return None
        total = 0
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                total += value
        if total:
            return total
        prompt = usage.get("prompt_tokens")
        if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt > 0:
            return prompt
        return None

    def _calibrate_estimator(self, result: Any | None) -> None:
        store = self._transcript_store
        if store is None or result is None:
            return
        actual = self._prompt_tokens(getattr(result, "usage", None))
        with suppress(Exception):
            store.update_calibration(actual)

    async def execute(self, request: RuntimeRequest) -> RuntimeResponse:
        sdk = self._sdk()
        options = self._options(request, sdk)
        messages: list[RuntimeMessage] = []
        result: Any | None = None
        context_usage: dict[str, Any] = {}
        self._interrupt_requested = False
        self._context_rollover_requested = None
        self.last_runtime_session_id = request.resume_session_id
        self.observer.on_runtime_started(request)
        try:
            async with sdk.ClaudeSDKClient(options=options) as client:
                self._client = client
                await self._preflight_context(client, request)
                await client.query(request.prompt)
                self._report_compaction()
                async for message in client.receive_response():
                    self._note_session_id(message)
                    if isinstance(message, sdk.AssistantMessage):
                        blocks = self._assistant_blocks(message, sdk)
                        messages.extend(blocks)
                        for block in blocks:
                            self.observer.on_message(block)
                    elif isinstance(message, sdk.ResultMessage):
                        result = message
                    elif isinstance(message, getattr(sdk, "UserMessage", ())):
                        # PostToolUse has already validated and committed the state patch by the
                        # time this frame arrives. This is the safe live-compaction boundary: stop
                        # the replaceable model conversation, then let the service reconstruct it
                        # from the authoritative scientific session and continue the same turn.
                        if self._contains_tool_result(message, sdk):
                            context_usage = await self._current_context_usage(client, request)
                            rollover = self._rollover_for_usage(context_usage, request)
                            if rollover is not None:
                                self._context_rollover_requested = rollover
                                with suppress(Exception):
                                    await client.interrupt()
                context_usage = await self._current_context_usage(client, request)
        except asyncio.CancelledError:
            self.observer.on_runtime_interrupted(forced=True)
            raise
        except RuntimeExecutionError:
            raise
        except Exception as exc:
            if self._context_rollover_requested is not None:
                raise self._context_rollover_requested from exc
            if self._interrupt_requested:
                # The runtime tore itself down while stopping. That is the interrupt landing,
                # not a scientific failure.
                self.observer.on_runtime_interrupted(forced=False)
                return self._interrupted_response(messages, result)
            self.observer.on_runtime_failed(str(exc))
            raise RuntimeExecutionError(f"Claude Agent SDK execution failed: {exc}") from exc
        finally:
            self._client = None

        if self._context_rollover_requested is not None:
            raise self._context_rollover_requested
        if self._interrupt_requested:
            self.observer.on_runtime_interrupted(forced=False)
            return self._interrupted_response(messages, result)
        self._calibrate_estimator(result)
        if result is None:
            raise RuntimeExecutionError("Claude Agent SDK ended without a ResultMessage")
        if not isinstance(result.session_id, str) or not result.session_id:
            raise RuntimeExecutionError("Claude Agent SDK returned no resumable session ID")
        text_blocks = [
            str(message.content)
            for message in messages
            if message.kind == "text" and str(message.content).strip()
        ]
        final_text = result.result or "\n".join(text_blocks)
        response = RuntimeResponse(
            runtime_session_id=result.session_id,
            messages=tuple(messages),
            final_text=final_text,
            stop_reason=result.stop_reason,
            is_error=bool(result.is_error),
            subtype=result.subtype,
            usage=dict(result.usage or {}),
            model_usage=dict(result.model_usage or {}),
            context_usage=context_usage,
            total_cost_usd=result.total_cost_usd,
        )
        self.observer.on_runtime_finished(response)
        return response
