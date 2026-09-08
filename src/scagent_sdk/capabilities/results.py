"""Deterministic capability execution inputs and result envelopes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scagent_sdk.contracts._json import ensure_jsonable, require_mapping
from scagent_sdk.errors import CapabilityExecutionError

RESULT_SCHEMA_VERSION = 1
INLINE_RESULT_LIMIT_BYTES = 48 * 1024
# Figures are model context, not merely transport bytes. Skills keep complete figure sets as
# artifacts and attach only the compact views needed for the immediate decision; this ceiling is
# a second line of defense against accidentally replaying dozens of images into one turn.
MODEL_MEDIA_LIMIT = 8
# Per-image and per-result byte budgets for pixels sent to the model. A downscaled preview is
# far below these; the ceilings exist so one oversized figure cannot consume a turn's transport.
MODEL_MEDIA_LIMIT_BYTES = 2 * 1024 * 1024
MODEL_MEDIA_TOTAL_BYTES = 8 * 1024 * 1024
MODEL_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


def capability_artifact_directory_name(tool_name: str, execution_id: str) -> str:
    """Stable, readable canonical directory name for one capability execution.

    The committed artifact directory is named ``<action>--<execution_id>`` so a session tree reads
    as what each step did rather than as a wall of UUIDs. The execution ID is kept verbatim as the
    suffix, so it remains recoverable from the directory name and every ``endswith("--<id>")`` check
    that resolves an artifact by ID still matches. ``action`` is the tool name reduced to lowercase
    alphanumerics with runs of other characters collapsed to a single ``-``.
    """

    action = "".join(
        character if character.isalnum() else "-"
        for character in tool_name.casefold()
    )
    action = "-".join(part for part in action.split("-") if part)
    return f"{action or 'capability'}--{execution_id}"


@dataclass(frozen=True)
class CapabilityContext:
    scientific_session_id: str
    session_dir: Path
    staging_dir: Path
    skill_id: str
    tool_name: str
    execution_id: str
    state_revision: int
    state_facts: dict[str, Any]
    # Read-only view of the artifact lineage. Supplied so a tool can describe the available
    # versions or validate a switch target; only the executor ever mutates it.
    state_lineage: dict[str, Any] = field(default_factory=dict)
    # The session-relative directory this execution's committed artifacts will live under, e.g.
    # ``artifacts/capabilities/<action>--<execution_id>``. A skill that records a path to one of
    # its own outputs into facts MUST build it from this prefix rather than re-deriving it from
    # ``execution_id``: the committed directory is named for the action (see
    # ``capability_artifact_directory_name``), so a hand-built ``artifacts/capabilities/<id>/…``
    # path points at a directory that does not exist and every later resolve-by-path fails.
    artifact_relative_path: str = ""
    # Durable user/scientist decisions are session-scoped and can outlive the node-scoped fact
    # snapshot on the active artifact. Reporting capabilities need this read-only view so they can
    # distinguish "not recorded" from "recorded on another lineage / no longer current".
    state_decisions: dict[str, Any] = field(default_factory=dict)
    # Manifest version of the executing skill. Report/recipe generators must use this value rather
    # than a hand-maintained literal that can drift from capability.yaml.
    skill_version: str = ""


@dataclass(frozen=True)
class ProducedArtifact:
    name: str
    relative_path: str
    media_type: str = "application/octet-stream"

    @classmethod
    def from_dict(cls, value: Any) -> ProducedArtifact:
        data = require_mapping(value, name="artifact")
        try:
            artifact = cls(
                name=str(data["name"]),
                relative_path=str(data["relative_path"]),
                media_type=str(data.get("media_type", "application/octet-stream")),
            )
        except KeyError as exc:
            raise CapabilityExecutionError(
                f"artifact is missing required field: {exc.args[0]}"
            ) from exc
        if not artifact.name.strip() or not artifact.relative_path.strip():
            raise CapabilityExecutionError("artifact name and relative_path must not be empty")
        if Path(artifact.relative_path).is_absolute():
            raise CapabilityExecutionError("artifact relative_path must be relative")
        return artifact


@dataclass(frozen=True)
class ModelMedia:
    """A staged artifact that should also be attached to the model tool result."""

    name: str
    relative_path: str
    media_type: str

    @classmethod
    def from_dict(cls, value: Any) -> ModelMedia:
        data = require_mapping(value, name="model_media")
        try:
            media = cls(
                name=str(data["name"]),
                relative_path=str(data["relative_path"]),
                media_type=str(data["media_type"]),
            )
        except KeyError as exc:
            raise CapabilityExecutionError(
                f"model_media is missing required field: {exc.args[0]}"
            ) from exc
        if not media.name.strip() or not media.relative_path.strip():
            raise CapabilityExecutionError("model_media name and relative_path must not be empty")
        if Path(media.relative_path).is_absolute():
            raise CapabilityExecutionError("model_media relative_path must be relative")
        if media.media_type not in MODEL_IMAGE_TYPES:
            raise CapabilityExecutionError(
                f"unsupported model image media type: {media.media_type}"
            )
        return media


@dataclass(frozen=True)
class CapabilityResult:
    summary: str
    details: Any = None
    facts_patch: dict[str, Any] = field(default_factory=dict)
    decisions_patch: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[ProducedArtifact, ...] = ()
    model_media: tuple[ModelMedia, ...] = ()
    schema_version: int = RESULT_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: Any) -> CapabilityResult:
        data = require_mapping(value, name="capability result")
        try:
            result = cls(
                schema_version=int(data.get("schema_version", RESULT_SCHEMA_VERSION)),
                summary=str(data["summary"]),
                details=data.get("details"),
                facts_patch=require_mapping(data.get("facts_patch", {}), name="facts_patch"),
                decisions_patch=require_mapping(
                    data.get("decisions_patch", {}), name="decisions_patch"
                ),
                artifacts=tuple(
                    ProducedArtifact.from_dict(item) for item in data.get("artifacts", [])
                ),
                model_media=tuple(
                    ModelMedia.from_dict(item) for item in data.get("model_media", [])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CapabilityExecutionError(f"invalid capability result: {exc}") from exc
        if result.schema_version != RESULT_SCHEMA_VERSION:
            raise CapabilityExecutionError(
                f"unsupported capability result schema: {result.schema_version}"
            )
        if not result.summary.strip():
            raise CapabilityExecutionError("capability result summary must not be empty")
        ensure_jsonable(result.details, name="details")
        ensure_jsonable(result.facts_patch, name="facts_patch")
        ensure_jsonable(result.decisions_patch, name="decisions_patch")
        if len(result.model_media) > MODEL_MEDIA_LIMIT:
            raise CapabilityExecutionError(
                f"model_media may contain at most {MODEL_MEDIA_LIMIT} images"
            )
        return result
