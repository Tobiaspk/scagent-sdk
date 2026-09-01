"""Lineage recorded at commit: node creation, fact routing, staleness, and recovery.

Stage 2 of ``docs/artifact-lineage-and-head-spec.md``. These drive the real executor rather than
the pure helpers, so they pin the wiring: what a dispatch records, what a commit writes into the
forest, and which facts become globally visible.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.capabilities.results import capability_artifact_directory_name
from scagent_sdk.errors import CapabilityExecutionError
from scagent_sdk.session import AnalysisSession
from scagent_sdk.state.lineage import (
    active_head,
    ancestry,
    classify_input,
    node_scoped_roots,
    resolve_node_facts,
)
from scagent_sdk.state.store import apply_merge_patch


def _skill(root: Path) -> Path:
    """A skill that can write a matrix, write only evidence, or clone its input identities."""

    skill = root / "skills" / "matrix"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: matrix\ndescription: test skill\n---\n\nMatrix.\n", encoding="utf-8"
    )
    (skill / "capability.yaml").write_text(
        """\
schema_version: 1
skill: {id: matrix, version: "1", description: test}
tools:
  - name: make_matrix
    description: write a matrix artifact
    entrypoint: scripts/run.py:make_matrix
    input_schema:
      type: object
      properties:
        path: {type: string}
        clustering: {type: string}
        facts: {type: object}
    primary_matrix_input: path
    primary_matrix_output: matrix
  - name: write_evidence
    description: write node-scoped evidence without a matrix
    entrypoint: scripts/run.py:write_evidence
    input_schema:
      type: object
      properties:
        path: {type: string}
        facts: {type: object}
    primary_matrix_input: path
  - name: export_matrix
    description: publish a format-only matrix artifact
    entrypoint: scripts/run.py:export_matrix
    input_schema:
      type: object
      properties:
        path: {type: string}
    primary_matrix_input: path
    primary_matrix_output: portable-matrix
    advances_lineage: false
  - name: review_facts
    description: write node-scoped evidence from facts only
    entrypoint: scripts/run.py:write_evidence
    input_schema:
      type: object
      properties:
        facts: {type: object}
