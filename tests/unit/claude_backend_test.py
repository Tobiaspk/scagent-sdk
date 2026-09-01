from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from scagent_sdk.errors import ContextRolloverRequired
from scagent_sdk.models.profile import ModelProfile
from scagent_sdk.models.thinking import ThinkingSettings
from scagent_sdk.runtime.claude import ClaudeAgentSDKBackend, ClaudeRuntimeExtensions
from scagent_sdk.runtime.protocol import RuntimeRequest


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: object
    is_error: bool = False


@dataclass
class AssistantMessage:
    content: list


@dataclass
class UserMessage:
    content: list


@dataclass
class ResultMessage:
    session_id: str = "sdk-session"
    result: str = "final"
    stop_reason: str = "end_turn"
    is_error: bool = False
    subtype: str = "success"
    usage: dict | None = None
    model_usage: dict | None = None
    total_cost_usd: float | None = None


class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeClient:
    last_options = None
    last_prompt = None
    context_calls = 0

    def __init__(self, *, options):
        type(self).last_options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def query(self, prompt):
        type(self).last_prompt = prompt

    async def get_context_usage(self):
        type(self).context_calls += 1
        return {
            "totalTokens": 100_000,
            "maxTokens": 230_000,
            "rawMaxTokens": 200_000,
            "percentage": 38.1,
            "model": "Qwen3.6-27B",
            "isAutoCompactEnabled": True,
        }

    async def interrupt(self):
        return None

    async def receive_response(self):
        yield AssistantMessage(
            [
                ThinkingBlock("reason"),
                TextBlock("answer"),
                ToolUseBlock("tool-1", "inspect", {"path": "a.h5ad"}),
            ]
        )
        yield ResultMessage(usage={"input_tokens": 4})


def _sdk():
    return SimpleNamespace(
        ClaudeAgentOptions=ClaudeAgentOptions,
        ClaudeSDKClient=FakeClient,
        TextBlock=TextBlock,
        ThinkingBlock=ThinkingBlock,
        ToolUseBlock=ToolUseBlock,
        ToolResultBlock=ToolResultBlock,
        AssistantMessage=AssistantMessage,
        UserMessage=UserMessage,
        ResultMessage=ResultMessage,
    )


def test_claude_backend_builds_isolated_resumable_sdk_options(tmp_path: Path) -> None:
    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        small_fast_model="fast",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
    )
    request = RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
        resume_session_id="sdk-old",
        fork_session=True,
        context_window_tokens=262_144,
        context_limit_source="upstream:models",
    )

    response = asyncio.run(ClaudeAgentSDKBackend(sdk_module=_sdk()).execute(request))

    options = FakeClient.last_options
    assert options.system_prompt == "System"
    assert options.resume == "sdk-old"
    assert options.fork_session is True
    assert options.session_store_flush == "eager"
    assert options.tools == []
    assert options.strict_mcp_config is True
    assert options.skills == []
    assert options.env["ANTHROPIC_BASE_URL"] == "http://localhost:4000"
    assert response.runtime_session_id == "sdk-session"
    assert [message.kind for message in response.messages] == ["thinking", "text", "tool_use"]
    assert response.context_usage == {
        "total_tokens": 100_000,
        "context_window_tokens": 262_144,
        "sdk_raw_context_window_tokens": 200_000,
        "effective_limit_tokens": 230_000,
        "percentage": pytest.approx(38.14697),
        "model": "Qwen3.6-27B",
        "auto_compact_enabled": True,
        "source": "claude-agent-sdk:get_context_usage",
        "context_window_source": "upstream:models",
    }
    assert response.to_dict()["context_usage"]["total_tokens"] == 100_000


def test_claude_backend_applies_only_explicit_extensions(tmp_path: Path) -> None:
    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
        skills=("profile-skill",),
    )
    request = RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
    )
    hooks = {"PreToolUse": [object()]}
    backend = ClaudeAgentSDKBackend(
        sdk_module=_sdk(),
        extensions=ClaudeRuntimeExtensions(
            mcp_servers={"science": {"type": "sdk"}},
            hooks=hooks,
            tools=("Skill",),
            allowed_tools=("mcp__science__inspect",),
            disallowed_tools=("Bash",),
            skills=("extension-skill",),
            include_hook_events=True,
            system_prompt_suffix="## Local prerequisites on this host\n\n- ready",
        ),
    )

    asyncio.run(backend.execute(request))

    options = FakeClient.last_options
    assert options.system_prompt == "System\n\n## Local prerequisites on this host\n\n- ready"
    assert options.mcp_servers == {"science": {"type": "sdk"}}
    assert options.hooks == hooks
    assert options.tools == ["Skill"]
    assert options.allowed_tools == ["mcp__science__inspect"]
    assert options.disallowed_tools == ["Bash"]
    assert options.skills == ["extension-skill"]
    assert options.include_hook_events is True


