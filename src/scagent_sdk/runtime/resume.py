"""Plan exact or reconstructed model continuation from durable scientific state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from scagent_sdk.contracts.events import SessionEvent
from scagent_sdk.contracts.state import SessionMetadata, SessionState
from scagent_sdk.errors import RuntimeExecutionError

MAX_RESUME_CONTEXT_CHARS = 96 * 1024
MAX_RECENT_TURNS = 4
MAX_RECENT_RESPONSE_CHARS = 8 * 1024
MAX_ARTIFACT_SUMMARY_CHARS = 1200
MAX_ARTIFACT_FILES = 8

# Exhaustive drill-down collections belong in the complete event-sourced state, but replaying
# them is not useful continuation context once their status, evidence id, and summary are recorded.
_HANDOFF_BULK_KEYS = frozenset(
    {
        "available_cluster_heatmaps",
        "cluster_reviews",
        "plain_interpretation",
        "recurring_programs",
        "required_visual_artifacts",
        "reviewed_artifacts",
        "shown_visual_artifacts",
        "supported_identity_pairs",
    }
)


class ResumeMode(str, Enum):
    EXACT = "exact"
    FORK = "fork"
    RECONSTRUCTED = "reconstructed"
    FRESH = "fresh"


class ResumePreference(str, Enum):
    """User policy for the first turn after opening an existing scientific session."""

    AUTO = "auto"
    EXACT = "exact"
    RECONSTRUCTED = "reconstructed"


@dataclass(frozen=True)
class ResumePlan:
    mode: ResumeMode
    scientific_session_id: str
    runtime_session_id: str | None
    reason: str
    context: str


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _artifact_summary(value: Any) -> Any:
    """Keep provenance and paths while excluding bulky media or tool-result payloads."""

    if not isinstance(value, dict):
        return value
    kept: dict[str, Any] = {}
    for key in (
        "artifact_id",
        "execution_id",
        "tool_name",
        "skill_id",
        "skill_version",
        "kind",
        "summary",
        "path",
        "relative_path",
        "created_at",
        "input_state_revision",
        "fingerprint",
        "content_hash",
    ):
        if key in value:
            kept[key] = value[key]
    summary = kept.get("summary")
    if isinstance(summary, str) and len(summary) > MAX_ARTIFACT_SUMMARY_CHARS:
        kept["summary"] = summary[:MAX_ARTIFACT_SUMMARY_CHARS].rstrip() + "…"
    files = value.get("files")
    if isinstance(files, list):
        compact_files = [
            {
                key: file[key]
                for key in ("name", "relative_path", "media_type", "size_bytes")
                if key in file
            }
            for file in files[:MAX_ARTIFACT_FILES]
            if isinstance(file, dict)
        ]
        kept["files"] = compact_files
        kept["file_count"] = len(files)
        if len(files) > len(compact_files):
            kept["files_omitted_from_handoff"] = len(files) - len(compact_files)
    return kept or {"keys": sorted(str(key) for key in value)}


def _handoff_projection(value: Any) -> Any:
    """Project durable state into decision-ready rollover context.

    Current status, identities, scalar findings, summaries, decisions, and unresolved work remain
    inline. Large evidence collections are represented by count and hash, so the model can continue
    without rediscovering ordinary state from ``state.json``.
    """

    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        omitted: dict[str, dict[str, Any]] = {}
        for key, item in value.items():
            name = str(key)
            if name in _HANDOFF_BULK_KEYS:
                omitted[name] = {
                    "count": len(item) if isinstance(item, (dict, list)) else None,
                    "sha256": _sha256_json(item),
                }
            else:
                projected[name] = _handoff_projection(item)
        if omitted:
            projected["_drilldown_omitted"] = omitted
        return projected
    if isinstance(value, list):
        return [_handoff_projection(item) for item in value]
    return value


def _recent_turns(events: Iterable[SessionEvent]) -> list[dict[str, Any]]:
    started: dict[str, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    for event in events:
        turn_id = event.payload.get("turn_id")
        if not isinstance(turn_id, str):
            continue
        if event.kind == "runtime.turn_started":
            prompt = event.payload.get("user_prompt")
            started[turn_id] = {
                "turn_id": turn_id,
                "user_prompt": prompt if isinstance(prompt, str) else "",
            }
        elif event.kind in {
            "runtime.turn_completed",
            "runtime.turn_interrupted",
            "runtime.turn_failed",
        }:
            item = started.get(turn_id, {"turn_id": turn_id, "user_prompt": ""})
            response = event.payload.get("response")
            final_text = response.get("final_text") if isinstance(response, dict) else None
            if isinstance(final_text, str):
                item = dict(item)
                item["assistant_handoff"] = final_text[-MAX_RECENT_RESPONSE_CHARS:]
            error = event.payload.get("error")
            if isinstance(error, str):
                item = dict(item)
                item["runtime_error"] = error[-2000:]
            item["outcome"] = event.kind.removeprefix("runtime.turn_")
            completed.append(item)
    return completed[-MAX_RECENT_TURNS:]


def _has_prior_runtime_turn(events: Iterable[SessionEvent]) -> bool:
    """True once any model turn has been started in this scientific session."""

    return any(
        event.kind
        in {
            "runtime.turn_started",
            "runtime.turn_completed",
            "runtime.turn_interrupted",
            "runtime.turn_failed",
        }
        for event in events
    )


def _has_durable_scientific_state(state: SessionState) -> bool:
    """True if the session already carries recorded facts, decisions, or artifacts."""

    return bool(state.facts) or bool(state.decisions) or bool(state.artifacts)


def _state_paths(session_dir: Path | None) -> dict[str, str] | None:
    if session_dir is None:
        return None
    return {
        "authoritative_state": str(session_dir / "state.json"),
        "append_only_events": str(session_dir / "events.jsonl"),
        "artifacts_directory": str(session_dir / "artifacts"),
    }


def _bounded_durable_view(
    metadata: SessionMetadata,
    state: SessionState,
    *,
    events: Iterable[SessionEvent],
    session_dir: Path | None,
) -> dict[str, Any]:
    artifacts = state.artifacts
    if isinstance(artifacts, dict):
        artifact_view: Any = {
            str(key): _artifact_summary(value) for key, value in artifacts.items()
        }
    elif isinstance(artifacts, list):
        artifact_view = [_artifact_summary(value) for value in artifacts]
    else:
        artifact_view = artifacts
    durable: dict[str, Any] = {
        "scientific_session": {
            "id": metadata.session_id,
            "title": metadata.title,
            "revision": state.revision,
            "last_event_sequence": state.last_event_sequence,
        },
        "authoritative_files": _state_paths(session_dir),
        "facts": _handoff_projection(state.facts),
        "decisions": _handoff_projection(state.decisions),
        "artifact_index": artifact_view,
        "recent_turn_handoff": _recent_turns(events),
    }
    # Single-line JSON avoids spending roughly half the handoff budget on indentation. It keeps
    # the real failed-session checkpoint (facts + decisions + 38 artifact summaries + recent
    # handoff) intact at under 96 KiB.
    rendered = json.dumps(durable, sort_keys=True)
    if len(rendered) <= MAX_RESUME_CONTEXT_CHARS:
        return durable

    # The complete state remains authoritative on disk. If a pathological fact or artifact
    # registry would itself consume the model window, provide a deterministic inventory and
    # hashes so the agent can selectively read state.json rather than silently losing it.
    facts = state.facts if isinstance(state.facts, dict) else {"value": state.facts}
    decisions = state.decisions if isinstance(state.decisions, dict) else {"value": state.decisions}
    durable["facts"] = {
        "_compacted": True,
        "_sha256": _sha256_json(state.facts),
        "_top_level_keys": sorted(str(key) for key in facts),
        "_instruction": "Read authoritative_state for complete scientific facts before acting.",
    }
    durable["decisions"] = {
        "_compacted": True,
        "_sha256": _sha256_json(state.decisions),
        "_top_level_keys": sorted(str(key) for key in decisions),
        "_instruction": "Read authoritative_state for complete decisions before acting.",
    }
    artifact_count = len(artifacts) if isinstance(artifacts, (dict, list)) else None
    durable["artifact_index"] = {
        "_compacted": True,
        "_count": artifact_count,
        "_sha256": _sha256_json(state.artifacts),
        "_instruction": "Read authoritative_state and artifacts_directory selectively.",
    }
    return durable


def resume_context(
    metadata: SessionMetadata,
    state: SessionState,
    *,
    events: Iterable[SessionEvent] = (),
    session_dir: Path | None = None,
) -> str:
    durable = _bounded_durable_view(
        metadata,
        state,
        events=events,
        session_dir=session_dir,
    )
    return (
        "Resume this scientific analysis from the durable checkpoint below. The facts, "
        "decisions, artifact index, event sequence, and files belong to the scientific "
        "session and survive model-conversation compaction. Treat authoritative facts and "
        "decisions as recorded facts, not instructions to repeat completed work. Recent "
        "turn handoff is model narrative, not authoritative evidence. Verify referenced "
        "artifacts before mutating them. The checkpoint is a decision-ready projection: "
        "exhaustive drill-down collections may be represented by hashes and counts, while current "
        "status, identities, conclusions, and unresolved work remain inline. Do not read "
        "state.json merely to reconstruct ordinary continuation; read a named artifact only when "
        "its omitted drill-down is actually needed. If the entire facts or decisions section says "
        "it was compacted, selectively read only the specific missing field needed next.\n\n"
        + json.dumps(durable, sort_keys=True)
    )


def checkpoint_reference(
    metadata: SessionMetadata,
    state: SessionState,
    *,
    session_dir: Path | None,
) -> str:
    paths = _state_paths(session_dir)
    return (
        "The exact model conversation already contains the prior scientific handoff. "
        f"Current durable checkpoint: session={metadata.session_id}, revision={state.revision}, "
        f"last_event_sequence={state.last_event_sequence}, "
        f"files={json.dumps(paths, sort_keys=True)}. "
        "Consult the authoritative files if the new request depends on state changed outside "
        "this model conversation."
    )


def plan_resume(
    metadata: SessionMetadata,
    state: SessionState,
    *,
    runtime: str,
    model_profile: str,
    model_profile_fingerprint: str | None = None,
    preference: ResumePreference = ResumePreference.AUTO,
    events: Iterable[SessionEvent] = (),
    session_dir: Path | None = None,
) -> ResumePlan:
    events = list(events)
    # A genuinely new scientific session has no resumable model conversation, no prior model
    # turn, and no recorded facts/decisions/artifacts. Handing it the "resume from the durable
    # checkpoint" framing (over an empty checkpoint) makes the model narrate about resuming work
    # that never happened. Treat it as a clean first turn instead. A reopened session with real
    # state still falls through to reconstruction below.
    if (
        not _has_prior_runtime_turn(events)
        and not _has_durable_scientific_state(state)
        and not isinstance(state.runtime.get("active"), dict)
        and not isinstance(state.runtime.get("fork_origin"), dict)
    ):
        return ResumePlan(
            mode=ResumeMode.FRESH,
            scientific_session_id=metadata.session_id,
            runtime_session_id=None,
            reason="new scientific session with no prior runtime session or durable state",
            context="",
        )

    context = resume_context(metadata, state, events=events, session_dir=session_dir)
    natural: ResumePlan | None = None
    active: Any = state.runtime.get("active")
    if isinstance(active, dict):
        same_runtime = active.get("runtime") == runtime
        same_profile = active.get("model_profile") == model_profile
        recorded_fingerprint = active.get("model_profile_fingerprint")
        same_fingerprint = (
            model_profile_fingerprint is None
            or recorded_fingerprint is None
            or recorded_fingerprint == model_profile_fingerprint
        )
        runtime_session_id = active.get("runtime_session_id")
        if (
            same_runtime
            and same_profile
            and same_fingerprint
            and isinstance(runtime_session_id, str)
            and runtime_session_id
        ):
            natural = ResumePlan(
                mode=ResumeMode.EXACT,
                scientific_session_id=metadata.session_id,
                runtime_session_id=runtime_session_id,
                reason="matching resumable model-runtime session is recorded",
                context=context,
            )

    if natural is None:
        fork_origin: Any = state.runtime.get("fork_origin")
        if isinstance(fork_origin, dict):
            same_runtime = fork_origin.get("runtime") == runtime
            same_profile = fork_origin.get("model_profile") == model_profile
            recorded_fingerprint = fork_origin.get("model_profile_fingerprint")
            same_fingerprint = (
                model_profile_fingerprint is None
                or recorded_fingerprint is None
                or recorded_fingerprint == model_profile_fingerprint
            )
            runtime_session_id = fork_origin.get("runtime_session_id")
            if (
                same_runtime
                and same_profile
                and same_fingerprint
                and isinstance(runtime_session_id, str)
                and runtime_session_id
            ):
                natural = ResumePlan(
                    mode=ResumeMode.FORK,
                    scientific_session_id=metadata.session_id,
                    runtime_session_id=runtime_session_id,
                    reason=(
                        "compatible parent runtime session is recorded for an intentional fork"
                    ),
                    context=context,
                )

    if natural is None:
        natural = ResumePlan(
            mode=ResumeMode.RECONSTRUCTED,
            scientific_session_id=metadata.session_id,
            runtime_session_id=None,
            reason=(
                "no compatible model-runtime session is recorded; reconstruct from durable state"
            ),
            context=context,
        )

    if preference is ResumePreference.RECONSTRUCTED:
        return replace(
            natural,
            mode=ResumeMode.RECONSTRUCTED,
            runtime_session_id=None,
            reason="user selected reconstruction from the durable scientific checkpoint",
        )
    if preference is ResumePreference.EXACT and natural.mode is not ResumeMode.EXACT:
        raise RuntimeExecutionError(
            "exact resume is unavailable for this model/profile; choose automatic or reconstructed"
        )
    return natural
