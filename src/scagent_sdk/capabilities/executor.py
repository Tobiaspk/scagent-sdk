"""Stage deterministic tool results and commit them after PostToolUse."""

from __future__ import annotations

import asyncio
import base64
import filecmp
import hashlib
import inspect
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from scagent_sdk.capabilities.manifest import CapabilityTool
from scagent_sdk.capabilities.registry import SkillPackage
from scagent_sdk.capabilities.results import (
    INLINE_RESULT_LIMIT_BYTES,
    MODEL_MEDIA_LIMIT_BYTES,
    MODEL_MEDIA_TOTAL_BYTES,
    CapabilityContext,
    CapabilityResult,
    capability_artifact_directory_name,
)
from scagent_sdk.contracts.state import SessionState
from scagent_sdk.errors import CapabilityExecutionError, CapabilityInterrupted
from scagent_sdk.execution.broker import EnvironmentBroker
from scagent_sdk.floors import FloorEvaluator
from scagent_sdk.session import AnalysisSession
from scagent_sdk.state.lineage import (
    LineageNode,
    active_head,
    attach_patch,
    checkout,
    classify_input,
    has_prepared_path,
    head_path,
    identity_signature,
    merge_diff,
    node_for_path,
    node_patch,
    node_scoped_roots,
    partition_facts_patch,
    resolve_node_facts,
    resolve_node_reference,
    with_prepared_path,
)
from scagent_sdk.state.store import apply_merge_patch

_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning):\s+(.*)$")
# Which argument carries the matrix a tool reads, and which suffix marks the matrix it writes.
# Interim: every current manifest names its matrix input ``path`` (89 of 89 recorded calls) and no
# execution has ever produced more than one ``.h5ad`` (0 of 61). Both become declared fields --
# ``primary_matrix_input``/``primary_matrix_output`` -- in the spec's D5; until then a second
# matrix output raises rather than being guessed at.
# A matrix artifact is an .h5ad file or a .zarr store; the stray-output check must catch either, or
# an undeclared matrix silently detaches from lineage.
_MATRIX_SUFFIX = (".h5ad", ".zarr")
# Executor-owned control argument. Declared in the schemas of tools that can transform the dataset
# so the model may pass it, but removed before dispatch: branching is a lineage concern and no skill
# should have to know the forest exists.
_BRANCH_ARGUMENT = "branch_from"
_ADOPT_UNTRACKED_ARGUMENT = "adopt_untracked"
# How long a forced stop waits for a signalled worker to unwind before giving up on it.
_WORKER_STOP_SECONDS = 15.0
_PATH_ARGUMENT_NAMES = frozenset({"cwd", "path"})
_PATH_ARGUMENT_SUFFIXES = ("_dir", "_directory", "_file", "_path")


class _AlreadyCommitted(Exception):
    """Internal signal for a duplicate commit observed only after the store reloads state."""


# Longest edge, in pixels, of an image sent to the model. The model's own image pipeline resamples
# to roughly this edge, so a larger preview costs transport without adding legibility (mirrors the
# inspect-media skill's DEFAULT_IMAGE_SIDE).
_MODEL_IMAGE_EDGE = 1568
# Progressively smaller edges tried when a figure is still over budget at the default edge.
_MODEL_IMAGE_FALLBACK_EDGES = (1280, 1024, 768, 512)


def _bounded_model_image(data: bytes, media_type: str, limit: int) -> tuple[bytes, str]:
    """Return image bytes that fit ``limit``, downscaling and re-encoding only if needed.

    A dense scientific figure -- a multi-panel UMAP of tens of thousands of cells -- can exceed the
    per-image transport budget as a full-resolution PNG. Discarding the whole capability result over
    that (the prior behavior) threw away real science for a presentation-layer concern; the model's
    own pipeline would have resampled the image anyway. Instead we shrink the pixels sent to the
    model to fit, exactly as the ``inspect-media`` skill normalizes previews. The committed artifact
    on disk is untouched and stays full resolution; only the attached copy is bounded.

    PNG is kept when it already fits or is smaller than the JPEG; otherwise the figure is flattened
    to JPEG, which is far smaller for antialiased scatter/heatmap content. If even the smallest edge
    cannot get under ``limit`` (pathological), the smallest encoding produced is returned rather
    than raising -- an oversized-but-present figure beats a lost result.
    """

    if len(data) <= limit:
        return data, media_type
    try:
        import io

        from PIL import Image
    except Exception:
        # Without Pillow we cannot resample; return the original and let transport handle it rather
        # than losing the result.
        return data, media_type

    def _encode(edge: int) -> list[tuple[int, bytes, str]]:
        with Image.open(io.BytesIO(data)) as source:
            image = source.copy()
        image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        options: list[tuple[int, bytes, str]] = []
        png_buffer = io.BytesIO()
        (image if image.mode in {"RGB", "RGBA", "L"} else image.convert("RGB")).save(
            png_buffer, format="PNG", optimize=True
        )
        options.append((png_buffer.tell(), png_buffer.getvalue(), "image/png"))
        flat = image.convert("RGB")
        jpeg_buffer = io.BytesIO()
        flat.save(jpeg_buffer, format="JPEG", quality=85, optimize=True)
        options.append((jpeg_buffer.tell(), jpeg_buffer.getvalue(), "image/jpeg"))
        return options

    best: tuple[int, bytes, str] | None = None
    for edge in (_MODEL_IMAGE_EDGE, *_MODEL_IMAGE_FALLBACK_EDGES):
        for size, encoded, mime in _encode(edge):
            if size <= limit:
                return encoded, mime
            if best is None or size < best[0]:
                best = (size, encoded, mime)
    if best is not None:
        return best[1], best[2]
    return data, media_type


