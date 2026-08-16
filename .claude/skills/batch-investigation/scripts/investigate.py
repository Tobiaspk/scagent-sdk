"""Gene-first batch evidence, then a separate identity-bound handling decision.

`investigate_batch` (``run_evidence``) produces evidence and records no decision. It is gene-first:
it finds sample-enriched cluster regions, characterizes each with a within-sample identity DEG
(that cluster versus the rest of its OWN batch, holding batch constant), matches the same population
across batches by shared **discriminating** identity genes, compares matched regions directly, and
flags any sample-associated program that recurs across >=2 distinct populations. Composition,
Cramer's V, neighborhood mixing, and per-batch QC are retained only as advisory context. The verdict
is two independent axes — gene evidence x experimental design — and a deterministic, non-binding
recommendation. The DE test is scanpy's in-environment Wilcoxon rank test.

`decide_batch_handling` (``run_decision``) consumes a current evidence id after the model has
inspected the evidence and records the handling decision. It validates the decision against the
recommendation and never lets integration proceed silently against the evidence or without a basis.

Pure classifiers live at module scope for unit testing without Scanpy.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

BATCH_EVIDENCE_SCHEMA = 1
DE_ENGINE = "scanpy_wilcoxon"
# Version of the decision-authorization policy applied by ``run_decision``. Decisions record it so
# a floor can refuse decisions validated under an older, weaker policy.
DECISION_POLICY_VERSION = 1

# --- versioned gene classification (shared vocabulary with cluster QC) -------
GENE_CLASS_VERSION = "batch-gene-class-v1"

_NUISANCE_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"^MT-",
        r"^MRP[LS]\d",
        r"^RP[LS]\d",
        r"^MALAT1$",
        r"^NEAT1$",
        r"^HB[ABDEGMQZ]$",
        r"^HBA\d$",
        r"^LINC\d",
        r"^A[CL]\d{6}\.",
        r"\.\d+$",
    )
)
# Broad/context: antigen presentation, heat-shock/ER stress, activation/IEG, housekeeping, cell
# cycle. Real programs, but shared across lineages, so they must not decide a population match.
_BROAD_PATTERNS = tuple(re.compile(p) for p in (r"^HLA-", r"^HSP", r"^HIST\d", r"^DNAJ"))
_BROAD_CONTEXT_GENES = frozenset(
    {
        "B2M",
        "ACTB",
        "ACTG1",
        "GAPDH",
        "TMSB4X",
        "TMSB10",
        "FTL",
        "FTH1",
        "FOS",
        "FOSB",
        "JUN",
        "JUNB",
        "JUND",
        "EGR1",
        "DUSP1",
        "NFKBIA",
        "IER2",
        "CD74",
        "XIST",
        "TPT1",
        "EEF1A1",
        "EEF2",
        "UBC",
        "UBB",
        "MKI67",
        "TOP2A",
        "STMN1",
        "TUBB",
        "TUBA1B",
        "PCNA",
        "CENPF",
        "HMGB1",
        "HMGB2",
        "S100A4",
        # ER/secretory stress program (the live false-match culprits: DERL3, HSP90B1, ...).
        "HSP90B1",
        "HSPA5",
        "DERL3",
        "XBP1",
        "SSR4",
        "SEC61B",
        "SDF2L1",
        "PDIA4",
        "PDIA6",
        "CALR",
        "CANX",
        "MANF",
        "DNAJB9",
        "SEL1L",
        "H13",
        "HM13",
    }
)


def _read_matrix(path):
    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)

def gene_class(gene: str) -> str:
    """Classify a gene symbol as ``nuisance``, ``broad``, or ``discriminating``."""
    symbol = str(gene).upper()
    for pattern in _NUISANCE_PATTERNS:
        if pattern.search(symbol):
            return "nuisance"
    if symbol in _BROAD_CONTEXT_GENES:
        return "broad"
    for pattern in _BROAD_PATTERNS:
        if pattern.search(symbol):
            return "broad"
    return "discriminating"


def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


# --- pure classifiers --------------------------------------------------------
def region_enrichment(n_in_region: int, n_in_cluster: int, batch_global_fraction: float) -> float:
    """Enrichment of a batch within a cluster over that batch's dataset-wide frequency.

    A region that is 42% one sample when that sample is 9% of the data returns ~4.6 — caught even
    though raw purity (0.42) is modest.
    """
    if n_in_cluster <= 0 or batch_global_fraction <= 0:
        return 0.0
    return (n_in_region / n_in_cluster) / batch_global_fraction


def match_regions(
    disc_a: list[str], disc_b: list[str], *, min_shared: int, min_jaccard: float
) -> dict[str, Any]:
    """Decide whether two regions are the same population using DISCRIMINATING genes only.

    Broad/context and nuisance genes are excluded upstream, so an ER/stress or housekeeping overlap
    can no longer manufacture a match. A match needs both a minimum count of shared discriminating
    genes and a minimum Jaccard, and records an explicit rejection reason otherwise.
    """
    set_a = {str(g).upper() for g in disc_a if gene_class(g) == "discriminating"}
    set_b = {str(g).upper() for g in disc_b if gene_class(g) == "discriminating"}
    shared = sorted(set_a & set_b)
    union = set_a | set_b
    jaccard = len(shared) / len(union) if union else 0.0
    if len(shared) < min_shared:
        supported, reason = False, f"only {len(shared)} shared discriminating genes (<{min_shared})"
    elif jaccard < min_jaccard:
        supported, reason = False, f"jaccard {jaccard:.2f} below {min_jaccard:.2f}"
    else:
        supported, reason = True, "shared discriminating identity above thresholds"
    return {
        "shared": len(shared),
        "jaccard": jaccard,
        "shared_genes": shared,
        "supported": supported,
        "reason": reason,
    }


def summarize_recurrence(
    direct_rows: list[dict[str, Any]], *, min_populations: int = 2
) -> list[dict[str, Any]]:
    """Programs where the same batch is higher for a gene across >=2 distinct populations.

    Order-invariant: the result depends only on the set of (gene, batch, population) facts.
    """
    seen: dict[tuple[str, str], set[Any]] = {}
    for row in direct_rows:
        seen.setdefault((str(row["gene"]), str(row["higher_in_batch"])), set()).add(
            row["population"]
        )
    recurring = [
        {
            "gene": gene,
            "higher_in_batch": batch,
            "n_populations": len(pops),
            "populations": sorted(map(str, pops)),
        }
        for (gene, batch), pops in seen.items()
        if len(pops) >= min_populations
    ]
    recurring.sort(key=lambda r: (-r["n_populations"], r["gene"], r["higher_in_batch"]))
    return recurring


def classify_gene_evidence(n_matched_with_diffs: int, n_recurring_populations: int) -> str:
    """gene_evidence axis from cross-sample matches and program recurrence."""
    if n_recurring_populations >= 2:
        return "recurring_sample_associated"
    if n_matched_with_diffs >= 1:
        return "localized"
    return "none"


def classify_design(
    *, confounded_columns: list[str], technical_documented: bool, has_orthogonal_condition: bool
) -> str:
    """design_interpretation axis. Confounding takes priority over documented-technical status.

    Perfect biological confounding is never silently reclassified as technical: even when a batch is
    documented technical, a confounded design stays ``confounded_with_biology`` (so the
    recommendation is cannot-determine and integration requires an explicit override).
    """
    if confounded_columns:
        return "confounded_with_biology"
    if technical_documented:
        return "documented_technical_batch"
    if has_orthogonal_condition:
        return "orthogonal_but_not_known_technical"
    return "unknown"


def recommend(gene_evidence: str, design_interpretation: str) -> str:
    """Deterministic, non-binding recommendation.

    Only a recurring program together with a documented technical batch yields support.
    """
    if gene_evidence in ("none", "localized"):
        return "do_not_integrate_based_on_current_evidence"
    if design_interpretation == "documented_technical_batch":
        return "integration_supported"
    if design_interpretation == "orthogonal_but_not_known_technical":
        return "integration_optional_for_confirmed_replicates"
    return "cannot_determine_technical_vs_biological"


def validate_decision(
    decision: str, recommendation: str, integration_basis: str | None, override_warning: str | None
) -> dict[str, Any]:
    """Gate a submitted decision. Non-integration decisions are always allowed (conservative).

    Integration always requires an explicit basis, is permitted directly only when the
    recommendation supports it, and otherwise needs an explicit override warning — never
    silent against the evidence.
    """
    if decision != "integrate":
        return {"ok": True, "violation": None}
    if integration_basis not in (
        "documented_technical_batch",
        "user_authorized_comparable_replicates",
    ):
        return {
            "ok": False,
            "violation": "integrate requires integration_basis (documented_technical_batch or "
            "user_authorized_comparable_replicates)",
        }
    if recommendation == "integration_supported":
        return {"ok": True, "violation": None}
    if (
        recommendation == "integration_optional_for_confirmed_replicates"
        and integration_basis == "user_authorized_comparable_replicates"
    ):
        return {"ok": True, "violation": None}
    if override_warning:
        return {"ok": True, "violation": None}
    return {
        "ok": False,
        "violation": f"recommendation {recommendation!r} does not support integration; provide an "
        "explicit override_warning to proceed",
    }


def validate_integration_basis(
    integration_basis: str | None, evidence: dict[str, Any]
) -> dict[str, Any]:
    """A ``documented_technical_batch`` basis must be backed by the evidence itself.

    The model cannot assert a documented technical batch at decision time: the claim has to be
    present in the evidence object (``technical_batch_documented`` with a non-empty basis), which
    in turn required a real basis string when the evidence was produced.
    """
    if integration_basis != "documented_technical_batch":
        return {"ok": True, "violation": None}
    documented = bool(evidence.get("technical_batch_documented"))
    basis = evidence.get("technical_batch_basis")
    if documented and isinstance(basis, str) and basis.strip():
        return {"ok": True, "violation": None}
    return {
        "ok": False,
        "violation": "integration_basis='documented_technical_batch' requires evidence recorded "
        "with technical_batch_documented=true and a non-empty technical_batch_basis",
    }


LEGEND_CARDINALITY_LIMIT = 12


def _figure_layout(n_batches: int, n_clusters: int) -> dict[str, Any]:
    """Choose a readable composition figure for the batch cardinality (advisory only)."""
    if n_batches > LEGEND_CARDINALITY_LIMIT:
        width = float(max(6.0, min(0.35 * n_batches + 3.0, 26.0)))
        height = float(max(4.0, min(0.30 * n_clusters + 2.0, 22.0)))
        return {
            "mode": "heatmap",
            "figsize": (width, height),
            "legend": "colorbar",
            "annotate": n_batches <= 30 and n_clusters <= 30,
            "tick_fontsize": 7 if max(n_batches, n_clusters) > 20 else 8,
        }
    width = float(max(7.0, min(0.5 * n_clusters + 2.0, 26.0)))
    return {
        "mode": "bar",
        "figsize": (width, 5.0),
        "legend": "outside",
        "legend_ncol": 1 if n_batches <= 8 else 2,
        "rotate_xticks": n_clusters > 12,
    }


def _cramers_v(table: Any) -> float:
    import numpy as np
    from scipy.stats import chi2_contingency

    values = np.asarray(table, dtype=float)
    n = values.sum()
    if n <= 0 or min(values.shape) <= 1:
        return 0.0
    chi2 = chi2_contingency(values, correction=False)[0]
    phi2 = chi2 / n
    rows, cols = values.shape
    corrected = max(0.0, phi2 - ((cols - 1) * (rows - 1)) / max(n - 1, 1))
    rows_corrected = rows - ((rows - 1) ** 2) / max(n - 1, 1)
    cols_corrected = cols - ((cols - 1) ** 2) / max(n - 1, 1)
    denominator = min(cols_corrected - 1, rows_corrected - 1)
    return float(math.sqrt(corrected / denominator)) if denominator > 0 else 0.0


def _resolve_input_identities(
    provenance: Any, adata: Any, cluster_key: str
) -> dict[str, str]:
    """Resolve portable identities from the artifact without consulting session state."""

    provenance = provenance if isinstance(provenance, dict) else {}
    cell_set_id = provenance.get("cell_set_id")
    if not isinstance(cell_set_id, str):
        cell_set_id = _identity("cells", sorted(map(str, adata.obs_names)))
    count_representation_id = provenance.get("count_representation_id")
    if not isinstance(count_representation_id, str):
        count_representation_id = _identity(
            "count-representation",
            {
                "cell_set_id": cell_set_id,
                "count_matrix_id": provenance.get("count_matrix_id"),
                "genes": list(map(str, adata.var_names)),
            },
        )
    representation_id = provenance.get("representation_id")
    if not isinstance(representation_id, str):
        representation_id = _identity(
            "representation",
            {
                "cell_set_id": cell_set_id,
                "shape": [int(adata.n_obs), int(adata.n_vars)],
                "source": "X",
            },
        )
    clustering_id = provenance.get("clustering_id")
    if not isinstance(clustering_id, str):
        clustering_id = _identity(
            "clustering",
            {
                "cell_set_id": cell_set_id,
                "key": cluster_key,
                "assignments": list(
                    zip(
                        map(str, adata.obs_names),
                        map(str, adata.obs[cluster_key]),
                        strict=True,
                    )
                ),
            },
        )
    return {
        "cell_set_id": cell_set_id,
        "count_representation_id": count_representation_id,
        "representation_id": representation_id,
        "clustering_id": clustering_id,
    }


def _analysis_patch(
    identities: dict[str, str], adata: Any, cluster_key: str
) -> dict[str, Any]:
    """Re-assert the identities this evidence is bound to.

    Deliberately does not touch ``dataset_revision.prepared_path``. Batch investigation reads a
    matrix and writes none, so asserting a head would let a read-only tool redirect the analysis to
    whatever file it happened to be handed -- including a stale one.
    """

    return {
        "cell_set": {"id": identities["cell_set_id"], "n_cells": int(adata.n_obs)},
        "count_representation": {"id": identities["count_representation_id"]},
        "representation": {"id": identities["representation_id"]},
        "clustering": {
            "id": identities["clustering_id"],
            "key": cluster_key,
            "n_clusters": int(adata.obs[cluster_key].astype(str).nunique()),
        },
    }


def _region_markers(
    sub: Any,
    target_mask: Any,
    *,
    n_top: int,
    min_lfc: float,
    max_padj: float,
    min_frac_diff: float,
    sc: Any,
    np: Any,
) -> dict[str, Any]:
    """One-vs-rest positive Wilcoxon markers for a region within its own batch subset.

    Returns full per-gene records (effect, q-value, target/reference fractions) plus the
    discriminating-only identity gene list used for cross-sample matching.
    """
    n_target = int(target_mask.sum())
    n_rest = int((~target_mask).sum())
    if n_target < 3 or n_rest < 3:
        return {"discriminating_genes": [], "records": [], "n_target": n_target, "n_rest": n_rest}
    sub = sub.copy()
    sub.obs["_grp"] = np.where(target_mask, "target", "rest")
    sc.tl.rank_genes_groups(
        sub, "_grp", groups=["target"], reference="rest", method="wilcoxon", pts=True
    )
    frame = sc.get.rank_genes_groups_df(sub, group="target")
    has_pts = "pct_nz_group" in frame.columns and "pct_nz_reference" in frame.columns
    positive = frame[(frame["logfoldchanges"] >= min_lfc) & (frame["pvals_adj"] <= max_padj)]
    positive = positive.sort_values("scores", ascending=False)
    records: list[dict[str, Any]] = []
    discriminating: list[str] = []
    for _, row in positive.head(max(n_top * 3, 60)).iterrows():
        gene = str(row["names"])
        frac_diff = float(row["pct_nz_group"] - row["pct_nz_reference"]) if has_pts else None
        record = {
            "gene": gene,
            "gene_class": gene_class(gene),
            "logfoldchange": float(row["logfoldchanges"]),
            "score": float(row["scores"]),
            "pvals_adj": float(row["pvals_adj"]),
            "pct_target": float(row["pct_nz_group"]) if has_pts else None,
            "pct_reference": float(row["pct_nz_reference"]) if has_pts else None,
        }
        records.append(record)
        if record["gene_class"] == "discriminating" and (
            frac_diff is None or frac_diff >= min_frac_diff
        ):
            discriminating.append(gene)
    return {
        "discriminating_genes": discriminating[:n_top],
        "records": records[: n_top * 2],
        "n_target": n_target,
        "n_rest": n_rest,
    }


def _union_find_components(
    region_keys: list[tuple[str, str]], edges: list[tuple[int, int]]
) -> dict[tuple[str, str], int]:
    parent = list(range(len(region_keys)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in edges:
        parent[find(a)] = find(b)
    return {region_keys[i]: find(i) for i in range(len(region_keys))}


def run_evidence(arguments: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: C901
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import scanpy as sc

    path = Path(str(arguments["path"])).expanduser().resolve()
    batch_key = arguments.get("batch_key")
    cluster_key = str(arguments.get("cluster_key", "leiden"))
    condition_keys = [str(k) for k in arguments.get("condition_keys", [])]
    technical_documented = bool(arguments.get("technical_batch_documented", False))
    technical_basis = arguments.get("technical_batch_basis")
    min_cells_region = int(arguments.get("min_cells_per_region", 30))
    min_enrichment = float(arguments.get("min_enrichment", 2.0))
    n_identity_genes = int(arguments.get("n_identity_genes", 25))
    max_regions = int(arguments.get("max_regions", 40))
    max_candidate_pairs = int(arguments.get("max_candidate_pairs", 60))
    min_shared = int(arguments.get("min_shared_identity_genes", 3))
    min_jaccard = float(arguments.get("min_match_jaccard", 0.15))
    n_neighbors = int(arguments.get("n_neighbors_for_mixing", 30))
    max_cells = int(arguments.get("max_cells_for_mixing", 20000))
    min_lfc = float(arguments.get("min_logfoldchange", 0.5))
    max_padj = float(arguments.get("max_adjusted_pvalue", 0.05))
    min_frac_diff = float(arguments.get("min_fraction_difference", 0.1))
    seed = int(arguments.get("random_seed", 0))
    if technical_documented and not (isinstance(technical_basis, str) and technical_basis.strip()):
        raise ValueError(
            "technical_batch_documented=true requires a non-empty technical_batch_basis"
        )

    effective_parameters = {
        "cluster_key": cluster_key,
        "condition_keys": sorted(condition_keys),
        "min_cells_per_region": min_cells_region,
        "min_enrichment": min_enrichment,
        "n_identity_genes": n_identity_genes,
        "max_regions": max_regions,
        "max_candidate_pairs": max_candidate_pairs,
        "min_shared_identity_genes": min_shared,
        "min_match_jaccard": min_jaccard,
        "min_logfoldchange": min_lfc,
        "max_adjusted_pvalue": max_padj,
        "min_fraction_difference": min_frac_diff,
        "random_seed": seed,
    }

    if not path.exists():
        raise FileNotFoundError(path)
    adata = _read_matrix(path)
    if cluster_key not in adata.obs:
        raise ValueError(f"observation column {cluster_key!r} is absent")
    ident = _resolve_input_identities(adata.uns.get("scagent_sdk", {}), adata, cluster_key)
    analysis_patch = _analysis_patch(ident, adata, cluster_key)

    if batch_key is None:
        evidence = {
            "schema_version": BATCH_EVIDENCE_SCHEMA,
            "status": "not_applicable",
            "batch_key": None,
            "gene_evidence": "none",
            "design_interpretation": "unknown",
            "recommendation": "not_applicable",
            "de_engine": DE_ENGINE,
            "technical_batch_documented": technical_documented,
            "technical_batch_basis": technical_basis,
            "effective_parameters": effective_parameters,
            **ident,
        }
        evidence["evidence_id"] = _identity("batch-evidence", evidence)
        (context.staging_dir / "batch-investigation.md").write_text(
            "# Batch investigation\n\nNo meaningful batch key was selected; recorded "
            "`not_applicable` bound to the current analysis identities.\n",
            encoding="utf-8",
        )
        return {
            "summary": "Recorded not-applicable batch evidence for the input artifact identities.",
            "details": evidence,
            "facts_patch": {
                "analysis": analysis_patch,
                "batch": {"evidence": evidence, "decision": None},
            },
            "artifacts": [
                {
                    "name": "batch-report",
                    "relative_path": "batch-investigation.md",
                    "media_type": "text/markdown",
                }
            ],
        }

    batch_key = str(batch_key)
    if batch_key not in adata.obs:
        raise ValueError(f"observation column {batch_key!r} is absent")
    if bool(adata.obs[batch_key].isna().any()):
        raise ValueError(f"batch column {batch_key!r} contains missing values")
    batch = adata.obs[batch_key].astype(str)
    cluster = adata.obs[cluster_key].astype(str)
    if batch.nunique() < 2:
        raise ValueError("batch investigation requires at least two observed batch levels")

    # --- advisory context ---------------------------------------------------
    table = pd.crosstab(cluster, batch)
    proportions = table.div(table.sum(axis=1), axis=0)
    association = _cramers_v(table)
    table.to_csv(context.staging_dir / "batch-cluster-counts.csv")
    proportions.to_csv(context.staging_dir / "batch-cluster-proportions.csv")
    n_batches = int(proportions.shape[1])
    layout = _figure_layout(n_batches, int(proportions.shape[0]))
    if layout["mode"] == "heatmap":
        fig, ax = plt.subplots(figsize=layout["figsize"])
        image = ax.imshow(proportions.to_numpy(), aspect="auto", cmap="viridis", vmin=0.0, vmax=1.0)
        ax.set_xticks(range(n_batches))
        ax.set_xticklabels(
            list(map(str, proportions.columns)), rotation=90, fontsize=layout["tick_fontsize"]
        )
        ax.set_yticks(range(int(proportions.shape[0])))
        ax.set_yticklabels(list(map(str, proportions.index)), fontsize=layout["tick_fontsize"])
        ax.set_title(f"{batch_key} fraction within each {cluster_key} ({n_batches} batches)")
        fig.colorbar(image, ax=ax, label="fraction within cluster")
    else:
        ax = proportions.plot(kind="bar", stacked=True, figsize=layout["figsize"])
        ax.set_ylabel("fraction within cluster")
        ax.set_title(f"{batch_key} composition by {cluster_key}")
        ax.legend(
            title=str(batch_key),
            bbox_to_anchor=(1.02, 1.0),
            loc="upper left",
            ncol=layout["legend_ncol"],
            fontsize=8,
            frameon=False,
        )
        if layout["rotate_xticks"]:
            plt.setp(ax.get_xticklabels(), rotation=90, fontsize=8)
    plt.tight_layout()
    plt.savefig(context.staging_dir / "batch-composition.png", dpi=160, bbox_inches="tight")
    plt.close("all")

    mixing: dict[str, Any] = {"status": "unavailable", "reason": "X_pca is absent"}
    if "X_pca" in adata.obsm:
        from sklearn.neighbors import NearestNeighbors

        rng = np.random.default_rng(seed)
        indices = np.arange(adata.n_obs)
        if indices.size > max_cells:
            indices = np.sort(rng.choice(indices, size=max_cells, replace=False))
        embedding = np.asarray(adata.obsm["X_pca"])[indices]
        sampled_batch = batch.iloc[indices].to_numpy()
        used = min(n_neighbors, len(indices) - 1)
        nn = NearestNeighbors(n_neighbors=used + 1).fit(embedding)
        neighbor_idx = nn.kneighbors(return_distance=False)[:, 1:]
        neighbor_batches = sampled_batch[neighbor_idx]
        same = (neighbor_batches == sampled_batch[:, None]).mean(axis=1)
        levels = sorted(map(str, batch.unique()))
        entropies = [
            float(
                -(
                    pd.Series(v).value_counts(normalize=True)
                    * pd.Series(v).value_counts(normalize=True).map(math.log)
                ).sum()
            )
            / math.log(len(levels))
            for v in neighbor_batches
        ]
        freq = batch.value_counts(normalize=True)
        mixing = {
            "status": "complete",
            "representation": "X_pca",
            "mean_same_batch_neighbor_fraction": float(same.mean()),
            "random_composition_same_batch_fraction": float((freq**2).sum()),
            "mean_normalized_batch_entropy": float(np.mean(entropies)),
            "caution": "Descriptive mixing; not an integration objective.",
        }

    # --- stage 1: sample-enriched regions -----------------------------------
    global_freq = batch.value_counts(normalize=True).to_dict()
    cluster_sizes = cluster.value_counts().to_dict()
    regions: list[dict[str, Any]] = []
    for cl in table.index:
        for bt in table.columns:
            n_region = int(table.loc[cl, bt])
            if n_region < min_cells_region:
                continue
            enr = region_enrichment(
                n_region, int(cluster_sizes[str(cl)]), float(global_freq.get(str(bt), 0.0))
            )
            if enr >= min_enrichment:
                regions.append(
                    {"cluster": str(cl), "batch": str(bt), "n_cells": n_region, "enrichment": enr}
                )
    regions.sort(key=lambda r: r["enrichment"] * r["n_cells"], reverse=True)
    regions = regions[:max_regions]
    pd.DataFrame(
        [
            {
                "cluster": r["cluster"],
                "batch": r["batch"],
                "n_cells_in_region": r["n_cells"],
                "n_cells_in_cluster": int(cluster_sizes[r["cluster"]]),
                "batch_global_fraction": float(global_freq.get(r["batch"], 0.0)),
                "fraction_within_cluster": r["n_cells"] / int(cluster_sizes[r["cluster"]]),
                "enrichment_over_baseline": r["enrichment"],
            }
            for r in regions
        ]
    ).to_csv(context.staging_dir / "sample-enriched-regions.csv", index=False)

    # --- stage 2: within-sample identity DEGs (batch held constant) ---------
    within_rows: list[dict[str, Any]] = []
    for region in regions:
        sub = adata[batch.to_numpy() == region["batch"]]
        target = cluster[batch == region["batch"]].to_numpy() == region["cluster"]
        markers = _region_markers(
            sub,
            target,
            n_top=n_identity_genes,
            min_lfc=min_lfc,
            max_padj=max_padj,
            min_frac_diff=min_frac_diff,
            sc=sc,
            np=np,
        )
        region["discriminating_genes"] = markers["discriminating_genes"]
        for record in markers["records"]:
            within_rows.append({"cluster": region["cluster"], "batch": region["batch"], **record})
    pd.DataFrame(within_rows).to_csv(
        context.staging_dir / "within-sample-identity-degs.csv", index=False
    )

    # --- stage 3: cross-sample matching on discriminating genes -------------
    region_keys = [(r["cluster"], r["batch"]) for r in regions]
    match_rows: list[dict[str, Any]] = []
    supported_edges: list[tuple[int, int, int]] = []
    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            if regions[i]["batch"] == regions[j]["batch"]:
                continue
            m = match_regions(
                regions[i]["discriminating_genes"],
                regions[j]["discriminating_genes"],
                min_shared=min_shared,
                min_jaccard=min_jaccard,
            )
            match_rows.append(
                {
                    "cluster_a": regions[i]["cluster"],
                    "batch_a": regions[i]["batch"],
                    "cluster_b": regions[j]["cluster"],
                    "batch_b": regions[j]["batch"],
                    "shared_discriminating": m["shared"],
                    "jaccard": round(m["jaccard"], 3),
                    "shared_genes": ";".join(m["shared_genes"][:15]),
                    "identity_match_supported": m["supported"],
                    "rejection_reason": "" if m["supported"] else m["reason"],
                }
            )
            if m["supported"]:
                supported_edges.append((i, j, m["shared"]))
    pd.DataFrame(match_rows).to_csv(context.staging_dir / "population-matches.csv", index=False)
    # Bound the expensive direct-DE step: keep the strongest matches only.
    supported_edges.sort(key=lambda e: e[2], reverse=True)
    de_edges = [(i, j) for (i, j, _shared) in supported_edges[:max_candidate_pairs]]
    components = _union_find_components(region_keys, [(i, j) for (i, j, _s) in supported_edges])

    # --- stage 4 + 5: direct matched comparison and recurrence --------------
    direct_rows: list[dict[str, Any]] = []
    for i, j in de_edges:
        ri, rj = regions[i], regions[j]
        mask = ((cluster == ri["cluster"]) & (batch == ri["batch"])) | (
            (cluster == rj["cluster"]) & (batch == rj["batch"])
        )
        pair = adata[mask.to_numpy()].copy()
        is_a = (cluster[mask] == ri["cluster"]).to_numpy() & (batch[mask] == ri["batch"]).to_numpy()
        pair.obs["_pair"] = np.where(is_a, "A", "B")
        if int(is_a.sum()) < 3 or int((~is_a).sum()) < 3:
            continue
        sc.tl.rank_genes_groups(
            pair, "_pair", groups=["A"], reference="B", method="wilcoxon", pts=True
        )
        frame = sc.get.rank_genes_groups_df(pair, group="A")
        # Compute detection fractions directly: scanpy omits ``pct_nz_reference`` when the
        # reference is a named group, and these fractions are the noise gate below.
        matrix = pair.X
        matrix = matrix.toarray() if hasattr(matrix, "toarray") else np.asarray(matrix)
        pct_a_all = (matrix[is_a] > 0).mean(axis=0)
        pct_b_all = (matrix[~is_a] > 0).mean(axis=0)
        gene_pos = {str(name): idx for idx, name in enumerate(pair.var_names)}
        comp = components[region_keys[i]]
        sig = frame[frame["pvals_adj"] <= max_padj]
        for _, row in sig.iterrows():
            lfc = float(row["logfoldchanges"])
            if lfc >= min_lfc:
                higher = ri["batch"]
            elif lfc <= -min_lfc:
                higher = rj["batch"]
            else:
                continue
            # Detection fractions are recorded so the model can judge effect magnitude. NOTE: a
            # naive detection-fraction gate over-filters real programs on this data, so recurrence
            # is currently NOT noise-gated -- cell-level q-values rank separation and are not
            # sample-level replication, so low-expression genes can recur by chance. Weigh
            # pct_a/pct_b and effect size when reading recurring-programs.csv.
            position = gene_pos.get(str(row["names"]))
            if position is None:
                continue
            pct_a = float(pct_a_all[position])
            pct_b = float(pct_b_all[position])
            direct_rows.append(
                {
                    "population": comp,
                    "gene": str(row["names"]),
                    "gene_class": gene_class(str(row["names"])),
                    "higher_in_batch": higher,
                    "logfoldchange": lfc,
                    "score": float(row["scores"]),
                    "pvals_adj": float(row["pvals_adj"]),
                    "pct_a": pct_a,
                    "pct_b": pct_b,
                    "cluster_a": ri["cluster"],
                    "batch_a": ri["batch"],
                    "cluster_b": rj["cluster"],
                    "batch_b": rj["batch"],
                }
            )
    pd.DataFrame(direct_rows).to_csv(
        context.staging_dir / "direct-matched-region-degs.csv", index=False
    )
    recurring = summarize_recurrence(direct_rows)
    pd.DataFrame(recurring).to_csv(context.staging_dir / "recurring-programs.csv", index=False)

    # --- stage 6: design / confounding --------------------------------------
    confounding_rows: list[dict[str, Any]] = []
    confounded_columns: list[str] = []
    has_orthogonal = False
    for column in condition_keys:
        if column not in adata.obs:
            confounding_rows.append({"column": column, "status": "absent"})
            continue
        raw = adata.obs[column]
        n_missing = int(raw.isna().sum())
        values = raw.astype(str)
        ctab = pd.crosstab(batch, values)
        perfectly_confounded = bool(((ctab > 0).sum(axis=1) == 1).all())
        assoc = _cramers_v(ctab)
        # Record how batch levels map onto condition levels so a reader can see the design, not
        # just a scalar association.
        mapping = {
            str(level): sorted(map(str, row[row > 0].index)) for level, row in ctab.iterrows()
        }
        confounding_rows.append(
            {
                "column": column,
                "status": "present",
                "perfectly_confounded": perfectly_confounded,
                "cramers_v": assoc,
                "n_missing": n_missing,
                "n_condition_levels": int(values.nunique()),
                "batch_to_condition_levels": json.dumps(mapping, sort_keys=True),
            }
        )
        if perfectly_confounded:
            confounded_columns.append(column)
        elif assoc < 0.8:
            has_orthogonal = True
    pd.DataFrame(confounding_rows).to_csv(
        context.staging_dir / "design-confounding.csv", index=False
    )

    n_matched_with_diffs = len({r["population"] for r in direct_rows})
    n_recurring_populations = max((r["n_populations"] for r in recurring), default=0)
    gene_evidence = classify_gene_evidence(n_matched_with_diffs, n_recurring_populations)
    design_interpretation = classify_design(
        confounded_columns=confounded_columns,
        technical_documented=technical_documented,
        has_orthogonal_condition=has_orthogonal,
    )
    recommendation = recommend(gene_evidence, design_interpretation)

    match_summary = [
        {"a": list(region_keys[i]), "b": list(region_keys[j]), "shared": s}
        for (i, j, s) in supported_edges
    ]
    evidence = {
        "schema_version": BATCH_EVIDENCE_SCHEMA,
        "status": "complete",
        "batch_key": batch_key,
        "cluster_key": cluster_key,
        "n_batches": int(batch.nunique()),
        "n_clusters": int(cluster.nunique()),
        "gene_evidence": gene_evidence,
        "design_interpretation": design_interpretation,
        "recommendation": recommendation,
        "n_enriched_regions": len(regions),
        "n_supported_matches": len(supported_edges),
        "n_direct_compared_pairs": len(de_edges),
        "n_recurring_programs": len(recurring),
        "recurring_programs": recurring[:20],
        "confounding": confounding_rows,
        "confounded_columns": confounded_columns,
        "technical_batch_documented": technical_documented,
        "technical_batch_basis": technical_basis,
        "de_engine": DE_ENGINE,
        "gene_class_version": GENE_CLASS_VERSION,
        "advisory": {"cramers_v": association, "mixing": mixing},
        "figure_mode": layout["mode"],
        "effective_parameters": effective_parameters,
        "artifact_path": f"artifacts/capabilities/{context.execution_id}/batch-evidence.json",
        **ident,
    }
    evidence["evidence_id"] = _identity(
        "batch-evidence",
        {
            "schema": BATCH_EVIDENCE_SCHEMA,
            "gene_evidence": gene_evidence,
            "design_interpretation": design_interpretation,
            "recommendation": recommendation,
            "batch_key": batch_key,
            "identities": {k: ident[k] for k in ident},
            "effective_parameters": effective_parameters,
            "technical_batch_documented": technical_documented,
            "technical_batch_basis": technical_basis,
            "regions": region_keys,
            "matches": match_summary,
            "recurring": [(r["gene"], r["higher_in_batch"], r["n_populations"]) for r in recurring],
            "gene_class_version": GENE_CLASS_VERSION,
        },
    )
    (context.staging_dir / "batch-evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    _write_evidence_report(context.staging_dir / "batch-investigation.md", evidence)

    artifacts = [
        {
            "name": "batch-report",
            "relative_path": "batch-investigation.md",
            "media_type": "text/markdown",
        },
        {
            "name": "batch-evidence",
            "relative_path": "batch-evidence.json",
            "media_type": "application/json",
        },
        {
            "name": "batch-cluster-counts",
            "relative_path": "batch-cluster-counts.csv",
            "media_type": "text/csv",
        },
        {
            "name": "batch-cluster-proportions",
            "relative_path": "batch-cluster-proportions.csv",
            "media_type": "text/csv",
        },
        {
            "name": "sample-enriched-regions",
            "relative_path": "sample-enriched-regions.csv",
            "media_type": "text/csv",
        },
        {
            "name": "within-sample-identity-degs",
            "relative_path": "within-sample-identity-degs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "population-matches",
            "relative_path": "population-matches.csv",
            "media_type": "text/csv",
        },
        {
            "name": "direct-matched-region-degs",
            "relative_path": "direct-matched-region-degs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "recurring-programs",
            "relative_path": "recurring-programs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "design-confounding",
            "relative_path": "design-confounding.csv",
            "media_type": "text/csv",
        },
        {
            "name": "batch-composition",
            "relative_path": "batch-composition.png",
            "media_type": "image/png",
        },
    ]
    return {
        "summary": (
            f"Batch evidence for {batch_key!r}: gene_evidence={gene_evidence}, "
            f"design={design_interpretation}, recommendation={recommendation} "
            f"({len(regions)} regions, {len(supported_edges)} matches, {len(recurring)} recurring)."
        ),
        "details": evidence,
        "facts_patch": {
            "analysis": analysis_patch,
            "batch": {"evidence": evidence, "decision": None},
        },
        "artifacts": artifacts,
        "model_media": [a for a in artifacts if a["media_type"].startswith("image/")],
    }


def _write_evidence_report(path: Path, evidence: dict[str, Any]) -> None:
    lines = [
        "# Batch investigation (gene-first evidence)",
        "",
        f"- Batch key: `{evidence['batch_key']}`",
        f"- Gene evidence: **{evidence['gene_evidence']}**",
        f"- Design interpretation: **{evidence['design_interpretation']}**"
        + (
            f" (confounded columns: {evidence.get('confounded_columns')})"
            if evidence.get("confounded_columns")
            else ""
        ),
        f"- Recommendation (non-binding): **{evidence['recommendation']}**",
        f"- Enriched regions: {evidence['n_enriched_regions']}; supported matches: "
        f"{evidence['n_supported_matches']}; direct pairs: "
        f"{evidence.get('n_direct_compared_pairs')}; recurring: {evidence['n_recurring_programs']}",
        f"- DE engine: {evidence['de_engine']}; gene-class: {evidence['gene_class_version']}",
        "",
        "**Recurrence is a legacy-compatible advisory signal computed from cell-level Wilcoxon "
        "tests.** It is NOT biological replication and may contain low-expression or "
        "compositional false positives: with many cells, small random differences reach "
        "significance. Weigh `pct_a`/`pct_b`, effect size, and gene class in "
        "`recurring-programs.csv` and `direct-matched-region-degs.csv` before treating a "
        "recurring program as real.",
        "",
        "Gene-level, within-sample identity DEGs are primary; matches use DISCRIMINATING "
        "genes only (broad/stress/housekeeping and nuisance genes cannot manufacture a "
        "match). Composition, Cramér's V, and neighborhood mixing are advisory context. A "
        "matched identity plus a direct gene list does NOT prove a technical batch effect; "
        "cell-level q-values rank separation and are not sample-level replication. The "
        "verdict is advisory — the decision is recorded separately, and a confounded design "
        "is never silently reclassified as technical.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_decision(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    evidence_id = str(arguments["evidence_id"])
    decision = str(arguments["decision"])
    rationale = str(arguments["rationale"]).strip()
    integration_basis = arguments.get("integration_basis")
    override_warning = arguments.get("override_warning")
    if not rationale:
        raise ValueError("rationale must not be empty")

    batch = context.state_facts.get("batch")
    evidence = batch.get("evidence") if isinstance(batch, dict) else None
    if not isinstance(evidence, dict) or evidence.get("status") not in (
        "complete",
        "not_applicable",
    ):
        raise ValueError("no current batch evidence is available; run investigate_batch first")
    if evidence.get("evidence_id") != evidence_id:
        raise ValueError("evidence_id does not match the current batch evidence")
    if decision == "not_applicable" and evidence.get("status") != "not_applicable":
        raise ValueError("not_applicable decision requires not_applicable evidence")

    recommendation = str(evidence.get("recommendation"))
    basis_verdict = validate_integration_basis(integration_basis, evidence)
    if not basis_verdict["ok"]:
        raise ValueError(basis_verdict["violation"])
    verdict = validate_decision(decision, recommendation, integration_basis, override_warning)
    if not verdict["ok"]:
        raise ValueError(verdict["violation"])

    decision_fact = {
        "decision": decision,
        "rationale": rationale,
        "evidence_id": evidence_id,
        "integration_basis": integration_basis,
        "override_warning": override_warning,
        "recommendation": recommendation,
        "validated": True,
        "decision_policy_version": DECISION_POLICY_VERSION,
        "cell_set_id": evidence.get("cell_set_id"),
        "count_representation_id": evidence.get("count_representation_id"),
        "representation_id": evidence.get("representation_id"),
        "clustering_id": evidence.get("clustering_id"),
    }
    (context.staging_dir / "batch-decision.md").write_text(
        f"# Batch handling decision\n\n- Decision: **{decision}**\n- Recommendation: "
        f"{recommendation}\n- Evidence: `{evidence_id}`\n- Integration basis: {integration_basis}\n"
        + (f"- Override warning: {override_warning}\n" if override_warning else "")
        + f"\n## Rationale\n\n{rationale}\n",
        encoding="utf-8",
    )
    return {
        "summary": f"Recorded batch decision {decision!r} (recommendation {recommendation!r}).",
        "details": decision_fact,
        "facts_patch": {"batch": {"decision": decision_fact}},
        "decisions_patch": {"batch_handling": {"decision": decision, "rationale": rationale}},
        "artifacts": [
            {
                "name": "batch-decision",
                "relative_path": "batch-decision.md",
                "media_type": "text/markdown",
            }
        ],
    }
