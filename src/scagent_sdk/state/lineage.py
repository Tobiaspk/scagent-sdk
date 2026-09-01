"""Executor-owned artifact lineage: identity signatures, fact scope, and forest topology.

Session facts merge by identity and always converge. The H5AD chain merges only by whichever path
the model passed forward, so a capability that adds columns can derive from a sibling artifact and
silently drop another capability's contribution from the delivered file. Identity cannot detect that
-- both branches carry the same identities -- so topology has to be recorded separately.

This module holds the pure, side-effect-free half of that design: what an identity signature is,
which fact roots belong to a node rather than the session, and how a committed execution is
classified against the current forest. The executor owns the mutations; nothing here touches disk.

Specification: ``docs/artifact-lineage-and-head-spec.md`` (v2.2), decisions D1, D2, D10.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

# --- identity signature (D1) ---------------------------------------------------------------

# Every ``facts.analysis`` node that currently carries an id. Signatures no longer define topology,
# so over-inclusion is harmless -- it only makes staleness and overlay-eligibility queries finer,
# while a short list would collide two genuinely different matrices onto one signature.
IDENTITY_AXES_V1: tuple[str, ...] = (
    "cell_set",
    "clustering",
    "count_representation",
    "dataset_revision",
    "expression",
    "hvg",
    "representation",
    "umap",
)
IDENTITY_SIGNATURE_VERSION = 1

# --- fact scope registry (D1) --------------------------------------------------------------

Scope = Literal["node", "session"]

# Scope is semantic and cannot be inferred from JSON shape: ``reference_runs`` nests a
# ``cell_set_id`` under each execution, so a "contains an identity id" rule would classify it
# node-scoped, when it is a reuse cache that is already self-describing and is meant to be shared
# across branches. Hence an explicit table.
FACT_ROOT_SCOPES: dict[str, Scope] = {
    # Node-scoped: describes one matrix and must travel with it across a checkout.
    "analysis": "node",
    "annotation": "node",
    "batch": "node",
    "cell_qc": "node",
    "cluster_qc": "node",
    "doublets": "node",
    "finalization": "node",
    "group_gene_ranking": "node",
    # Session-scoped: properties of the input, or caches keyed independently of the active branch.
    "custom_analysis": "session",
    "dataset": "session",
    "dataset_contents": "session",
    "gene_conversion": "session",
    "reference_runs": "session",
}

# Historical events predate declared matrix roles. Keep the role vocabulary versioned here so a
# migration does not guess by extension: CellBender's continuing matrix is ``.h5``, and some tools
# may emit more than one AnnData artifact in one execution.
LEGACY_PRIMARY_MATRIX_OUTPUTS_V1: dict[tuple[str, str], str] = {
    ("cellbender-background-removal", "remove_ambient_background"): "cellbender-filtered-output",
    ("celltypist-annotation", "run_celltypist_annotation"): "celltypist-annotated-anndata",
    ("cluster-qc", "evaluate_cluster_qc"): "cluster-qc-filtered-raw-counts",
    ("dimensionality-reduction", "compute_single_cell_pca"): "pca-anndata",
    ("dimensionality-reduction", "build_single_cell_neighbors"): "neighbors-anndata",
    ("dimensionality-reduction", "compute_single_cell_umap"): "umap-anndata",
    ("doublet-evidence", "evaluate_doublet_evidence"): "doublet-annotated-anndata",
    ("doublet-evidence", "review_doublet_evidence"): "doublet-filtered-raw-counts",
    ("expression-preprocessing", "normalize_single_cell_expression"): "log-normalized-anndata",
    ("expression-preprocessing", "select_highly_variable_genes"): "hvg-anndata",
    ("finalize-analysis", "finalize_analysis"): "final-annotated-anndata",
    ("inspect-dataset", "convert_gene_ids"): "gene-symbols",
    ("scimilarity-annotation", "run_scimilarity_annotation"): "scimilarity-annotated-anndata",
    ("scvi-integration", "train_scvi_latent"): "scvi-latent-anndata",
    ("single-cell-clustering", "cluster_single_cells"): "clustered-anndata",
    ("single-cell-clustering", "rank_single_cell_groups"): "ranked-groups-anndata",
    ("single-cell-counts", "materialize_count_matrix"): "count-ready-anndata",
    ("single-cell-qc", "calculate_single_cell_qc"): "qc-anndata",
    ("single-cell-qc", "filter_single_cells"): "qc-anndata",
    ("single-cell-qc", "filter_single_cell_genes"): "gene-filtered-anndata",
}


class LineageContractError(ValueError):
    """An identity axis or fact root outside the registered vocabulary.

    Raised rather than defaulted: silently guessing a scope decides whether a fact survives a
    branch checkout, and silently ignoring an axis lets two different matrices share a signature.
    """


def identity_signature(
    facts: Mapping[str, Any], *, axes: tuple[str, ...] = IDENTITY_AXES_V1
) -> str:
    """Hash the identity axes present in ``facts.analysis`` into one comparable signature.

    Absent axes are omitted rather than encoded as null, so a session that has not clustered yet
    does not collide with one whose clustering was explicitly cleared. An ``analysis`` child that
    carries an ``id`` but is not a registered axis raises.
    """

    analysis = facts.get("analysis")
    if not isinstance(analysis, Mapping):
        analysis = {}
    unknown = sorted(
        name
        for name, value in analysis.items()
        if isinstance(value, Mapping) and "id" in value and name not in axes
    )
    if unknown:
        raise LineageContractError(
            "unregistered identity axis in facts.analysis: "
            + ", ".join(unknown)
            + ". Add it to IDENTITY_AXES_V1 deliberately, or the signature silently ignores it."
        )
    present: dict[str, str] = {}
    for axis in axes:
        node = analysis.get(axis)
        if isinstance(node, Mapping) and isinstance(node.get("id"), str):
            present[axis] = str(node["id"])
    encoded = json.dumps(present, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return f"identity:v{IDENTITY_SIGNATURE_VERSION}:sha256:{digest}"


def node_scoped_roots() -> frozenset[str]:
    return frozenset(root for root, scope in FACT_ROOT_SCOPES.items() if scope == "node")


def merge_diff(current: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    """An RFC 7396 patch that turns ``current`` into exactly ``target``.

    Needed because a merge patch only ever adds or deletes the keys it names. Moving the head to a
    line of descent that does not inherit the previous head's facts has to *replace* the node-scoped
    view, and ``{"cluster_qc": new_value}`` would leave stale nested keys behind.
    """

    patch: dict[str, Any] = {}
    for key in current:
        if key not in target:
            patch[key] = None
    for key, desired in target.items():
        existing = current.get(key)
        if isinstance(desired, Mapping) and isinstance(existing, Mapping):
            nested = merge_diff(existing, desired)
            if nested:
                patch[key] = nested
        elif key not in current or existing != desired:
            patch[key] = desired
    return patch


def fact_scope(root: str) -> Scope:
    """Return the registered scope for a top-level fact root, or raise."""

    try:
        return FACT_ROOT_SCOPES[root]
    except KeyError:
        raise LineageContractError(
            f"unregistered fact root: {root!r}. Register it in FACT_ROOT_SCOPES as 'node' "
            "(travels with one matrix) or 'session' (independent of the active branch)."
        ) from None


def partition_facts_patch(
    patch: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a facts patch into its node-scoped and session-scoped halves.

    Node-scoped keys land on a lineage node's snapshot and reach global facts only when that node
    is active; session-scoped keys always merge globally. Raises on an unregistered root.
    """

    node: dict[str, Any] = {}
    session: dict[str, Any] = {}
    for root, value in patch.items():
        target = node if fact_scope(str(root)) == "node" else session
        target[str(root)] = value
    return node, session