def _concise_capability_error(message: str) -> str:
    """Reduce a capability failure (often a wrapped subprocess traceback) to one clean line.

    The full message is still handed to the model; this is only what the terminal shows the user.
    """

    lines = [line.strip() for line in message.strip().splitlines() if line.strip()]
    if not lines:
        return "capability failed"
    last = lines[-1]
    match = _EXCEPTION_LINE.match(last)
    concise = match.group(1) if match else last
    return concise if len(concise) <= 300 else concise[:297] + "..."


def _resolve_session_paths(
    value: Any,
    session_dir: Path,
    *,
    argument_name: str | None = None,
) -> Any:
    """Resolve existing session-relative path arguments before capability dispatch.

    Capability artifacts are stored below the scientific session. A model may pass either the
    canonical absolute path returned by the executor or the portable session-relative path kept
    in provenance. Resolve the latter centrally so every skill, current-runtime utility, and
    brokered worker observes the same filesystem contract.
    """

    if isinstance(value, dict):
        return {
            key: _resolve_session_paths(item, session_dir, argument_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_session_paths(item, session_dir, argument_name=argument_name)
            for item in value
        ]
    is_path_argument = argument_name is not None and (
        argument_name in _PATH_ARGUMENT_NAMES
        or argument_name.endswith(_PATH_ARGUMENT_SUFFIXES)
    )
    if not is_path_argument or not isinstance(value, str) or not value.strip():
        return value
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve())
    session_candidate = (session_dir / candidate).resolve()
    return str(session_candidate) if session_candidate.exists() else value


def _matrix_input(tool: CapabilityTool, arguments: Mapping[str, Any]) -> str | None:
    """The matrix path this tool was asked to read, per its declaration."""

    if tool.primary_matrix_input is None:
        return None
    value = arguments.get(tool.primary_matrix_input)
    return value if isinstance(value, str) and value.strip() else None


def _matrix_output(tool: CapabilityTool, files: list[dict[str, Any]]) -> str | None:
    """The artifact that continues the lineage, per this tool's declaration.

    Named rather than detected: CellBender emits three ``.h5`` matrices of which only the filtered
    one continues the analysis. A tool may declare an output it does not always produce -- cluster
    QC writes a matrix only when it removes clusters -- so an absent artifact simply means no node.
    """

    declared = tool.primary_matrix_output
    if declared is not None:
        for item in files:
            if str(item.get("name")) == declared:
                return str(item["relative_path"])
        return None
    # Fail closed on an undeclared AnnData container: silently skipping it would detach the
    # analysis from its lineage, which is precisely the defect this design removes.
    stray = sorted(
        str(item["relative_path"])
        for item in files
        if str(item.get("relative_path", "")).lower().endswith(_MATRIX_SUFFIX)
    )
    if stray:
        raise CapabilityExecutionError(
            f"tool {tool.name} produced {', '.join(stray)} but declares no "
            "primary_matrix_output, so lineage cannot record what continues the analysis. "
            "Declare primary_matrix_output in capability.yaml."
        )
    return None


def _tree_size(root: Path) -> int:
    """Total bytes of a store artifact's member files (a ``.zarr`` directory is one artifact)."""

    return sum(item.stat().st_size for item in root.rglob("*") if item.is_file())


