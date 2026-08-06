"""Explicit branching and version switching.

Stage 4 of ``docs/artifact-lineage-and-head-spec.md``. Stage 3 made continuing from a superseded
artifact impossible, which also removed the only way to express a deliberate comparison. These pin
the vocabulary that restores it, and the atomicity a switch has to have.
"""

from __future__ import annotations

import asyncio
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

_SKILLS = Path(__file__).parents[2] / ".claude" / "skills"


def _skill(root: Path) -> Path:
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
        branch_from: {type: string}
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
""",
        encoding="utf-8",
    )
    (skill / "scripts" / "run.py").write_text(
        '''
def make_matrix(arguments, context):
    # Lineage controls are executor-owned and must never reach a skill.
    assert "branch_from" not in arguments, arguments
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
        "details": {"received_path": arguments.get("path")},
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
'''.lstrip(),
        encoding="utf-8",
    )
    return skill.parent


class _Harness:
    """Drives the test skill plus the real ``analysis-versions`` package."""

    def __init__(self, tmp_path: Path, title: str):
        packages = [CapabilityRegistry(_skill(tmp_path)).discover()[0]]
        packages += [
            item
            for item in CapabilityRegistry(_SKILLS).discover()
            if item.manifest.skill_id == "analysis-versions"
        ]
        self.packages = {t.name: (p, t) for p in packages for t in p.manifest.tools}
        self.session = AnalysisSession.create(tmp_path / "sessions", title=title)
        self.executor = CapabilityExecutor(self.session)
        self.seed_path = tmp_path / f"{title}-seed.h5ad"
        self.seed_path.write_bytes(b"H5AD")

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        package, spec = self.packages[tool]
        if tool == "make_matrix" and "path" not in arguments and active_head(self.lineage) is None:
            arguments["path"] = str(self.seed_path)
        return asyncio.run(self.executor.execute(package, spec, dict(arguments)))

    def run(self, tool: str, **arguments: Any) -> str:
        response = self.call(tool, **arguments)
        assert response.get("is_error") is not True, response
        execution_id = response["structuredContent"]["scagent_execution_id"]
        assert self.executor.commit(execution_id)
        return execution_id

    def matrix_path(self, execution_id: str) -> str:
        # The committed directory is named ``<action>--<id>``; read its recorded path rather than
        # reconstructing it from the ID.
        record = self.session.store.state.artifacts[execution_id]
        return str(self.session.directory / record["path"] / "matrix.h5ad")

    @property
    def lineage(self) -> dict[str, Any]:
        return self.session.store.state.lineage

    @property
    def facts(self) -> dict[str, Any]:
        return self.session.store.state.facts


# --- branching -----------------------------------------------------------------------------


def test_branch_from_records_an_alternative_without_moving_the_head(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "branch")
    base = harness.run("make_matrix", clustering="clust:base")
    branch = harness.run(
        "make_matrix", branch_from=harness.matrix_path(base), clustering="clust:alt"
    )

    assert active_head(harness.lineage) == base
    assert harness.lineage["nodes"][branch]["parent_execution_id"] == base
    assert harness.lineage["nodes"][branch]["branch_intent"] is True
    assert classify_input(harness.lineage, branch) == "sibling"
    # The active line still describes the base clustering.
    assert harness.facts["analysis"]["clustering"]["id"] == "clust:base"


def test_a_resolution_sweep_is_expressible_again(tmp_path: Path) -> None:
    """Three clusterings of one embedding, held at once, none of them silently winning."""

    harness = _Harness(tmp_path, "sweep")
    embedding = harness.run("make_matrix")
    branches = [
        harness.run(
            "make_matrix",
            branch_from=harness.matrix_path(embedding),
            clustering=f"clust:{resolution}",
        )
        for resolution in ("2.0", "1.5", "1.0")
    ]

    assert active_head(harness.lineage) == embedding
    for branch in branches:
        assert ancestry(harness.lineage, branch) == [branch, embedding]
    assert len(set(branches)) == 3


def test_branch_evidence_stays_on_its_branch(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "branch-evidence")
    base = harness.run("make_matrix")
    branch = harness.run("make_matrix", branch_from=harness.matrix_path(base))

    harness.run(
        "write_evidence",
        path=harness.matrix_path(branch),
        facts={"cluster_qc": {"status": "on-branch"}},
    )

    assert "cluster_qc" not in harness.facts
    assert resolve_node_facts(harness.lineage, branch, merge=apply_merge_patch)["cluster_qc"] == {
        "status": "on-branch"
    }


def test_branch_from_is_never_passed_through_to_the_skill(tmp_path: Path) -> None:
    """The skill asserts its absence; this records that the contract is the executor's."""

    harness = _Harness(tmp_path, "control-argument")
    _, tool = harness.packages["make_matrix"]
    assert "adopt_untracked" in tool.input_schema["properties"]
    base = harness.run("make_matrix")
    assert harness.run("make_matrix", branch_from=harness.matrix_path(base))


