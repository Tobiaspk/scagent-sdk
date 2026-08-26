from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scagent_sdk.errors import ContextRolloverRequired, RuntimeExecutionError
from scagent_sdk.models.limits import ModelLimits
from scagent_sdk.models.profile import ModelProfile
from scagent_sdk.runtime.observer import NullRuntimeObserver
from scagent_sdk.runtime.protocol import RuntimeMessage, RuntimeRequest, RuntimeResponse
from scagent_sdk.runtime.resume import ResumeMode, ResumePreference
from scagent_sdk.runtime.service import AgentRuntimeService
from scagent_sdk.session import AnalysisSession


class FakeBackend:
    runtime_name = "claude-agent-sdk"

    def __init__(self, response: RuntimeResponse | Exception):
        self.response = response
        self.requests: list[RuntimeRequest] = []

    async def execute(self, request: RuntimeRequest) -> RuntimeResponse:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _profile(tmp_path: Path, *, model: str = "model-a") -> ModelProfile:
    prompt = tmp_path / f"{model}.md"
    prompt.write_text("System", encoding="utf-8")
    return ModelProfile(
        name="local",
        runtime="claude-agent-sdk",
        transport="litellm",
        model=model,
        system_prompt=str(prompt),
        base_url="http://localhost:4000",
        allow_noauth=True,
    )


def _response(session_id: str = "sdk-new") -> RuntimeResponse:
    return RuntimeResponse(
        runtime_session_id=session_id,
        messages=(RuntimeMessage("text", "done"),),
        final_text="done",
        stop_reason="end_turn",
        is_error=False,
        subtype="success",
        usage={"input_tokens": 10, "output_tokens": 2},
    )