def _file_digest(path: Path) -> str:
    """Streaming sha256 of a file, used to content-address artifact files for dedup."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _fsync_tree(path: Path) -> None:
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        with child.open("rb") as handle:
            os.fsync(handle.fileno())
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _find_execution_id(value: Any) -> str | None:
    if isinstance(value, dict):
        direct = value.get("scagent_execution_id")
        if isinstance(direct, str):
            return direct
        for item in value.values():
            found = _find_execution_id(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_execution_id(item)
            if found:
                return found
    elif isinstance(value, str):
        try:
            return _find_execution_id(json.loads(value))
        except (json.JSONDecodeError, RecursionError):
            return None
    return None


class CapabilityExecutor:
    def __init__(
        self, session: AnalysisSession, *, environment_broker: EnvironmentBroker | None = None
    ):
        self.session = session
        self.environment_broker = environment_broker
        self.pending_root = session.directory / "runtime" / "capabilities" / "pending"
        self.artifact_root = session.directory / "artifacts" / "capabilities"
        self.pending_root.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    async def execute(
        self,
        package: SkillPackage,
        tool: CapabilityTool,
        arguments: dict[str, Any],
        *,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        execution_id = str(uuid4())
        staging_dir = self.pending_root / execution_id
        staging_dir.mkdir()
        # Decide the committed artifact directory up front so a skill can record a correct path to
        # its own output; the same name is honored at commit (see _stage_result), so the path a
        # skill writes into facts and the eventual on-disk location always agree.
        artifact_relative_path = "artifacts/capabilities/" + capability_artifact_directory_name(
            tool.name, execution_id
        )
        context = CapabilityContext(
            scientific_session_id=self.session.session_id,
            session_dir=self.session.directory,
            staging_dir=staging_dir,
            skill_id=package.manifest.skill_id,
            tool_name=tool.name,
            execution_id=execution_id,
            state_revision=self.session.store.state.revision,
            state_facts=deepcopy(self.session.store.state.facts),
            state_lineage=deepcopy(self.session.store.state.lineage),
            artifact_relative_path=artifact_relative_path,
        )
        resolved_arguments = _resolve_session_paths(arguments, self.session.directory)
        try:
            dispatch = self._resolve_matrix_input(tool, resolved_arguments)
            failures = FloorEvaluator().failures(self.session.store.state, tool.floors)
            if failures:
                reason = " ".join(
                    f"[{failure.floor}] {failure.reason} {failure.remediation}"
                    for failure in failures
                )
                raise CapabilityExecutionError(f"scientific floor denied execution: {reason}")
            if tool.environment == "current":
                handler = package.load_handler(tool)
                value = handler(resolved_arguments, context)
                if inspect.isawaitable(value):
                    value = await value
                environment = {"name": "current", "python": sys.executable}
            elif self.environment_broker is not None:
                execution = await self._execute_in_environment(
                    package, tool, resolved_arguments, context, progress=progress
                )
                value = execution.value
                environment = execution.provenance
            else:
                raise CapabilityExecutionError(
                    f"environment {tool.environment!r} requires the environment broker"
                )
            result = CapabilityResult.from_value(value)
            model_content = self._model_content(context, result)
            envelope = self._stage_result(
                package,
                tool,
                context,
                result,
                environment,
                arguments=arguments,
                dispatch=dispatch,
            )
        except asyncio.CancelledError:
            # A forced stop unwinds the whole turn; record why this execution has no result
            # before the cancellation continues. Nothing is committed, so state stays sound.
            self._record_interrupted(package, tool, execution_id, forced=True)
            raise
        except CapabilityInterrupted as exc:
            self._record_interrupted(package, tool, execution_id, forced=False)
            summary = f"{tool.name} was stopped before it finished"
            return {
                "content": [{"type": "text", "text": f"{summary}: {exc}"}],
                "is_error": True,
                "error_summary": summary,
            }
        except Exception as exc:
            self.session.store.record(
                "capability.execution_failed",
                payload={
                    "execution_id": execution_id,
                    "skill_id": package.manifest.skill_id,
                    "tool_name": tool.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            concise = _concise_capability_error(str(exc))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"{tool.name} failed: {concise}\n\nFull detail:\n{exc}",
                    }
                ],
                "is_error": True,
                "error_summary": f"{tool.name} failed: {concise}",
            }
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(envelope, sort_keys=True, allow_nan=False),
                }
            ]
            + model_content,
            "structuredContent": envelope,
        }

    def _resolve_matrix_input(
        self, tool: CapabilityTool, resolved_arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve this tool's matrix input against the lineage, injecting the head if omitted.

        An omitted input is the common case and the safe one: the executor supplies the artifact
        the analysis is actually on, so the model has no path to get wrong. A supplied input is
        honoured but validated -- a tracked artifact that is no longer the head is refused for any
        tool that continues the lineage, because that is exactly how two annotators diverge and one
        contribution disappears from the delivered file.

        Read-only tools are not restricted. Inspecting some other file mid-analysis is legitimate
        and creates no node, so it cannot detach anything.
        """

        lineage = self.session.store.state.lineage
        head = active_head(lineage)
        continues_lineage = tool.primary_matrix_output is not None
        requested = _matrix_input(tool, resolved_arguments)
        source = "supplied"

        branch_from = resolved_arguments.pop(_BRANCH_ARGUMENT, None)
        if not isinstance(branch_from, str) or not branch_from.strip():
            branch_from = None
        adopt_value = resolved_arguments.pop(_ADOPT_UNTRACKED_ARGUMENT, False)
        if not isinstance(adopt_value, bool):
            raise CapabilityExecutionError(
                f"{_ADOPT_UNTRACKED_ARGUMENT} must be true or false"
            )
        adopt_untracked = adopt_value
        if adopt_untracked and not continues_lineage:
            raise CapabilityExecutionError(
                f"{tool.name} does not transform the dataset, so it cannot adopt a new analysis "
                f"root; drop {_ADOPT_UNTRACKED_ARGUMENT}."
            )
        if adopt_untracked and branch_from is not None:
            raise CapabilityExecutionError(
                f"pass either {_BRANCH_ARGUMENT} or {_ADOPT_UNTRACKED_ARGUMENT}, not both"
            )
        if branch_from is not None:
            if not continues_lineage:
                raise CapabilityExecutionError(
                    f"{tool.name} does not transform the dataset, so there is nothing to branch; "
                    f"drop {_BRANCH_ARGUMENT}."
                )
            if requested is not None:
                raise CapabilityExecutionError(
                    f"pass either {tool.primary_matrix_input} or {_BRANCH_ARGUMENT}, not both: "
                    "one continues the current line of work and the other deliberately forks it."
                )
            branch_node = resolve_node_reference(
                lineage, branch_from, session_dir=self.session.directory
            )
            if branch_node is None:
                raise CapabilityExecutionError(
                    f"{_BRANCH_ARGUMENT} must name a version or artifact this analysis produced, "
                    f"and {branch_from} is not one. Branch from a recorded version."
                )
            nodes = lineage.get("nodes")
            branch = nodes.get(branch_node, {}) if isinstance(nodes, Mapping) else {}
            branch_path = branch.get("head_path") if isinstance(branch, Mapping) else None
            if not isinstance(branch_path, str) or not branch_path:
                raise CapabilityExecutionError(
                    f"recorded version {branch_node} has no analysis artifact"
                )
            requested = str((self.session.directory / branch_path).resolve())
            source = "branch"
            if tool.primary_matrix_input is not None:
                resolved_arguments[tool.primary_matrix_input] = requested

        if tool.primary_matrix_input is not None and requested is None:
            resolved_head = head_path(lineage)
            if resolved_head is None:
                raise CapabilityExecutionError(
                    f"{tool.name} needs a matrix but the lineage has no active artifact; "
                    f"pass {tool.primary_matrix_input} explicitly."
                )
            else:
                requested = str((self.session.directory / resolved_head).resolve())
                resolved_arguments[tool.primary_matrix_input] = requested
                source = "injected"

        resolved_node = (
            node_for_path(lineage, requested, session_dir=self.session.directory)
            if requested
            else None
        )
        relation = classify_input(lineage, resolved_node) if requested else None
        if continues_lineage and branch_from is None and relation in {"ancestor", "sibling"}:
            current = head_path(lineage) or "(none)"
            raise CapabilityExecutionError(
                f"{tool.name} was given {requested}, which is a tracked {relation} of the active "
                f"artifact rather than the artifact itself. Continuing from it would drop whatever "
                f"later steps added. Omit {tool.primary_matrix_input} to use the current artifact "
                f"({current}), or pass {_BRANCH_ARGUMENT} instead to keep both as alternatives."
            )
        nodes = lineage.get("nodes")
        forest_nonempty = isinstance(nodes, Mapping) and bool(nodes)
        untracked_source = relation == "untracked" or (
            requested is None and tool.primary_matrix_input is None
        )
        if continues_lineage and adopt_untracked:
            if not untracked_source:
                raise CapabilityExecutionError(
                    f"{_ADOPT_UNTRACKED_ARGUMENT} is only for an untracked input; use the active "
                    f"head normally or {_BRANCH_ARGUMENT} for a recorded alternative."
                )
            source = "adopted"
        elif continues_lineage and forest_nonempty and untracked_source and branch_from is None:
            label = requested or "this tool's external matrix source"
            raise CapabilityExecutionError(
                f"{tool.name} would replace the active analysis with untracked input {label}. "
                f"Pass {_ADOPT_UNTRACKED_ARGUMENT}=true to adopt it explicitly, or omit the matrix "
                "path to continue from the active artifact."
            )
        return {
            "base_head_execution_id": head,
            "requested_input": requested,
            "resolved_input_execution_id": resolved_node,
            "input_relation": relation,
            "input_source": source if requested else None,
            "branch_intent": branch_from is not None,
            "adopt_intent": adopt_untracked,
        }

    async def _execute_in_environment(
        self,
        package: SkillPackage,
        tool: CapabilityTool,
        arguments: dict[str, Any],
        context: CapabilityContext,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> Any:
        """Run a compute capability off the event loop so the turn stays interruptible.

        The broker blocks on a worker that can run for minutes; calling it directly would
        freeze the event loop, and neither a keyboard interrupt nor the model runtime's own
        control channel could be serviced while science is running. The worker future is
        shielded so a cancellation deliberately stops the worker and waits for it to unwind,
        instead of orphaning a GPU process.
        """

        broker = self.environment_broker
        assert broker is not None
        worker = asyncio.ensure_future(
            asyncio.to_thread(
                partial(broker.execute, package, tool, arguments, context, progress=progress)
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            broker.cancel(context.execution_id)
            with suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(worker), timeout=_WORKER_STOP_SECONDS)
            worker.cancel()
            raise

    def _record_interrupted(
        self,
        package: SkillPackage,
        tool: CapabilityTool,
        execution_id: str,
        *,
        forced: bool,
    ) -> None:
        with suppress(Exception):
            self.session.store.record(
                "capability.execution_interrupted",
                payload={
                    "execution_id": execution_id,
                    "skill_id": package.manifest.skill_id,
                    "tool_name": tool.name,
                    "forced": forced,
                },
                actor="user",
            )

    @staticmethod
    def _figure_directive(result: CapabilityResult) -> str:
        """The instruction that turns attached pixels into an actual reading of them.

        Attaching an image only guarantees the model *received* it. Nothing obliges it to say
        anything before calling the next tool, so figures were routinely generated, passed over,
        and then attested to much later when a review floor blocked progress — a review written
        from recall instead of from the figure. Legacy `scagent` avoided this by appending the
        figures as their own user turn carrying an interpretive prompt, which the model had to
        answer before it could do anything else. This is the same instruction, delivered as the
        last block of the tool result so it is the final thing read before the model responds.

        Deliberately generic: what a given figure *means* is the skill's business, and the
        runtime package holds no biology.
        """

        names = [media.name for media in result.model_media]
        count = len(names)
        listed = ", ".join(names)
        return (
            f"{count} figure{'s are' if count != 1 else ' is'} attached above ({listed}).\n\n"
            "Read them now, before your next tool call. For each one, state what it actually "
            "shows, what it implies for the decision in front of you, and anything that needs "
            "acting on. Reference what you can see in the pixels — not what the summary or the "
            "table behind the figure says.\n\n"
            "If a panel is empty, a legend is unreadable, categories share a color, or the data "
            "occupies a sliver of the axes, say so and treat the visual review as incomplete: "
            "regenerate a legible view or read the underlying table instead of guessing. If you "
            "are mid-pipeline and no genuine decision point has been reached, keep this brief "
            "and carry on with the next step — but do not skip it, and do not defer it to a "
            "later review call."
        )

    @staticmethod
    def _model_content(
        context: CapabilityContext, result: CapabilityResult
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        declared = {artifact.relative_path for artifact in result.artifacts}
        total = 0
        for media in result.model_media:
            if media.relative_path not in declared:
                raise CapabilityExecutionError(
                    f"model_media must also be declared as an artifact: {media.relative_path}"
                )
            source = (context.staging_dir / media.relative_path).resolve()
            try:
                source.relative_to(context.staging_dir.resolve())
            except ValueError as exc:
                raise CapabilityExecutionError(
                    f"model_media escapes staging directory: {media.relative_path}"
                ) from exc
            if not source.is_file():
                raise CapabilityExecutionError(f"declared model_media does not exist: {source}")
            # A figure over the per-image budget is downscaled to fit rather than discarded: the
            # full-resolution artifact stays committed on disk and only the attached copy shrinks.
            data, mime = _bounded_model_image(
                source.read_bytes(), media.media_type, MODEL_MEDIA_LIMIT_BYTES
            )
            total += len(data)
            if total > MODEL_MEDIA_TOTAL_BYTES:
                raise CapabilityExecutionError(
                    f"model_media totals more than {MODEL_MEDIA_TOTAL_BYTES} bytes; attach fewer "
                    "or smaller figures"
                )
            content.append(
                {
                    "type": "image",
                    "data": base64.b64encode(data).decode("ascii"),
                    "mimeType": mime,
                }
            )
        if content:
            content.append({"type": "text", "text": CapabilityExecutor._figure_directive(result)})
        return content

    def _stage_result(
        self,
        package: SkillPackage,
        tool: CapabilityTool,
        context: CapabilityContext,
        result: CapabilityResult,
        environment: dict[str, Any],
        *,
        arguments: dict[str, Any],
        dispatch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        details_bytes = json.dumps(
            result.details, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        files: list[dict[str, Any]] = []
        inline_details: Any = result.details
        if len(details_bytes) > INLINE_RESULT_LIMIT_BYTES:
            details_path = context.staging_dir / "details.json"
            details_path.write_bytes(details_bytes + b"\n")
            inline_details = None
            files.append(
                {
                    "name": "details",
                    "relative_path": "details.json",
                    "media_type": "application/json",
                    "size_bytes": details_path.stat().st_size,
                }
            )
        for produced in result.artifacts:
            source = (context.staging_dir / produced.relative_path).resolve()
            try:
                source.relative_to(context.staging_dir.resolve())
            except ValueError as exc:
                raise CapabilityExecutionError(
                    f"artifact escapes staging directory: {produced.relative_path}"
                ) from exc
            # An artifact is either a single file (.h5ad) or a store directory (.zarr). The whole
            # staging tree is moved atomically at commit, so a directory needs no special promotion;
            # its recorded size is the sum of its member files rather than one stat().
            if source.is_file():
                size_bytes = source.stat().st_size
            elif source.is_dir():
                size_bytes = _tree_size(source)
            else:
                raise CapabilityExecutionError(f"declared artifact does not exist: {source}")
            files.append(
                {
                    "name": produced.name,
                    "relative_path": produced.relative_path,
                    "media_type": produced.media_type,
                    "size_bytes": size_bytes,
                }
            )
        dispatch_lineage = dict(dispatch or {})
        # Validate here, while the result is still only staged, so a contract breach surfaces as an
        # ordinary tool error the model can act on rather than as a PostToolUse hook failure after
        # the compute has already been reported as successful.
        matrix_output = _matrix_output(tool, files)
        dispatch_lineage["matrix_output"] = matrix_output
        dispatch_lineage["operation"] = tool.lineage_operation
        if has_prepared_path(result.facts_patch):
            raise CapabilityExecutionError(
                "facts.analysis.dataset_revision.prepared_path is executor-owned; "
                "remove it from the skill result"
            )
        node_facts, _ = partition_facts_patch(result.facts_patch)
        if matrix_output is None and node_facts:
            requested = dispatch_lineage.get("requested_input")
            parent = dispatch_lineage.get("resolved_input_execution_id")
            if isinstance(requested, str) and not isinstance(parent, str):
                raise CapabilityExecutionError(
                    "a read-only tool returned node-scoped facts for an untracked matrix; those "
                    "facts cannot be attached to the active analysis. Adopt the matrix through a "
                    "transforming tool first, or return only session-scoped provenance."
                )
            if requested is None and dispatch_lineage.get("base_head_execution_id") is None:
                raise CapabilityExecutionError(
                    "a read-only tool returned node-scoped facts before an analysis version "
                    "existed, so there is no lineage node to attach them to"
                )
        # The committed artifact directory is named for the action, not just its UUID, so a session
        # tree reads legibly. The name is decided at dispatch, handed to the skill on the context,
        # recorded in result.json, and honored at commit, so the model-facing paths and the
        # eventual on-disk location always agree.
        artifact_relative_path = context.artifact_relative_path or (
            "artifacts/capabilities/"
            + capability_artifact_directory_name(tool.name, context.execution_id)
        )
        persisted = {
            "schema_version": result.schema_version,
            "execution_id": context.execution_id,
            "artifact_relative_path": artifact_relative_path,
            "lineage": dispatch_lineage,
            "skill_id": package.manifest.skill_id,
            "skill_version": package.manifest.version,
            "skill_fingerprint": package.fingerprint,
            "tool_name": tool.name,
            "environment": environment,
            "input_state_revision": context.state_revision,
            "arguments": arguments,
            "summary": result.summary,
            "details": inline_details,
            "facts_patch": result.facts_patch,
            "decisions_patch": result.decisions_patch,
            "files": files,
            "model_media": [
                {
                    "name": media.name,
                    "relative_path": media.relative_path,
                    "media_type": media.media_type,
                }
                for media in result.model_media
            ],
        }
        _atomic_json(context.staging_dir / "result.json", persisted)
        _fsync_tree(context.staging_dir)
        self.session.store.record(
            "capability.result_staged",
            payload={
                "execution_id": context.execution_id,
                "skill_id": package.manifest.skill_id,
                "skill_version": package.manifest.version,
                "skill_fingerprint": package.fingerprint,
                "tool_name": tool.name,
                "environment": environment,
                "input_state_revision": context.state_revision,
                "arguments": arguments,
                "files": files,
                "lineage": dispatch_lineage,
            },
        )
        artifact_path = (self.session.directory / artifact_relative_path).resolve()
        model_files = [
            {
                **item,
                "session_relative_path": f"{artifact_relative_path}/{item['relative_path']}",
                "path": str(artifact_path / item["relative_path"]),
            }
            for item in files
        ]
        return {
            "schema_version": result.schema_version,
            "scagent_execution_id": context.execution_id,
            "status": "validated",
            "state_commit": "PostToolUse",
            "summary": result.summary,
            "details": inline_details,
            "artifact_path": str(artifact_path) if files else None,
            "artifact_relative_path": artifact_relative_path if files else None,
            "files": model_files,
            "model_media": persisted["model_media"],
            # Report which artifact was actually read and why, so an injected input is visible
            # rather than a silent substitution.
            "resolved_input": (
                {
                    "path": dispatch_lineage["requested_input"],
                    "relation": dispatch_lineage.get("input_relation"),
                    "source": dispatch_lineage.get("input_source"),
                }
                if dispatch_lineage.get("requested_input")
                else None
            ),
        }

    def _reject_stale_base(
        self, execution_id: str, data: Mapping[str, Any], state: SessionState
    ) -> None:
        """Refuse a matrix commit whose head moved while it was running.

        Compute runs for minutes in a subprocess. Without this, a long training run that started
        from one head silently rebases onto whatever appeared meanwhile, which is the divergence
        this design exists to prevent. Only matrix-producing results are checked: read-only
        evidence moves no head.
        """

        dispatch = data.get("lineage")
        if not isinstance(dispatch, Mapping) or not dispatch.get("matrix_output"):
            return
        if dispatch.get("branch_intent"):
            return
        base_value = dispatch.get("base_head_execution_id")
        base = base_value if isinstance(base_value, str) else None
        current = active_head(state.lineage)
        requested = dispatch.get("requested_input")
        parent = dispatch.get("resolved_input_execution_id")
        parent = parent if isinstance(parent, str) else None
        if parent is None and isinstance(requested, str):
            parent = node_for_path(
                state.lineage, requested, session_dir=self.session.directory
            )
        # Recovery may stage a child before its parent is committed. Once ordered recovery has
        # committed that parent, the recorded input resolves to the current head and is safe.
        if current != base and parent != current:
            raise CapabilityExecutionError(
                f"cannot commit {execution_id}: it derived from head {base} but the active head "
                f"is now {current}. Re-run it against the current head, or declare explicit "
                "branch intent to keep both."
            )

    def _checkout_state_patch(
        self,
        data: Mapping[str, Any],
        session_facts: Mapping[str, Any],
        state: SessionState,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Switch the active version, moving the head and the node-scoped facts together.

        Atomic by construction: both land in one recorded event, so an interrupted checkout leaves
        the head and the facts at their previous values rather than describing different matrices.
        The skill that requested this only validated and described the target; the executor owns
        every lineage mutation.
        """

        details = data.get("details")
        target = details.get("target_execution_id") if isinstance(details, Mapping) else None
        if not isinstance(target, str) or not target:
            raise CapabilityExecutionError(
                "a checkout capability must report details.target_execution_id"
            )
        lineage = state.lineage
        switched = checkout(lineage, target)
        roots = node_scoped_roots()
        current_view = {
            key: value for key, value in state.facts.items() if key in roots
        }
        target_view = resolve_node_facts(lineage, target, merge=apply_merge_patch)
        visible = dict(session_facts)
        visible.update(merge_diff(current_view, target_view))
        return {"active_execution_id": switched["active_execution_id"]}, visible

    def _lineage_state_patch(
        self,
        execution_id: str,
        data: Mapping[str, Any],
        artifact_relative: str,
        state: SessionState,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Route a committed result into the lineage forest and global facts.

        Returns ``(lineage_patch, facts_patch)``. Node-scoped facts always land on the node they
        describe; they reach global facts only when that node is the active head, so a branch
        cannot rewrite the identities of the matrix the session is actually working on. Session-
        scoped facts always merge globally.
        """

        lineage = state.lineage
        dispatch = data.get("lineage")
        dispatch = dispatch if isinstance(dispatch, Mapping) else {}
        facts_patch = data.get("facts_patch")
        facts_patch = facts_patch if isinstance(facts_patch, Mapping) else {}
        node_facts, session_facts = partition_facts_patch(facts_patch)

        if dispatch.get("operation") == "checkout":
            return self._checkout_state_patch(data, session_facts, state)

        matrix_output = dispatch.get("matrix_output")
        branch_intent = bool(dispatch.get("branch_intent"))
        adopt_intent = bool(dispatch.get("adopt_intent"))
        requested = dispatch.get("requested_input")
        requested = requested if isinstance(requested, str) else None
        parent = dispatch.get("resolved_input_execution_id")
        parent = parent if isinstance(parent, str) else None
        if parent is None and requested is not None:
            # Crash recovery stages several executions before any of them commits, so the forest
            # was empty when their inputs were resolved at dispatch. The path was still recorded,
            # so resolve it against the forest as it stands now.
            parent = node_for_path(
                lineage, requested, session_dir=self.session.directory
            )
        head = active_head(lineage)

        if not isinstance(matrix_output, str) or not matrix_output:
            # Read-only: no node, no head movement. Its node-scoped evidence still has to be
            # attached to the node it describes, or a later checkout loses it.
            if requested is not None and parent is None and node_facts:
                raise CapabilityExecutionError(
                    "a read-only tool returned node-scoped facts for an untracked matrix; those "
                    "facts cannot be attached to the active analysis. Adopt the matrix through a "
                    "transforming tool first, or return only session-scoped provenance."
                )
            target = parent if requested is not None else head
            if target is None and node_facts:
                raise CapabilityExecutionError(
                    "a read-only tool returned node-scoped facts before an analysis version "
                    "existed, so there is no lineage node to attach them to"
                )
            if target is None:
                return {}, dict(session_facts)
            lineage_patch = attach_patch(lineage, target, node_facts) if node_facts else {}
            visible = dict(session_facts)
            # Global facts are the active head's resolved view. A patch on the head, or on any
            # ancestor the head inherits from, belongs in that view; a patch on a sibling branch
            # does not. Testing for "head or ancestor" rather than "head" keeps global facts equal
            # to resolve_node_facts(active) instead of silently diverging from it.
            if classify_input(lineage, target) in {"head", "ancestor"}:
                visible.update(node_facts)
            return lineage_patch, visible

        # Matrix-producing: a new node whose parent is the artifact actually consumed.
        node_head_path = f"{artifact_relative}/{matrix_output}"
        node_facts = with_prepared_path(node_facts, node_head_path)
        inherited = (
            resolve_node_facts(lineage, parent, merge=apply_merge_patch) if parent else {}
        )
        node_view = apply_merge_patch(inherited, node_facts)
        node = LineageNode(
            execution_id=execution_id,
            parent_execution_id=parent,
            head_path=node_head_path,
            identity_signature=identity_signature(node_view),
            requested_input=(
                str(dispatch["requested_input"])
                if isinstance(dispatch.get("requested_input"), str)
                else None
            ),
            resolved_input_execution_id=parent,
            branch_intent=branch_intent,
            adopt_intent=adopt_intent,
            skill_id=str(data.get("skill_id", "")),
            tool_name=str(data.get("tool_name", "")),
            fact_patches=(dict(node_facts),) if node_facts else (),
        )
        lineage_patch = node_patch(
            node, active_execution_id=head if branch_intent else execution_id
        )
        visible = dict(session_facts)
        if not branch_intent:
            if parent == head:
                # Continuing the active line: the head inherits exactly what global facts hold.
                visible.update(node_facts)
            else:
                # Forking off an ancestor or sibling moves the head onto a line that does not
                # inherit the previous head's node-scoped facts, so that view is replaced rather
                # than merged. Without this, evidence from the abandoned line lingers in global
                # facts and floors evaluate against a matrix the session is no longer on.
                roots = node_scoped_roots()
                current_view = {
                    key: value
                    for key, value in state.facts.items()
                    if key in roots
                }
                visible.update(merge_diff(current_view, node_view))
        return lineage_patch, visible

    def commit(self, execution_id: str) -> bool:
        pending = self.pending_root / execution_id
        if execution_id in self.session.store.state.artifacts:
            return False
        # Normal path: the staging tree is still under pending/<id>. Recovery path: a prior attempt
        # already moved it into the artifact root, where it carries its descriptive name; find it by
        # the ID preserved as the directory's suffix (or the whole name, pre-naming).
        source = pending
        if not source.is_dir():
            matches = [
                item
                for item in (self.artifact_root.iterdir() if self.artifact_root.exists() else [])
                if item.is_dir()
                and (item.name == execution_id or item.name.endswith(f"--{execution_id}"))
                and (item / "result.json").is_file()
            ]
            if len(matches) != 1:
                raise CapabilityExecutionError(
                    f"pending capability result not found: {execution_id}"
                )
            source = matches[0]
        result_path = source / "result.json"
        if not result_path.is_file():
            raise CapabilityExecutionError(f"pending capability result not found: {execution_id}")
        data = json.loads(result_path.read_text(encoding="utf-8"))
        if source is not pending:
            # Recovery: the tree is already in the artifact root; that location is authoritative.
            final = source
        else:
            # Normal: the staging step chose the descriptive directory name and recorded it; honor
            # it so the paths already handed to the model resolve. Validate it stays directly under
            # the artifact root and still carries this execution ID; fall back to the legacy name.
            recorded_relative = data.get("artifact_relative_path")
            if isinstance(recorded_relative, str) and recorded_relative:
                final = self.session.directory / recorded_relative
                if final.parent != self.artifact_root or not (
                    final.name == execution_id or final.name.endswith(f"--{execution_id}")
                ):
                    raise CapabilityExecutionError(
                        f"unsafe capability artifact directory: {recorded_relative}"
                    )
            else:
                final = self.artifact_root / execution_id
        artifact_relative = str(final.relative_to(self.session.directory))
        artifact_record = {
            "kind": "capability-result",
            "skill_id": data["skill_id"],
            "skill_version": data["skill_version"],
            "skill_fingerprint": data["skill_fingerprint"],
            "tool_name": data["tool_name"],
            "environment": data["environment"],
            "input_state_revision": data.get("input_state_revision"),
            "arguments": data.get("arguments", {}),
            "summary": data["summary"],
            "path": str(final.relative_to(self.session.directory)),
            "files": data["files"],
            "model_media": data.get("model_media", []),
            "lineage": data.get("lineage", {}),
        }
        def prepare_commit(state: SessionState) -> Mapping[str, Any]:
            # The store invokes this after reloading state while holding the session lock. Checking
            # earlier is racy across two processes: both can observe one head and then commit in
            # sequence. The filesystem move remains after the check and before the event append.
            if execution_id in state.artifacts:
                raise _AlreadyCommitted
            self._reject_stale_base(execution_id, data, state)
            lineage_patch, visible_facts = self._lineage_state_patch(
                execution_id, data, artifact_relative, state
            )
            if pending.is_dir():
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(pending, final)
            state_patch: dict[str, Any] = {
                "facts": visible_facts,
                "decisions": data["decisions_patch"],
                "artifacts": {execution_id: artifact_record},
            }
            if lineage_patch:
                state_patch["lineage"] = lineage_patch
            return state_patch

        try:
            self.session.store.record(
                "capability.result_committed",
                payload={"execution_id": execution_id, **artifact_record},
                state_patch_factory=prepare_commit,
            )
        except _AlreadyCommitted:
            return False
        # Dedup after the commit is durable, not under the session lock: the parent is already
        # immutable and hard-linking never changes content, so it is safe to do outside the lock and
        # must never fail a commit that already succeeded.
        parent_id = data.get("lineage", {}).get("resolved_input_execution_id")
        with suppress(OSError):
            self._dedup_against_parent(execution_id, parent_id)
        self.session.refresh_outputs_best_effort()
        return True

    def _committed_artifact_dir(self, execution_id: str) -> Path | None:
        """Resolve a committed execution's on-disk directory.

        The directory is named ``<action>--<execution_id>``, so it can no longer be reconstructed
        from the ID alone. Prefer the path recorded on the artifact record (authoritative); fall
        back to scanning the artifact root for the ID as the whole name (legacy) or the ``--<id>``
        suffix. Returns ``None`` when no single directory matches.
        """

        record = self.session.store.state.artifacts.get(execution_id)
        if isinstance(record, Mapping):
            recorded = record.get("path")
            if isinstance(recorded, str) and recorded:
                candidate = self.session.directory / recorded
                if candidate.is_dir():
                    return candidate
        matches = [
            item
            for item in (self.artifact_root.iterdir() if self.artifact_root.exists() else [])
            if item.is_dir()
            and (item.name == execution_id or item.name.endswith(f"--{execution_id}"))
        ]
        return matches[0] if len(matches) == 1 else None

    def _dedup_against_parent(self, execution_id: str, parent_execution_id: Any) -> tuple[int, int]:
        """Hard-link a committed artifact's files to byte-identical files in its lineage parent.

        A step usually changes only part of the matrix (adds an embedding, an obs column), so most
        of a ``.zarr`` store's chunk files are byte-identical to the parent version's. Replacing
        each such file with a hard link to the parent's frozen copy makes the two versions share
        bytes on disk; retention's reclaimable accounting (which measures by ``st_nlink``) then
        reflects the true cost of a prune. Artifacts are immutable, so sharing an inode is safe.

        Best-effort and per-file guarded: any file that cannot be linked -- cross-device, a race --
        simply keeps its own independent copy. Returns ``(linked_files, reclaimed_bytes)``.
        """

        if not isinstance(parent_execution_id, str) or not parent_execution_id:
            return (0, 0)
        new_root = self._committed_artifact_dir(execution_id)
        parent_root = self._committed_artifact_dir(parent_execution_id)
        if new_root is None or parent_root is None:
            return (0, 0)
        child_files = [
            item
            for item in sorted(new_root.rglob("*"))
            if item.is_file() and not item.is_symlink()
        ]
        if not child_files:
            return (0, 0)
        # Match by CONTENT, not by path. Each step names its store differently (pca.zarr vs
        # neighbors.zarr), so the same unchanged array lives at a different artifact-relative path
        # in parent and child; a path match would never fire. Index the parent by digest, restricted
        # to sizes the child has so a parent file that cannot match is never read.
        child_sizes = {item.stat().st_size for item in child_files}
        parent_by_digest: dict[str, Path] = {}
        for parent_file in parent_root.rglob("*"):
            try:
                if not parent_file.is_file() or parent_file.is_symlink():
                    continue
                if parent_file.stat().st_size not in child_sizes:
                    continue
                parent_by_digest.setdefault(_file_digest(parent_file), parent_file)
            except OSError:
                continue

        linked = 0
        reclaimed = 0
        for new_file in child_files:
            link_tmp: Path | None = None
            try:
                new_stat = new_file.stat()
                match_file = parent_by_digest.get(_file_digest(new_file))
                if match_file is None:
                    continue
                match_stat = match_file.stat()
                if (new_stat.st_dev, new_stat.st_ino) == (match_stat.st_dev, match_stat.st_ino):
                    continue  # already one inode
                if new_stat.st_dev != match_stat.st_dev:
                    continue  # cross-device: cannot hard-link
                if not filecmp.cmp(new_file, match_file, shallow=False):
                    continue  # digest-collision guard
                link_tmp = new_file.with_name(f".{new_file.name}.dedup-{uuid4().hex}")
                os.link(match_file, link_tmp)
                os.replace(link_tmp, new_file)  # atomic swap; frees the new file's own inode
                linked += 1
                reclaimed += new_stat.st_size
            except OSError:
                if link_tmp is not None:
                    with suppress(OSError):
                        link_tmp.unlink()
                continue
        return (linked, reclaimed)

    def commit_from_hook(self, input_data: dict[str, Any]) -> bool:
        execution_id = _find_execution_id(input_data.get("tool_response", input_data))
        if execution_id is None:
            raise CapabilityExecutionError("PostToolUse response has no execution ID")
        return self.commit(execution_id)

    def _staged_sequence(self) -> dict[str, int]:
        """Map execution ID to the sequence of the event that staged it.

        ``_stage_result`` records ``capability.result_staged`` for every execution it stages, so
        this is the authoritative order in which pending work was produced.
        """

        order: dict[str, int] = {}
        try:
            events = self.session.store.events()
        except Exception:
            return order
        for event in events:
            if event.kind != "capability.result_staged":
                continue
            execution_id = event.payload.get("execution_id")
            if isinstance(execution_id, str):
                order.setdefault(execution_id, event.sequence)
        return order

    @property
    def quarantine_root(self) -> Path:
        return self.session.directory / "runtime" / "capabilities" / "quarantine"

    def recover_pending(self) -> list[str]:
        """Commit orphaned staged results in the order they were produced.

        Directory names are UUID4s, so sorting them replays crash recovery in an arbitrary order.
        That matters because commits apply state patches in sequence and advance the lineage head:
        recovering two executions backwards leaves the later patch overwritten by the earlier one,
        and can record a parent that is not the artifact actually consumed.

        ``result.json`` is written just before ``capability.result_staged`` is recorded, so a crash
        between the two leaves a directory with no sequence. Such a result has no defensible
        position in the order, so it is quarantined and reported rather than adopted at a guessed
        one. Nothing is deleted: the directory is moved aside intact for inspection.
        """

        recovered: list[str] = []
        candidates = list(self.pending_root.iterdir() if self.pending_root.exists() else [])
        candidates.extend(
            path
            for path in (self.artifact_root.iterdir() if self.artifact_root.exists() else [])
            if path.name not in self.session.store.state.artifacts
        )
        order = self._staged_sequence()
        pending: list[tuple[int, Path]] = []
        unsequenced: list[Path] = []
        for path in candidates:
            if not path.is_dir() or not (path / "result.json").is_file():
                continue
            sequence = order.get(path.name)
            if sequence is None:
                unsequenced.append(path)
            else:
                pending.append((sequence, path))
        for _, path in sorted(pending, key=lambda item: (item[0], item[1].name)):
            if self.commit(path.name):
                recovered.append(path.name)
        for path in sorted(unsequenced):
            self._quarantine(path)
        return recovered

    def _quarantine(self, path: Path) -> None:
        """Move an unorderable staged result aside and record why."""

        target = self.quarantine_root / path.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                os.replace(path, target)
        except OSError:
            return
        with suppress(Exception):
            self.session.store.record(
                "capability.result_quarantined",
                payload={
                    "execution_id": path.name,
                    "reason": "no capability.result_staged event; commit order is undefined",
                    "path": str(target.relative_to(self.session.directory)),
                },
            )