@pytest.mark.parametrize("reference_kind", ["version", "short-version", "reported-artifact"])
def test_branch_reference_resolves_to_the_canonical_absolute_artifact(
    tmp_path: Path, reference_kind: str
) -> None:
    harness = _Harness(tmp_path, f"branch-reference-{reference_kind}")
    base = harness.run("make_matrix")
    reported = harness.lineage["nodes"][base]["head_path"]
    reference = {
        "version": base,
        "short-version": base[:8],
        "reported-artifact": reported,
    }[reference_kind]

    response = harness.call("make_matrix", branch_from=reference)

    assert response.get("is_error") is not True, response
    assert response["structuredContent"]["details"]["received_path"] == harness.matrix_path(base)
    branch = response["structuredContent"]["scagent_execution_id"]
    assert harness.executor.commit(branch)
    assert harness.lineage["nodes"][branch]["parent_execution_id"] == base


def test_branch_signature_is_derived_from_its_parent_not_the_active_line(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "branch-signature")
    base = harness.run(
        "make_matrix", facts={"analysis": {"representation": {"id": "rep:base"}}}
    )
    active = harness.run(
        "make_matrix", facts={"analysis": {"representation": {"id": "rep:active"}}}
    )
    branch = harness.run("make_matrix", branch_from=base)

    nodes = harness.lineage["nodes"]
    assert nodes[branch]["identity_signature"] == nodes[base]["identity_signature"]
    assert nodes[branch]["identity_signature"] != nodes[active]["identity_signature"]


def test_branch_from_rejects_an_unrecorded_artifact(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "branch-unknown")
    harness.run("make_matrix")
    external = tmp_path / "outside.h5ad"
    external.write_bytes(b"H5AD")

    response = harness.call("make_matrix", branch_from=str(external))
    assert response["is_error"] is True
    assert "must name a version or artifact this analysis produced" in response["error_summary"]


def test_branch_from_and_an_explicit_path_together_are_refused(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "branch-both")
    base = harness.run("make_matrix")

    response = harness.call(
        "make_matrix", path=harness.matrix_path(base), branch_from=harness.matrix_path(base)
    )
    assert response["is_error"] is True
    assert "not both" in response["error_summary"]


def test_branch_from_is_meaningless_for_a_read_only_tool(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "branch-read-only")
    base = harness.run("make_matrix")

    package, spec = harness.packages["write_evidence"]
    with pytest.raises(CapabilityExecutionError, match="nothing to branch"):
        harness.executor._resolve_matrix_input(
            spec, {"path": harness.matrix_path(base), "branch_from": harness.matrix_path(base)}
        )


def test_the_superseded_error_points_at_branching(tmp_path: Path) -> None:
    """The refusal has to name the alternative, or the model has no way to express a comparison."""

    harness = _Harness(tmp_path, "refusal-message")
    base = harness.run("make_matrix")
    harness.run("make_matrix")

    response = harness.call("make_matrix", path=harness.matrix_path(base))
    assert response["is_error"] is True
    assert "branch_from" in response["content"][0]["text"]


# --- switching -----------------------------------------------------------------------------


def test_listing_reports_the_active_version_and_alternatives(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "listing")
    base = harness.run("make_matrix")
    branch = harness.run("make_matrix", branch_from=harness.matrix_path(base))

    response = harness.call("list_analysis_versions")
    details = response["structuredContent"]["details"]

    assert details["active_version_id"] == base
    rows = {row["version_id"]: row for row in details["versions"]}
    assert rows[base]["active"] is True and rows[base]["on_active_line"] is True
    assert rows[branch]["active"] is False and rows[branch]["on_active_line"] is False
    assert rows[branch]["created_as_alternative"] is True