def test_runtime_turn_binds_sdk_session_and_persists_transcript_events(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Run")
    backend = FakeBackend(_response())
    service = AgentRuntimeService(backend)
    profile = _profile(tmp_path)

    response = asyncio.run(
        service.run_turn(session, user_prompt="Continue analysis", profile=profile, cwd=tmp_path)
    )

    assert response.final_text == "done"
    assert backend.requests[0].resume_session_id is None
    # A brand-new session has nothing to resume: the model gets its request directly, with no
    # "resume from the durable checkpoint" preamble over an empty checkpoint.
    assert backend.requests[0].prompt == "Continue analysis"
    assert "durable checkpoint" not in backend.requests[0].prompt
    started = [e for e in session.store.events() if e.kind == "runtime.turn_started"][0]
    assert started.payload["resume_mode"] == "fresh"
    assert session.store.state.runtime["active"]["runtime_session_id"] == "sdk-new"
    assert session.store.state.runtime["active"]["model_profile_fingerprint"] == profile.fingerprint
    kinds = [event.kind for event in session.store.events()]
    assert kinds[-3:] == ["runtime.turn_started", "runtime.bound", "runtime.turn_completed"]


def test_runtime_turn_uses_exact_resume_only_for_matching_fingerprint(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Resume")
    original = _profile(tmp_path, model="model-a")
    session.bind_runtime(
        runtime="claude-agent-sdk",
        runtime_session_id="sdk-old",
        model_profile="local",
        model_profile_fingerprint=original.fingerprint,
        transport="litellm",
        model=original.model,
    )
    matching_backend = FakeBackend(_response("sdk-old"))
    asyncio.run(
        AgentRuntimeService(matching_backend).run_turn(
            session, user_prompt="same", profile=original, cwd=tmp_path
        )
    )
    assert matching_backend.requests[0].resume_session_id == "sdk-old"
    assert not matching_backend.requests[0].fork_session

    changed = _profile(tmp_path, model="model-b")
    changed_backend = FakeBackend(_response("sdk-new"))
    asyncio.run(
        AgentRuntimeService(changed_backend).run_turn(
            session, user_prompt="changed", profile=changed, cwd=tmp_path
        )
    )
    assert changed_backend.requests[0].resume_session_id is None


def test_scientific_fork_uses_sdk_fork_when_parent_binding_is_compatible(tmp_path: Path) -> None:
    parent = AnalysisSession.create(tmp_path / "sessions", title="Parent", session_id="parent")
    profile = _profile(tmp_path)
    parent.checkpoint_facts({"dataset": {"path": "/data/a.h5ad"}}, reason="loaded")
    parent.bind_runtime(
        runtime="claude-agent-sdk",
        runtime_session_id="sdk-parent",
        model_profile=profile.name,
        model_profile_fingerprint=profile.fingerprint,
        transport=profile.transport,
        model=profile.model,
    )
    forked = parent.fork(title="Alternative", session_id="branch")

    plan = forked.plan_resume(
        runtime="claude-agent-sdk",
        model_profile=profile.name,
        model_profile_fingerprint=profile.fingerprint,
    )
    backend = FakeBackend(_response("sdk-branch"))
    asyncio.run(
        AgentRuntimeService(backend).run_turn(
            forked, user_prompt="try another approach", profile=profile, cwd=tmp_path
        )
    )

    assert plan.mode is ResumeMode.FORK
    assert forked.store.state.facts["dataset"]["path"] == "/data/a.h5ad"
    assert backend.requests[0].resume_session_id == "sdk-parent"
    assert backend.requests[0].fork_session
    assert forked.store.state.runtime["active"]["runtime_session_id"] == "sdk-branch"


def test_clean_interrupt_records_a_stopped_turn_and_keeps_exact_resume(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Stopped")
    session.checkpoint_facts({"dataset": {"path": "/data/a.h5ad"}}, reason="loaded")
    interrupted = RuntimeResponse(
        runtime_session_id="sdk-live",
        messages=(RuntimeMessage("text", "partial"),),
        final_text="partial",
        stop_reason="interrupted",
        is_error=False,
        subtype="interrupted",
        interrupted=True,
    )
    backend = FakeBackend(interrupted)
    profile = _profile(tmp_path)

    response = asyncio.run(
        AgentRuntimeService(backend).run_turn(
            session, user_prompt="stop me", profile=profile, cwd=tmp_path
        )
    )

    assert response.interrupted
    assert session.store.state.facts["dataset"]["path"] == "/data/a.h5ad"
    # The model conversation is still resumable exactly, so the next turn is not a rebuild.
    assert session.store.state.runtime["active"]["runtime_session_id"] == "sdk-live"
    kinds = [event.kind for event in session.store.events()]
    assert kinds[-2:] == ["runtime.bound", "runtime.turn_interrupted"]
    assert session.store.events()[-1].payload["forced"] is False


def test_forced_interrupt_records_the_stop_and_binds_the_session_reached(tmp_path: Path) -> None:
    class CancellingBackend:
        runtime_name = "claude-agent-sdk"
        last_runtime_session_id = "sdk-partial"

        async def execute(self, request: RuntimeRequest) -> RuntimeResponse:
            raise asyncio.CancelledError

    session = AnalysisSession.create(tmp_path / "sessions", title="Forced")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            AgentRuntimeService(CancellingBackend()).run_turn(
                session, user_prompt="stop", profile=_profile(tmp_path), cwd=tmp_path
            )
        )

    event = session.store.events()[-1]
    assert event.kind == "runtime.turn_interrupted"
    assert event.payload["forced"] is True
    assert session.store.state.runtime["active"]["runtime_session_id"] == "sdk-partial"


def test_runtime_failure_is_recorded_without_losing_scientific_state(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Failure")
    session.checkpoint_facts({"dataset": {"path": "/data/a.h5ad"}}, reason="loaded")
    backend = FakeBackend(RuntimeExecutionError("gateway down"))

    with pytest.raises(RuntimeExecutionError, match="gateway down"):
        asyncio.run(
            AgentRuntimeService(backend).run_turn(
                session,
                user_prompt="continue",
                profile=_profile(tmp_path),
                cwd=tmp_path,
            )
        )

    assert session.store.state.facts["dataset"]["path"] == "/data/a.h5ad"
    assert session.store.events()[-1].kind == "runtime.turn_failed"


class SequentialBackend:
    runtime_name = "claude-agent-sdk"

    def __init__(self, outcomes: list[RuntimeResponse | Exception]):
        self.outcomes = outcomes
        self.requests: list[RuntimeRequest] = []

    async def execute(self, request: RuntimeRequest) -> RuntimeResponse:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RecordingObserver(NullRuntimeObserver):
    def __init__(self) -> None:
        self.rollovers: list[dict[str, object]] = []

    def on_context_rollover(
        self,
        *,
        reason: str,
        total_tokens: int | None,
        context_window_tokens: int | None,
    ) -> None:
        self.rollovers.append(
            {
                "reason": reason,
                "total_tokens": total_tokens,
                "context_window_tokens": context_window_tokens,
            }
        )


def _bind_exact(session: AnalysisSession, profile: ModelProfile) -> None:
    session.bind_runtime(
        runtime="claude-agent-sdk",
        runtime_session_id="sdk-full",
        model_profile=profile.name,
        model_profile_fingerprint=profile.fingerprint,
        transport=profile.transport,
        model=profile.model,
    )


def test_preflight_rollover_reconstructs_without_losing_scientific_state(
    tmp_path: Path,
) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Rollover")
    profile = _profile(tmp_path)
    session.store.record(
        "state.science_recorded",
        payload={"reason": "test"},
        state_patch={
            "facts": {"dataset": {"path": "/data/pbmc.h5ad"}, "clusters": {"count": 9}},
            "decisions": {"normalization": {"method": "log1p"}},
            "artifacts": {"umap": {"path": "artifacts/umap.png", "summary": "UMAP"}},
        },
    )
    _bind_exact(session, profile)
    before = session.store.state.to_dict()
    backend = SequentialBackend(
        [
            ContextRolloverRequired(
                "full",
                total_tokens=230_145,
                context_window_tokens=262_144,
                output_reserve_tokens=32_000,
                safety_margin_tokens=7_864,
                source="upstream:models",
            ),
            _response("sdk-reconstructed"),
        ]
    )
    observer = RecordingObserver()
    service = AgentRuntimeService(
        backend,
        model_limits=ModelLimits(262_144, 32_000, "upstream:models"),
        observer=observer,
    )

    response = asyncio.run(
        service.run_turn(session, user_prompt="continue annotation", profile=profile, cwd=tmp_path)
    )

    assert response.runtime_session_id == "sdk-reconstructed"
    assert backend.requests[0].resume_session_id == "sdk-full"
    assert backend.requests[1].resume_session_id is None
    assert '"clusters"' in backend.requests[1].prompt
    assert '"normalization"' in backend.requests[1].prompt
    assert "artifacts/umap.png" in backend.requests[1].prompt
    after = session.store.state.to_dict()
    assert after["facts"] == before["facts"]
    assert after["decisions"] == before["decisions"]
    assert after["artifacts"] == before["artifacts"]
    kinds = [event.kind for event in session.store.events()]
    assert "runtime.context_rolled_over" in kinds
    rollover = next(
        event for event in session.store.events() if event.kind == "runtime.context_rolled_over"
    )
    assert rollover.payload["scientific_state_revision"] >= before["revision"]
    assert session.store.state.runtime["active"]["runtime_session_id"] == "sdk-reconstructed"
    assert observer.rollovers == [
        {
            "reason": "runtime context reached its reserve during the active turn",
            "total_tokens": 230_145,
            "context_window_tokens": 262_144,
        }
    ]


def test_provider_context_error_rolls_over_once_in_automatic_mode(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Provider rejection")
    profile = _profile(tmp_path)
    _bind_exact(session, profile)
    rejected = RuntimeResponse(
        runtime_session_id="sdk-full",
        messages=(),
        final_text="ContextWindowExceededError: maximum context length is 262144 tokens",
        stop_reason=None,
        is_error=True,
        subtype="error_max_turns",
    )
    backend = SequentialBackend([rejected, _response("sdk-fresh")])

    response = asyncio.run(
        AgentRuntimeService(backend).run_turn(
            session, user_prompt="continue", profile=profile, cwd=tmp_path
        )
    )

    assert response.runtime_session_id == "sdk-fresh"
    assert [request.resume_session_id for request in backend.requests] == ["sdk-full", None]


def test_provider_context_error_in_a_fresh_turn_reconstructs_and_continues(
    tmp_path: Path,
) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Fresh provider rejection")
    profile = _profile(tmp_path)
    rejected = RuntimeResponse(
        runtime_session_id="sdk-fresh-full",
        messages=(),
        final_text="ContextWindowExceededError: maximum context length is 262144 tokens",
        stop_reason=None,
        is_error=True,
        subtype="error",
    )
    backend = SequentialBackend([rejected, _response("sdk-rebuilt")])

    response = asyncio.run(
        AgentRuntimeService(backend).run_turn(
            session, user_prompt="analyze end to end", profile=profile, cwd=tmp_path
        )
    )

    assert response.runtime_session_id == "sdk-rebuilt"
    assert [request.resume_session_id for request in backend.requests] == [None, None]
    rollover = next(
        event for event in session.store.events() if event.kind == "runtime.context_rolled_over"
    )
    assert rollover.payload["old_runtime_session_id"] == "sdk-fresh-full"


def test_one_turn_can_roll_over_more_than_once(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Long analysis")
    profile = _profile(tmp_path)
    backend = SequentialBackend(
        [
            ContextRolloverRequired("first full", total_tokens=230_000),
            ContextRolloverRequired("second full", total_tokens=231_000),
            _response("sdk-third-conversation"),
        ]
    )

    response = asyncio.run(
        AgentRuntimeService(backend).run_turn(
            session, user_prompt="continue until complete", profile=profile, cwd=tmp_path
        )
    )

    assert response.runtime_session_id == "sdk-third-conversation"
    assert len(backend.requests) == 3
    assert all(request.resume_session_id is None for request in backend.requests)
    assert len(
        [event for event in session.store.events() if event.kind == "runtime.context_rolled_over"]
    ) == 2


def test_explicit_exact_resume_never_silently_reconstructs(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Exact only")
    profile = _profile(tmp_path)
    _bind_exact(session, profile)
    backend = SequentialBackend(
        [
            ContextRolloverRequired(
                "full",
                total_tokens=250_000,
                context_window_tokens=262_144,
            )
        ]
    )

    with pytest.raises(RuntimeExecutionError, match="Exact resume was selected"):
        asyncio.run(
            AgentRuntimeService(
                backend,
                resume_preference=ResumePreference.EXACT,
            ).run_turn(session, user_prompt="continue", profile=profile, cwd=tmp_path)
        )

    assert len(backend.requests) == 1
    assert not any(
        event.kind == "runtime.context_rolled_over" for event in session.store.events()
    )


def test_reconstructed_preference_skips_compatible_runtime_history(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Rebuild")
    profile = _profile(tmp_path)
    session.checkpoint_facts({"dataset": {"path": "/data/a.h5ad"}}, reason="loaded")
    _bind_exact(session, profile)
    backend = FakeBackend(_response("sdk-new"))

    asyncio.run(
        AgentRuntimeService(
            backend,
            resume_preference=ResumePreference.RECONSTRUCTED,
        ).run_turn(session, user_prompt="continue", profile=profile, cwd=tmp_path)
    )

    assert backend.requests[0].resume_session_id is None
    assert "/data/a.h5ad" in backend.requests[0].prompt


def test_subsequent_exact_turn_uses_checkpoint_reference_not_full_state_dump(
    tmp_path: Path,
) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Compact exact prompts")
    profile = _profile(tmp_path)
    session.checkpoint_facts({"large_note": "marker evidence " * 1000}, reason="evidence")
    _bind_exact(session, profile)
    backend = SequentialBackend([_response("sdk-full"), _response("sdk-full")])
    service = AgentRuntimeService(backend)

    asyncio.run(service.run_turn(session, user_prompt="first", profile=profile, cwd=tmp_path))
    asyncio.run(service.run_turn(session, user_prompt="second", profile=profile, cwd=tmp_path))

    assert "marker evidence" in backend.requests[0].prompt
    assert "marker evidence" not in backend.requests[1].prompt
    assert "authoritative_state" in backend.requests[1].prompt
    assert len(backend.requests[1].prompt) < len(backend.requests[0].prompt)


def test_new_session_plans_fresh_without_checkpoint_framing(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Fresh")
    profile = _profile(tmp_path)

    plan = session.plan_resume(
        runtime="claude-agent-sdk",
        model_profile=profile.name,
        model_profile_fingerprint=profile.fingerprint,
    )

    assert plan.mode is ResumeMode.FRESH
    assert plan.runtime_session_id is None
    assert plan.context == ""


def test_reopened_session_with_state_is_not_fresh(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="Has state")
    profile = _profile(tmp_path)
    session.checkpoint_facts({"dataset": {"path": "/data/a.h5ad"}}, reason="loaded")

    # No resumable runtime session recorded (e.g. a local backend that never bound one), so this
    # falls to reconstruction — but it must NOT be mistaken for a brand-new session.
    plan = session.plan_resume(
        runtime="claude-agent-sdk",
        model_profile=profile.name,
        model_profile_fingerprint=profile.fingerprint,
    )

    assert plan.mode is ResumeMode.RECONSTRUCTED
    assert "durable checkpoint" in plan.context
