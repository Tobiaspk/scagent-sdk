"""Three-axis cluster QC with bounded convergent auto-cleanup, tied to scientific identities.

Axis A (metric QC): robust per-cluster severity from QC covariates and separation.
Axis B (DEG identity): whether a cluster carries a discriminating cell-identity program.
Axis C (covariance/coherence): within-cluster gene-gene correlation structure + heatmaps.
Technical Moran's I for mitochondrial fraction and library size localizes technical pockets;
it is never cell-type evidence.

A cluster is auto-removed only when all three axes converge on junk (metric-adverse AND
junk markers AND unstructured/weak covariance) and the total removal stays below a bounded
fraction. A missing or inconclusive axis never counts as agreement. Conflicts are preserved
for review, never removed automatically.

Pure classifiers live at module scope so they can be unit-tested without Scanpy/GPU. ``run``
orchestrates the compute, renders artifacts, and (only on convergent evidence) performs a
bounded cell-set mutation with fresh identities and downstream invalidation.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

# Evidence schema version. A strengthened ``current_cluster_qc`` floor requires this exact
# version, so pre-restoration attestations fail closed and force a rerun of the restored QC.
CLUSTER_QC_EVIDENCE_SCHEMA = 3

# --- versioned gene classification -------------------------------------------
GENE_CLASS_VERSION = "cluster-qc-gene-class-v1"
_NUISANCE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^MT-",  # mitochondrial
        r"^MRP[LS]\d",  # mitochondrial ribosomal
        r"^RP[LS]\d",  # cytoplasmic ribosomal
        r"^MALAT1$",
        r"^NEAT1$",
        r"^HB[ABDEGMQZ]$",  # hemoglobin chains
        r"^HBA\d$",
        r"^LINC\d",  # long intergenic non-coding
        r"^A[CL]\d{6}\.",  # clone-based locus identifiers
        r"\.\d+$",  # versioned locus suffixes
    )
)

_BROAD_PATTERNS = tuple(re.compile(pattern) for pattern in (r"^HLA-", r"^HSP", r"^HIST\d"))

# Real but insufficiently specific programs: activation/stress/housekeeping/cell cycle.
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
    }
)


def _read_matrix(path):
    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)


def _write_matrix(adata, path):
    """Write an AnnData artifact: a .zarr store (blosc) or a gzipped .h5ad file (ADR 0011)."""
    import anndata as ad

    if str(path).endswith(".zarr"):
        ad.settings.zarr_write_format = 2  # v2 until the v3 sharding/dedup design lands
        adata.write_zarr(path)
    else:
        adata.write_h5ad(path, compression="gzip")

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


# --- Axis A: metric severity -------------------------------------------------
def classify_metric_severity(
    signals: dict[str, Any], *, warning_z: float, extreme_z: float
) -> dict[str, Any]:
    """Classify a cluster's QC severity from independent adverse signals.

    No single signal produces ``obvious`` on its own: ``obvious`` needs two independent
    moderate signals or one extreme degradation signal; a lone moderate signal is
    ``ambiguous``; none is ``clean``. ``signals`` may carry any subset of ``mt_z``,
    ``ribo_z``, ``lib_z`` (signed; low is negative), ``genes_z`` (signed), and the booleans
    ``doublet_enriched``, ``silhouette_negative``, ``tiny``.
    """
    adverse: list[str] = []
    extreme: list[str] = []

    def consider(name: str, value: Any, *, low: bool) -> None:
        if value is None:
            return
        moderate = value <= -warning_z if low else value >= warning_z
        severe = value <= -extreme_z if low else value >= extreme_z
        if moderate:
            adverse.append(name)
        if severe:
            extreme.append(name)

    consider("high_mitochondrial_fraction", signals.get("mt_z"), low=False)
    consider("high_ribosomal_fraction", signals.get("ribo_z"), low=False)
    consider("low_library_size", signals.get("lib_z"), low=True)
    consider("low_detected_genes", signals.get("genes_z"), low=True)
    if signals.get("doublet_enriched"):
        adverse.append("predicted_doublet_enrichment")
    if signals.get("silhouette_negative"):
        adverse.append("negative_mean_silhouette")
    if signals.get("tiny"):
        adverse.append("tiny_cluster")

    unique = sorted(set(adverse))
    if not unique:
        severity = "clean"
    elif extreme or len(unique) >= 2:
        severity = "obvious"
    else:
        severity = "ambiguous"
    return {
        "severity": severity,
        "adverse_signals": unique,
        "extreme_signals": sorted(set(extreme)),
    }


# --- Axis B: DEG identity ----------------------------------------------------
def classify_deg_identity(
    genes: list[dict[str, Any]], *, min_discriminating: int = 2, min_frac_diff: float = 0.1
) -> dict[str, Any]:
    """Decide whether a cluster carries a discriminating identity program.

    ``genes`` are significant positive DEGs, each ``{name, frac_diff?, significant?}``. A
    discriminating gene is non-nuisance, non-broad, and (when detection fractions are known)
    separates the cluster by at least ``min_frac_diff``. Zero discriminating genes is
    ``junk_markers``; fewer than ``min_discriminating`` is ``inconclusive``.
    """
    discriminating: list[str] = []
    nuisance: list[str] = []
    broad: list[str] = []
    for gene in genes:
        name = str(gene["name"])
        cls = gene_class(name)
        if cls == "nuisance":
            nuisance.append(name)
        elif cls == "broad":
            broad.append(name)
        else:
            frac_diff = gene.get("frac_diff")
            if frac_diff is None or frac_diff >= min_frac_diff:
                discriminating.append(name)
            else:
                broad.append(name)  # real gene, but too weakly separating to discriminate
    n_significant = sum(1 for gene in genes if gene.get("significant"))
    if len(discriminating) >= min_discriminating:
        verdict = "identity_supported"
    elif not discriminating:
        verdict = "junk_markers"
    else:
        verdict = "inconclusive"
    return {
        "verdict": verdict,
        "discriminating": discriminating[:12],
        "nuisance": nuisance[:12],
        "broad": broad[:12],
        "n_discriminating": len(discriminating),
        "n_significant": n_significant,
    }


# --- Axis C: covariance / coherence ------------------------------------------
def classify_structure(
    mean_abs_corr: float | None,
    frac_high_corr: float | None,
    n_cells: int,
    n_genes: int,
    *,
    min_cells: int,
    min_genes: int = 10,
) -> dict[str, Any]:
    """Classify within-cluster gene-gene correlation structure.

    Thresholds are legacy-compatibility starting points, not validated universal constants;
    they are recorded in artifacts and must be swept across datasets before being called mature.
    """
    if (
        mean_abs_corr is None
        or frac_high_corr is None
        or n_cells < min_cells
        or n_genes < min_genes
    ):
        return {"label": "inconclusive", "reason": "insufficient cells/genes or no correlation"}
    if mean_abs_corr < 0.08 and frac_high_corr < 0.05:
        label = "unstructured"
    elif mean_abs_corr < 0.12:
        label = "weak"
    elif mean_abs_corr < 0.18:
        label = "moderate"
    else:
        label = "strong"
    return {"label": label, "reason": "correlation-structure thresholds"}


# --- synthesis ---------------------------------------------------------------
def synthesize_decision(
    metric_severity: str, deg_verdict: str, structure_label: str
) -> dict[str, str]:
    """Combine the three axes into one decision. Only ``confirmed_junk`` may auto-remove.

    A missing/inconclusive axis never counts as agreement, so it can only yield review/keep.
    """
    adverse = metric_severity in ("ambiguous", "obvious")
    junk = deg_verdict == "junk_markers"
    identity = deg_verdict == "identity_supported"
    weak = structure_label in ("unstructured", "weak")
    strong = structure_label in ("moderate", "strong")
    axis_inconclusive = deg_verdict == "inconclusive" or structure_label == "inconclusive"

    if adverse and junk and weak:
        return {"synthesis": "confirmed_junk", "action": "remove"}
    if metric_severity == "clean" and junk and weak:
        return {"synthesis": "unstructured_junk_markers", "action": "review"}
    if identity and strong:
        return {"synthesis": "structured_identity", "action": "keep"}
    if junk and strong:
        return {"synthesis": "junk_markers_but_structured", "action": "review"}
    if identity and weak:
        # Weak covariance is common in valid small, transitional, or trajectory-associated
        # populations. Once identity DEGs are supported, it is not an independent defect and
        # should not manufacture a manual-review queue.
        return {"synthesis": "identity_without_structure", "action": "keep"}
    if axis_inconclusive:
        return {"synthesis": "inconclusive", "action": "review"}
    return {"synthesis": "conflicting", "action": "review"}


def select_cleanup_set(
    decisions: list[dict[str, Any]],
    sizes: dict[str, int],
    total_cells: int,
    *,
    auto_remove: bool,
    max_fraction: float,
) -> dict[str, Any]:
    """Select the bounded convergent-junk removal set.

    A removal strictly below ``max_fraction`` is applied; a removal at or above it is held
    for review. The set is never partially trimmed to fit under the bound.
    """
    confirmed = sorted(
        str(row["cluster"]) for row in decisions if row["synthesis"] == "confirmed_junk"
    )
    removed_cells = sum(int(sizes.get(cluster, 0)) for cluster in confirmed)
    fraction = removed_cells / total_cells if total_cells else 0.0
    applied = bool(auto_remove and confirmed and 0.0 < fraction < max_fraction)
    held_reason: str | None = None
    if confirmed and not applied:
        if not auto_remove:
            held_reason = "auto-removal disabled"
        elif fraction >= max_fraction:
            held_reason = (
                f"convergent removal fraction {fraction:.1%} is at or above the "
                f"{max_fraction:.0%} review threshold"
            )
    return {
        "confirmed_junk": confirmed,
        "removed_cells": removed_cells,
        "removal_fraction": fraction,
        "applied": applied,
        "held_reason": held_reason,
    }


def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _safe_group(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return token or "clusters"


def _dataset_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    # A .zarr artifact is a store directory (ADR 0011): hash each member by its in-store path and
    # content rather than open() the directory (which raises IsADirectoryError). A plain file hashes
    # exactly as before.
    if path.is_dir():
        members = sorted(item for item in path.rglob("*") if item.is_file())
    else:
        members = [path]
    for member in members:
        if path.is_dir():
            digest.update(f"{member.relative_to(path).as_posix()}\0".encode())
        with member.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _recorded_path(context: Any, value: str) -> Path:
    """Resolve a state-recorded artifact path (absolute, or relative to the session dir)."""
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(context.session_dir) / candidate
    return candidate.expanduser().resolve()


# --- orchestration -----------------------------------------------------------
def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: C901 - orchestration
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import scanpy as sc
    from scipy.cluster.hierarchy import leaves_list, linkage
    from sklearn.metrics import silhouette_samples

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    cluster_key = str(arguments.get("cluster_key", "leiden"))
    min_cells = int(arguments.get("min_cluster_cells", 20))
    # Exact silhouette is quadratic in sampled cells. Twenty thousand cells implies roughly
    # 400 million pair distances and dominated the live QC runtime. A deterministic 3k sample is
    # ample for a cluster-separation warning while reducing that work by about 44x.
    max_cells_sil = int(arguments.get("max_cells_for_silhouette", 3000))
    doublet_ratio = float(arguments.get("doublet_enrichment_ratio", 2.0))
    doublet_floor = float(arguments.get("doublet_rate_floor", 0.1))
    n_structure_genes = int(arguments.get("n_structure_genes", 150))
    min_cells_structure = int(arguments.get("min_cells_for_structure", 15))
    moran_min_cells = int(arguments.get("moran_min_cells", 40))
    corr_threshold = float(arguments.get("corr_threshold", 0.3))
    max_heatmaps_arg = arguments.get("max_heatmaps")
    max_heatmaps = int(max_heatmaps_arg) if max_heatmaps_arg is not None else None
    warning_z = float(arguments.get("metric_warning_z", 2.0))
    extreme_z = float(arguments.get("metric_extreme_z", 3.0))
    auto_remove = bool(arguments.get("auto_remove_convergent", False))
    max_fraction = float(arguments.get("auto_remove_max_fraction", 0.2))
    min_lfc = float(arguments.get("min_logfoldchange", 0.25))
    max_padj = float(arguments.get("max_adjusted_pvalue", 0.05))
    max_cells_structure = int(arguments.get("max_cells_for_structure", 3000))
    seed = int(arguments.get("random_seed", 0))

    adata = _read_matrix(path)
    if cluster_key not in adata.obs:
        raise ValueError(f"cluster key {cluster_key!r} is absent")
    provenance = adata.uns.get("scagent_sdk", {})
    cell_set_id = provenance.get("cell_set_id") or _identity(
        "cells", sorted(map(str, adata.obs_names))
    )
    representation_id = provenance.get("representation_id") or _identity(
        "representation",
        {
            "input_path": str(path),
            "key": "X_pca",
            "shape": list(map(int, adata.obsm["X_pca"].shape))
            if "X_pca" in adata.obsm
            else None,
        },
    )
    clustering_id = provenance.get("clustering_id") or _identity(
        "clustering",
        {
            "representation_id": representation_id,
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
    count_representation_id = provenance.get("count_representation_id") or _identity(
        "count-representation",
        {
            "input_path": str(path),
            "source": "counts" if "counts" in adata.layers else "not-recorded",
            "shape": [int(adata.n_obs), int(adata.n_vars)],
        },
    )
    ident = {
        "clustering_id": clustering_id,
        "cell_set_id": cell_set_id,
        "representation_id": representation_id,
        "count_representation_id": count_representation_id,
    }

    labels = adata.obs[cluster_key].astype(str)
    sizes = labels.value_counts().sort_index()
    size_map = {str(k): int(v) for k, v in sizes.items()}
    total_cells = int(adata.n_obs)
    warnings: list[str] = []

    # --- separation (silhouette) --------------------------------------------
    if "X_pca" not in adata.obsm:
        raise ValueError("X_pca is required for cluster separation QC")
    rng = np.random.default_rng(seed)
    indices = np.arange(adata.n_obs)
    if indices.size > max_cells_sil:
        indices = np.sort(rng.choice(indices, size=max_cells_sil, replace=False))
    sampled_labels = labels.iloc[indices].to_numpy()
    if np.unique(sampled_labels).size < 2:
        raise ValueError("at least two clusters are required for silhouette QC")
    sil = silhouette_samples(np.asarray(adata.obsm["X_pca"])[indices], sampled_labels)
    sil_mean = (
        pd.DataFrame({"cluster": sampled_labels, "silhouette": sil})
        .groupby("cluster", observed=True)["silhouette"]
        .mean()
    )

    # --- metric covariates: robust z of cluster median vs across-cell -------
    metric_cols = {
        "pct_counts_mt": "mt",
        "pct_counts_ribo": "ribo",
        "total_counts": "lib",
        "n_genes_by_counts": "genes",
    }
    robust: dict[str, dict[str, float]] = {}
    for column, short in metric_cols.items():
        if column not in adata.obs:
            continue
        values = pd.to_numeric(adata.obs[column], errors="coerce")
        center = float(values.median())
        mad = float((values - center).abs().median())
        scale = 1.4826 * mad if mad > 0 else float(values.std(ddof=0)) or 1.0
        cluster_median = values.groupby(labels, observed=True).median()
        robust[short] = {
            str(cl): (float(val) - center) / scale for cl, val in cluster_median.items()
        }

    # --- doublet enrichment --------------------------------------------------
    doublet_signal_missing = "predicted_doublet" not in adata.obs
    enriched_doublet: set[str] = set()
    global_doublet_rate: float | None = None
    if not doublet_signal_missing:
        calls = adata.obs["predicted_doublet"].astype(bool)
        global_doublet_rate = float(calls.mean())
        threshold = max(doublet_floor, global_doublet_rate * doublet_ratio)
        per_cluster = calls.groupby(labels, observed=True).mean()
        enriched_doublet = {
            str(cl)
            for cl, rate in per_cluster.items()
            if float(rate) >= threshold and size_map.get(str(cl), 0) >= min_cells
        }
    else:
        warnings.append("doublet signal is absent; predicted-call enrichment cannot be assessed")

    # --- DEGs ----------------------------------------------------------------
    rgg = adata.uns.get("rank_genes_groups")
    if not rgg or rgg.get("params", {}).get("groupby") != cluster_key:
        sc.tl.rank_genes_groups(adata, cluster_key, method="wilcoxon", pts=True)
    deg = sc.get.rank_genes_groups_df(adata, group=None)
    has_pts = "pct_nz_group" in deg.columns and "pct_nz_reference" in deg.columns

    # --- structure gene pool -------------------------------------------------
    hv_mask = (
        adata.var["highly_variable"].to_numpy()
        if "highly_variable" in adata.var
        else np.ones(adata.n_vars, dtype=bool)
    )
    non_nuisance = np.array([gene_class(name) != "nuisance" for name in adata.var_names])
    structure_pool = np.where(hv_mask & non_nuisance)[0]
    if structure_pool.size < 10:
        structure_pool = np.where(non_nuisance)[0]

    output_prefix = f"cluster-qc/{_safe_group(cluster_key)}"
    heatmap_dir = context.staging_dir / output_prefix / "cluster-structure"

    # --- technical Moran's I (global + per-cell local, variance-normalized) --
    connect = adata.obsp.get("connectivities") if hasattr(adata, "obsp") else None
    moran_local: dict[str, np.ndarray] = {}
    moran_global: dict[str, float | None] = {}
    moran_skipped: dict[str, str] = {}
    if connect is None:
        moran_skipped["graph"] = "neighbor connectivities graph is absent"
    else:
        rowsum = np.asarray(connect.sum(axis=1)).ravel()
        rowsum[rowsum == 0] = 1.0
        w_sum = float(connect.sum())
        for column, short in (("pct_counts_mt", "mt"), ("total_counts", "lib")):
            if column not in adata.obs:
                moran_skipped[short] = f"{column} covariate is absent"
                continue
            vec = pd.to_numeric(adata.obs[column], errors="coerce").to_numpy(dtype=float)
            if short == "lib":
                vec = np.log1p(vec)
            z = vec - vec.mean()
            # Variance (second moment) m2 = sum(z^2)/n normalizes local Moran's I so the
            # per-cell values are scale-invariant (Anselin 1995), matching global Moran's I.
            m2 = float((z * z).mean())
            neighbor = np.asarray(connect @ z).ravel()
            if m2 <= 0 or w_sum <= 0:
                moran_skipped[short] = f"{column} has zero variance"
                continue
            moran_local[short] = (z * (neighbor / rowsum)) / m2
            moran_global[short] = (z.size / w_sum) * (float(z @ neighbor) / (m2 * z.size))

    # --- per-cluster evidence ------------------------------------------------
    metric_rows: list[dict[str, Any]] = []
    deg_rows: list[dict[str, Any]] = []
    structure_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    heatmap_artifacts: list[dict[str, str]] = []
    rendered_heatmaps = 0

    for cluster in sizes.index.astype(str):
        n_cluster = size_map[cluster]
        cluster_mask = (labels == cluster).to_numpy()

        # Axis A
        mt_z = robust.get("mt", {}).get(cluster)
        ribo_z = robust.get("ribo", {}).get(cluster)
        lib_z = robust.get("lib", {}).get(cluster)
        genes_z = robust.get("genes", {}).get(cluster)
        sil_c = float(sil_mean.get(cluster, float("nan")))
        metric = classify_metric_severity(
            {
                "mt_z": mt_z,
                "ribo_z": ribo_z,
                "lib_z": lib_z,
                "genes_z": genes_z,
                "doublet_enriched": cluster in enriched_doublet,
                "silhouette_negative": (sil_c == sil_c) and sil_c < 0,
                "tiny": n_cluster < min_cells,
            },
            warning_z=warning_z,
            extreme_z=extreme_z,
        )
        metric_rows.append(
            {
                "cluster": cluster,
                "n_cells": n_cluster,
                "mt_z": mt_z,
                "ribo_z": ribo_z,
                "lib_z": lib_z,
                "genes_z": genes_z,
                "mean_silhouette": None if sil_c != sil_c else sil_c,
                "doublet_enriched": cluster in enriched_doublet,
                "severity": metric["severity"],
                "adverse_signals": ";".join(metric["adverse_signals"]),
            }
        )

        # Axis B
        cluster_deg = deg[deg["group"].astype(str) == cluster]
        positive = cluster_deg[
            (cluster_deg["logfoldchanges"] >= min_lfc) & (cluster_deg["pvals_adj"] <= max_padj)
        ].sort_values("scores", ascending=False)
        gene_records: list[dict[str, Any]] = []
        for _, gene_row in positive.head(40).iterrows():
            frac_diff = (
                float(gene_row["pct_nz_group"] - gene_row["pct_nz_reference"]) if has_pts else None
            )
            gene_records.append(
                {"name": str(gene_row["names"]), "frac_diff": frac_diff, "significant": True}
            )
        deg_identity = classify_deg_identity(gene_records)
        deg_rows.append(
            {
                "cluster": cluster,
                "verdict": deg_identity["verdict"],
                "n_discriminating": deg_identity["n_discriminating"],
                "n_significant": deg_identity["n_significant"],
                "discriminating": ";".join(deg_identity["discriminating"]),
                "nuisance": ";".join(deg_identity["nuisance"]),
                "broad": ";".join(deg_identity["broad"]),
            }
        )

        # Axis C
        mean_abs_corr: float | None = None
        frac_high_corr: float | None = None
        n_struct_genes = 0
        heatmap_rel: str | None = None
        if n_cluster >= min_cells_structure and structure_pool.size >= 10:
            cell_idx = np.where(cluster_mask)[0]
            if cell_idx.size > max_cells_structure:
                cell_idx = np.sort(rng.choice(cell_idx, size=max_cells_structure, replace=False))
            block = adata[cell_idx][:, structure_pool].X
            block = np.asarray(block.todense()) if hasattr(block, "todense") else np.asarray(block)
            variances = block.var(axis=0)
            keep = np.where(variances > 0)[0]
            if keep.size > n_structure_genes:
                keep = keep[np.argsort(variances[keep])[::-1][:n_structure_genes]]
            n_struct_genes = int(keep.size)
            if keep.size >= 10:
                sub = block[:, keep]
                corr = np.corrcoef(sub, rowvar=False)
                corr = np.nan_to_num(corr, nan=0.0)
                offdiag = corr[~np.eye(corr.shape[0], dtype=bool)]
                mean_abs_corr = float(np.abs(offdiag).mean())
                frac_high_corr = float((np.abs(offdiag) >= corr_threshold).mean())
                if max_heatmaps is None or rendered_heatmaps < max_heatmaps:
                    heatmap_rel = _render_heatmap(
                        corr,
                        structure_pool[keep],
                        adata.var_names,
                        cluster,
                        heatmap_dir,
                        output_prefix,
                        plt,
                        np,
                        linkage,
                        leaves_list,
                    )
                    if heatmap_rel is not None:
                        rendered_heatmaps += 1
                        heatmap_artifacts.append(
                            {
                                "name": f"cluster-structure-{cluster}",
                                "relative_path": heatmap_rel,
                                "media_type": "image/png",
                            }
                        )
        structure = classify_structure(
            mean_abs_corr, frac_high_corr, n_cluster, n_struct_genes, min_cells=min_cells_structure
        )
        moran_mt = float(moran_local["mt"][cluster_mask].mean()) if "mt" in moran_local else None
        moran_lib = float(moran_local["lib"][cluster_mask].mean()) if "lib" in moran_local else None
        structure_rows.append(
            {
                "cluster": cluster,
                "n_structure_genes": n_struct_genes,
                "mean_abs_corr": mean_abs_corr,
                "frac_pairs_above_threshold": frac_high_corr,
                "structure": structure["label"],
                "moran_local_mt": moran_mt if n_cluster >= moran_min_cells else None,
                "moran_local_lib": moran_lib if n_cluster >= moran_min_cells else None,
                "heatmap_path": (
                    f"{context.artifact_relative_path}/{heatmap_rel}"
                    if heatmap_rel
                    else None
                ),
            }
        )

        # synthesis
        decision = synthesize_decision(
            metric["severity"], deg_identity["verdict"], structure["label"]
        )
        reasons = [
            f"metric={metric['severity']}({','.join(metric['adverse_signals']) or 'none'})",
            f"deg={deg_identity['verdict']}",
            f"covariance={structure['label']}",
        ]
        decision_rows.append(
            {
                "cluster": cluster,
                "n_cells": n_cluster,
                "metric_severity": metric["severity"],
                "deg_verdict": deg_identity["verdict"],
                "structure": structure["label"],
                "synthesis": decision["synthesis"],
                "action": decision["action"],
                "reasons": " | ".join(reasons),
            }
        )

    # --- decision synthesis --------------------------------------------------
    cleanup = select_cleanup_set(
        decision_rows, size_map, total_cells, auto_remove=auto_remove, max_fraction=max_fraction
    )
    tiny_clusters = [c for c, n in size_map.items() if n < min_cells]
    if tiny_clusters:
        warnings.append("clusters below minimum size: " + ", ".join(sorted(tiny_clusters)))
    if enriched_doublet:
        warnings.append(
            "clusters enriched for Scrublet predicted calls (review, not auto-removed): "
            + ", ".join(sorted(enriched_doublet))
        )
    review_clusters = [row["cluster"] for row in decision_rows if row["action"] == "review"]
    if review_clusters:
        warnings.append("clusters flagged for review: " + ", ".join(sorted(review_clusters)))
    if cleanup["held_reason"]:
        warnings.append(
            f"convergent-junk clusters held for review ({cleanup['held_reason']}): "
            + ", ".join(cleanup["confirmed_junk"])
        )

    # --- write evidence artifacts -------------------------------------------
    evidence_dir = context.staging_dir / output_prefix
    evidence_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(decision_rows).to_csv(
        evidence_dir / "cluster-qc-decision-table.csv", index=False
    )
    pd.DataFrame(metric_rows).to_csv(
        evidence_dir / "cluster-metric-evidence.csv", index=False
    )
    pd.DataFrame(deg_rows).to_csv(evidence_dir / "cluster-deg-identity.csv", index=False)
    pd.DataFrame(structure_rows).to_csv(
        evidence_dir / "cluster-structure-evidence.csv", index=False
    )

    metric_figure = _render_metric_boxplots(
        adata,
        labels,
        decision_rows,
        cluster_key,
        context,
        output_prefix,
        plt,
        pd,
    )
    umap_media = _render_umap(adata, cluster_key, context, output_prefix, plt, sc)

    effective_parameters = {
        "cluster_key": cluster_key,
        "min_cluster_cells": min_cells,
        "max_cells_for_silhouette": max_cells_sil,
        "doublet_enrichment_ratio": doublet_ratio,
        "doublet_rate_floor": doublet_floor,
        "n_structure_genes": n_structure_genes,
        "min_cells_for_structure": min_cells_structure,
        "max_cells_for_structure": max_cells_structure,
        "moran_min_cells": moran_min_cells,
        "corr_threshold": corr_threshold,
        "max_heatmaps": max_heatmaps,
        "metric_warning_z": warning_z,
        "metric_extreme_z": extreme_z,
        "min_logfoldchange": min_lfc,
        "max_adjusted_pvalue": max_padj,
        "auto_remove_convergent": auto_remove,
        "auto_remove_max_fraction": max_fraction,
        "random_seed": seed,
    }
    # The evidence id binds the decisions to the exact identities, effective parameters, and
    # gene-class version that produced them, so two differently configured evaluations that happen
    # to yield the same decision table cannot collide on one id.
    evidence_id = _identity(
        "cluster-qc-evidence",
        {
            "schema": CLUSTER_QC_EVIDENCE_SCHEMA,
            "cell_set_id": ident["cell_set_id"],
            "count_representation_id": ident["count_representation_id"],
            "representation_id": ident["representation_id"],
            "clustering_id": ident["clustering_id"],
            "decisions": decision_rows,
            "effective_parameters": effective_parameters,
            "gene_class_version": GENE_CLASS_VERSION,
        },
    )

    common_details = {
        "schema_version": CLUSTER_QC_EVIDENCE_SCHEMA,
        "evidence_id": evidence_id,
        "clustering_id": ident["clustering_id"],
        "cell_set_id": ident["cell_set_id"],
        "representation_id": ident["representation_id"],
        "count_representation_id": ident["count_representation_id"],
        "n_clusters": int(sizes.size),
        "n_cells_sampled_for_silhouette": int(len(indices)),
        "gene_class_version": GENE_CLASS_VERSION,
        "doublet_signal_missing": doublet_signal_missing,
        "global_predicted_doublet_rate": global_doublet_rate,
        "doublet_enriched_clusters": sorted(enriched_doublet),
        "moran_global": moran_global,
        "moran_skipped": moran_skipped,
        "decision_table": decision_rows,
        "cleanup": {
            "confirmed_junk": cleanup["confirmed_junk"],
            "removed_cells": cleanup["removed_cells"],
            "removal_fraction": cleanup["removal_fraction"],
            "applied": cleanup["applied"],
            "held_reason": cleanup["held_reason"],
        },
        "warnings": warnings,
        "output_group": output_prefix,
        "review_clusters": sorted(
            set(review_clusters)
            | (set(cleanup["confirmed_junk"]) if not cleanup["applied"] else set())
        ),
        "effective_parameters": effective_parameters,
        "thresholds": {
            "structure_bins": [0.08, 0.12, 0.18],
        },
    }
    (evidence_dir / "cluster-qc-evidence.json").write_text(
        json.dumps(common_details, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    base_artifacts = [
        {
            "name": "cluster-qc-decision-table",
            "relative_path": f"{output_prefix}/cluster-qc-decision-table.csv",
            "media_type": "text/csv",
        },
        {
            "name": "cluster-metric-evidence",
            "relative_path": f"{output_prefix}/cluster-metric-evidence.csv",
            "media_type": "text/csv",
        },
        {
            "name": "cluster-deg-identity",
            "relative_path": f"{output_prefix}/cluster-deg-identity.csv",
            "media_type": "text/csv",
        },
        {
            "name": "cluster-structure-evidence",
            "relative_path": f"{output_prefix}/cluster-structure-evidence.csv",
            "media_type": "text/csv",
        },
        {
            "name": "cluster-qc-evidence",
            "relative_path": f"{output_prefix}/cluster-qc-evidence.json",
            "media_type": "application/json",
        },
        *([metric_figure] if metric_figure else []),
        *umap_media,
        *heatmap_artifacts,
    ]
    # The overview is the immediate model decision surface. Per-cluster heatmaps remain complete
    # registered artifacts behind the deterministic cluster table, but replaying one image per
    # cluster consumed most of the context in repeated QC rounds. A reviewer can selectively open
    # a heatmap when the overview/table leaves a specific cluster ambiguous.
    overview_media = ([metric_figure] if metric_figure else []) + umap_media
    model_media = overview_media
    heatmap_paths = [item["relative_path"] for item in heatmap_artifacts]

    if cleanup["applied"]:
        return _apply_cleanup(
            adata=adata,
            labels=labels,
            cluster_key=cluster_key,
            confirmed=cleanup["confirmed_junk"],
            evidence=common_details,
            ident=ident,
            path=path,
            context=context,
            base_artifacts=base_artifacts,
            model_media=model_media,
            report_rows=decision_rows,
            np=np,
            pd=pd,
        )

    _write_report(evidence_dir / "cluster-qc-report.md", common_details, decision_rows)
    base_artifacts.append(
        {
            "name": "cluster-qc-report",
            "relative_path": f"{output_prefix}/cluster-qc-report.md",
            "media_type": "text/markdown",
        }
    )
    return {
        "summary": (
            f"Attested three-axis QC for {sizes.size} clusters; "
            f"{len(cleanup['confirmed_junk'])} convergent-junk, "
            f"{len(review_clusters)} for review; {len(warnings)} warning(s). "
            f"{len(model_media)} overview figure(s) attached; "
            f"{len(heatmap_paths)} per-cluster heatmap(s) remain available for selective review."
        ),
        "details": {**common_details, "available_cluster_heatmaps": heatmap_paths},
        "facts_patch": {
            "cluster_qc": {
                "status": "attested",
                "evidence_schema_version": CLUSTER_QC_EVIDENCE_SCHEMA,
                "evidence_id": evidence_id,
                "clustering_id": ident["clustering_id"],
                "cell_set_id": ident["cell_set_id"],
                "representation_id": ident["representation_id"],
                "count_representation_id": ident["count_representation_id"],
                "confirmed_junk": cleanup["confirmed_junk"],
                "held_for_review": cleanup["held_reason"],
                "warnings": warnings,
                "review_clusters": common_details["review_clusters"],
                "review_status": "pending",
                "required_visual_artifacts": [
                    f"{context.artifact_relative_path}/{item['relative_path']}"
                    for item in overview_media
                ],
                "shown_visual_artifacts": [
                    f"{context.artifact_relative_path}/{item['relative_path']}"
                    for item in overview_media
                ],
                "available_cluster_heatmaps": [
                    f"{context.artifact_relative_path}/{path}" for path in heatmap_paths
                ],
                "artifact_path": (
                    f"{context.artifact_relative_path}/{output_prefix}/"
                    "cluster-qc-decision-table.csv"
                ),
            }
        },
        "artifacts": base_artifacts,
        "model_media": model_media,
    }


def _render_heatmap(
    corr: Any,
    gene_indices: Any,
    var_names: Any,
    cluster: str,
    heatmap_dir: Path,
    output_prefix: str,
    plt: Any,
    np: Any,
    linkage: Any,
    leaves_list: Any,
) -> str | None:
    try:
        heatmap_dir.mkdir(parents=True, exist_ok=True)
        order = np.arange(corr.shape[0])
        if corr.shape[0] >= 3:
            distance = 1.0 - corr
            condensed = distance[np.triu_indices(distance.shape[0], k=1)]
            condensed = np.clip(condensed, 0.0, None)
            order = leaves_list(linkage(condensed, method="average"))
        ordered = corr[np.ix_(order, order)]
        fig, ax = plt.subplots(figsize=(6, 5))
        image = ax.imshow(ordered, vmin=-1, vmax=1, cmap="RdBu_r", interpolation="nearest")
        ax.set_title(f"Cluster {cluster} gene-gene correlation")
        ax.set_xticks([])
        ax.set_yticks([])
        fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
        relative = f"{output_prefix}/cluster-structure/cluster_{cluster}_correlation.png"
        fig.savefig(
            heatmap_dir / f"cluster_{cluster}_correlation.png",
            dpi=140,
            bbox_inches="tight",
        )
        plt.close(fig)
        return relative
    except Exception:
        plt.close("all")
        return None


def _render_metric_boxplots(
    adata: Any,
    labels: Any,
    decisions: list[dict[str, Any]],
    cluster_key: str,
    context: Any,
    output_prefix: str,
    plt: Any,
    pd: Any,
) -> dict[str, str] | None:
    metrics = [
        ("total_counts", "Library size", True),
        ("n_genes_by_counts", "Genes per cell", True),
        ("pct_counts_mt", "Mitochondrial %", False),
        ("pct_counts_ribo", "Ribosomal %", False),
        ("doublet_score", "Doublet score", False),
    ]
    available = [item for item in metrics if item[0] in adata.obs]
    if not available:
        return None
    clusters = sorted(map(str, labels.astype(str).unique()), key=_natural_cluster_key)
    flagged = {
        str(row["cluster"])
        for row in decisions
        if row.get("metric_severity") in {"ambiguous", "obvious"}
    }
    fig, axes = plt.subplots(
        len(available),
        1,
        figsize=(max(10, len(clusters) * 0.75), 3.2 * len(available)),
        squeeze=False,
    )
    for axis, (column, title, log_scale) in zip(axes[:, 0], available, strict=True):
        values = pd.to_numeric(adata.obs[column], errors="coerce")
        grouped = [
            values[labels.astype(str) == cluster].dropna().to_numpy() for cluster in clusters
        ]
        boxes = axis.boxplot(
            grouped,
            tick_labels=clusters,
            patch_artist=True,
            showfliers=False,
        )
        for cluster, box in zip(clusters, boxes["boxes"], strict=True):
            box.set_facecolor("#d9534f" if cluster in flagged else "#8dbbd8")
            box.set_alpha(0.85)
        if log_scale and all((group > 0).all() for group in grouped if group.size):
            axis.set_yscale("log")
            # Default log labels over a two-decade range collide into "2 x 10^3 3 x 10^3 ...";
            # plain numbers at 1/2/5 per decade stay readable at this panel height.
            from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

            axis.yaxis.set_major_locator(LogLocator(base=10.0, subs=(1.0, 2.0, 5.0)))
            axis.yaxis.set_major_formatter(
                FuncFormatter(
                    lambda value, _: f"{value / 1000:g}k" if value >= 1000 else f"{value:g}"
                )
            )
            axis.yaxis.set_minor_formatter(NullFormatter())
        axis.set_ylabel(title)
        axis.grid(axis="y", alpha=0.2)
    axes[-1, 0].set_xlabel(cluster_key)
    fig.suptitle(
        f"Per-cluster QC metrics — {cluster_key} "
        "(red = metric-flagged; distributions shown without outlier points)"
    )
    fig.tight_layout()
    relative = f"{output_prefix}/cluster-qc-metrics.png"
    (context.staging_dir / output_prefix).mkdir(parents=True, exist_ok=True)
    fig.savefig(context.staging_dir / relative, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return {
        "name": "cluster-qc-metrics",
        "relative_path": relative,
        "media_type": "image/png",
    }


def _natural_cluster_key(value: str) -> tuple[tuple[int, Any], ...]:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.lower())
        for part in re.split(r"(\d+)", str(value))
        if part
    )


def _cluster_grid_layout(n_clusters: int) -> tuple[int, int]:
    """Rows and columns for a one-panel-per-cluster highlight grid."""

    if n_clusters < 1:
        raise ValueError("a cluster grid needs at least one cluster")
    columns = 6 if n_clusters > 30 else min(5, n_clusters)
    return -(-n_clusters // columns), columns


def _cluster_grid_point_sizes(n_cells: int) -> tuple[float, float]:
    """Background and foreground marker areas for a small grid panel."""

    if n_cells < 1:
        raise ValueError("point sizes need at least one cell")
    return max(0.4, min(4.0, 80_000.0 / n_cells)), max(0.8, min(7.0, 120_000.0 / n_cells))


def _render_cluster_grid(
    adata: Any,
    cluster_key: str,
    context: Any,
    output_prefix: str,
    plt: Any,
) -> dict[str, str] | None:
    """One small UMAP panel per cluster, that cluster colored and the rest grey.

    Cluster QC is a per-cluster judgement, but the overlaid UMAP is the one figure where a
    per-cluster judgement cannot be made: at the resolutions this skill adjudicates, thirty-plus
    colors compete for the same pixels and a flagged cluster of 200 cells is unfindable. The grid
    gives each cluster its own panel, so "where is cluster 24 and is it one blob or scattered
    debris" is answerable by looking.
    """

    import numpy as np

    if "X_umap" not in adata.obsm:
        return None
    labels = adata.obs[cluster_key].astype(str).to_numpy()
    clusters = sorted(set(labels.tolist()), key=_natural_cluster_key)
    if not 2 <= len(clusters) <= 150:
        return None
    coordinates = np.asarray(adata.obsm["X_umap"])[:, :2]
    rows, columns = _cluster_grid_layout(len(clusters))
    background_size, foreground_size = _cluster_grid_point_sizes(int(adata.n_obs))
    palettes = [plt.get_cmap(name) for name in ("tab20", "tab20b", "tab20c")]
    fig, axes = plt.subplots(rows, columns, figsize=(2.6 * columns, 2.75 * rows), squeeze=False)
    # Shared limits: autoscaling each panel would silently rescale one whose cluster sits in a
    # corner, so panels would no longer be comparable.
    x_low, x_high = float(coordinates[:, 0].min()), float(coordinates[:, 0].max())
    y_low, y_high = float(coordinates[:, 1].min()), float(coordinates[:, 1].max())
    x_pad = max((x_high - x_low) * 0.03, 1e-6)
    y_pad = max((y_high - y_low) * 0.03, 1e-6)
    for index, cluster in enumerate(clusters):
        axis = axes[index // columns][index % columns]
        selected = labels == cluster
        other = ~selected
        if other.any():
            axis.scatter(
                coordinates[other, 0],
                coordinates[other, 1],
                c="#cccccc",
                s=background_size,
                alpha=0.25,
                linewidths=0,
                rasterized=True,
            )
        color = palettes[(index // 20) % 3]((index % 20) / 20.0)
        axis.scatter(
            coordinates[selected, 0],
            coordinates[selected, 1],
            c=[color],
            s=foreground_size,
            alpha=0.9,
            linewidths=0,
            rasterized=True,
        )
        axis.set_title(f"{cluster_key} {cluster}  ({int(selected.sum()):,})", fontsize=8, pad=3)
        axis.set_xlim(x_low - x_pad, x_high + x_pad)
        axis.set_ylim(y_low - y_pad, y_high + y_pad)
        axis.set_aspect("equal", adjustable="box")
        axis.set_axis_off()
    for index in range(len(clusters), rows * columns):
        axes[index // columns][index % columns].set_visible(False)
    fig.suptitle(f"{cluster_key} — one panel per cluster ({len(clusters)} clusters)", fontsize=11)
    fig.subplots_adjust(hspace=0.28, wspace=0.04)
    relative = f"{output_prefix}/cluster-qc-umap-grid.png"
    (context.staging_dir / output_prefix).mkdir(parents=True, exist_ok=True)
    fig.savefig(context.staging_dir / relative, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return {
        "name": "cluster-qc-umap-grid",
        "relative_path": relative,
        "media_type": "image/png",
    }


def _render_umap(
    adata: Any,
    cluster_key: str,
    context: Any,
    output_prefix: str,
    plt: Any,
    sc: Any,
) -> list[dict[str, str]]:
    umap_key = "X_umap" if "X_umap" in adata.obsm else None
    if umap_key is None:
        declared = adata.uns.get("scagent_sdk", {}).get("umap_key")
        if isinstance(declared, str) and declared in adata.obsm:
            umap_key = declared
    if umap_key is None and "umap" in adata.obsm:
        umap_key = "umap"
    if umap_key is None:
        return []
    if umap_key != "X_umap":
        adata = adata.copy()
        adata.obsm["X_umap"] = adata.obsm[umap_key]
    colors = [cluster_key]
    colors.extend(
        column
        for column in (
            "total_counts",
            "n_genes_by_counts",
            "pct_counts_mt",
            "pct_counts_ribo",
            "doublet_score",
            "predicted_doublet",
        )
        if column in adata.obs
    )
    axes = sc.pl.umap(adata, color=colors, ncols=3, show=False)
    # Rasterize the per-cell point clouds (as the cluster grid below already does): a vector
    # scatter of tens of thousands of cells across several panels produces a PNG that overruns
    # the executor's 2 MiB per-figure model_media cap and discards the whole result.
    for axis in axes if isinstance(axes, list) else [axes]:
        for collection in getattr(axis, "collections", []):
            collection.set_rasterized(True)
    relative = f"{output_prefix}/cluster-qc-umap.png"
    (context.staging_dir / output_prefix).mkdir(parents=True, exist_ok=True)
    plt.savefig(context.staging_dir / relative, dpi=120, bbox_inches="tight")
    plt.close("all")
    media = [
        {
            "name": "cluster-qc-umap",
            "relative_path": relative,
            "media_type": "image/png",
        }
    ]
    grid = _render_cluster_grid(adata, cluster_key, context, output_prefix, plt)
    if grid is not None:
        media.append(grid)
    return media


def _write_report(path: Path, details: dict[str, Any], decisions: list[dict[str, Any]]) -> None:
    lines = [
        "# Cluster QC attestation (three-axis)",
        "",
        f"- Evidence id: `{details['evidence_id']}`",
        f"- Clustering identity: `{details['clustering_id']}`",
        f"- Representation identity: `{details['representation_id']}`",
        f"- Clusters: {details['n_clusters']}",
        f"- Convergent-junk clusters: {details['cleanup']['confirmed_junk'] or 'none'}",
        f"- Cleanup applied: {details['cleanup']['applied']}",
        f"- Global Moran's I (MT/lib): {details['moran_global']}",
        "- Visual review required: yes",
        f"- Clusters requiring adjudication: {details['review_clusters'] or 'none'}",
        "",
        "## Per-cluster decisions",
        "",
        "| cluster | n | metric | DEG | covariance | synthesis | action |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in decisions:
        lines.append(
            f"| {row['cluster']} | {row['n_cells']} | {row['metric_severity']} | "
            f"{row['deg_verdict']} | {row['structure']} | {row['synthesis']} | {row['action']} |"
        )
    lines += ["", "## Warnings", ""]
    lines += [f"- {warning}" for warning in details["warnings"]] or ["- none"]
    lines += [
        "",
        "## Required visual review",
        "",
        "Inspect the attached cluster-QC metric and UMAP overview. Use the decision table and "
        "open a per-cluster correlation heatmap only where those views leave a specific cluster "
        "ambiguous. Then call `review_cluster_qc`; this attestation alone is not "
        "publication-ready.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def review(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    evidence = context.state_facts.get("cluster_qc")
    if not isinstance(evidence, dict) or evidence.get("status") != "attested":
        raise ValueError("current cluster-QC evidence is absent; run evaluate_cluster_qc")
    evidence_id = str(arguments["evidence_id"])
    if evidence_id != evidence.get("evidence_id"):
        raise ValueError("evidence_id does not match the current cluster-QC evidence")
    reviewed = {str(value) for value in arguments.get("reviewed_artifacts", [])}
    # Attached pixels were already presented with an immediate-read directive. Persist that
    # runtime fact rather than forcing the model to copy every path back into the review call.
    reviewed.update(str(value) for value in evidence.get("shown_visual_artifacts", []))
    required = {str(value) for value in evidence.get("required_visual_artifacts", [])}
    missing = sorted(required - reviewed)
    if missing:
        raise ValueError(
            "cluster-QC visual review is incomplete; inspect and include every required "
            "artifact: " + ", ".join(missing)
        )
    expected_clusters = {str(value) for value in evidence.get("review_clusters", [])}
    supplied = arguments.get("cluster_reviews", {})
    if not isinstance(supplied, dict):
        raise ValueError("cluster_reviews must be an object keyed by cluster")
    supplied_clusters = set(map(str, supplied))
    missing_clusters = expected_clusters - supplied_clusters
    if missing_clusters:
        raise ValueError(
            "cluster_reviews must cover every cluster requiring review; "
            f"missing={sorted(missing_clusters)}"
        )
    normalized: dict[str, dict[str, str]] = {}
    unresolved: list[str] = []
    allowed = {"keep", "remove", "merge", "split", "recluster", "defer"}
    for cluster, raw in supplied.items():
        if not isinstance(raw, dict):
            raise ValueError(f"cluster review for {cluster!r} must be an object")
        disposition = str(raw.get("disposition", ""))
        rationale = str(raw.get("rationale", "")).strip()
        if disposition not in allowed:
            raise ValueError(
                f"cluster {cluster!r} disposition must be one of {sorted(allowed)}"
            )
        if not rationale:
            raise ValueError(f"cluster {cluster!r} review rationale must not be empty")
        normalized[str(cluster)] = {
            "disposition": disposition,
            "rationale": rationale,
        }
        if disposition != "keep":
            unresolved.append(str(cluster))
    findings = [str(value).strip() for value in arguments.get("visual_findings", [])]
    if not findings or any(not value for value in findings):
        raise ValueError("visual_findings must contain at least one non-empty observation")
    review_fact = {
        "status": "resolved" if not unresolved else "action_required",
        "evidence_id": evidence_id,
        "clustering_id": evidence.get("clustering_id"),
        "reviewed_artifacts": sorted(reviewed),
        "visual_findings": findings,
        "cluster_reviews": normalized,
        "unresolved_clusters": sorted(unresolved, key=_natural_cluster_key),
    }
    return {
        "summary": (
            f"Reviewed {len(reviewed)} cluster-QC figures; "
            + (
                "all flagged clusters were retained with explicit rationale."
                if not unresolved
                else f"{len(unresolved)} cluster action(s) remain unresolved."
            )
        ),
        "details": review_fact,
        "facts_patch": {"cluster_qc": {"review": review_fact}},
        "decisions_patch": {"cluster_qc_review": review_fact},
    }


def _cleanup_identities(
    *,
    confirmed: list[str],
    n_removed: int,
    remaining_cell_names: list[str],
    remaining_gene_names: list[str],
    evidence: dict[str, Any],
    ident: dict[str, Any],
) -> dict[str, str]:
    """Pure lineage-identity computation for a convergent-junk cleanup (no AnnData I/O)."""
    cells = sorted(map(str, remaining_cell_names))
    new_cell_set_id = _identity("cells", cells)
    new_count_id = _identity(
        "counts",
        {
            "parent": ident["count_representation_id"],
            "cell_set_id": new_cell_set_id,
            "filter": "cluster-qc-convergent-junk",
            "removed_clusters": confirmed,
        },
    )
    new_revision_id = _identity(
        "dataset-revision",
        {
            "parent": evidence["evidence_id"],
            "cell_set_id": new_cell_set_id,
            "removed_cells": n_removed,
        },
    )
    count_matrix_id = _identity(
        "count-matrix", {"cells": cells, "genes": sorted(map(str, remaining_gene_names))}
    )
    return {
        "cell_set_id": new_cell_set_id,
        "count_representation_id": new_count_id,
        "dataset_revision_id": new_revision_id,
        "count_matrix_id": count_matrix_id,
    }


def _cleanup_facts_patch(
    *,
    ids: dict[str, str],
    ident: dict[str, Any],
    evidence: dict[str, Any],
    source_path: str,
    final_path: str,
    dataset_abs_path: str,
    n_obs: int,
    n_vars: int,
    size_bytes: int,
    modified_time_ns: int,
    fingerprint: str,
    confirmed: list[str],
    n_removed: int,
    removal_fraction: float,
) -> dict[str, Any]:
    """Pure state invalidation for a convergent-junk cleanup (no AnnData I/O).

    Issues fresh dataset/cell-set/count identities, nulls the representation and clustering, and
    invalidates every downstream fact and decision — cell QC, **doublets**, batch, annotation, and
    finalization — so no stale evidence survives the reducer's merge-patch semantics.
    """
    facts_patch = {
        "dataset": {
            "path": dataset_abs_path,
            "size_bytes": size_bytes,
            "modified_time_ns": modified_time_ns,
            "fingerprint": fingerprint,
            "fingerprint_mode": "full",
            "format": {
                "extension": "zarr",
                "suffixes": [".zarr"],
                "byte_signature": "zarr",
                "extension_signature_consistent": True,
            },
            "lineage": {
                "parent_path": source_path,
                "parent_evidence_id": evidence["evidence_id"],
                "transformation": "remove-cluster-qc-convergent-junk",
            },
        },
        "analysis": {
            "dataset_revision": {
                "id": ids["dataset_revision_id"],
                "source_path": source_path,
                "n_cells": n_obs,
                "n_genes": n_vars,
            },
            "cell_set": {"id": ids["cell_set_id"], "n_cells": n_obs},
            "count_representation": {
                "id": ids["count_representation_id"],
                "method": "cluster-qc-convergent-junk-filter",
                "parent_id": ident["count_representation_id"],
            },
            "representation": None,
            "clustering": None,
        },
        "cell_qc": None,
        "cluster_qc": {
            "status": "cleanup_applied",
            "evidence_schema_version": CLUSTER_QC_EVIDENCE_SCHEMA,
            "evidence_id": evidence["evidence_id"],
            "removed_clusters": confirmed,
            "removed_cells": n_removed,
            "new_cell_set_id": ids["cell_set_id"],
        },
        "doublets": None,
        "batch": None,
        "annotation": None,
        "finalization": None,
    }
    decisions_patch = {
        "cluster_cleanup": {
            "removed_clusters": confirmed,
            "removed_cells": n_removed,
            "removal_fraction": removal_fraction,
        },
        "doublet_handling": None,
        "batch_handling": None,
        "integration": None,
        "final_labels": None,
    }
    return {"facts_patch": facts_patch, "decisions_patch": decisions_patch}


def _apply_cleanup(
    *,
    adata: Any,
    labels: Any,
    cluster_key: str,
    confirmed: list[str],
    evidence: dict[str, Any],
    ident: dict[str, Any],
    path: Path,
    context: Any,
    base_artifacts: list[dict[str, str]],
    model_media: list[dict[str, str]],
    report_rows: list[dict[str, Any]],
    np: Any,
    pd: Any,
) -> dict[str, Any]:
    if "counts" not in adata.layers:
        raise ValueError("cannot apply cleanup: prepared counts layer is absent")
    remove_mask = labels.isin(confirmed).to_numpy()
    n_removed = int(remove_mask.sum())
    removed_names = list(map(str, adata.obs_names[remove_mask]))
    pd.DataFrame(
        {"cell": removed_names, "removed_from_cluster": labels[remove_mask].to_numpy()}
    ).to_csv(context.staging_dir / "removed-cells.csv", index=False)

    adata.X = adata.layers["counts"]
    filtered = adata[~remove_mask].copy()
    filtered.raw = None
    filtered.layers.clear()
    filtered.layers["counts"] = filtered.X.copy()
    filtered.obsm.clear()
    filtered.obsp.clear()
    filtered.varm.clear()
    if cluster_key in filtered.obs:
        del filtered.obs[cluster_key]

    ids = _cleanup_identities(
        confirmed=confirmed,
        n_removed=n_removed,
        remaining_cell_names=list(filtered.obs_names),
        remaining_gene_names=list(filtered.var_names),
        evidence=evidence,
        ident=ident,
    )
    output_relative = "cluster-qc-filtered-raw-counts.zarr"
    output_path = context.staging_dir / output_relative
    final_path = f"{context.artifact_relative_path}/{output_relative}"
    filtered.uns = {
        "scagent_sdk": {
            "schema_version": 1,
            "source_path": str(path),
            "dataset_revision_id": ids["dataset_revision_id"],
            "cell_set_id": ids["cell_set_id"],
            "count_representation_id": ids["count_representation_id"],
            "count_matrix_id": ids["count_matrix_id"],
        },
        "cluster_qc_cleanup": {
            "parent_evidence_id": evidence["evidence_id"],
            "removed_clusters": confirmed,
            "removed_cells": n_removed,
            "removal_fraction": evidence["cleanup"]["removal_fraction"],
        },
    }
    _write_matrix(filtered, output_path)
    stat = output_path.stat()
    fingerprint = _dataset_fingerprint(output_path)
    dataset_abs_path = str(
        context.session_dir / "artifacts" / "capabilities" / context.execution_id / output_relative
    )
    lineage = _cleanup_facts_patch(
        ids=ids,
        ident=ident,
        evidence=evidence,
        source_path=str(path),
        final_path=final_path,
        dataset_abs_path=dataset_abs_path,
        n_obs=int(filtered.n_obs),
        n_vars=int(filtered.n_vars),
        size_bytes=stat.st_size,
        modified_time_ns=stat.st_mtime_ns,
        fingerprint=fingerprint,
        confirmed=confirmed,
        n_removed=n_removed,
        removal_fraction=evidence["cleanup"]["removal_fraction"],
    )

    _write_report(context.staging_dir / "cluster-qc-report.md", evidence, report_rows)
    artifacts = base_artifacts + [
        {
            "name": "cluster-qc-report",
            "relative_path": "cluster-qc-report.md",
            "media_type": "text/markdown",
        },
        {
            "name": "cluster-qc-filtered-raw-counts",
            "relative_path": output_relative,
            "media_type": "application/vnd.zarr",
        },
        {"name": "removed-cells", "relative_path": "removed-cells.csv", "media_type": "text/csv"},
    ]
    return {
        "summary": (
            f"Removed {n_removed:,} cells in {len(confirmed)} convergent-junk cluster(s) "
            f"({evidence['cleanup']['removal_fraction']:.1%}); re-prepare and recluster the "
            f"{filtered.n_obs:,} remaining cells, then re-run cluster QC."
        ),
        "details": {
            **evidence,
            "status": "cleanup_applied",
            "removed_cells": n_removed,
            "new_cell_set_id": ids["cell_set_id"],
            "filtered_path": final_path,
        },
        "facts_patch": lineage["facts_patch"],
        "decisions_patch": lineage["decisions_patch"],
        "artifacts": artifacts,
        "model_media": model_media,
    }