def has_prepared_path(patch: Mapping[str, Any]) -> bool:
    """Whether a skill patch tries to write the executor-owned active artifact pointer."""

    analysis = patch.get("analysis")
    revision = analysis.get("dataset_revision") if isinstance(analysis, Mapping) else None
    return isinstance(revision, Mapping) and "prepared_path" in revision


def strip_prepared_path(patch: Mapping[str, Any]) -> dict[str, Any]:
    """Drop legacy skill ownership of ``analysis.dataset_revision.prepared_path``."""

    cleaned = deepcopy(dict(patch))
    analysis = cleaned.get("analysis")
    if not isinstance(analysis, dict):
        return cleaned
    revision = analysis.get("dataset_revision")
    if isinstance(revision, dict):
        revision.pop("prepared_path", None)
    return cleaned


def with_prepared_path(patch: Mapping[str, Any], head_path: str) -> dict[str, Any]:
    """Return a node patch whose active-artifact pointer is derived from its lineage node."""

    owned = strip_prepared_path(patch)
    analysis_value = owned.get("analysis")
    analysis = dict(analysis_value) if isinstance(analysis_value, Mapping) else {}
    revision_value = analysis.get("dataset_revision")
    revision = dict(revision_value) if isinstance(revision_value, Mapping) else {}
    revision["prepared_path"] = head_path
    analysis["dataset_revision"] = revision
    owned["analysis"] = analysis
    return owned