""",
        encoding="utf-8",
    )
    (skill / "scripts" / "run.py").write_text(
        '''
def make_matrix(arguments, context):
    assert "adopt_untracked" not in arguments, arguments
    (context.staging_dir / "matrix.h5ad").write_bytes(b"H5AD")
    facts = dict(arguments.get("facts") or {})
    clustering = arguments.get("clustering")
    if clustering:
        analysis = dict(facts.get("analysis") or {})
        analysis["clustering"] = {"id": clustering}
        facts["analysis"] = analysis
    return {
        "summary": "wrote a matrix",
        "details": {},
        "facts_patch": facts,
        "artifacts": [
            {
                "name": "matrix",
                "relative_path": "matrix.h5ad",
                "media_type": "application/x-hdf5",
            }
        ],
    }


def write_evidence(arguments, context):
    (context.staging_dir / "evidence.json").write_text("{}\\n")
    return {
        "summary": "wrote evidence",
        "details": {},
        "facts_patch": dict(arguments.get("facts") or {}),
        "artifacts": [
            {
                "name": "evidence",
                "relative_path": "evidence.json",
                "media_type": "application/json",
            }
        ],
    }


def export_matrix(arguments, context):
    (context.staging_dir / "portable.h5ad").write_bytes(b"H5AD")
    return {
        "summary": "exported a matrix",
        "details": {},
        "facts_patch": {},
        "artifacts": [
            {
                "name": "portable-matrix",
                "relative_path": "portable.h5ad",
                "media_type": "application/x-h5ad",
            }
        ],
    }
'''.lstrip(),
        encoding="utf-8",
    )
    return skill.parent


class _Harness:
    def __init__(self, tmp_path: Path, title: str):
        self.package = CapabilityRegistry(_skill(tmp_path)).discover()[0]
        self.tools = {tool.name: tool for tool in self.package.manifest.tools}
        self.session = AnalysisSession.create(tmp_path / "sessions", title=title)
        self.executor = CapabilityExecutor(self.session)
        self.seed_path = tmp_path / f"{title}-seed.h5ad"
        self.seed_path.write_bytes(b"H5AD")

    def run(self, tool: str, commit: bool = True, **arguments: Any) -> str:
        if tool == "make_matrix" and "path" not in arguments and active_head(self.lineage) is None:
            arguments["path"] = str(self.seed_path)
        response = asyncio.run(
            self.executor.execute(self.package, self.tools[tool], dict(arguments))
        )
        assert response.get("is_error") is not True, response
        execution_id = response["structuredContent"]["scagent_execution_id"]
        if commit:
            assert self.executor.commit(execution_id)
        return execution_id

    def matrix_path(self, execution_id: str) -> str:
        # The committed directory is named ``<action>--<id>``. Prefer the recorded path; before a
        # commit exists (recovery-ordering tests stage without committing), compute the same name
        # the executor would -- matrix_path only ever names ``make_matrix`` outputs here.
        record = self.session.store.state.artifacts.get(execution_id)
        directory = (
            record["path"]
            if isinstance(record, dict) and record.get("path")
            else "artifacts/capabilities/"
            + capability_artifact_directory_name("make_matrix", execution_id)
        )
        return str(self.session.directory / directory / "matrix.h5ad")

    @property
    def lineage(self) -> dict[str, Any]:
        return self.session.store.state.lineage

    @property
    def facts(self) -> dict[str, Any]:
        return self.session.store.state.facts


# --- node creation -------------------------------------------------------------------------


def test_first_matrix_creates_a_root_node_and_becomes_the_head(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "root")
    first = harness.run("make_matrix")

    node = harness.lineage["nodes"][first]
    assert active_head(harness.lineage) == first
    # Optional fields are omitted rather than stored as null: a merge patch deletes null keys.
    assert node.get("parent_execution_id") is None
    first_dir = capability_artifact_directory_name("make_matrix", first)
    assert node["head_path"] == f"artifacts/capabilities/{first_dir}/matrix.h5ad"
    assert node["identity_signature"].startswith("identity:v1:sha256:")
    prepared = f"artifacts/capabilities/{first_dir}/matrix.h5ad"
    assert harness.facts["analysis"]["dataset_revision"]["prepared_path"] == prepared
    assert resolve_node_facts(harness.lineage, first, merge=apply_merge_patch)["analysis"][
        "dataset_revision"
    ]["prepared_path"] == prepared


def test_parent_is_the_artifact_actually_consumed(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "chain")
    first = harness.run("make_matrix")
    second = harness.run("make_matrix", path=harness.matrix_path(first))

    assert harness.lineage["nodes"][second]["parent_execution_id"] == first
    assert harness.lineage["nodes"][second]["resolved_input_execution_id"] == first
    assert ancestry(harness.lineage, second) == [second, first]


def test_format_only_matrix_export_records_parent_without_advancing_head(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, "format-only-export")
    first = harness.run("make_matrix")
    second = harness.run("make_matrix")

    exported = harness.run("export_matrix", path=harness.matrix_path(first))

    assert active_head(harness.lineage) == second
    assert exported not in harness.lineage["nodes"]
    artifact = harness.session.store.state.artifacts[exported]
    assert artifact["lineage"]["resolved_input_execution_id"] == first
    assert artifact["lineage"]["input_relation"] == "ancestor"
    assert artifact["lineage"]["matrix_output"] == "portable.h5ad"
    assert artifact["lineage"]["advances_lineage"] is False
    assert (harness.session.directory / artifact["path"] / "portable.h5ad").is_file()
    assert harness.facts["analysis"]["dataset_revision"]["prepared_path"].endswith(
        f"make-matrix--{second}/matrix.h5ad"
    )


def test_committing_a_child_hard_links_unchanged_files_to_its_parent(tmp_path: Path) -> None:
    # make_matrix writes identical bytes, so a child that descends from a parent should have its
    # matrix.h5ad hard-linked onto the parent's inode at commit (ADR 0011 P1 dedup).
    harness = _Harness(tmp_path, "dedup-chain")
    first = harness.run("make_matrix")
    second = harness.run("make_matrix", path=harness.matrix_path(first))

    parent_matrix = Path(harness.matrix_path(first))
    child_matrix = Path(harness.matrix_path(second))
    assert child_matrix.read_bytes() == b"H5AD"  # content intact
    assert child_matrix.stat().st_ino == parent_matrix.stat().st_ino  # shared inode
    assert parent_matrix.stat().st_nlink >= 2


def test_a_sweep_from_one_parent_now_needs_explicit_branch_intent(tmp_path: Path) -> None:
    """A resolution sweep used to be expressible by passing the same parent repeatedly.

    That is indistinguishable from the divergence bug, so it is refused. Deliberate forks get an
    explicit vocabulary with D4; sibling topology itself is covered in lineage_contract_test.
    """

    harness = _Harness(tmp_path, "sweep")
    base = harness.run("make_matrix")
    first = harness.run("make_matrix", path=harness.matrix_path(base), clustering="clust:0")
    assert harness.lineage["nodes"][first]["parent_execution_id"] == base

    response = asyncio.run(
        harness.executor.execute(
            harness.package,
            harness.tools["make_matrix"],
            {"path": harness.matrix_path(base), "clustering": "clust:1"},
        )
    )
    assert response["is_error"] is True
    assert "tracked ancestor" in response["error_summary"]


def test_untracked_input_starts_a_root_rather_than_inventing_a_parent(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "untracked")
    external = tmp_path / "external.h5ad"
    external.write_bytes(b"H5AD")
    execution = harness.run("make_matrix", path=str(external))

    assert harness.lineage["nodes"][execution].get("parent_execution_id") is None
    assert harness.lineage["nodes"][execution]["requested_input"] == str(external)


def test_untracked_input_mid_session_requires_explicit_adoption(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "untracked-mid-session")
    original = harness.run("make_matrix")
    external = tmp_path / "replacement.h5ad"
    external.write_bytes(b"replacement")

    response = asyncio.run(
        harness.executor.execute(
            harness.package, harness.tools["make_matrix"], {"path": str(external)}
        )
    )

    assert response["is_error"] is True
    assert "adopt_untracked" in response["error_summary"]
    assert active_head(harness.lineage) == original


def test_explicit_adoption_starts_a_new_root_and_moves_the_head(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "adopt")
    original = harness.run("make_matrix")
    external = tmp_path / "replacement.h5ad"
    external.write_bytes(b"replacement")

    adopted = harness.run(
        "make_matrix", path=str(external), adopt_untracked=True, clustering="clust:new"
    )

    assert active_head(harness.lineage) == adopted
    assert harness.lineage["nodes"][adopted].get("parent_execution_id") is None
    assert harness.lineage["nodes"][adopted]["adopt_intent"] is True
    assert classify_input(harness.lineage, original) == "sibling"


def test_identity_signature_tracks_a_changed_clustering(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "signature")
    first = harness.run("make_matrix", clustering="clust:1")
    second = harness.run("make_matrix", path=harness.matrix_path(first), clustering="clust:2")
    third = harness.run("make_matrix", path=harness.matrix_path(second))

    nodes = harness.lineage["nodes"]
    assert nodes[first]["identity_signature"] != nodes[second]["identity_signature"]
    # An execution that changes no identity keeps the signature it inherited.
    assert nodes[second]["identity_signature"] == nodes[third]["identity_signature"]


# --- fact routing --------------------------------------------------------------------------


def test_node_scoped_facts_reach_global_state_and_the_node(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "routing")
    execution = harness.run("make_matrix", facts={"cluster_qc": {"status": "complete"}})

    assert harness.facts["cluster_qc"] == {"status": "complete"}
    resolved = resolve_node_facts(harness.lineage, execution, merge=apply_merge_patch)
    assert resolved["cluster_qc"] == {"status": "complete"}


def test_session_scoped_facts_are_not_snapshotted(tmp_path: Path) -> None:
    """``gene_conversion`` describes the input file, not one matrix, so it stays global only."""

    harness = _Harness(tmp_path, "session-scope")
    execution = harness.run("make_matrix", facts={"gene_conversion": {"changed": True}})

    assert harness.facts["gene_conversion"] == {"changed": True}
    resolved = resolve_node_facts(harness.lineage, execution, merge=apply_merge_patch)
    assert "gene_conversion" not in resolved


def test_read_only_evidence_attaches_to_the_head_without_moving_it(tmp_path: Path) -> None:
    """Acceptance case 8. Most session evidence comes from tools that write no matrix."""

    harness = _Harness(tmp_path, "read-only")
    matrix = harness.run("make_matrix")
    before = json.dumps(harness.lineage["nodes"][matrix], sort_keys=True)

    harness.run(
        "write_evidence",
        path=harness.matrix_path(matrix),
        facts={"annotation": {"evidence": {"markers": "complete"}}},
    )

    assert active_head(harness.lineage) == matrix
    assert set(harness.lineage["nodes"]) == {matrix}
    assert harness.facts["annotation"] == {"evidence": {"markers": "complete"}}

    resolved = resolve_node_facts(harness.lineage, matrix, merge=apply_merge_patch)
    assert resolved["annotation"] == {"evidence": {"markers": "complete"}}
    # The node gained a patch but nothing structural changed.
    after = json.loads(json.dumps(harness.lineage["nodes"][matrix], sort_keys=True))
    structural = {k: v for k, v in after.items() if k != "fact_patches"}
    assert structural == {
        k: v for k, v in json.loads(before).items() if k != "fact_patches"
    }


def test_read_only_evidence_without_an_input_targets_the_head(tmp_path: Path) -> None:
    """A pure review over facts carries no matrix path; rule 4 sends it to the active node."""

    harness = _Harness(tmp_path, "review")
    matrix = harness.run("make_matrix")
    harness.run("write_evidence", facts={"annotation": {"review": "resolved"}})

    resolved = resolve_node_facts(harness.lineage, matrix, merge=apply_merge_patch)
    assert resolved["annotation"] == {"review": "resolved"}


def test_read_only_node_facts_from_an_untracked_matrix_are_refused(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "external-evidence")
    head = harness.run("make_matrix")
    external = tmp_path / "unrelated.h5ad"
    external.write_bytes(b"unrelated")
    response = asyncio.run(
        harness.executor.execute(
            harness.package,
            harness.tools["write_evidence"],
            {
                "path": str(external),
                "facts": {"annotation": {"evidence": {"markers": "wrong-dataset"}}},
            },
        )
    )

    assert response["is_error"] is True
    assert "untracked matrix" in response["error_summary"]
    assert active_head(harness.lineage) == head
    assert "annotation" not in harness.facts


def test_pathless_node_facts_require_an_existing_analysis_version(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "evidence-before-version")

    response = asyncio.run(
        harness.executor.execute(
            harness.package,
            harness.tools["review_facts"],
            {"facts": {"annotation": {"review": "orphan"}}},
        )
    )

    assert response["is_error"] is True
    assert "before an analysis version existed" in response["error_summary"]
    assert harness.facts == {}


def test_read_only_session_facts_from_an_untracked_matrix_remain_allowed(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "external-provenance")
    head = harness.run("make_matrix")
    external = tmp_path / "unrelated.h5ad"
    external.write_bytes(b"unrelated")

    harness.run(
        "write_evidence",
        path=str(external),
        facts={"dataset_contents": {"path": str(external)}},
    )

    assert active_head(harness.lineage) == head
    assert harness.facts["dataset_contents"]["path"] == str(external)


def test_evidence_on_an_ancestor_is_inherited_by_the_head(tmp_path: Path) -> None:
    """An ancestor's facts are part of the head's resolved view, so they stay globally visible.

    Suppressing them would make global facts disagree with ``resolve_node_facts(active)``, which is
    the invariant that lets a checkout reconstruct state.
    """

    harness = _Harness(tmp_path, "ancestor")
    first = harness.run("make_matrix")
    second = harness.run("make_matrix", path=harness.matrix_path(first))
    assert classify_input(harness.lineage, first) == "ancestor"

    harness.run(
        "write_evidence",
        path=harness.matrix_path(first),
        facts={"cluster_qc": {"status": "on-ancestor"}},
    )

    assert harness.facts["cluster_qc"] == {"status": "on-ancestor"}
    resolved = resolve_node_facts(harness.lineage, second, merge=apply_merge_patch)
    assert resolved["cluster_qc"] == {"status": "on-ancestor"}


def test_evidence_on_a_sibling_branch_stays_out_of_global_facts(tmp_path: Path) -> None:
    """The isolation that matters: a branch the head does not descend from is not visible.

    Reachable without explicit branch intent -- deriving twice from the same parent leaves the
    first derivation as a sibling of the new head.
    """

    harness = _Harness(tmp_path, "sibling")
    base = harness.run("make_matrix")
    head = harness.run("make_matrix", path=harness.matrix_path(base), clustering="clust:b")
    # Creating a sibling through the executor needs the branch intent that arrives with D4, so the
    # node is inserted directly to exercise the routing rule that will govern it.
    sibling = "51b11ac6-0000-4000-8000-000000000000"
    harness.session.store.record(
        "test.sibling_node",
        state_patch={
            "lineage": {
                "nodes": {
                    sibling: {
                        "parent_execution_id": base,
                        "head_path": f"artifacts/capabilities/{sibling}/matrix.h5ad",
                        "identity_signature": "identity:v1:sha256:sibling",
                        "branch_intent": True,
                        "fact_patches": [],
                    }
                }
            }
        },
    )

    assert active_head(harness.lineage) == head
    assert classify_input(harness.lineage, sibling) == "sibling"

    sibling_dir = harness.session.directory / "artifacts" / "capabilities" / sibling
    sibling_dir.mkdir(parents=True)
    (sibling_dir / "matrix.h5ad").write_bytes(b"H5AD")
    harness.run(
        "write_evidence",
        path=str(sibling_dir / "matrix.h5ad"),
        facts={"cluster_qc": {"status": "on-sibling"}},
    )

    assert "cluster_qc" not in harness.facts
    assert resolve_node_facts(harness.lineage, sibling, merge=apply_merge_patch)["cluster_qc"] == {
        "status": "on-sibling"
    }
    assert "cluster_qc" not in resolve_node_facts(harness.lineage, head, merge=apply_merge_patch)


def test_global_facts_match_the_resolved_view_of_the_active_head(tmp_path: Path) -> None:
    """The invariant the routing rules exist to maintain, exercised over a mixed history."""

    harness = _Harness(tmp_path, "invariant")
    first = harness.run("make_matrix", facts={"cell_qc": {"step": 1}})
    harness.run("write_evidence", facts={"annotation": {"step": 2}})
    second = harness.run(
        "make_matrix", path=harness.matrix_path(first), facts={"cluster_qc": {"step": 3}}
    )
    harness.run("write_evidence", path=harness.matrix_path(second), facts={"batch": {"step": 4}})
    harness.run("make_matrix", facts={"doublets": {"step": 5}})

    active = active_head(harness.lineage)
    assert active is not None
    resolved = resolve_node_facts(harness.lineage, active, merge=apply_merge_patch)
    node_scoped = {
        key: value for key, value in harness.facts.items() if key in node_scoped_roots()
    }
    assert node_scoped == resolved


def test_unregistered_fact_root_fails_as_a_tool_error_not_a_hook_error(tmp_path: Path) -> None:
    """Fail closed, but early: the model should see an ordinary tool error it can act on.

    Validating only at commit would report the capability as successful and then fail inside
    PostToolUse, after the compute had already been paid for.
    """

    harness = _Harness(tmp_path, "unregistered")
    response = asyncio.run(
        harness.executor.execute(
            harness.package,
            harness.tools["make_matrix"],
            {"path": str(harness.seed_path), "facts": {"pathways": {"a": 1}}},
        )
    )

    assert response["is_error"] is True
    assert "unregistered fact root: 'pathways'" in response["error_summary"]
    # Nothing was staged into state, so the session is untouched.
    assert harness.session.store.state.artifacts == {}
    assert harness.lineage["nodes"] == {}


def test_skill_cannot_write_the_executor_owned_prepared_path(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "prepared-owner")

    response = asyncio.run(
        harness.executor.execute(
            harness.package,
            harness.tools["make_matrix"],
            {
                "path": str(harness.seed_path),
                "facts": {
                    "analysis": {"dataset_revision": {"prepared_path": "wrong.h5ad"}}
                },
            },
        )
    )

    assert response["is_error"] is True
    assert "prepared_path is executor-owned" in response["error_summary"]
    assert harness.lineage["nodes"] == {}


# --- staleness and recovery ----------------------------------------------------------------


def test_commit_is_refused_when_the_head_moved_underneath_it(tmp_path: Path) -> None:
    """D6. A long compute must not silently rebase onto a head that appeared while it ran."""

    harness = _Harness(tmp_path, "stale-base")
    base = harness.run("make_matrix")
    slow = harness.run("make_matrix", commit=False, path=harness.matrix_path(base))
    fast = harness.run("make_matrix", path=harness.matrix_path(base))

    assert active_head(harness.lineage) == fast
    with pytest.raises(CapabilityExecutionError, match="active head is now"):
        harness.executor.commit(slow)

    # Rejected before the staging directory moved, so nothing looks committed.
    assert (harness.executor.pending_root / slow).is_dir()
    assert not (harness.executor.artifact_root / slow).exists()
    assert slow not in harness.session.store.state.artifacts


def test_stale_head_check_uses_state_reloaded_inside_the_store_lock(tmp_path: Path) -> None:
    """A second process has a stale in-memory checkpoint even though disk already advanced."""

    harness = _Harness(tmp_path, "cross-process-stale")
    base = harness.run("make_matrix")
    second_session = AnalysisSession.resume(
        harness.session.directory.parent, harness.session.session_id
    )
    second_executor = CapabilityExecutor(second_session)
    response = asyncio.run(
        second_executor.execute(
            harness.package, harness.tools["make_matrix"], {"path": harness.matrix_path(base)}
        )
    )
    slow = response["structuredContent"]["scagent_execution_id"]

    fast = harness.run("make_matrix", path=harness.matrix_path(base))
    assert active_head(harness.lineage) == fast
    with pytest.raises(CapabilityExecutionError, match="active head is now"):
        second_executor.commit(slow)

    assert (second_executor.pending_root / slow).is_dir()


def test_two_initial_roots_cannot_both_commit_without_adoption(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "cross-process-empty")
    second_session = AnalysisSession.resume(
        harness.session.directory.parent, harness.session.session_id
    )
    second_executor = CapabilityExecutor(second_session)
    other = tmp_path / "other-root.h5ad"
    other.write_bytes(b"other")
    response = asyncio.run(
        second_executor.execute(
            harness.package, harness.tools["make_matrix"], {"path": str(other)}
        )
    )
    second_root = response["structuredContent"]["scagent_execution_id"]

    harness.run("make_matrix")
    with pytest.raises(CapabilityExecutionError, match="active head is now"):
        second_executor.commit(second_root)


def test_duplicate_commit_from_a_stale_store_is_idempotent(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "duplicate-commit")
    execution = harness.run("make_matrix", commit=False)
    second_session = AnalysisSession.resume(
        harness.session.directory.parent, harness.session.session_id
    )
    second_executor = CapabilityExecutor(second_session)

    assert harness.executor.commit(execution) is True
    assert second_executor.commit(execution) is False

    committed = [
        event
        for event in second_executor.session.store.events()
        if event.kind == "capability.result_committed"
        and event.payload.get("execution_id") == execution
    ]
    assert len(committed) == 1


def test_read_only_commit_is_not_blocked_by_a_moved_head(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "read-only-stale")
    base = harness.run("make_matrix")
    evidence = harness.run("write_evidence", commit=False, path=harness.matrix_path(base))
    harness.run("make_matrix", path=harness.matrix_path(base))

    assert harness.executor.commit(evidence)


def test_unsequenced_staging_directory_is_quarantined_not_adopted(tmp_path: Path) -> None:
    """D9. A crash between writing result.json and recording its event leaves no order."""

    harness = _Harness(tmp_path, "quarantine")
    sequenced = harness.run("make_matrix", commit=False)

    orphan = harness.executor.pending_root / "00000000-0000-4000-8000-000000000000"
    orphan.mkdir()
    (orphan / "result.json").write_text(
        (harness.executor.pending_root / sequenced / "result.json")
        .read_text(encoding="utf-8")
        .replace(sequenced, orphan.name),
        encoding="utf-8",
    )
    (orphan / "matrix.h5ad").write_bytes(b"H5AD")

    assert orphan.name < sequenced  # name order would have taken it first
    assert harness.executor.recover_pending() == [sequenced]

    # Moved aside intact rather than deleted, and recorded.
    quarantined = harness.executor.quarantine_root / orphan.name
    assert (quarantined / "result.json").is_file()
    assert not orphan.exists()
    assert orphan.name not in harness.session.store.state.artifacts
    kinds = [event.kind for event in harness.session.store.events()]
    assert "capability.result_quarantined" in kinds


def test_two_annotators_chained_forward_compose_into_one_head(tmp_path: Path) -> None:
    """The reported incident, done correctly: the second reads the first's output.

    Acceptance case 2's shape. Once D3 resolves omitted paths from the head this becomes the only
    reachable behaviour; today it records that chaining forward produces a single line of descent
    carrying both contributions.
    """

    harness = _Harness(tmp_path, "chained-annotators")
    clustered = harness.run("make_matrix")
    first = harness.run(
        "make_matrix",
        path=harness.matrix_path(clustered),
        facts={"annotation": {"evidence": {"scimilarity": "complete"}}},
    )
    second = harness.run(
        "make_matrix",
        path=harness.matrix_path(first),
        facts={"annotation": {"evidence": {"celltypist": "complete"}}},
    )

    assert active_head(harness.lineage) == second
    assert ancestry(harness.lineage, second) == [second, first, clustered]
    # Both contributions survive on the head's resolved view and in global facts.
    resolved = resolve_node_facts(harness.lineage, second, merge=apply_merge_patch)
    assert resolved["annotation"]["evidence"] == {
        "scimilarity": "complete",
        "celltypist": "complete",
    }
    assert harness.facts["annotation"]["evidence"] == {
        "scimilarity": "complete",
        "celltypist": "complete",
    }


def test_the_original_defect_is_now_refused(tmp_path: Path) -> None:
    """The reported incident, prevented rather than merely recorded.

    Both annotators are handed ``clustered``. Once the first commits, that artifact is an ancestor,
    so continuing from it would drop the first's column from the delivered file. The executor
    refuses and names the artifact to use instead.
    """

    harness = _Harness(tmp_path, "divergent-annotators")
    clustered = harness.run("make_matrix")
    first = harness.run(
        "make_matrix",
        path=harness.matrix_path(clustered),
        facts={"annotation": {"evidence": {"scimilarity": "complete"}}},
    )

    response = asyncio.run(
        harness.executor.execute(
            harness.package,
            harness.tools["make_matrix"],
            {
                "path": harness.matrix_path(clustered),
                "facts": {"annotation": {"evidence": {"celltypist": "complete"}}},
            },
        )
    )

    assert response["is_error"] is True
    assert "tracked ancestor" in response["error_summary"]
    # Nothing was committed, and the first annotator's evidence is intact.
    assert active_head(harness.lineage) == first
    assert harness.facts["annotation"]["evidence"] == {"scimilarity": "complete"}


def test_omitting_the_path_chains_annotators_automatically(tmp_path: Path) -> None:
    """Acceptance case 2: the correct outcome with no path named at all.

    This is what makes the defect unrepresentable rather than merely rejected -- with nothing to
    name, there is nothing to get wrong.
    """

    harness = _Harness(tmp_path, "injected-chain")
    clustered = harness.run("make_matrix")
    first = harness.run(
        "make_matrix", facts={"annotation": {"evidence": {"scimilarity": "complete"}}}
    )
    second = harness.run(
        "make_matrix", facts={"annotation": {"evidence": {"celltypist": "complete"}}}
    )

    assert ancestry(harness.lineage, second) == [second, first, clustered]
    assert harness.facts["annotation"]["evidence"] == {
        "scimilarity": "complete",
        "celltypist": "complete",
    }


def test_the_envelope_reports_an_injected_input(tmp_path: Path) -> None:
    """Resolution must be visible, or a FileNotFound is traded for an invisible wrong input."""

    harness = _Harness(tmp_path, "envelope")
    first = harness.run("make_matrix")

    response = asyncio.run(
        harness.executor.execute(harness.package, harness.tools["make_matrix"], {})
    )
    resolved = response["structuredContent"]["resolved_input"]

    assert resolved["source"] == "injected"
    assert resolved["relation"] == "head"
    assert resolved["path"] == harness.matrix_path(first)


def test_supplying_the_current_head_explicitly_is_accepted(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "explicit-head")
    first = harness.run("make_matrix")
    second = harness.run("make_matrix", path=harness.matrix_path(first))

    assert harness.lineage["nodes"][second]["parent_execution_id"] == first


def test_read_only_tools_may_still_read_a_non_head_artifact(tmp_path: Path) -> None:
    """The carve-out: inspecting an earlier artifact creates no node and detaches nothing."""

    harness = _Harness(tmp_path, "read-only-carve-out")
    first = harness.run("make_matrix")
    harness.run("make_matrix")

    execution = harness.run("write_evidence", path=harness.matrix_path(first))
    assert execution in harness.session.store.state.artifacts


def test_recovery_replays_sequenced_results_in_staging_order(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "recovery-order")
    first = harness.run("make_matrix", commit=False)
    second = harness.run("make_matrix", commit=False, path=harness.matrix_path(first))

    assert harness.executor.recover_pending() == [first, second]
    assert active_head(harness.lineage) == second
    assert harness.lineage["nodes"][second]["parent_execution_id"] == first
