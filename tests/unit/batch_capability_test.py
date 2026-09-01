from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _package() -> Any:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "batch-investigation"
    )


def _handler(name: str) -> Any:
    package = _package()
    tool = next(tool for tool in package.manifest.tools if tool.name == name)
    return package.load_handler(tool)


def _g(name: str) -> Any:
    return _handler("investigate_batch").__globals__[name]


# --- manifest ----------------------------------------------------------------


def test_manifest_splits_evidence_and_decision_tools() -> None:
    package = _package()
    assert package.manifest.version == "0.9.0"
    names = {tool.name for tool in package.manifest.tools}
    assert names == {"investigate_batch", "decide_batch_handling"}
    evidence = next(t for t in package.manifest.tools if t.name == "investigate_batch")
    decision = next(t for t in package.manifest.tools if t.name == "decide_batch_handling")
    # Evidence tool records no decision.
    assert "decision" not in evidence.input_schema["properties"]
    assert evidence.entrypoint.endswith(":run_evidence")
    # ``path`` is the declared matrix input, so it is deliberately optional: the executor resolves
    # an omitted value to the active lineage artifact.
    assert set(evidence.input_schema["required"]) == {"batch_key"}
    assert evidence.primary_matrix_input == "path"
    assert evidence.primary_matrix_output is None  # reads a matrix, writes none
    props = evidence.input_schema["properties"]
    # No design/condition inputs: the caller cannot steer the verdict, only supply the matrix,
    # the batch column, and the clustering.
    assert set(props) == {
        "path",
        "batch_key",
        "cluster_key",
    }
    # Decision tool consumes an evidence id and validates it intrinsically in the handler.
    assert decision.floors == ()
    assert decision.entrypoint.endswith(":run_decision")
    assert set(decision.input_schema["properties"]["decision"]["enum"]) == {
        "keep_uncorrected",
        "integrate",
        "separate",
    }
    assert set(decision.input_schema["properties"]) == {"evidence_id", "decision", "rationale"}


# --- gene-first pure helpers -------------------------------------------------


def test_region_enrichment_catches_modest_purity_high_enrichment() -> None:
    enrich = _g("region_enrichment")
    # 42% of a cluster is one sample that is only 9% of the dataset.
    assert enrich(42, 100, 0.09) == pytest.approx(4.666, abs=1e-2)
    assert enrich(0, 0, 0.5) == 0.0
    assert enrich(10, 100, 0.0) == 0.0


def test_gene_class_separates_broad_stress_from_discriminating() -> None:
    gene_class = _g("gene_class")
    # The live false-match culprits are broad ER/stress, not identity genes.
    assert gene_class("DERL3") == "broad"
    assert gene_class("HSP90B1") == "broad"
    assert gene_class("H13") == "broad"
    assert gene_class("HLA-DRA") == "broad"
    assert gene_class("MT-CO1") == "nuisance"
    assert gene_class("RPL13") == "nuisance"
    assert gene_class("CD3D") == "discriminating"


def test_match_rejects_broad_stress_only_overlap() -> None:
    """The live cluster-19/cluster-9 false match (DERL3/H13/HSP90B1) must not be supported."""
    match = _g("match_regions")
    result = match(
        ["DERL3", "H13", "HSP90B1", "IGKC"],
        ["DERL3", "H13", "HSP90B1", "COL1A1"],
        min_shared=3,
        min_jaccard=0.15,
    )
    assert result["supported"] is False
    assert result["shared"] == 0
    assert "discriminating" in result["reason"]


def test_match_supported_on_real_shared_identity() -> None:
    match = _g("match_regions")
    result = match(
        ["CD3D", "TRAC", "IL7R", "CD2"],
        ["CD3D", "TRAC", "IL7R", "CD7"],
        min_shared=3,
        min_jaccard=0.15,
    )
    assert result["supported"] is True
    assert result["shared"] == 3
    assert result["jaccard"] > 0.15