def test_sdk_stderr_filters_only_known_connector_notice(capsys) -> None:
    ClaudeAgentSDKBackend._sdk_stderr(
        "claude.ai connectors are disabled because an API key takes precedence"
    )
    ClaudeAgentSDKBackend._sdk_stderr("actionable runtime diagnostic")

    assert capsys.readouterr().err == "actionable runtime diagnostic\n"


def _thinking_request(tmp_path: Path, thinking: ThinkingSettings) -> RuntimeRequest:
    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
        thinking=thinking,
    )
    return RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
    )


def test_claude_backend_injects_profile_thinking_options(tmp_path: Path) -> None:
    request = _thinking_request(
        tmp_path, ThinkingSettings(mode="enabled", budget_tokens=12000, effort="high")
    )

    asyncio.run(ClaudeAgentSDKBackend(sdk_module=_sdk()).execute(request))

    options = FakeClient.last_options
    assert options.thinking == {"type": "enabled", "budget_tokens": 12000}
    assert options.effort == "high"


def test_claude_backend_native_thinking_injects_nothing(tmp_path: Path) -> None:
    request = _thinking_request(tmp_path, ThinkingSettings(mode="native"))

    asyncio.run(ClaudeAgentSDKBackend(sdk_module=_sdk()).execute(request))

    options = FakeClient.last_options
    # "native" defers entirely to the backend: no thinking/effort keys are set.
    assert getattr(options, "thinking", None) is None
    assert getattr(options, "effort", None) is None


@dataclass
class SystemMessage:
    subtype: str
    data: dict


class SlowClient:
    """A client whose turn never ends on its own, and that answers interrupt()."""

    instances: list = []

    def __init__(self, *, options):
        self.options = options
        self.interrupted = asyncio.Event()
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def query(self, prompt):
        return None

    async def interrupt(self):
        self.interrupted.set()

    async def receive_response(self):
        yield SystemMessage("init", {"session_id": "sdk-live"})
        yield AssistantMessage([TextBlock("partial answer")])
        await self.interrupted.wait()
        # A stopped turn ends without a ResultMessage.


def _slow_sdk():
    sdk = _sdk()
    sdk.ClaudeSDKClient = SlowClient
    sdk.SystemMessage = SystemMessage
    return sdk


def _plain_request(tmp_path: Path) -> RuntimeRequest:
    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
    )
    return RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
    )


def test_interrupt_ends_the_turn_without_a_result_message(tmp_path: Path) -> None:
    SlowClient.instances.clear()
    backend = ClaudeAgentSDKBackend(sdk_module=_slow_sdk())

    async def scenario():
        turn = asyncio.ensure_future(backend.execute(_plain_request(tmp_path)))
        while not SlowClient.instances:
            await asyncio.sleep(0)
        assert await backend.interrupt() is True
        return await turn

    response = asyncio.run(scenario())

    assert response.interrupted is True
    assert response.is_error is False
    assert response.subtype == "interrupted"
    # The session ID seen on the wire is kept, so the next turn can resume this conversation.
    assert response.runtime_session_id == "sdk-live"
    assert response.final_text == "partial answer"


def test_interrupt_without_a_running_turn_reports_nothing_to_stop(tmp_path: Path) -> None:
    backend = ClaudeAgentSDKBackend(sdk_module=_slow_sdk())

    assert asyncio.run(backend.interrupt()) is False


def test_cancelled_turn_keeps_the_runtime_session_it_reached(tmp_path: Path) -> None:
    SlowClient.instances.clear()
    backend = ClaudeAgentSDKBackend(sdk_module=_slow_sdk())

    async def scenario():
        turn = asyncio.ensure_future(backend.execute(_plain_request(tmp_path)))
        while not SlowClient.instances:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

    asyncio.run(scenario())

    assert backend.last_runtime_session_id == "sdk-live"


