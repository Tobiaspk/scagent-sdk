from __future__ import annotations

from pathlib import Path

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _packages() -> dict[str, object]:
    root = Path(__file__).parents[2] / ".claude" / "skills"
    return {package.manifest.skill_id: package for package in CapabilityRegistry(root).discover()}


def test_preprocessing_operations_are_separate_and_ungated() -> None:
    packages = _packages()
    expected = {
        "single-cell-counts": {"materialize_count_matrix"},
        "single-cell-qc": {
            "calculate_single_cell_qc",
            "review_single_cell_qc",
            "filter_single_cells",
            "filter_single_cell_genes",
        },
        "expression-preprocessing": {
            "normalize_single_cell_expression",
            "select_highly_variable_genes",
        },
        "dimensionality-reduction": {
            "compute_single_cell_pca",
            "build_single_cell_neighbors",
            "compute_single_cell_umap",
        },
        "single-cell-clustering": {
            "cluster_single_cells",
            "rank_single_cell_groups",
        },
    }
    for skill_id, names in expected.items():
        tools = packages[skill_id].manifest.tools  # type: ignore[union-attr]
        assert {tool.name for tool in tools} == names
        assert all(tool.floors == () for tool in tools)
    assert "prepare-single-cell" not in packages


def test_core_single_cell_transformations_declare_rapids_gpu_backend() -> None:
    root = Path(__file__).parents[2] / ".claude" / "skills"
    sources = [
        root / "expression-preprocessing/scripts/preprocess.py",
        root / "dimensionality-reduction/scripts/reduce.py",
        root / "single-cell-clustering/scripts/clustering.py",
    ]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert "rapids_singlecell" in text
        assert '"compute_backend": "rapids_singlecell"' in text
    assert "rsc.pp.normalize_total" in sources[0].read_text(encoding="utf-8")
    reduction = sources[1].read_text(encoding="utf-8")
    assert "rsc.pp.pca" in reduction
    assert "rsc.pp.neighbors" in reduction
    assert "rsc.tl.umap" in reduction
    assert "rsc.tl.leiden" in sources[2].read_text(encoding="utf-8")


def test_reference_inference_does_not_require_clustering_or_qc() -> None:
    packages = _packages()
    scimilarity = {
        tool.name: tool for tool in packages["scimilarity-annotation"].manifest.tools  # type: ignore[union-attr]
    }
    celltypist = {
        tool.name: tool for tool in packages["celltypist-annotation"].manifest.tools  # type: ignore[union-attr]
    }
    scimilarity_run = scimilarity["run_scimilarity_annotation"]
    celltypist_run = celltypist["run_celltypist_annotation"]
    assert scimilarity_run.floors == ()
    assert celltypist_run.floors == ()
    assert "cluster_key" not in scimilarity_run.input_schema["properties"]
    assert "cluster_key" not in celltypist_run.input_schema["properties"]
    assert set(celltypist_run.input_schema["required"]) == {"model"}
    assert "default" not in celltypist_run.input_schema["properties"]["model"]
    # Both consume the analysis matrix and produce a new one, declared so the executor can resolve
    # an omitted path to the active artifact and chain them without the model naming files.
    for tool, artifact in (
        (scimilarity_run, "scimilarity-annotated-anndata"),
        (celltypist_run, "celltypist-annotated-anndata"),
    ):
        assert tool.primary_matrix_input == "path"
        assert tool.primary_matrix_output == artifact
        # ``path`` was the only required argument, so the key is gone entirely.
        assert "path" not in tool.input_schema.get("required", [])
    assert "summarize_scimilarity_by_cluster" in scimilarity
    assert "summarize_celltypist_by_cluster" in celltypist


def test_scvi_training_is_representation_only_and_requires_integrate_choice() -> None:
    package = _packages()["scvi-integration"]
    tool = package.manifest.tools[0]  # type: ignore[union-attr]
    assert tool.name == "train_scvi_latent"
    assert tool.floors == ("integration_authorized",)
    assert "resolution" not in tool.input_schema["properties"]


def test_marker_computation_is_not_gated_by_cluster_qc() -> None:
    package = _packages()["marker-annotation"]
    tool = package.manifest.tools[0]  # type: ignore[union-attr]
    assert tool.name == "evaluate_marker_evidence"
    assert tool.floors == ()


def test_evidence_generation_is_portable_but_decisions_remain_bound() -> None:
    packages = _packages()
    expected = {
        "doublet-evidence": {
            "evaluate_doublet_evidence": (),
            "review_doublet_evidence": ("current_doublet_evidence",),
        },
        "batch-investigation": {
            "investigate_batch": (),
            "decide_batch_handling": ("current_batch_evidence",),
        },
        "cellbender-background-removal": {
            "validate_cellbender_input": (),
            "remove_ambient_background": (
                "dataset_identity",
                "cellbender_input_suitable",
            ),
        },
    }
    for skill_id, tool_floors in expected.items():
        tools = {
            tool.name: tool for tool in packages[skill_id].manifest.tools  # type: ignore[union-attr]
        }
        assert {name: tools[name].floors for name in tool_floors} == tool_floors