def test_match_rejected_with_reason_when_jaccard_too_low() -> None:
    match = _g("match_regions")
    disc_a = ["CD3D", "TRAC", "IL7R"] + [f"GENE{i}" for i in range(40)]
    result = match(disc_a, ["CD3D", "TRAC", "IL7R"], min_shared=3, min_jaccard=0.5)
    assert result["supported"] is False
    assert "jaccard" in result["reason"]


def test_recurrence_is_order_invariant_and_needs_two_populations() -> None:
    summarize = _g("summarize_recurrence")
    rows = [
        {"gene": "SOD2", "higher_in_batch": "S1", "population": 1},
        {"gene": "SOD2", "higher_in_batch": "S1", "population": 2},
        {"gene": "ONCE", "higher_in_batch": "S1", "population": 1},
    ]
    forward = summarize(rows)
    reverse = summarize(list(reversed(rows)))
    assert forward == reverse
    assert [r["gene"] for r in forward] == ["SOD2"]
    assert forward[0]["n_populations"] == 2


def test_profile_nomination_ranks_cross_sample_identity_before_deg() -> None:
    nominate = _g("nominate_cross_sample_pairs")
    keys = [("myeloid-a", "S1"), ("myeloid-b", "S2"), ("lymphoid", "S2")]
    means = [
        [8.0, 7.0, 0.0, 0.0],
        [7.5, 6.5, 0.2, 0.0],
        [0.0, 0.0, 7.0, 8.0],
    ]
    pairs = nominate(keys, means, ["LYZ", "CTSS", "CD3D", "TRAC"], min_corr=0.4)
    assert pairs[0]["cluster_a"] == "myeloid-a"
    assert pairs[0]["cluster_b"] == "myeloid-b"
    assert pairs[0]["profile_correlation"] > 0.9


def test_recurrence_ties_rank_by_effect_not_gene_name() -> None:
    summarize = _g("summarize_recurrence")
    rows = [
        {"gene": "ZZZ", "higher_in_batch": "S1", "population": 1, "logfoldchange": 4.0},
        {"gene": "ZZZ", "higher_in_batch": "S1", "population": 2, "logfoldchange": 3.0},
        {"gene": "AAA", "higher_in_batch": "S1", "population": 1, "logfoldchange": 1.0},
        {"gene": "AAA", "higher_in_batch": "S1", "population": 2, "logfoldchange": 1.0},
    ]
    assert [row["gene"] for row in summarize(rows)] == ["ZZZ", "AAA"]


def test_recurrence_same_gene_opposite_batches_does_not_recur() -> None:
    summarize = _g("summarize_recurrence")
    rows = [
        {"gene": "SOD2", "higher_in_batch": "S1", "population": 1},
        {"gene": "SOD2", "higher_in_batch": "S2", "population": 2},
    ]
    assert summarize(rows) == []


def test_classify_gene_evidence_axis() -> None:
    classify = _g("classify_gene_evidence")
    assert classify(0, 0) == "none"
    assert classify(3, 1) == "localized"
    assert classify(3, 2) == "recurring_sample_associated"


def test_recommendation_is_gene_evidence_only() -> None:
    # The recommendation depends ONLY on the gene evidence — there is no design axis to pass, so
    # the caller can no longer steer it. Confirmed cross-sample split populations recommend
    # integration; everything else keeps the uncorrected representation.
    recommend = _g("recommend")
    assert recommend("none") == "do_not_integrate_based_on_current_evidence"
    assert recommend("localized") == "do_not_integrate_based_on_current_evidence"
    assert recommend("recurring_sample_associated") == "integration_recommended"


# --- compact decision state --------------------------------------------------


def test_compact_evidence_keeps_currency_and_artifact_pointer_only() -> None:
    compact = _g("compact_evidence")
    result = compact(
        {
            "schema_version": 1,
            "status": "complete",
            "evidence_id": "batch-evidence:e1",
            "batch_key": "donor",
            "recommendation": "integration_recommended",
            "artifact_path": "artifacts/batch-evidence.json",
            "cell_set_id": "cells-a",
            "count_representation_id": "counts-a",
            "representation_id": "rep-a",
            "clustering_id": "cluster-a",
            "supported_identity_pairs": [{"shared_genes": ["LYZ"]}],
        }
    )
    assert result == {
        "schema_version": 1,
        "status": "complete",
        "evidence_id": "batch-evidence:e1",
        "batch_key": "donor",
        "recommendation": "integration_recommended",
        "artifact_path": "artifacts/batch-evidence.json",
        "cell_set_id": "cells-a",
        "count_representation_id": "counts-a",
    }