# --- forest topology (D2) ------------------------------------------------------------------


@dataclass(frozen=True)
class LineageNode:
    """One H5AD-producing execution.

    Keyed by execution rather than by identity, because two deliberate branches can carry identical
    identities -- the exact case identity-keyed topology cannot represent.
    """

    execution_id: str
    parent_execution_id: str | None
    head_path: str
    identity_signature: str
    requested_input: str | None
    resolved_input_execution_id: str | None
    branch_intent: bool
    skill_id: str
    tool_name: str
    adopt_intent: bool = False
    # Node-scoped fact patches attached to this node, in commit order: the creating execution's
    # own patch first, then any read-only evidence recorded against it.
    fact_patches: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_execution_id": self.parent_execution_id,
            "head_path": self.head_path,
            "identity_signature": self.identity_signature,
            "requested_input": self.requested_input,
            "resolved_input_execution_id": self.resolved_input_execution_id,
            "branch_intent": self.branch_intent,
            "adopt_intent": self.adopt_intent,
            "created_by": {"skill_id": self.skill_id, "tool_name": self.tool_name},
            "fact_patches": [dict(patch) for patch in self.fact_patches],
        }


def empty_forest() -> dict[str, Any]:
    return {"active_execution_id": None, "nodes": {}}


def active_head(lineage: Mapping[str, Any]) -> str | None:
    value = lineage.get("active_execution_id")
    return value if isinstance(value, str) else None


def head_path(lineage: Mapping[str, Any]) -> str | None:
    """Path of the artifact an omitted matrix input should resolve to."""

    node = _node(lineage, active_head(lineage))
    path = node.get("head_path") if node is not None else None
    return path if isinstance(path, str) else None


def _node(lineage: Mapping[str, Any], execution_id: str | None) -> Mapping[str, Any] | None:
    if execution_id is None:
        return None
    nodes = lineage.get("nodes")
    if not isinstance(nodes, Mapping):
        return None
    node = nodes.get(execution_id)
    return node if isinstance(node, Mapping) else None


def ancestry(lineage: Mapping[str, Any], execution_id: str) -> list[str]:
    """Execution IDs from ``execution_id`` up to its root, nearest first.

    Cycles cannot occur through the normal commit path, where a parent always exists before its
    child, but a corrupted or hand-edited forest must not hang the process.
    """

    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = execution_id
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(current)
        node = _node(lineage, current)
        parent = node.get("parent_execution_id") if node is not None else None
        current = parent if isinstance(parent, str) else None
    return chain


PathRelation = Literal["head", "ancestor", "sibling", "untracked"]


def classify_input(lineage: Mapping[str, Any], execution_id: str | None) -> PathRelation:
    """Describe a supplied matrix input relative to the active head.

    ``ancestor`` is called out separately from ``sibling`` because rejecting only siblings is
    exactly what let two annotators diverge: once the first advanced the head, the artifact both
    were handed became an ancestor rather than a sibling.
    """

    if execution_id is None or _node(lineage, execution_id) is None:
        return "untracked"
    head = active_head(lineage)
    if execution_id == head:
        return "head"
    if head is not None and execution_id in ancestry(lineage, head):
        return "ancestor"
    return "sibling"


