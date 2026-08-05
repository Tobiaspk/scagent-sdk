"""Cluster DEG plus explicit marker-program overlap."""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any


def _read_matrix(path):
    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)

def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


HUMAN_MARKERS = {
    "T cell": ["CD3D", "CD3E", "TRAC", "IL7R", "LTB"],
    "Cytotoxic lymphocyte": ["NKG7", "GNLY", "PRF1", "GZMB", "CTSW"],
    "B cell": ["MS4A1", "CD79A", "CD37", "CD74", "HLA-DRA"],
    # Plasma cells are defined by immunoglobulin/secretory program, not by GZMB alone.
    "Plasma cell": ["MZB1", "JCHAIN", "SDC1", "XBP1", "IGHG1"],
    "Monocyte": ["LYZ", "CTSS", "FCN1", "S100A8", "S100A9"],
    "Conventional dendritic cell": ["FCER1A", "CST3", "CD1C", "CLEC10A", "HLA-DPA1"],
    # pDCs share GZMB with cytotoxic lymphocytes but are distinguished by LILRA4/IL3RA/CLEC4C
    # and the IRF7/TCF4 program; they lack immunoglobulin/secretory plasma markers.
    "Plasmacytoid dendritic cell": [
        "LILRA4",
        "IL3RA",
        "CLEC4C",
        "GZMB",
        "IRF7",
        "TCF4",
        "SERPINF1",
    ],
    "Platelet": ["PPBP", "PF4", "NRGN", "GNG11", "RGS18"],
    "Erythroid": ["HBB", "HBA1", "HBA2", "AHSP", "ALAS2"],
    "Endothelial": ["KDR", "ESAM", "EMCN", "PECAM1", "VWF"],
    "Epithelial": ["EPCAM", "KRT8", "KRT18", "KRT19", "MUC1"],
    "Fibroblast": ["COL1A1", "COL1A2", "DCN", "LUM", "COL3A1"],
}

CYTOPUS_PROGRAMS = {
    "T cell": ["abT", "CD4-T", "CD8-T"],
    "Cytotoxic lymphocyte": ["CD8-T", "NK", "CD56dim-NK"],
    "B cell": ["B-naive", "B-memory"],
    "Plasma cell": ["plasma", "plasma-blast"],
    "Monocyte": ["mono", "c-mono", "nc-mono"],
    "Conventional dendritic cell": ["cDC1", "cDC2"],
    "Plasmacytoid dendritic cell": ["p-DC"],
}


def _cytopus_marker_sets() -> tuple[dict[str, list[str]], dict[str, Any]]:
    """Load curated Cytopus identities without making the package a hard runtime dependency."""

    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            import cytopus

            knowledge_base = cytopus.KnowledgeBase()
        available = set(map(str, knowledge_base.celltypes))
        marker_sets: dict[str, list[str]] = {}
        resolved_programs: dict[str, list[str]] = {}
        for label, requested in CYTOPUS_PROGRAMS.items():
            selected = [program for program in requested if program in available]
            if not selected:
                continue
            identities = knowledge_base.get_identities(selected, include_subsets=False)
            genes = sorted(
                {
                    str(gene).upper()
                    for program_genes in identities.values()
                    for gene in program_genes
                }
            )
            if genes:
                marker_sets[label] = genes
                resolved_programs[label] = selected
        return marker_sets, {
            "status": "available",
            "package_version": str(getattr(cytopus, "__version__", "unknown")),
            "resolved_programs": resolved_programs,
            "n_programs": len(resolved_programs),
        }
    except Exception as exc:
        return {}, {
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
            "resolved_programs": {},
            "n_programs": 0,
        }


def _marker_frequency(marker_sets: dict[str, list[str]]) -> dict[str, int]:
    """Count how many programs each gene appears in, so shared markers can be down-weighted."""

    frequency: dict[str, int] = {}
    for markers in marker_sets.values():
        for gene in {gene.upper() for gene in markers}:
            frequency[gene] = frequency.get(gene, 0) + 1
    return frequency


