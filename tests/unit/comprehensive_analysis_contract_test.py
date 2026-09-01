from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _packages() -> dict[str, Any]:
    root = Path(__file__).parents[2] / ".claude" / "skills"
    return {package.manifest.skill_id: package for package in CapabilityRegistry(root).discover()}


def _handler(skill: str, tool_name: str) -> Any:
    package = _packages()[skill]
    tool = next(tool for tool in package.manifest.tools if tool.name == tool_name)
    return package.load_handler(tool)


def test_qc_defaults_to_auto_and_exposes_review_tool() -> None:
    package = _packages()["single-cell-qc"]
    tools = {tool.name: tool for tool in package.manifest.tools}
    assert package.manifest.version == "0.3.0"
    assert tools["calculate_single_cell_qc"].input_schema["properties"]["counts_layer"][
        "default"
    ] == "auto"
    assert "review_single_cell_qc" in tools


def test_qc_auto_layer_prefers_counts_then_x() -> None:
    resolve = _handler("single-cell-qc", "calculate_single_cell_qc").__globals__[
        "_resolve_layer"
    ]
    assert resolve(SimpleNamespace(layers={"counts": object()}), "auto") == "counts"
    assert resolve(SimpleNamespace(layers={}), "auto") is None
    assert resolve(SimpleNamespace(layers={}), None) is None
    assert resolve(SimpleNamespace(layers={"raw": object()}), "raw") == "raw"


def test_qc_review_requires_every_figure_and_resolves_keep_all() -> None:
    review = _handler("single-cell-qc", "review_single_cell_qc")
    context = SimpleNamespace(
        state_facts={
            "cell_qc": {
                "status": "assessed",
                "assessment_id": "qc-a",
                "cell_set_id": "cells-a",
                "count_representation_id": "counts-a",
                "required_visual_artifacts": ["figures/a.png", "figures/b.png"],
            }
        }
    )
    arguments = {
        "assessment_id": "qc-a",
        "decision": "keep_all",
        "rationale": "The high-MT population is coherent and retained for cluster review.",
        "visual_findings": ["The high-MT tail is continuous rather than bimodal."],
        "reviewed_artifacts": ["figures/a.png"],
    }
    with pytest.raises(ValueError, match="visual review is incomplete"):
        review(arguments, context)
    arguments["reviewed_artifacts"].append("figures/b.png")
    result = review(arguments, context)
    assert result["facts_patch"]["cell_qc"]["review"]["status"] == "resolved"


def test_qc_review_automatically_records_figures_already_shown_by_the_runtime() -> None:
    review = _handler("single-cell-qc", "review_single_cell_qc")
    context = SimpleNamespace(
        state_facts={
            "cell_qc": {
                "status": "assessed",
                "assessment_id": "qc-a",
                "required_visual_artifacts": ["figures/a.png", "figures/b.png"],
                "shown_visual_artifacts": ["figures/a.png", "figures/b.png"],
            }
        }
    )

    result = review(
        {
            "assessment_id": "qc-a",
            "decision": "keep_all",
            "rationale": "Both attached views support retaining the continuous tails.",
            "visual_findings": ["The attached views contain no isolated quality island."],
        },
        context,
    )

    assert result["details"]["reviewed_artifacts"] == ["figures/a.png", "figures/b.png"]


def test_umap_default_uses_scanpy_convention_without_stripping_prefix() -> None:
    helper = _handler(
        "dimensionality-reduction", "compute_single_cell_umap"
    ).__globals__["_scanpy_umap_key"]
    assert helper("X_umap") is None
    assert helper("umap_secondary") == "umap_secondary"


def test_cluster_review_requires_all_figures_and_exact_flagged_clusters() -> None:
    review = _handler("cluster-qc", "review_cluster_qc")
    context = SimpleNamespace(
        state_facts={
            "cluster_qc": {
                "status": "attested",
                "evidence_id": "cluster-qc-a",
                "clustering_id": "clusters-a",
                "review_clusters": ["2"],
                "required_visual_artifacts": ["metric.png", "umap.png", "cluster_2.png"],
            }
        }
    )
    arguments = {
        "evidence_id": "cluster-qc-a",
        "reviewed_artifacts": ["metric.png", "umap.png", "cluster_2.png"],
        "visual_findings": ["Cluster 2 has a coherent covariance block."],
        "cluster_reviews": {
            "2": {
                "disposition": "keep",
                "rationale": "Identity DEGs and structured covariance outweigh the metric flag.",
            }
        },
    }
    result = review(arguments, context)
    fact = result["facts_patch"]["cluster_qc"]["review"]
    assert fact["status"] == "resolved"
    assert fact["unresolved_clusters"] == []

    arguments["cluster_reviews"]["2"]["disposition"] = "recluster"
    result = review(arguments, context)
    assert result["facts_patch"]["cluster_qc"]["review"]["status"] == "action_required"


def test_cluster_review_accepts_extra_well_supported_cluster_notes_and_shown_overview() -> None:
    review = _handler("cluster-qc", "review_cluster_qc")
    context = SimpleNamespace(
        state_facts={
            "cluster_qc": {
                "status": "attested",
                "evidence_id": "cluster-qc-a",
                "review_clusters": ["2"],
                "required_visual_artifacts": ["metric.png", "umap.png"],
                "shown_visual_artifacts": ["metric.png", "umap.png"],
            }
        }
    )

    result = review(
        {
            "evidence_id": "cluster-qc-a",
            "visual_findings": ["Cluster 2 is compact in the attached overview."],
            "cluster_reviews": {
                "2": {"disposition": "keep", "rationale": "Coherent identity evidence."},
                "7": {"disposition": "keep", "rationale": "Useful negative-control note."},
            },
        },
        context,
    )

    assert result["details"]["reviewed_artifacts"] == ["metric.png", "umap.png"]
    assert set(result["details"]["cluster_reviews"]) == {"2", "7"}


def test_annotation_review_accepts_one_reference_and_preserves_uncertainty() -> None:
    review = _handler("marker-annotation", "review_annotation_evidence")
    context = SimpleNamespace(
        state_facts={
            "analysis": {"clustering": {"id": "clusters-a"}},
            "annotation": {
                "evidence": {
                    "markers": {
                        "status": "complete",
                        "clustering_id": "clusters-a",
                        "evidence_id": "markers-a",
                    },
                    "scimilarity": {
                        "status": "complete",
                        "clustering_id": "clusters-a",
                        "evidence_id": "scim-a",
                    },
                }
            },
        }
    )
    arguments = {
        "methods_reviewed": ["markers", "scimilarity"],
        "reviewed_artifacts": ["cluster-deg.csv", "scimilarity-clusters.csv"],
        "agreement_findings": ["DEGs and SCimilarity agree at broad lineage level."],
        "unresolved_clusters": ["2"],
        "rationale": "Labels remain DEG-led.",
    }
    result = review(arguments, context)
    fact = result["facts_patch"]["annotation"]["review"]
    assert fact["status"] == "reviewed"
    assert fact["uncertain_clusters"] == ["2"]


def test_annotation_review_infers_current_method_keys() -> None:
    review = _handler("marker-annotation", "review_annotation_evidence")
    context = SimpleNamespace(
        state_facts={
            "analysis": {"clustering": {"id": "clusters-a"}},
            "annotation": {
                "evidence": {
                    "markers": {
                        "status": "complete",
                        "clustering_id": "clusters-a",
                        "evidence_id": "markers-a",
                    }
                }
            },
        }
    )
    result = review({}, context)
    assert result["details"]["methods_reviewed"] == ["markers"]