def node_for_path(
    lineage: Mapping[str, Any], path: str, *, session_dir: str | Path | None = None
) -> str | None:
    """Find the execution that produced ``path``.

    With a session directory, compare canonical absolute paths. Suffix matching would let an
    unrelated file ending in ``artifacts/capabilities/<id>/...`` impersonate a tracked artifact.
    The no-directory fallback remains for pure contract tests and legacy callers.
    """

    nodes = lineage.get("nodes")
    if not isinstance(nodes, Mapping) or not path:
        return None
    normalized = str(path).replace("\\", "/").rstrip("/")
    supplied = None
    root = Path(session_dir).expanduser().resolve() if session_dir is not None else None
    if root is not None:
        supplied_path = Path(path).expanduser()
        supplied = (
            supplied_path.resolve()
            if supplied_path.is_absolute()
            else (root / supplied_path).resolve()
        )
    for execution_id, node in nodes.items():
        if not isinstance(node, Mapping):
            continue
        candidate = node.get("head_path")
        if not isinstance(candidate, str) or not candidate:
            continue
        relative = candidate.replace("\\", "/").rstrip("/")
        if root is not None:
            candidate_path = Path(candidate).expanduser()
            canonical = (
                candidate_path.resolve()
                if candidate_path.is_absolute()
                else (root / candidate_path).resolve()
            )
            matches = canonical == supplied
        else:
            matches = normalized == relative or normalized.endswith("/" + relative)
        if matches:
            return str(execution_id)
    return None


def _historical_node_for_path(
    lineage: Mapping[str, Any], path: str, *, session_dir: str | Path | None = None
) -> str | None:
    """Resolve a recorded input path while rebuilding a possibly moved session.

    Live dispatch deliberately accepts only a canonical path match. Historical events, however,
    recorded absolute paths rooted at the session's location at execution time. After a backup is
    restored elsewhere those paths are stale even though the executor-owned
    ``artifacts/capabilities/<action>--<execution_id>/...`` identity is unchanged.

    Try the strict live rule first, then match the complete executor-owned artifact tail. This
    fallback is migration-only: it cannot weaken live input validation, and requiring the known
    execution ID plus the complete relative artifact path avoids basename-only collisions.
    """

    matched = node_for_path(lineage, path, session_dir=session_dir)
    if matched is not None:
        return matched
    nodes = lineage.get("nodes")
    if not isinstance(nodes, Mapping) or not path:
        return None
    normalized = str(path).replace("\\", "/").rstrip("/")
    matches: list[str] = []
    for execution_id, node in nodes.items():
        if not isinstance(node, Mapping):
            continue
        candidate = node.get("head_path")
        if not isinstance(candidate, str) or not candidate:
            continue
        relative = candidate.replace("\\", "/").lstrip("./").rstrip("/")
        parts = relative.split("/")
        # The owned tail is ``artifacts/capabilities/<dir>/<file...>``. The directory is named
        # ``<action>--<execution_id>`` (or the bare ID, pre-descriptive-naming), so match on the ID
        # preserved as its suffix rather than reconstructing a fixed prefix.
        if len(parts) < 4 or parts[0] != "artifacts" or parts[1] != "capabilities":
            continue
        directory = parts[2]
        if directory != str(execution_id) and not directory.endswith(f"--{execution_id}"):
            continue
        if normalized == relative or normalized.endswith("/" + relative):
            matches.append(str(execution_id))
    return matches[0] if len(matches) == 1 else None


def resolve_node_reference(
    lineage: Mapping[str, Any], reference: str, *, session_dir: str | Path
) -> str | None:
    """Resolve an exact/short execution ID or an artifact path to one node."""

    nodes = lineage.get("nodes")
    if not isinstance(nodes, Mapping):
        return None
    if reference in nodes:
        return reference
    prefixes = sorted(str(key) for key in nodes if str(key).startswith(reference))
    if len(prefixes) > 1:
        raise LineageContractError(
            f"lineage reference {reference!r} matches several versions; supply more characters"
        )
    if prefixes:
        return prefixes[0]
    return node_for_path(lineage, reference, session_dir=session_dir)


def place_node(
    lineage: Mapping[str, Any],
    node: LineageNode,
) -> dict[str, Any]:
    """Return a new forest with ``node`` added.

    The active head advances to the new node unless it was created under branch intent, in which
    case the branch stays reachable but the session keeps working on the previous head. Parentage
    comes from the node itself -- the resolved input's producer -- never from the active head at
    commit time, which is what made three clusterings off one UMAP look like a chain.
    """

    nodes = dict(lineage.get("nodes") or {})
    nodes[node.execution_id] = node.to_dict()
    active = active_head(lineage)
    return {
        "active_execution_id": active if node.branch_intent else node.execution_id,
        "nodes": nodes,
    }