def test_switching_moves_the_head_and_the_facts_together(tmp_path: Path) -> None:
    """Acceptance case 10. A partial switch would describe two different matrices at once."""

    harness = _Harness(tmp_path, "switch")
    base = harness.run("make_matrix", clustering="clust:base")
    branch = harness.run(
        "make_matrix", branch_from=harness.matrix_path(base), clustering="clust:alt"
    )
    harness.run(
        "write_evidence",
        path=harness.matrix_path(branch),
        facts={"cluster_qc": {"status": "alt-evidence"}},
    )

    assert harness.facts["analysis"]["clustering"]["id"] == "clust:base"
    assert "cluster_qc" not in harness.facts

    harness.run(
        "switch_analysis_version",
        version_id=branch,
        rationale="the alternative resolution separates the populations of interest",
    )

    assert active_head(harness.lineage) == branch
    assert harness.facts["analysis"]["clustering"]["id"] == "clust:alt"
    branch_dir = capability_artifact_directory_name("make_matrix", branch)
    assert harness.facts["analysis"]["dataset_revision"]["prepared_path"] == (
        f"artifacts/capabilities/{branch_dir}/matrix.h5ad"
    )
    assert harness.facts["cluster_qc"] == {"status": "alt-evidence"}
    # Global facts equal the newly active version's resolved view.
    resolved = resolve_node_facts(harness.lineage, branch, merge=apply_merge_patch)
    node_scoped = {k: v for k, v in harness.facts.items() if k in node_scoped_roots()}
    assert node_scoped == resolved


def test_switching_back_restores_the_original_view(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "switch-back")
    base = harness.run("make_matrix", clustering="clust:base", facts={"cell_qc": {"step": 1}})
    branch = harness.run(
        "make_matrix", branch_from=harness.matrix_path(base), clustering="clust:alt"
    )

    harness.run("switch_analysis_version", version_id=branch, rationale="try the alternative")
    harness.run(
        "switch_analysis_version", version_id=base, rationale="the base is better supported"
    )

    assert active_head(harness.lineage) == base
    assert harness.facts["analysis"]["clustering"]["id"] == "clust:base"
    # Inherited facts survive the round trip.
    assert harness.facts["cell_qc"] == {"step": 1}


def test_switching_accepts_a_short_version_id(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "short-id")
    base = harness.run("make_matrix")
    branch = harness.run("make_matrix", branch_from=harness.matrix_path(base))

    harness.run("switch_analysis_version", version_id=branch[:8], rationale="compare the fork")
    assert active_head(harness.lineage) == branch


def test_switching_to_an_unknown_version_fails(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "switch-unknown")
    harness.run("make_matrix")

    response = harness.call(
        "switch_analysis_version", version_id="ffffffff", rationale="does not exist"
    )
    assert response["is_error"] is True
    assert "no recorded version matches" in response["error_summary"]


def test_switching_to_the_active_version_is_refused(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "switch-noop")
    base = harness.run("make_matrix")

    response = harness.call(
        "switch_analysis_version", version_id=base, rationale="already here"
    )
    assert response["is_error"] is True
    assert "already active" in response["error_summary"]


def test_switching_requires_a_rationale(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "switch-rationale")
    base = harness.run("make_matrix")
    branch = harness.run("make_matrix", branch_from=harness.matrix_path(base))

    response = harness.call("switch_analysis_version", version_id=branch, rationale="   ")
    assert response["is_error"] is True
    assert "rationale must not be empty" in response["error_summary"]


def test_a_switch_does_not_create_a_lineage_node(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "switch-no-node")
    base = harness.run("make_matrix")
    branch = harness.run("make_matrix", branch_from=harness.matrix_path(base))

    switch = harness.run(
        "switch_analysis_version", version_id=branch, rationale="compare the fork"
    )
    assert set(harness.lineage["nodes"]) == {base, branch}
    assert switch not in harness.lineage["nodes"]


def test_work_continues_from_the_switched_version(tmp_path: Path) -> None:
    """The point of switching: the next omitted-path step derives from the chosen version."""

    harness = _Harness(tmp_path, "continue")
    base = harness.run("make_matrix")
    branch = harness.run("make_matrix", branch_from=harness.matrix_path(base))
    harness.run("switch_analysis_version", version_id=branch, rationale="the fork is better")

    following = harness.run("make_matrix")
    assert harness.lineage["nodes"][following]["parent_execution_id"] == branch
    assert ancestry(harness.lineage, following) == [following, branch, base]
