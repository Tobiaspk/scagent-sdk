"""Reconstructing the lineage forest from committed events, and the v1 -> v2 migration.

Stage 5 of ``docs/artifact-lineage-and-head-spec.md``. A schema default cannot substitute for a
reducer: historical events carry no lineage patch, so replaying them leaves every recorded artifact
unreachable. These pin the reconstruction, the migration that applies it, and what it reports when
the recorded history cannot answer a question.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from scagent_sdk.state.lineage import (
    active_head,
    ancestry,
    node_scoped_roots,
    rebuild_forest,
    resolve_node_facts,
)
from scagent_sdk.state.store import SessionStore, apply_merge_patch

_REFERENCE_SESSION = Path(__file__).parents[2] / "sessions" / "run_20260727T052254Z_58b435"


def _event(
    execution_id: str,
    *,
    tool: str = "make_matrix",
    skill: str = "test-skill",
    reads: str | None = None,
    writes: str | None = None,
    facts: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    files = [{"name": "matrix", "relative_path": writes}] if writes else [
        {"name": "evidence", "relative_path": "evidence.json"}
    ]
    return (
        execution_id,
        {
            "execution_id": execution_id,
            "tool_name": tool,
            "skill_id": skill,
            "path": f"artifacts/capabilities/{execution_id}",
            "arguments": {"path": reads} if reads else {},
            "files": files,
        },
        {"facts": facts or {}},
    )


def _artifact(execution_id: str, name: str = "matrix.h5ad") -> str:
    return f"artifacts/capabilities/{execution_id}/{name}"


# --- reconstruction ------------------------------------------------------------------------


def test_a_chain_reconstructs_in_order() -> None:
    forest, warnings = rebuild_forest(
        [
            _event("a", writes="matrix.h5ad"),
            _event("b", reads=_artifact("a"), writes="matrix.h5ad"),
            _event("c", reads=_artifact("b"), writes="matrix.h5ad"),
        ],
        merge=apply_merge_patch,
    )

    assert active_head(forest) == "c"
    assert ancestry(forest, "c") == ["c", "b", "a"]
    assert warnings == []


def test_two_children_of_one_parent_reconstruct_as_siblings() -> None:
    forest, _ = rebuild_forest(
        [
            _event("base", writes="matrix.h5ad"),
            _event("left", reads=_artifact("base"), writes="matrix.h5ad"),
            _event("right", reads=_artifact("base"), writes="matrix.h5ad"),
        ],
        merge=apply_merge_patch,
    )

    assert forest["nodes"]["left"]["parent_execution_id"] == "base"
    assert forest["nodes"]["right"]["parent_execution_id"] == "base"
    assert "left" not in ancestry(forest, "right")


def test_read_only_evidence_is_replayed_onto_its_version() -> None:
    """Replaying only matrix-producing events would rebuild nodes describing almost nothing.

    Most of a session's evidence -- cluster QC, annotation -- comes from tools that write no matrix.
    """

    forest, _ = rebuild_forest(
        [
            _event("a", writes="matrix.h5ad"),
            _event("qc", tool="evaluate_cluster_qc", reads=_artifact("a"),
                   facts={"cluster_qc": {"status": "complete"}}),
            _event("review", tool="review_cluster_qc", facts={"cluster_qc": {"reviewed": True}}),
        ],
        merge=apply_merge_patch,
    )

    assert set(forest["nodes"]) == {"a"}
    resolved = resolve_node_facts(forest, "a", merge=apply_merge_patch)
    assert resolved["cluster_qc"] == {"status": "complete", "reviewed": True}


def test_format_only_matrix_export_does_not_become_a_node_during_replay() -> None:
    base = _event("base", writes="matrix.h5ad")
    exported = _event("export", tool="export_anndata", reads=_artifact("base"))
    exported[1]["files"] = [{"name": "portable-anndata", "relative_path": "portable.h5ad"}]
    exported[1]["lineage"] = {
        "matrix_output": "portable.h5ad",
        "advances_lineage": False,
    }

    forest, _warnings = rebuild_forest([base, exported], merge=apply_merge_patch)

    assert set(forest["nodes"]) == {"base"}
    assert active_head(forest) == "base"


def test_session_scoped_facts_are_not_attached_to_a_version() -> None:
    forest, _ = rebuild_forest(
        [
            _event("a", writes="matrix.h5ad", facts={"gene_conversion": {"changed": True}}),
        ],
        merge=apply_merge_patch,
    )
    assert "gene_conversion" not in resolve_node_facts(forest, "a", merge=apply_merge_patch)


def test_an_unregistered_fact_root_is_reported_not_fatal() -> None:
    """Reconstruction is more forgiving than a live commit.

    Refusing to open an existing session is worse than declining to place one root, so it is kept
    session-wide and named in the warnings.
    """

    forest, warnings = rebuild_forest(
        [_event("a", writes="matrix.h5ad", facts={"pathways": {"x": 1}})],
        merge=apply_merge_patch,
    )

    assert set(forest["nodes"]) == {"a"}
    assert any("pathways" in warning for warning in warnings)


def test_missing_input_arguments_are_reported_rather_than_implied() -> None:
    """Early sessions recorded no arguments, so parentage is genuinely unrecoverable.

    A flat set of roots is the honest reconstruction; the warning stops it reading as evidence that
    the work was unrelated.
    """

    forest, warnings = rebuild_forest(
        [
            _event("a", writes="matrix.h5ad"),
            _event("b", writes="matrix.h5ad"),
            _event("c", writes="matrix.h5ad"),
        ],
        merge=apply_merge_patch,
    )

    assert all(node.get("parent_execution_id") is None for node in forest["nodes"].values())
    assert any("no input path was recorded" in warning for warning in warnings)


def test_multiple_matrix_artifacts_are_reported() -> None:
    execution_id, payload, patch = _event("a", writes="matrix.h5ad")
    payload["files"].append({"name": "extra", "relative_path": "second.h5ad"})

    _, warnings = rebuild_forest([(execution_id, payload, patch)], merge=apply_merge_patch)
    assert any("found 2 possible matrix artifacts" in warning for warning in warnings)


def test_cellbender_h5_primary_output_reconstructs_by_historical_role() -> None:
    event = _event(
        "cellbender",
        skill="cellbender-background-removal",
        tool="remove_ambient_background",
    )
    event[1]["files"] = [
        {"name": "cellbender-full-output", "relative_path": "output.h5"},
        {"name": "cellbender-filtered-output", "relative_path": "output_filtered.h5"},
        {"name": "cellbender-posterior", "relative_path": "posterior.h5"},
    ]
    child = _event(
        "counts",
        skill="single-cell-counts",
        tool="materialize_count_matrix",
        reads=_artifact("cellbender", "output_filtered.h5"),
        writes="counts-ready.h5ad",
    )

    forest, warnings = rebuild_forest([event, child], merge=apply_merge_patch)

    assert ancestry(forest, "counts") == ["counts", "cellbender"]
    assert forest["nodes"]["cellbender"]["head_path"].endswith("output_filtered.h5")
    assert warnings == []


def test_historical_role_selects_the_primary_when_one_execution_has_two_h5ads() -> None:
    event = _event(
        "sci",
        skill="scimilarity-annotation",
        tool="run_scimilarity_annotation",
    )
    event[1]["files"] = [
        {"name": "diagnostic-copy", "relative_path": "scimilarity-mahal.h5ad"},
        {
            "name": "scimilarity-annotated-anndata",
            "relative_path": "scimilarity-annotated.h5ad",
        },
    ]

    forest, warnings = rebuild_forest([event], merge=apply_merge_patch)

    assert forest["nodes"]["sci"]["head_path"].endswith("scimilarity-annotated.h5ad")
    assert len(warnings) == 1
    assert "no input path was recorded" in warnings[0]


def test_reconstruction_is_deterministic() -> None:
    events = [
        _event("a", writes="matrix.h5ad", facts={"cell_qc": {"n": 1}}),
        _event("b", reads=_artifact("a"), writes="matrix.h5ad"),
    ]
    first, _ = rebuild_forest(events, merge=apply_merge_patch)
    second, _ = rebuild_forest(events, merge=apply_merge_patch)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- migration -----------------------------------------------------------------------------


def _v1_session(tmp_path: Path, events: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> Path:
    """Build a session whose checkpoint is at state schema v1, as sessions on disk are."""

    root = tmp_path / "sessions"
    store = SessionStore.create(root, title="v1", session_id="run_v1_000000")
    for execution_id, payload, patch in events:
        store.record(
            "capability.result_committed",
            payload=payload,
            state_patch={**patch, "artifacts": {execution_id: {"kind": "capability-result"}}},
        )
    state = json.loads((store.session_dir / "state.json").read_text())
    state["schema_version"] = 1
    state.pop("lineage", None)
    (store.session_dir / "state.json").write_text(json.dumps(state, indent=2, sort_keys=True))
    return root


def test_opening_a_v1_session_reconstructs_and_upgrades_it(tmp_path: Path) -> None:
    root = _v1_session(
        tmp_path,
        [
            _event(
                "a",
                writes="matrix.h5ad",
                facts={
                    "analysis": {
                        "dataset_revision": {"id": "rev:1", "prepared_path": "stale.h5ad"}
                    },
                    "cell_qc": {"step": 1},
                },
            ),
            _event(
                "b",
                reads=_artifact("a"),
                writes="matrix.h5ad",
                facts={"analysis": {"dataset_revision": {"prepared_path": "also-stale.h5ad"}}},
            ),
        ],
    )

    store = SessionStore.open(root, "run_v1_000000")

    assert store.state.schema_version == 2
    assert active_head(store.state.lineage) == "b"
    assert ancestry(store.state.lineage, "b") == ["b", "a"]
    expected_path = _artifact("b")
    assert store.state.facts["analysis"]["dataset_revision"]["prepared_path"] == expected_path
    assert resolve_node_facts(
        store.state.lineage, "b", merge=apply_merge_patch
    )["analysis"]["dataset_revision"]["prepared_path"] == expected_path
    # Written through, so the reconstruction is paid for once.
    assert json.loads((store.session_dir / "state.json").read_text())["schema_version"] == 2


def test_moved_session_reconstructs_absolute_historical_paths_without_losing_facts(
    tmp_path: Path,
) -> None:
    """Absolute event arguments must not make a restored backup look like separate roots."""

    execution_ids = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    )
    original_parent = tmp_path / "original"
    original_session = original_parent / "sessions" / "run_v1_000000"
    events = [
        _event(execution_ids[0], writes="matrix.h5ad", facts={"cell_qc": {"kept": True}}),
        _event(
            execution_ids[1],
            reads=str(original_session / _artifact(execution_ids[0])),
            writes="matrix.h5ad",
            facts={"batch": {"status": "reviewed"}},
        ),
        _event(
            execution_ids[2],
            reads=str(original_session / _artifact(execution_ids[1])),
            writes="matrix.h5ad",
            facts={"doublets": {"status": "reviewed"}},
        ),
    ]
    original_root = _v1_session(original_parent, events)
    for execution_id in execution_ids:
        artifact = original_root / "run_v1_000000" / _artifact(execution_id)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"H5AD")

    restored_root = tmp_path / "restored" / "sessions"
    restored_root.mkdir(parents=True)
    shutil.copytree(
        original_root / "run_v1_000000",
        restored_root / "run_v1_000000",
        symlinks=False,
    )

    store = SessionStore.open(restored_root, "run_v1_000000")

    assert ancestry(store.state.lineage, execution_ids[2]) == list(reversed(execution_ids))
    assert {"cell_qc", "batch", "doublets"} <= store.state.facts.keys()
    assert not [event for event in store.events() if event.kind == "session.state_migrated"]


def test_degraded_reconstruction_preserves_the_legacy_fact_checkpoint(tmp_path: Path) -> None:
    """Unrecoverable topology may stay flat, but migration must never shrink recorded evidence."""

    root = _v1_session(
        tmp_path,
        [
            _event("a", writes="matrix.h5ad", facts={"cell_qc": {"kept": True}}),
            _event(
                "b",
                reads="/unrelated/location/a/matrix.h5ad",
                writes="matrix.h5ad",
                facts={"batch": {"status": "reviewed"}},
            ),
            _event(
                "c",
                reads="/unrelated/location/b/matrix.h5ad",
                writes="matrix.h5ad",
                facts={"doublets": {"status": "reviewed"}},
            ),
        ],
    )

    store = SessionStore.open(root, "run_v1_000000")

    assert all(
        node.get("parent_execution_id") is None
        for node in store.state.lineage["nodes"].values()
    )
    assert {"cell_qc", "batch", "doublets"} <= store.state.facts.keys()
    migration = next(event for event in store.events() if event.kind == "session.state_migrated")
    assert any(
        "preserved legacy global facts" in warning
        for warning in migration.payload["warnings"]
    )


def test_migration_is_idempotent(tmp_path: Path) -> None:
    root = _v1_session(tmp_path, [_event("a", writes="matrix.h5ad")])

    first = SessionStore.open(root, "run_v1_000000")
    revision = first.state.revision
    second = SessionStore.open(root, "run_v1_000000")

    assert second.state.schema_version == 2
    assert second.state.lineage == first.state.lineage
    assert second.state.revision == revision  # no second migration event


def test_migration_records_its_warnings(tmp_path: Path) -> None:
    root = _v1_session(
        tmp_path, [_event("a", writes="matrix.h5ad", facts={"pathways": {"x": 1}})]
    )
    store = SessionStore.open(root, "run_v1_000000")

    migrations = [e for e in store.events() if e.kind == "session.state_migrated"]
    assert len(migrations) == 1
    assert migrations[0].payload["lineage_nodes"] == 1
    assert any("pathways" in w for w in migrations[0].payload["warnings"])


def test_a_clean_migration_records_no_event(tmp_path: Path) -> None:
    root = _v1_session(tmp_path, [_event("a", writes="matrix.h5ad")])
    before = len(SessionStore.open(root, "run_v1_000000").events())

    # Reopening must not append anything either.
    assert len(SessionStore.open(root, "run_v1_000000").events()) == before


def test_a_session_with_no_matrix_work_migrates_to_an_empty_forest(tmp_path: Path) -> None:
    root = _v1_session(
        tmp_path, [_event("look", tool="describe_dataset", facts={"dataset_contents": {"n": 1}})]
    )
    store = SessionStore.open(root, "run_v1_000000")

    assert store.state.schema_version == 2
    assert store.state.lineage["nodes"] == {}
    assert store.state.facts["dataset_contents"] == {"n": 1}


# --- the reference session -----------------------------------------------------------------


@pytest.mark.skipif(
    not (_REFERENCE_SESSION / "events.jsonl").is_file(),
    reason="the reference session is local host state, not a committed fixture",
)
def test_the_reference_session_reconstructs_exactly() -> None:
    """Acceptance case 5, against the session the whole design was derived from.

    The strongest available check: the reducer reaches the session's own facts by a different route
    -- replaying events through node routing -- than the one that originally accumulated them.
    """

    committed = []
    for line in (_REFERENCE_SESSION / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if event["kind"] != "capability.result_committed":
            continue
        committed.append(
            (str(event["payload"]["execution_id"]), event["payload"], event["state_patch"])
        )
    forest, warnings = rebuild_forest(
        committed, merge=apply_merge_patch, session_dir=_REFERENCE_SESSION
    )

    head = active_head(forest)
    assert head is not None
    chain = ancestry(forest, head)

    # The lineage traced by hand from this session's artifacts, head first.
    assert [Path(str(forest["nodes"][node]["head_path"])).name for node in chain] == [
        "final-annotated.h5ad",
        "celltypist-annotated.h5ad",
        "scimilarity-annotated.h5ad",
        "clustered.h5ad",
        "umap.h5ad",
        "neighbors.h5ad",
        "pca.h5ad",
        "hvg-selected.h5ad",
        "log-normalized.h5ad",
        "doublet-annotated.h5ad",
        "qc-assessed.h5ad",
        "counts-ready.h5ad",
        "gene-symbols.h5ad",
    ]

    # Exactly the two abandoned clusterings from the resolution sweep, both children of the UMAP.
    off_line = sorted(set(forest["nodes"]) - set(chain))
    assert len(off_line) == 2
    umap = next(
        node
        for node in chain
        if Path(str(forest["nodes"][node]["head_path"])).name == "umap.h5ad"
    )
    for node in off_line:
        assert Path(str(forest["nodes"][node]["head_path"])).name == "clustered.h5ad"
        assert forest["nodes"][node]["parent_execution_id"] == umap

    assert warnings == []


@pytest.mark.skipif(
    not (_REFERENCE_SESSION / "state.json").is_file(),
    reason="the reference session is local host state, not a committed fixture",
)
def test_the_reference_session_facts_survive_reconstruction() -> None:
    committed = []
    for line in (_REFERENCE_SESSION / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if event["kind"] == "capability.result_committed":
            committed.append(
                (str(event["payload"]["execution_id"]), event["payload"], event["state_patch"])
            )
    forest, _ = rebuild_forest(
        committed, merge=apply_merge_patch, session_dir=_REFERENCE_SESSION
    )

    recorded = json.loads((_REFERENCE_SESSION / "state.json").read_text())["facts"]
    expected = {key: value for key, value in recorded.items() if key in node_scoped_roots()}
    resolved = resolve_node_facts(forest, active_head(forest), merge=apply_merge_patch)

    assert sorted(resolved) == sorted(expected)
    for key in expected:
        if key != "analysis":
            assert resolved[key] == expected[key], key
    expected_analysis = json.loads(json.dumps(expected["analysis"]))
    expected_analysis["dataset_revision"]["prepared_path"] = forest["nodes"][
        active_head(forest)
    ]["head_path"]
    assert resolved["analysis"] == expected_analysis