def node_patch(node: LineageNode, *, active_execution_id: str | None) -> dict[str, Any]:
    """Minimal RFC 7396 patch that adds ``node`` to the stored forest.

    Sending the whole forest on every commit would rewrite every node into every event; only the
    new node and the head pointer actually change.

    ``None`` fields are omitted, because a merge patch treats null as *delete*: emitting
    ``parent_execution_id: None`` for a root node erases the key instead of storing it. Readers use
    ``.get()``, so an absent optional field and a null one mean the same thing.
    """

    stored = {key: value for key, value in node.to_dict().items() if value is not None}
    patch: dict[str, Any] = {"nodes": {node.execution_id: stored}}
    if active_execution_id is not None:
        patch["active_execution_id"] = active_execution_id
    return patch


def attach_patch(
    lineage: Mapping[str, Any], execution_id: str, patch: Mapping[str, Any]
) -> dict[str, Any]:
    """Minimal patch appending one node-scoped fact patch to an existing node.

    Read-only evidence tools produce no matrix and so create no node, but they do write node-scoped
    facts. Those must land on the node they describe, or a later checkout restores a snapshot that
    silently lacks them. Merge-patch replaces lists wholesale, so the full new list is emitted.
    """

    node = _node(lineage, execution_id)
    if node is None:
        raise LineageContractError(
            f"cannot attach facts to an unknown lineage node: {execution_id}"
        )
    existing = node.get("fact_patches")
    patches = [dict(item) for item in existing if isinstance(item, Mapping)] if isinstance(
        existing, list
    ) else []
    patches.append(dict(patch))
    return {"nodes": {execution_id: {"fact_patches": patches}}}


def resolve_node_facts(
    lineage: Mapping[str, Any],
    execution_id: str,
    *,
    merge: Any,
) -> dict[str, Any]:
    """Fold every node-scoped patch from the root down to ``execution_id``.

    Patches are stored rather than snapshots: a snapshot per node would place one full copy of the
    session's facts on every node, which for a real session is hundreds of kilobytes each. Folding
    along the ancestry costs a walk and reproduces the same value.

    ``merge`` is the RFC 7396 apply function, injected so this module stays free of imports from
    the store it supports.
    """

    resolved: dict[str, Any] = {}
    for node_id in reversed(ancestry(lineage, execution_id)):
        node = _node(lineage, node_id)
        if node is None:
            continue
        patches = node.get("fact_patches")
        if not isinstance(patches, list):
            continue
        for patch in patches:
            if isinstance(patch, Mapping):
                resolved = merge(resolved, patch)
    return resolved


def checkout(lineage: Mapping[str, Any], execution_id: str) -> dict[str, Any]:
    """Make an existing node active. Raises if it is not in the forest."""

    if _node(lineage, execution_id) is None:
        raise LineageContractError(f"cannot check out an unknown lineage node: {execution_id}")
    return {"active_execution_id": execution_id, "nodes": dict(lineage.get("nodes") or {})}


