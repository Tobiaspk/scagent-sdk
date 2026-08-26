from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scagent_sdk.errors import (
    EventLogCorruptionError,
    SessionIdentityError,
    SessionNotFoundError,
)
from scagent_sdk.runtime.resume import ResumeMode
from scagent_sdk.session import AnalysisSession
from scagent_sdk.state.store import (
    SessionStore,
    apply_merge_patch,
    new_session_id,
    validate_session_id,
)

_ID_PATTERN = re.compile(r"run_\d{8}T\d{6}Z_[0-9a-f]{6}")


def test_default_session_id_is_time_ordered_and_unique(tmp_path: Path) -> None:
    first = AnalysisSession.create(tmp_path, title="A")
    second = AnalysisSession.create(tmp_path, title="B")

    assert _ID_PATTERN.fullmatch(first.session_id)
    assert _ID_PATTERN.fullmatch(second.session_id)
    assert first.session_id != second.session_id
    # The leading UTC timestamp means later sessions never sort before earlier
    # ones (suffix ordering only breaks ties within the same second).
    stamp = len("run_20260723T175848Z")
    assert first.session_id[:stamp] <= second.session_id[:stamp]


def test_generated_ids_are_unique_within_same_second() -> None:
    ids = {new_session_id() for _ in range(200)}
    assert len(ids) == 200


@pytest.mark.parametrize("bad", ["../escape", "..", "a/b", "", ".hidden", "with space"])
def test_traversal_and_unsafe_ids_are_rejected(bad: str) -> None:
    with pytest.raises(SessionIdentityError):
        validate_session_id(bad)


def test_open_rejects_unsafe_session_id(tmp_path: Path) -> None:
    with pytest.raises(SessionIdentityError):
        SessionStore.open(tmp_path, "../etc")


def test_merge_patch_is_recursive_and_does_not_mutate_input() -> None:
    original = {"a": {"b": 1, "remove": True}, "keep": [1]}
    updated = apply_merge_patch(original, {"a": {"b": 2, "remove": None}, "new": 3})

    assert updated == {"a": {"b": 2}, "keep": [1], "new": 3}
    assert original == {"a": {"b": 1, "remove": True}, "keep": [1]}


def test_create_and_resume_preserve_scientific_state(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path, title="PBMC analysis", session_id="session-a")
    session.checkpoint_facts(
        {
            "dataset": {"path": "/data/pbmc.h5ad", "revision": "sha256:abc"},
            "clustering": {"id": "leiden-r1", "n_clusters": 9},
        },
        reason="inspection and clustering completed",
        actor="test",
    )

    resumed = AnalysisSession.resume(tmp_path, "session-a")

    assert resumed.store.metadata.title == "PBMC analysis"
    assert resumed.store.state.facts["dataset"]["revision"] == "sha256:abc"
    assert resumed.store.state.facts["clustering"]["id"] == "leiden-r1"
    assert resumed.store.state.revision == 1
    assert [event.sequence for event in resumed.store.events()] == [1, 2]
    assert (resumed.directory / "outputs.md").is_file()
    assert (resumed.directory / "outputs.json").is_file()


def test_scientific_state_survives_without_model_runtime_history(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path, title="Runtime-independent")
    session.checkpoint_facts({"qc": {"clustering_id": "round-1", "passed": True}}, reason="qc")

    plan = session.plan_resume(runtime="claude-agent-sdk", model_profile="local-qwen")

    assert plan.mode is ResumeMode.RECONSTRUCTED
    assert plan.runtime_session_id is None
    assert '"clustering_id": "round-1"' in plan.context
    assert "not instructions to repeat completed work" in plan.context


def test_matching_runtime_binding_enables_exact_resume(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path, title="Exact resume")
    session.bind_runtime(
        runtime="claude-agent-sdk",
        runtime_session_id="sdk-session-123",
        model_profile="local-qwen",
        transport="litellm",
        model="Qwen3.6-35B-A3B",
    )

    exact = session.plan_resume(runtime="claude-agent-sdk", model_profile="local-qwen")
    changed = session.plan_resume(runtime="claude-agent-sdk", model_profile="local-nemotron")

    assert exact.mode is ResumeMode.EXACT
    assert exact.runtime_session_id == "sdk-session-123"
    assert changed.mode is ResumeMode.RECONSTRUCTED
    assert changed.runtime_session_id is None


def test_reconstructed_handoff_includes_recent_narrative_and_authoritative_paths(
    tmp_path: Path,
) -> None:
    session = AnalysisSession.create(tmp_path, title="Handoff")
    session.checkpoint_facts(
        {"annotation": {"cluster_0": {"label": "CD4 T", "evidence": ["IL7R", "LTB"]}}},
        reason="consensus",
    )
    session.store.record(
        "runtime.turn_started",
        payload={"turn_id": "turn-1", "user_prompt": "Annotate the clusters"},
        actor="user",
    )
    session.store.record(
        "runtime.turn_completed",
        payload={
            "turn_id": "turn-1",
            "response": {
                "final_text": "Cluster 0 was assigned only after consensus validation."
            },
        },
    )

    plan = session.plan_resume(runtime="claude-agent-sdk", model_profile="new-profile")

    assert '"cluster_0"' in plan.context
    assert "Cluster 0 was assigned only after consensus validation." in plan.context
    assert str(session.directory / "state.json") in plan.context
    assert "model narrative, not authoritative evidence" in plan.context


