from __future__ import annotations

from pathlib import Path

import pytest

from scagent_sdk.capabilities.manifest import CapabilityManifest, CapabilityTool
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.errors import CapabilityManifestError


def test_registry_discovers_and_loads_executable_project_skill() -> None:
    root = Path(__file__).parents[2] / ".claude" / "skills"

    packages = CapabilityRegistry(root).discover()

    package_by_id = {package.manifest.skill_id: package for package in packages}
    assert set(package_by_id) == {
        "analysis-notebook",
        "analysis-versions",
        "analysis-workspace",
        "batch-investigation",
        "cellbender-background-removal",
        "celltypist-annotation",
        "cluster-qc",
        "doublet-evidence",
        "dimensionality-reduction",
        "expression-preprocessing",
        "export-dataset",
        "finalize-analysis",
        "inspect-dataset",
        "inspect-media",
        "marker-annotation",
        "research-literature",
        "research-web",
        "scimilarity-annotation",
        "single-cell-clustering",
        "single-cell-counts",
        "single-cell-qc",
        "visualize-single-cell",
        "scvi-integration",
    }
    package = package_by_id["inspect-dataset"]
    tool = package.manifest.tools[0]
    assert tool.name == "inspect_dataset"
    assert callable(package.load_handler(tool))
    skills = CapabilityRegistry(root).skills()
    assert len(skills) == 24
    assert {skill.name for skill in skills if not skill.executable} == {"orchestrate-single-cell"}


def test_every_transforming_tool_exposes_the_executor_owned_adoption_control() -> None:
    """The model sees this runtime-injected property despite strict raw YAML schemas."""

    root = Path(__file__).parents[2] / ".claude" / "skills"
    transforming = [
        (package.manifest.skill_id, tool)
        for package in CapabilityRegistry(root).discover()
        for tool in package.manifest.tools
        if tool.primary_matrix_output is not None and tool.advances_lineage
    ]

    assert len(transforming) == 20
    missing = [
        f"{skill_id}.{tool.name}"
        for skill_id, tool in transforming
        if "adopt_untracked" not in tool.input_schema.get("properties", {})
    ]
    assert missing == []
    for _, tool in transforming:
        schema = tool.input_schema["properties"]["adopt_untracked"]
        assert schema["type"] == "boolean"
        assert schema["default"] is False
        assert "adopt_untracked" not in tool.input_schema.get("required", [])

    exporter = next(
        tool
        for package in CapabilityRegistry(root).discover()
        if package.manifest.skill_id == "export-dataset"
        for tool in package.manifest.tools
    )
    assert exporter.primary_matrix_output == "portable-anndata"
    assert exporter.advances_lineage is False
    assert "adopt_untracked" not in exporter.input_schema.get("properties", {})


def test_manifest_rejects_non_advancing_tool_without_a_matrix_output() -> None:
    with pytest.raises(CapabilityManifestError, match="requires primary_matrix_output"):
        CapabilityTool.from_dict(
            {
                "name": "invalid_export",
                "description": "invalid",
                "entrypoint": "scripts/run.py:run",
                "input_schema": {"type": "object"},
                "advances_lineage": False,
            }
        )


def test_manifest_rejects_escaping_entrypoint(tmp_path: Path) -> None:
    manifest_path = tmp_path / "capability.yaml"
    manifest_path.write_text(
        """\
schema_version: 1
skill:
  id: unsafe-skill
  version: "1"
  description: unsafe
tools:
  - name: escape
    description: escape
    entrypoint: ../outside.py:run
    input_schema: {type: object}
""",
        encoding="utf-8",
    )
    CapabilityManifest.from_path(manifest_path)
    skill = tmp_path / "unsafe-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: unsafe-skill\ndescription: unsafe\n---\n", encoding="utf-8"
    )
    (skill / "capability.yaml").write_text(manifest_path.read_text(), encoding="utf-8")

    with pytest.raises(CapabilityManifestError, match="escapes skill package"):
        CapabilityRegistry(tmp_path).discover()