def rebuild_forest(
    committed: Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any]]],
    *,
    merge: Any,
    session_dir: str | Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Reconstruct the forest by replaying committed capability events.

    ``committed`` is ``(execution_id, payload, state_patch)`` in event order. Everything needed is
    already recorded: the arguments carry the path each execution read, ``files`` carries what it
    wrote, and ``state_patch.facts`` carries the identities and evidence.

    Read-only executions must be replayed too, not only matrix-producing ones. Most of a session's
    evidence -- cluster QC, annotation -- comes from tools that write no matrix, and walking only
    parent chains would rebuild a forest whose nodes describe almost nothing.

    Returns the forest and a list of warnings. Reconstruction is deliberately more forgiving than a
    live commit: an unregistered fact root in already-committed history is preserved as session-wide
    rather than raising, because refusing to open an existing session is worse than declining to
    place one root, and the warning says exactly what was skipped.
    """

    forest = empty_forest()
    warnings: list[str] = []
    unknown_roots: set[str] = set()
    matrices_total = 0
    matrices_without_parent = 0

    for execution_id, payload, state_patch in committed:
        files = payload.get("files")
        files = files if isinstance(files, list) else []
        artifact_root = payload.get("path")
        arguments = payload.get("arguments")
        arguments = arguments if isinstance(arguments, Mapping) else {}

        dispatch = payload.get("lineage")
        advances_lineage = not (
            isinstance(dispatch, Mapping) and dispatch.get("advances_lineage", True) is False
        )
        declared_name = dispatch.get("matrix_output") if isinstance(dispatch, Mapping) else None
        if not isinstance(declared_name, str) or not declared_name:
            declared_name = LEGACY_PRIMARY_MATRIX_OUTPUTS_V1.get(
                (str(payload.get("skill_id", "")), str(payload.get("tool_name", "")))
            )
        declared = [
            str(item.get("relative_path"))
            for item in files
            if isinstance(item, Mapping) and item.get("name") == declared_name
        ]
        fallback = [
            str(item.get("relative_path"))
            for item in files
            if isinstance(item, Mapping)
            and str(item.get("relative_path", "")).lower().endswith((".h5ad", ".zarr"))
        ]
        matrices = (declared or fallback) if advances_lineage else []
        if not declared and len(fallback) > 1:
            warnings.append(
                f"{execution_id}: found {len(fallback)} possible matrix artifacts without a "
                f"historical role declaration; used {fallback[0]}"
            )

        raw_facts = state_patch.get("facts")
        raw_facts = raw_facts if isinstance(raw_facts, Mapping) else {}
        node_facts: dict[str, Any] = {}
        for root, value in raw_facts.items():
            scope = FACT_ROOT_SCOPES.get(str(root))
            if scope is None:
                unknown_roots.add(str(root))
                continue
            if scope == "node":
                node_facts[str(root)] = value
        node_facts = strip_prepared_path(node_facts)

        requested = arguments.get("path")
        requested = requested if isinstance(requested, str) and requested.strip() else None
        parent = (
            _historical_node_for_path(forest, requested, session_dir=session_dir)
            if requested
            else None
        )

        if not matrices:
            # Attach node-scoped evidence to the version it describes: the artifact it read, or the
            # head at this point in the replay for a review that took no matrix.
            target = parent or active_head(forest)
            if target is not None and node_facts:
                patch = attach_patch(forest, target, node_facts)
                nodes = dict(forest["nodes"])
                nodes[target] = {**nodes[target], **patch["nodes"][target]}
                forest = {"active_execution_id": forest["active_execution_id"], "nodes": nodes}
            continue

        matrices_total += 1
        if parent is None:
            matrices_without_parent += 1
        head_relative = f"{artifact_root}/{matrices[0]}" if artifact_root else matrices[0]
        node_facts = with_prepared_path(node_facts, head_relative)
        inherited = resolve_node_facts(forest, parent, merge=merge) if parent else {}
        created = payload.get("tool_name")
        node = LineageNode(
            execution_id=execution_id,
            parent_execution_id=parent,
            head_path=head_relative,
            identity_signature=identity_signature(merge(inherited, node_facts)),
            requested_input=requested,
            resolved_input_execution_id=parent,
            # Historical sessions had no branching vocabulary, so every recorded matrix continued
            # the line of work as it stood.
            branch_intent=False,
            adopt_intent=False,
            skill_id=str(payload.get("skill_id", "")),
            tool_name=str(created) if isinstance(created, str) else "",
            fact_patches=(dict(node_facts),) if node_facts else (),
        )
        forest = place_node(forest, node)

    if unknown_roots:
        warnings.append(
            "kept as session-wide because they are not in FACT_ROOT_SCOPES: "
            + ", ".join(sorted(unknown_roots))
        )
    if matrices_total and matrices_without_parent == matrices_total:
        # Early sessions committed no arguments, so the path each execution read was never
        # recorded and parentage is genuinely unrecoverable. Say so: a flat set of roots is the
        # honest reconstruction, not evidence that the work was unrelated.
        warnings.append(
            f"no input path was recorded for any of the {matrices_total} matrix executions, so "
            "parentage could not be reconstructed; every version is a root"
        )
    elif matrices_without_parent > 1:
        warnings.append(
            f"{matrices_without_parent} of {matrices_total} matrix executions read an untracked "
            "path, so they reconstruct as separate roots"
        )
    return forest, warnings


def reachable_from_heads(lineage: Mapping[str, Any], heads: list[str]) -> set[str]:
    """Executions on the ancestry of any supplied head.

    The prerequisite for pruning: an artifact outside this set is not reachable from live work.
    Retention policy is deliberately not decided here -- liveness is a branch-status question, not
    a topology one.
    """

    keep: set[str] = set()
    for head in heads:
        keep.update(ancestry(lineage, head))
    return keep