def test_reconstructed_handoff_preserves_the_request_from_a_failed_turn(
    tmp_path: Path,
) -> None:
    session = AnalysisSession.create(tmp_path, title="Failed handoff")
    session.store.record(
        "runtime.turn_started",
        payload={"turn_id": "turn-failed", "user_prompt": "Finish the consensus annotation"},
        actor="user",
    )
    session.store.record(
        "runtime.turn_failed",
        payload={
            "turn_id": "turn-failed",
            "error": "ContextWindowExceededError",
        },
    )

    plan = session.plan_resume(runtime="claude-agent-sdk", model_profile="new-profile")

    assert "Finish the consensus annotation" in plan.context
    assert "ContextWindowExceededError" in plan.context
    assert '"outcome": "failed"' in plan.context


def test_pathological_state_is_hashed_and_points_to_complete_authoritative_file(
    tmp_path: Path,
) -> None:
    session = AnalysisSession.create(tmp_path, title="Large state")
    session.checkpoint_facts(
        {"large_evidence": "scientific evidence " * 10_000},
        reason="large evidence registry",
    )

    plan = session.plan_resume(runtime="claude-agent-sdk", model_profile="new-profile")

    assert '"_compacted": true' in plan.context
    assert '"_sha256": "sha256:' in plan.context
    assert str(session.directory / "state.json") in plan.context
    assert len(plan.context) < 110_000


def test_normal_large_state_keeps_decision_ready_projection_inline(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path, title="Decision-ready rollover")
    session.checkpoint_facts(
        {
            "analysis": {"clustering": {"key": "leiden_res_1_0", "n_clusters": 29}},
            "batch": {
                "evidence": {
                    "evidence_id": "batch-evidence:abc",
                    "status": "complete",
                    "recommendation": "cannot_determine_technical_vs_biological",
                    "terminal_summary": "Donors separate, but design is unknown.",
                    "supported_identity_pairs": [
                        {"shared_genes": ["SFTPA1", "SFTPB"]} for _ in range(500)
                    ],
                }
            },
            "cluster_qc": {
                "status": "complete",
                "review_status": "resolved",
                "review_clusters": [],
                "available_cluster_heatmaps": [f"cluster-{i}.png" for i in range(500)],
            },
        },
        reason="realistic large scientific state",
    )
    session.store.record(
        "state.decision_recorded",
        payload={"reason": "user choice"},
        state_patch={
            "decisions": {
                "batch_handling": {
                    "decision": "integrate",
                    "evidence_id": "batch-evidence:abc",
                }
            }
        },
    )

    plan = session.plan_resume(runtime="claude-agent-sdk", model_profile="new-profile")

    assert '"evidence_id": "batch-evidence:abc"' in plan.context
    assert '"decision": "integrate"' in plan.context
    assert '"review_status": "resolved"' in plan.context
    assert '"_drilldown_omitted"' in plan.context
    assert '"_compacted": true' not in plan.context
    assert "Do not read state.json merely to reconstruct ordinary continuation" in plan.context


def test_open_replays_event_written_after_last_state_checkpoint(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path, title="Replay", session_id="replay")
    event_path = session.store.events_path
    state_path = session.store.state_path

    existing = session.store.events()
    event = existing[-1]
    replay_event = event.__class__(
        session_id="replay",
        sequence=2,
        kind="state.facts_checkpointed",
        payload={"reason": "simulated crash"},
        state_patch={"facts": {"dataset": {"path": "/data/recovered.h5ad"}}},
    )
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(replay_event.to_dict()) + "\n")

    before = json.loads(state_path.read_text())
    assert before["last_event_sequence"] == 1

    resumed = AnalysisSession.resume(tmp_path, "replay")

    assert resumed.store.state.last_event_sequence == 2
    assert resumed.store.state.facts["dataset"]["path"] == "/data/recovered.h5ad"


def test_corrupt_event_log_is_reported_instead_of_silently_ignored(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path, title="Corrupt", session_id="corrupt")
    with session.store.events_path.open("a", encoding="utf-8") as handle:
        handle.write("{not-json}\n")

    with pytest.raises(EventLogCorruptionError, match="invalid event"):
        AnalysisSession.resume(tmp_path, "corrupt")


def test_list_sessions_is_recent_first(tmp_path: Path) -> None:
    first = AnalysisSession.create(tmp_path, title="First", session_id="first")
    AnalysisSession.create(tmp_path, title="Second", session_id="second")
    first.checkpoint_facts({"touched": True}, reason="make first most recent")

    items = SessionStore.list_sessions(tmp_path)

    assert [item.session_id for item in items] == ["first", "second"]


def test_missing_session_has_specific_error(tmp_path: Path) -> None:
    with pytest.raises(SessionNotFoundError, match="not found"):
        AnalysisSession.resume(tmp_path, "missing")