def _score_programs(
    cluster_genes: set[str],
    marker_sets: dict[str, list[str]],
    marker_frequency: dict[str, int],
    min_overlap: int,
) -> list[dict[str, Any]]:
    """Score each marker program against a cluster's up-genes.

    Overlap is weighted by specificity (``1 / program-count``) so shared markers such as GZMB,
    which occur in both the cytotoxic and pDC programs, cannot on their own carry a call.
    """

    genes = {str(gene).upper() for gene in cluster_genes}
    scores: list[dict[str, Any]] = []
    for label, markers in marker_sets.items():
        normalized = {gene.upper() for gene in markers}
        overlap = sorted(genes & normalized)
        unique_overlap = [gene for gene in overlap if marker_frequency.get(gene) == 1]
        shared_overlap = [gene for gene in overlap if marker_frequency.get(gene, 0) > 1]
        coverage = len(overlap) / max(len(normalized), 1)
        specificity = sum(1 / marker_frequency[gene] for gene in overlap) / max(
            len(normalized), 1
        )
        scores.append(
            {
                "candidate": label,
                "coverage_score": coverage,
                "specificity_weighted_score": specificity,
                "overlap_count": len(overlap),
                "overlap_genes": ";".join(overlap),
                "unique_overlap_genes": ";".join(unique_overlap),
                "shared_overlap_genes": ";".join(shared_overlap),
                "support": "supported" if len(overlap) >= min_overlap else "insufficient",
            }
        )
    return scores


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import pandas as pd
    import scanpy as sc

    path = Path(str(arguments["path"])).expanduser().resolve()
    cluster_key = str(arguments.get("cluster_key", "leiden"))
    organism = str(arguments.get("organism", "human"))
    top_n = int(arguments.get("top_n", 30))
    min_logfoldchange = float(arguments.get("min_logfoldchange", 0.25))
    max_adjusted_pvalue = float(arguments.get("max_adjusted_pvalue", 0.05))
    min_overlap = int(arguments.get("min_marker_overlap", 2))
    use_cytopus = bool(arguments.get("use_cytopus", True))
    adata = _read_matrix(path)
    if cluster_key not in adata.obs:
        raise ValueError(f"cluster key {cluster_key!r} is absent")
    provenance = adata.uns.get("scagent_sdk", {})
    cell_set_id = provenance.get("cell_set_id") or _identity(
        "cells", sorted(map(str, adata.obs_names))
    )
    clustering_id = provenance.get("clustering_id") or _identity(
        "clustering",
        {
            "cell_set_id": cell_set_id,
            "cluster_key": cluster_key,
            "labels": sorted(
                zip(
                    map(str, adata.obs_names),
                    map(str, adata.obs[cluster_key]),
                    strict=True,
                )
            ),
        },
    )
    if "rank_genes_groups" not in adata.uns:
        sc.tl.rank_genes_groups(adata, cluster_key, method="wilcoxon", pts=True)
    deg = sc.get.rank_genes_groups_df(adata, group=None)
    deg.to_csv(context.staging_dir / "cluster-deg.csv", index=False)
    required_columns = {"group", "names", "scores", "logfoldchanges", "pvals_adj"}
    if not required_columns.issubset(deg.columns):
        raise ValueError(
            "cluster DEG result lacks required columns: "
            f"{required_columns - set(deg)}"
        )
    significant = deg.loc[
        (deg["pvals_adj"] <= max_adjusted_pvalue)
        & (deg["logfoldchanges"] >= min_logfoldchange)
    ].copy()
    top = (
        significant.sort_values(["group", "scores"], ascending=[True, False])
        .groupby("group", observed=True)
        .head(top_n)
    )
    custom = arguments.get("marker_sets", {})
    marker_sets = dict(HUMAN_MARKERS)
    if organism == "mouse":
        marker_sets = {
            label: [gene.title() for gene in genes] for label, genes in marker_sets.items()
        }
    cytopus_status: dict[str, Any] = {
        "status": "not_requested" if not use_cytopus else "not_applicable",
        "reason": "Cytopus identities are currently applied only to human data.",
        "resolved_programs": {},
        "n_programs": 0,
    }
    if use_cytopus and organism == "human":
        cytopus_markers, cytopus_status = _cytopus_marker_sets()
        for label, genes in cytopus_markers.items():
            marker_sets[label] = sorted(set(marker_sets.get(label, [])) | set(genes))
    if isinstance(custom, dict):
        marker_sets.update({str(label): list(map(str, genes)) for label, genes in custom.items()})
    marker_frequency = _marker_frequency(marker_sets)
    rows: list[dict[str, Any]] = []
    candidates: dict[str, list[dict[str, Any]]] = {}
    deg_summary: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    clusters = sorted(map(str, adata.obs[cluster_key].astype(str).unique()))
    for cluster in clusters:
        frame = top.loc[top["group"].astype(str) == cluster]
        genes = {str(gene).upper() for gene in frame["names"]}
        deg_summary[cluster] = {
            "n_significant_positive_degs": int(
                (significant["group"].astype(str) == cluster).sum()
            ),
            "top_degs": [
                {
                    "gene": str(row.names),
                    "score": float(row.scores),
                    "logfoldchange": float(row.logfoldchanges),
                    "adjusted_pvalue": float(row.pvals_adj),
                }
                for row in frame.head(10).itertuples(index=False)
            ],
        }
        if len(frame) < min(5, top_n):
            warnings.append(
                f"cluster {cluster!r} has only {len(frame)} significant positive DEGs at the "
                "configured thresholds"
            )
        scores = _score_programs(genes, marker_sets, marker_frequency, min_overlap)
        for row in scores:
            row["cluster"] = cluster
            rows.append(row)
        candidates[cluster] = sorted(
            scores,
            key=lambda item: (
                item["specificity_weighted_score"],
                item["coverage_score"],
            ),
            reverse=True,
        )[:3]
    pd.DataFrame(rows).to_csv(context.staging_dir / "marker-program-scores.csv", index=False)
    (context.staging_dir / "marker-candidates.json").write_text(
        json.dumps(candidates, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    marker_sets_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(marker_sets, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    evidence_id = _identity(
        "marker-evidence",
        {
            "clustering_id": clustering_id,
            "cluster_key": cluster_key,
            "marker_sets_fingerprint": marker_sets_fingerprint,
            "deg_summary": deg_summary,
            "candidates": candidates,
        },
    )
    (context.staging_dir / "marker-evidence.md").write_text(
        "# Marker and DEG evidence\n\n"
        f"- Clustering identity: `{clustering_id}`\n"
        f"- Cell-set identity: `{cell_set_id}`\n"
        f"- Positive DEG thresholds: adjusted p <= {max_adjusted_pvalue}, "
        f"log fold-change >= {min_logfoldchange}\n"
        f"- Marker-set fingerprint: `{marker_sets_fingerprint}`\n\n"
        "Candidates are hypotheses. Shared markers are down-weighted, insufficient overlap is "
        "reported, and final labels require independent reference evidence plus explicit "
        "DEG-derived adjudication.\n\n"
        "## Warnings\n\n"
        + "\n".join(f"- {warning}" for warning in warnings or ["No automated DEG warnings."])
        + "\n",
        encoding="utf-8",
    )
    return {
        "summary": f"Generated DEG and marker-program candidates for {len(candidates)} clusters.",
        "details": {
            "clustering_id": clustering_id,
            "cell_set_id": cell_set_id,
            "cluster_key": cluster_key,
            "top_n": top_n,
            "min_logfoldchange": min_logfoldchange,
            "max_adjusted_pvalue": max_adjusted_pvalue,
            "min_marker_overlap": min_overlap,
            "marker_sets_fingerprint": marker_sets_fingerprint,
            "cytopus": cytopus_status,
            "evidence_id": evidence_id,
            "deg_summary": deg_summary,
            "candidates": candidates,
            "warnings": warnings,
            "caution": "Candidates are evidence, not finalized labels.",
        },
        "facts_patch": {
            "annotation": {
                "evidence": {
                    "markers": {
                        "status": "complete",
                        "evidence_id": evidence_id,
                        "clustering_id": clustering_id,
                        "cell_set_id": cell_set_id,
                        "cluster_key": cluster_key,
                        "marker_sets_fingerprint": marker_sets_fingerprint,
                        "cytopus": cytopus_status,
                        "deg_summary": deg_summary,
                        "candidates": candidates,
                        "warnings": warnings,
                        "artifact_path": (
                            f"artifacts/capabilities/{context.execution_id}/marker-candidates.json"
                        ),
                    }
                }
            }
        },
        "artifacts": [
            {"name": "cluster-deg", "relative_path": "cluster-deg.csv", "media_type": "text/csv"},
            {
                "name": "marker-scores",
                "relative_path": "marker-program-scores.csv",
                "media_type": "text/csv",
            },
            {
                "name": "marker-candidates",
                "relative_path": "marker-candidates.json",
                "media_type": "application/json",
            },
            {
                "name": "marker-evidence-report",
                "relative_path": "marker-evidence.md",
                "media_type": "text/markdown",
            },
        ],
    }


def review_annotation_evidence(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    facts = context.state_facts
    analysis = facts.get("analysis")
    clustering = analysis.get("clustering") if isinstance(analysis, dict) else None
    clustering_id = clustering.get("id") if isinstance(clustering, dict) else None
    annotation = facts.get("annotation")
    evidence = annotation.get("evidence") if isinstance(annotation, dict) else None
    if not clustering_id or not isinstance(evidence, dict):
        raise ValueError("current clustering and annotation evidence are required")
    current = {
        str(name): value
        for name, value in evidence.items()
        if isinstance(value, dict)
        and value.get("status") == "complete"
        and value.get("clustering_id") == clustering_id
    }
    methods = {str(value) for value in arguments.get("methods_reviewed", [])}
    if "markers" not in methods:
        raise ValueError("methods_reviewed must include DEG/marker evidence ('markers')")
    absent = sorted(methods - set(current))
    if absent:
        raise ValueError(
            "methods_reviewed contains absent or stale evidence: " + ", ".join(absent)
        )
    references = sorted(methods - {"markers"})
    if not references:
        raise ValueError("at least one current independent reference method must be reviewed")
    waiver_arg = arguments.get("reference_waiver")
    waiver = str(waiver_arg).strip() if waiver_arg is not None else None
    if len(references) < 2 and not waiver:
        raise ValueError(
            "fewer than two independent reference methods requires a specific "
            "reference_waiver explaining model unavailability or incompatibility"
        )
    findings = [str(value).strip() for value in arguments.get("agreement_findings", [])]
    if not findings or any(not value for value in findings):
        raise ValueError("agreement_findings must contain at least one non-empty observation")
    reviewed_artifacts = sorted(
        {str(value).strip() for value in arguments.get("reviewed_artifacts", [])}
    )
    if not reviewed_artifacts or any(not value for value in reviewed_artifacts):
        raise ValueError("reviewed_artifacts must name the DEG and reference evidence inspected")
    unresolved = sorted(
        {str(value) for value in arguments.get("unresolved_clusters", [])}
    )
    review = {
        "status": "resolved" if not unresolved else "action_required",
        "clustering_id": clustering_id,
        "deg_primary": True,
        "methods_reviewed": sorted(methods),
        "evidence_ids": {
            method: current[method].get("evidence_id") for method in sorted(methods)
        },
        "reference_methods": references,
        "reference_waiver": waiver,
        "agreement_findings": findings,
        "reviewed_artifacts": reviewed_artifacts,
        "unresolved_clusters": unresolved,
        "rationale": str(arguments["rationale"]).strip(),
    }
    if not review["rationale"]:
        raise ValueError("rationale must not be empty")
    return {
        "summary": (
            f"Reviewed DEG evidence and {len(references)} independent reference method(s); "
            + (
                "annotation evidence is resolved for final adjudication."
                if not unresolved
                else f"{len(unresolved)} cluster(s) still require annotation work."
            )
        ),
        "details": review,
        "facts_patch": {"annotation": {"review": review}},
        "decisions_patch": {"annotation_evidence_review": review},
    }