def test_runtime_frame_limit_exceeds_the_capability_media_budget(tmp_path: Path) -> None:
    """Figure pixels must not overflow the transport that carries them.

    The SDK's default 1 MiB stdout frame is smaller than a few normal scientific figures, and
    exceeding it fails the entire turn rather than one tool call.
    """

    from scagent_sdk.capabilities.results import MODEL_MEDIA_TOTAL_BYTES
    from scagent_sdk.runtime.claude import MAX_RUNTIME_MESSAGE_BYTES

    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
    )
    request = RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
    )

    asyncio.run(ClaudeAgentSDKBackend(sdk_module=_sdk()).execute(request))

    assert FakeClient.last_options.max_buffer_size == MAX_RUNTIME_MESSAGE_BYTES
    # Base64 inflates by 4/3 and the CLI replays results in its own frames.
    assert MAX_RUNTIME_MESSAGE_BYTES > 2 * MODEL_MEDIA_TOTAL_BYTES


def test_skill_plugins_are_passed_through_without_enabling_project_settings(
    tmp_path: Path,
) -> None:
    prompt = tmp_path / "system.md"
    prompt.write_text("System", encoding="utf-8")
    profile = ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model="primary",
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
    )
    request = RuntimeRequest(
        prompt="user",
        profile=profile,
        scientific_session_id="scientific",
        scientific_session_dir=tmp_path / "session",
        cwd=tmp_path,
    )
    backend = ClaudeAgentSDKBackend(
        sdk_module=_sdk(),
        extensions=ClaudeRuntimeExtensions(
            tools=("Skill",),
            skills=("celltypist-annotation",),
            plugins=({"type": "local", "path": str(tmp_path / "plugin")},),
        ),
    )

    asyncio.run(backend.execute(request))

    options = FakeClient.last_options
    assert options.tools == ["Skill"]
    assert options.plugins == [{"type": "local", "path": str(tmp_path / "plugin")}]
    # Project settings stay off: they would import CLAUDE.md/AGENTS.md into a science session.
    assert options.setting_sources == []


def test_exact_resume_preflight_uses_advertised_context_window_before_query(
    tmp_path: Path,
) -> None:
    class ContextClient(FakeClient):
        queried = False

        async def get_context_usage(self):
            return {
                "totalTokens": 230_145,
                "maxTokens": 200_000,
                "rawMaxTokens": 262_144,
            }

        async def query(self, prompt):
            type(self).queried = True

    sdk = _sdk()
    sdk.ClaudeSDKClient = ContextClient
    request = _plain_request(tmp_path)
    request = RuntimeRequest(
        **{
            **request.__dict__,
            "resume_session_id": "sdk-old",
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_000,
            "context_limit_source": "upstream:models",
        }
    )

    with pytest.raises(ContextRolloverRequired) as captured:
        asyncio.run(ClaudeAgentSDKBackend(sdk_module=sdk).execute(request))

    assert captured.value.context_window_tokens == 262_144
    assert captured.value.total_tokens == 230_145
    assert captured.value.source == "upstream:models"
    assert ContextClient.queried is False


def test_live_context_rollover_stops_after_a_committed_tool_result(tmp_path: Path) -> None:
    class LiveContextClient(FakeClient):
        instances: list = []

        def __init__(self, *, options):
            super().__init__(options=options)
            self.interrupted = False
            type(self).instances.append(self)

        async def get_context_usage(self):
            return {
                "totalTokens": 230_145,
                "maxTokens": 230_000,
                "rawMaxTokens": 262_144,
            }

        async def interrupt(self):
            self.interrupted = True

        async def receive_response(self):
            yield SystemMessage("init", {"session_id": "sdk-live-full"})
            yield AssistantMessage([ToolUseBlock("tool-1", "science", {})])
            # Capability PostToolUse has committed the scientific state before this user-side
            # result frame reaches the model runtime.
            yield UserMessage([ToolResultBlock("tool-1", {"ok": True})])

    sdk = _sdk()
    sdk.ClaudeSDKClient = LiveContextClient
    sdk.SystemMessage = SystemMessage
    request = RuntimeRequest(
        **{
            **_plain_request(tmp_path).__dict__,
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_000,
            "context_limit_source": "upstream:models",
        }
    )
    backend = ClaudeAgentSDKBackend(sdk_module=sdk)

    with pytest.raises(ContextRolloverRequired) as captured:
        asyncio.run(backend.execute(request))

    assert captured.value.total_tokens == 230_145
    assert captured.value.context_window_tokens == 262_144
    assert LiveContextClient.instances[-1].interrupted is True
    assert backend.last_runtime_session_id == "sdk-live-full"