def test_decision_persists_only_choice_rationale_and_evidence_id(tmp_path: Path) -> None:
    context = SimpleNamespace(
        state_facts={
            "batch": {
                "evidence": {
                    "status": "complete",
                    "evidence_id": "batch-evidence:e1",
                }
            }
        },
        staging_dir=tmp_path,
    )
    result = _handler("decide_batch_handling")(
        {
            "decision": "integrate",
            "evidence_id": "batch-evidence:e1",
            "rationale": "The user selected a shared corrected representation.",
        },
        context,
    )
    expected = {
        "decision": "integrate",
        "evidence_id": "batch-evidence:e1",
        "rationale": "The user selected a shared corrected representation.",
    }
    assert result["details"] == expected
    assert result["facts_patch"] == {"batch": {"decision": expected}}
    assert result["decisions_patch"] == {
        "batch_handling": {
            "decision": "integrate",
            "rationale": "The user selected a shared corrected representation.",
        }
    }


# --- identity resolution -----------------------------------------------------


def _provenance() -> dict[str, str]:
    return {
        "cell_set_id": "cells-a",
        "count_representation_id": "counts-a",
        "representation_id": "rep-a",
        "clustering_id": "cluster-a",
    }


def test_resolve_identities_preserves_all_four_from_artifact() -> None:
    adata = SimpleNamespace()
    resolved = _g("_resolve_input_identities")(_provenance(), adata, "leiden")
    assert resolved == _provenance()


def test_resolve_identities_derives_missing_values_from_artifact() -> None:
    adata = SimpleNamespace(
        obs_names=["cell-1", "cell-2"],
        var_names=["GeneA", "GeneB"],
        obs={"leiden": ["0", "1"]},
        n_obs=2,
        n_vars=2,
    )
    resolved = _g("_resolve_input_identities")({}, adata, "leiden")
    assert set(resolved) == {
        "cell_set_id",
        "count_representation_id",
        "representation_id",
        "clustering_id",
    }
    assert all(":sha256:" in value for value in resolved.values())


# --- plain-language interpretation (grounded, no assumed biology) ------------


def test_concordance_reports_ari_nmi_and_flags_tracking() -> None:
    pytest.importorskip("sklearn")  # ARI/NMI run in the compute env; venv may lack scikit-learn
    concord = _g("cluster_batch_concordance")
    # Clusters that perfectly follow sample identity -> high agreement, tracks_sample True.
    tracked = concord(["s1", "s1", "s2", "s2"], ["0", "0", "1", "1"])
    assert tracked["tracks_sample"] is True
    assert tracked["ari"] > 0.5
    assert "correspond to individual samples" in tracked["interpretation"]
    # Clusters independent of sample -> low agreement, well mixed.
    mixed = concord(["s1", "s2", "s1", "s2"], ["0", "0", "1", "1"])
    assert mixed["tracks_sample"] is False
    assert "well mixed" in mixed["interpretation"]


def test_plain_interpretation_names_real_genes_and_recommends_when_recurring() -> None:
    build = _g("build_plain_interpretation")
    text = build(
        batch_key="sample",
        n_regions=30,
        pairs=[
            {
                "cluster_a": "17",
                "batch_a": "Donor_05",
                "cluster_b": "10",
                "batch_b": "Donor_08",
                "shared_genes": ["C1QB", "C1QA", "TYROBP"],
                "higher_in_a": ["CCL18", "FABP4"],
                "higher_in_b": ["FOLR3", "FN1"],
            }
        ],
        recurring_by_sample={"Donor_06": ["HLA-C", "XIST"]},
        gene_evidence="recurring_sample_associated",
        design_interpretation="unknown",
        recommendation="integration_recommended",
        concordance={
            "ari": 0.51,
            "nmi": 0.68,
            "interpretation": "clusters largely correspond to individual samples",
        },
        mixing={
            "status": "complete",
            "mean_same_batch_neighbor_fraction": 0.89,
            "random_composition_same_batch_fraction": 0.13,
        },
    )
    # Every gene named comes from the inputs; no disease/tissue is invented.
    assert "C1QB" in text and "CCL18" in text and "FOLR3" in text
    assert "tumor" not in text.lower() and "cancer" not in text.lower()
    # The design is still stated as unknown (the caveat travels alongside), and the mixing caveat
    # is present — but recurring evidence now RECOMMENDS integration rather than deferring.
    assert "no experimental-design information was provided" in text
    assert "is recommended" in text
    assert "only you can answer" in text
    assert "WHERE samples separate, never WHY" in text


def test_plain_interpretation_localized_when_no_recurrence() -> None:
    build = _g("build_plain_interpretation")
    text = build(
        batch_key="sample",
        n_regions=5,
        pairs=[
            {
                "cluster_a": "1",
                "batch_a": "A",
                "cluster_b": "2",
                "batch_b": "B",
                "shared_genes": ["CD3D"],
                "higher_in_a": ["IL7R"],
                "higher_in_b": ["GZMB"],
            }
        ],
        recurring_by_sample={},
        gene_evidence="localized",
        design_interpretation="unknown",
        recommendation="do_not_integrate_based_on_current_evidence",
        concordance={
            "ari": 0.1,
            "nmi": 0.1,
            "interpretation": "clusters are largely independent of sample (well mixed)",
        },
        mixing=None,
    )
    assert "localized to a few populations" in text


def test_terminal_summary_is_compact_legacy_shaped_and_stops_for_user_choice() -> None:
    build = _g("build_terminal_summary")
    text = build(
        batch_key="sample",
        n_regions=12,
        supported_pairs=[
            {
                "cluster_a": "17",
                "batch_a": "Donor_05",
                "cluster_b": "10",
                "batch_b": "Donor_08",
                "signature_similarity": 0.42,
                "shared_genes": ["C1QA", "C1QB", "TYROBP"],
            }
        ],
        recurring=[
            {
                "gene": "SOD2",
                "higher_in_batch": "Donor_05",
                "n_populations": 3,
            }
        ],
        gene_evidence="recurring_sample_associated",
        design_interpretation="unknown",
        recommendation="integration_recommended",
        concordance={"ari": 0.51, "nmi": 0.68},
        mixing={
            "status": "complete",
            "mean_same_batch_neighbor_fraction": 0.89,
            "random_composition_same_batch_fraction": 0.13,
        },
    )

    assert "cluster 17 in Donor_05 vs cluster 10 in Donor_08" in text
    assert "signature similarity 0.42" in text
    assert "C1QA" in text and "SOD2" in text
    assert "ARI 0.51, NMI 0.68" in text
    assert "strong-signature threshold" not in text
    # The pause is emphatic and self-deciding is called out as a bug; the tool never records.
    assert "Do NOT call decide_batch_handling" in text
    assert "END YOUR TURN now" in text
    # The selector's last option is always "describe the experiment setup".
    assert "describe the experiment setup so we can understand it better" in text
    # Recurring gene evidence presents integration as the recommended default (legacy
    # post-investigation checkpoint framing), while the user still makes the call.
    assert "integrate with scVI as the recommended default" in text
    assert len(text) < 4_000


# --- advisory figure layout (retained) --------------------------------------


def test_low_cardinality_uses_external_legend_bar() -> None:
    layout = _g("_figure_layout")(7, 12)
    assert layout["mode"] == "bar"
    assert layout["figsize"][0] <= 26.0


def test_high_cardinality_switches_to_heatmap() -> None:
    layout = _g("_figure_layout")(26, 17)
    assert layout["mode"] == "heatmap"
    assert layout["figsize"][0] <= 26.0
