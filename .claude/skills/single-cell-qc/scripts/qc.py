"""Flag-first single-cell QC and separately confirmed filtering."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_SYMBOL_COLUMNS = (
    "feature_name",
    "gene_symbols",
    "gene_symbol",
    "gene_name",
    "gene_names",
    "mgi_symbol",
    "hgnc_symbol",
    "symbol",
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

def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _count_matrix_identity(matrix: Any, obs_names: Any, var_names: Any) -> str:
    import numpy as np
    from scipy import sparse

    digest = hashlib.sha256()
    digest.update(b"scagent-count-matrix-v1\0")
    digest.update(str(tuple(map(int, matrix.shape))).encode() + b"\0")
    if sparse.issparse(matrix):
        value = matrix.tocsr()
        arrays = (value.data, value.indices, value.indptr)
    else:
        arrays = (np.asarray(matrix),)
    for array in arrays:
        contiguous = np.ascontiguousarray(array)
        digest.update(str(contiguous.dtype).encode() + b"\0")
        digest.update(memoryview(contiguous).cast("B"))
    for names in (obs_names, var_names):
        for name in names:
            digest.update(str(name).encode("utf-8", errors="replace") + b"\0")
    return f"counts:sha256:{digest.hexdigest()}"


def _resolve_layer(adata: Any, layer: str | None) -> str | None:
    if layer == "auto":
        return "counts" if "counts" in adata.layers else None
    return layer


def _matrix(adata: Any, layer: str | None) -> Any:
    import numpy as np

    layer = _resolve_layer(adata, layer)
    if layer is None:
        matrix = adata.X
        label = "X"
    elif layer in adata.layers:
        matrix = adata.layers[layer]
        label = f"layer:{layer}"
    else:
        raise ValueError(f"count layer {layer!r} is absent; use counts_layer=null for X")
    values = np.asarray(matrix.tocsr().data if hasattr(matrix, "tocsr") else matrix).ravel()
    if values.size and (
        not bool(np.all(np.isfinite(values)))
        or not bool(np.all(values >= 0))
        or not bool(np.all(values == np.round(values)))
    ):
        raise ValueError(f"{label} is not finite nonnegative integer counts")
    return matrix


def _symbols(adata: Any) -> list[str]:
    original = [str(value) for value in adata.var_names]
    for column in _SYMBOL_COLUMNS:
        if column in adata.var:
            values = [str(value) for value in adata.var[column]]
            if sum(bool(value) and value.lower() != "nan" for value in values) > len(values) / 2:
                return values
    return original


HISTOGRAM_BINS = 60
# Per-cell points drawn over a violin body; past this the strip is solid ink and the extra
# points only cost render time.
VIOLIN_JITTER_CELLS = 20000
VIOLIN_JITTER_WIDTH = 0.2


def positive_span(values: list[float]) -> tuple[float, float, int]:
    """Smallest and largest strictly positive value, and how many were not positive.

    The dropped count is returned rather than discarded: a log axis cannot draw a zero, and
    "1,204 cells have zero counts" is QC evidence, not a rendering detail.
    """

    positive = [float(value) for value in values if float(value) > 0.0]
    dropped = len(values) - len(positive)
    if not positive:
        raise ValueError("no positive values to plot on a log scale")
    return min(positive), max(positive), dropped


def log_spaced_bins(minimum: float, maximum: float, count: int = HISTOGRAM_BINS) -> list[float]:
    """Bin edges uniform in log space, for a metric drawn on a log axis.

    Linear bins under `set_xscale("log")` draw the leftmost bar hundreds of times wider than the
    rightmost, which compresses the low end of a library-size distribution into two or three
    blocks and hides the shape a threshold is chosen from.
    """

    import math

    if count < 1:
        raise ValueError("count must be at least 1")
    if minimum <= 0.0:
        raise ValueError("log-spaced bins need a positive minimum; drop non-positive values first")
    if maximum <= minimum:
        maximum = minimum * 10.0
    low, high = math.log10(minimum), math.log10(maximum)
    step = (high - low) / count
    return [10.0 ** (low + step * index) for index in range(count + 1)]


def compact_tick(value: float) -> str:
    """Short axis-tick text for count-scale numbers.

    Spelled-out thousands ("10,000", "20,000", "50,000") overrun each other on a multi-panel row,
    which reintroduces the collision the 1/2/5 locator was chosen to avoid.
    """

    magnitude = abs(float(value))
    if magnitude >= 1_000_000:
        return f"{float(value) / 1_000_000:g}M"
    if magnitude >= 1_000:
        return f"{float(value) / 1_000:g}k"
    return f"{float(value):g}"


def _plain_log_ticks(axis: Any, which: str = "x") -> None:
    """Label a log axis with plain numbers at 1/2/5 per decade instead of mantissa notation.

    Over a two-decade range matplotlib labels the minor ticks as "2 x 10^2", "3 x 10^2", ...,
    which run together into an unreadable smear on the genes-per-cell panel. Decade-only labels
    are legible but too sparse to read a threshold off, so 1/2/5 per decade is used.
    """

    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

    target = axis.xaxis if which == "x" else axis.yaxis
    target.set_major_locator(LogLocator(base=10.0, subs=(1.0, 2.0, 5.0)))
    target.set_major_formatter(FuncFormatter(lambda value, _: compact_tick(value)))
    target.set_minor_formatter(NullFormatter())


def _violin_panel(axis: Any, values: Any, label: str, rng: Any) -> int:
    """Violin body with the per-cell points jittered over it; returns the points drawn.

    The body alone is a smoothed density that looks identical over forty cells and forty
    thousand. Overlaying the cells is what the original scagent QC violins did
    (`sc.pl.violin(..., jitter=0.2)`) and it is what makes the sample size legible.
    """

    import numpy as np

    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    axis.set_xticks([])
    axis.set_title(label, fontsize=10)
    axis.set_ylabel("value")
    if finite.size == 0:
        axis.text(0.5, 0.5, "no finite values", ha="center", va="center", transform=axis.transAxes)
        return 0
    parts = axis.violinplot(finite, showextrema=False, widths=0.85)
    for body in parts["bodies"]:
        body.set_facecolor("#3b75af")
        body.set_edgecolor("#1f3f5f")
        body.set_linewidth(0.8)
        body.set_alpha(0.9)
    shown = min(int(finite.size), VIOLIN_JITTER_CELLS)
    sample = finite if shown == finite.size else rng.choice(finite, size=shown, replace=False)
    axis.scatter(
        1.0 + rng.uniform(-VIOLIN_JITTER_WIDTH, VIOLIN_JITTER_WIDTH, size=sample.size),
        sample,
        s=0.8,
        color="#111111",
        alpha=0.35,
        linewidths=0,
        rasterized=True,
    )
    lower, median, upper = (float(item) for item in np.percentile(finite, [25.0, 50.0, 75.0]))
    axis.vlines(1.0, lower, upper, color="#ffffff", linewidth=3.0, zorder=3)
    axis.plot([1.0], [median], marker="o", markersize=3.5, color="#ffffff", zorder=4)
    return shown


def _add_metrics(
    adata: Any,
    *,
    layer: str | None,
    organism: str,
    min_genes: int | None,
    max_genes: int | None,
    max_pct_mito: float | None,
) -> tuple[Any, dict[str, int]]:
    import numpy as np
    import scanpy as sc

    layer = _resolve_layer(adata, layer)
    counts = _matrix(adata, layer)
    work = adata.copy()
    work.X = counts.copy()
    names = _symbols(work)
    if organism == "mouse":
        mt = [name.startswith(("mt-", "Mt-", "MT-")) for name in names]
    else:
        mt = [name.upper().startswith("MT-") for name in names]
    ribo = [name.upper().startswith(("RPS", "RPL")) for name in names]
    work.var["mt"] = np.asarray(mt, dtype=bool)
    work.var["ribo"] = np.asarray(ribo, dtype=bool)
    sc.pp.calculate_qc_metrics(
        work, qc_vars=["mt", "ribo"], percent_top=None, log1p=True, inplace=True
    )
    keep = np.ones(work.n_obs, dtype=bool)
    flags: dict[str, int] = {}
    if min_genes is not None:
        flag = np.asarray(work.obs["n_genes_by_counts"] < min_genes)
        work.obs["qc_flag_low_genes"] = flag
        flags["low_genes"] = int(flag.sum())
        keep &= ~flag
    if max_genes is not None:
        flag = np.asarray(work.obs["n_genes_by_counts"] > max_genes)
        work.obs["qc_flag_high_genes"] = flag
        flags["high_genes"] = int(flag.sum())
        keep &= ~flag
    if max_pct_mito is not None:
        flag = np.asarray(work.obs["pct_counts_mt"] > max_pct_mito)
        work.obs["qc_flag_high_mito"] = flag
        flags["high_mito"] = int(flag.sum())
        keep &= ~flag
    work.obs["qc_pass_requested_thresholds"] = keep
    flags["any_requested_flag"] = int((~keep).sum())
    return work, flags


def _render_qc_figures(
    adata: Any,
    context: Any,
    *,
    thresholds: dict[str, int | float | None],
) -> list[dict[str, str]]:
    import matplotlib.pyplot as plt
    import numpy as np

    output_dir = context.staging_dir / "qc"
    output_dir.mkdir(parents=True, exist_ok=True)

    def values(column: str) -> Any:
        result = np.asarray(adata.obs[column], dtype=float)
        return result[np.isfinite(result)]

    def save(fig: Any, name: str) -> dict[str, str]:
        relative = f"qc/{name}"
        fig.savefig(context.staging_dir / relative, dpi=160, bbox_inches="tight")
        plt.close(fig)
        return {
            "name": Path(name).stem.replace("_", "-"),
            "relative_path": relative,
            "media_type": "image/png",
        }

    figures: list[dict[str, str]] = []
    total = values("total_counts")
    genes = values("n_genes_by_counts")
    mito = values("pct_counts_mt")
    ribo = values("pct_counts_ribo")

    rng = np.random.default_rng(0)

    def log_histogram(axis: Any, series: Any, label: str) -> None:
        """Histogram on a log axis with bins that are uniform *on that axis*."""

        low, high, dropped = positive_span([float(item) for item in series.tolist()])
        axis.hist(
            series[series > 0], bins=log_spaced_bins(low, high), edgecolor="black", linewidth=0.3
        )
        axis.set_xscale("log")
        _plain_log_ticks(axis)
        axis.set(xlabel=f"{label} (log scale)", ylabel="Cells")
        axis.set_title(label if not dropped else f"{label} ({dropped:,} zero-count omitted)")

    fig, axes = plt.subplots(1, 5, figsize=(24, 4.5))
    ranked = np.sort(total)[::-1]
    axes[0].plot(np.arange(1, ranked.size + 1), ranked)
    axes[0].set(xscale="log", yscale="log", xlabel="Cell rank", ylabel="Total counts")
    axes[0].set_title("UMI rank (knee)")
    log_histogram(axes[1], total, "Library size")
    log_histogram(axes[2], genes, "Genes per cell")
    axes[3].hist(mito, bins=HISTOGRAM_BINS, edgecolor="black", linewidth=0.3)
    if thresholds["max_pct_mito"] is not None:
        axes[3].axvline(
            float(thresholds["max_pct_mito"]),
            color="crimson",
            linestyle="--",
            label=f"threshold {float(thresholds['max_pct_mito']):g}%",
        )
        axes[3].legend()
    axes[3].set(xlabel="Mitochondrial percent", ylabel="Cells", title="Mitochondrial content")
    axes[4].hist(ribo, bins=HISTOGRAM_BINS, edgecolor="black", linewidth=0.3)
    axes[4].set(xlabel="Ribosomal percent", ylabel="Cells", title="Ribosomal content")
    fig.suptitle(f"Single-cell QC distributions ({adata.n_obs:,} cells)")
    fig.tight_layout()
    figures.append(save(fig, "qc_distributions.png"))

    violin_columns = [
        ("log1p_total_counts", "log1p_total_counts"),
        ("log1p_n_genes_by_counts", "log1p_n_genes_by_counts"),
        ("pct_counts_mt", "pct_counts_mt"),
        ("pct_counts_ribo", "pct_counts_ribo"),
    ]
    fig, axes = plt.subplots(1, len(violin_columns), figsize=(4.2 * len(violin_columns), 4.6))
    for axis, (column, label) in zip(axes, violin_columns, strict=True):
        _violin_panel(axis, values(column), label, rng)
    fig.suptitle(f"QC metric distributions ({adata.n_obs:,} cells)")
    fig.tight_layout()
    figures.append(save(fig, "qc_violin_metrics.png"))

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    log_histogram(axes[0], total, "Library size")
    log_histogram(axes[1], genes, "Genes per cell")
    fig.tight_layout()
    figures.append(save(fig, "qc_histograms.png"))

    fig, axis = plt.subplots(figsize=(8, 4.5))
    axis.hist(mito, bins=100)
    if thresholds["max_pct_mito"] is not None:
        axis.axvline(
            float(thresholds["max_pct_mito"]),
            color="crimson",
            linestyle="--",
            label=f"threshold {float(thresholds['max_pct_mito']):g}%",
        )
        axis.legend()
    axis.set(xlabel="Mitochondrial percent", ylabel="Cells", title="Mitochondrial content")
    fig.tight_layout()
    figures.append(save(fig, "qc_mt_histogram.png"))

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    axes[0].scatter(total, genes, s=8, alpha=0.55)
    axes[0].set(xscale="log", yscale="log", xlabel="Total counts", ylabel="Genes detected")
    _plain_log_ticks(axes[0], "x")
    _plain_log_ticks(axes[0], "y")
    axes[0].set_title("Genes versus counts")
    axes[1].scatter(total, mito, s=8, alpha=0.55)
    axes[1].set(xscale="log", xlabel="Total counts", ylabel="Mitochondrial percent")
    _plain_log_ticks(axes[1], "x")
    if thresholds["max_pct_mito"] is not None:
        axes[1].axhline(float(thresholds["max_pct_mito"]), color="crimson", linestyle="--")
    axes[1].set_title("Mitochondrial percent versus counts")
    fig.tight_layout()
    figures.append(save(fig, "qc_scatter.png"))

    fig, axis = plt.subplots(figsize=(6, 5))
    axis.scatter(mito, ribo, s=8, alpha=0.55)
    if thresholds["max_pct_mito"] is not None:
        axis.axvline(float(thresholds["max_pct_mito"]), color="crimson", linestyle="--")
    axis.set(
        xlabel="Mitochondrial percent",
        ylabel="Ribosomal percent",
        title="Ribosomal versus mitochondrial content",
    )
    fig.tight_layout()
    figures.append(save(fig, "qc_ribo_vs_mt.png"))

    # Doublet scores are plotted only when another skill has already computed them; this tool
    # neither runs Scrublet nor invents a cutoff, so no reference line is drawn.
    doublet_column = next(
        (name for name in ("doublet_score", "scrublet_score") if name in adata.obs), None
    )
    if doublet_column is not None:
        scores = values(doublet_column)
        fig, axes = plt.subplots(1, 2, figsize=(12, 4.4))
        _violin_panel(axes[0], scores, doublet_column, rng)
        axes[1].hist(scores, bins=HISTOGRAM_BINS, edgecolor="black", linewidth=0.3)
        axes[1].set(xlabel=doublet_column, ylabel="Cells", title="Doublet score histogram")
        fig.suptitle("Doublet evidence already present on this artifact")
        fig.tight_layout()
        figures.append(save(fig, "qc_doublet_scores.png"))
    return figures


def _base_metadata(adata: Any) -> dict[str, Any]:
    return dict(adata.uns.get("scagent_sdk", {}))


def _write_qc_artifacts(
    adata: Any, context: Any, *, output_name: str, report: dict[str, Any]
) -> list[dict[str, str]]:
    _write_matrix(adata, context.staging_dir / output_name)
    adata.obs[
        [
            column
            for column in (
                "total_counts",
                "n_genes_by_counts",
                "pct_counts_mt",
                "pct_counts_ribo",
                "qc_flag_low_genes",
                "qc_flag_high_genes",
                "qc_flag_high_mito",
                "qc_pass_requested_thresholds",
            )
            if column in adata.obs
        ]
    ].to_csv(context.staging_dir / "cell-qc-metrics.csv")
    (context.staging_dir / "cell-qc-summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return [
        {
            "name": "qc-anndata",
            "relative_path": output_name,
            "media_type": "application/vnd.zarr",
        },
        {
            "name": "cell-qc-metrics",
            "relative_path": "cell-qc-metrics.csv",
            "media_type": "text/csv",
        },
        {
            "name": "cell-qc-summary",
            "relative_path": "cell-qc-summary.json",
            "media_type": "application/json",
        },
    ]


def calculate_qc(arguments: dict[str, Any], context: Any) -> dict[str, Any]:

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    layer_arg = arguments.get("counts_layer", "auto")
    layer = str(layer_arg) if layer_arg is not None else None
    organism = str(arguments.get("organism", "human"))
    min_arg = arguments.get("min_genes", 200)
    max_arg = arguments.get("max_genes")
    mito_arg = arguments.get("max_pct_mito", 20)
    thresholds = {
        "min_genes": int(min_arg) if min_arg is not None else None,
        "max_genes": int(max_arg) if max_arg is not None else None,
        "max_pct_mito": float(mito_arg) if mito_arg is not None else None,
    }
    source = _read_matrix(path)
    layer = _resolve_layer(source, layer)
    adata, flags = _add_metrics(source, layer=layer, organism=organism, **thresholds)
    metadata = _base_metadata(adata)
    metadata["qc_assessment_id"] = _identity(
        "cell-qc",
        {
            "path": str(path),
            "cells": sorted(map(str, adata.obs_names)),
            "thresholds": thresholds,
            "flags": flags,
        },
    )
    adata.uns["scagent_sdk"] = metadata
    output_name = "qc-assessed.zarr"
    final_path = f"{context.artifact_relative_path}/{output_name}"
    report = {
        "operation": "calculate_only",
        "organism": organism,
        "counts_source": "X" if layer is None else f"layer:{layer}",
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "thresholds": thresholds,
        "flag_counts": flags,
        "metric_summary": {
            "median_total_counts": float(adata.obs["total_counts"].median()),
            "median_genes_detected": float(adata.obs["n_genes_by_counts"].median()),
            "median_pct_mito": float(adata.obs["pct_counts_mt"].median()),
            "median_pct_ribo": float(adata.obs["pct_counts_ribo"].median()),
            "n_mitochondrial_genes": int(adata.var["mt"].sum()),
            "n_ribosomal_genes": int(adata.var["ribo"].sum()),
        },
        "mitochondrial_threshold_options": {
            str(value): {
                "threshold": value,
                "cells_flagged": int((adata.obs["pct_counts_mt"] > value).sum()),
                "fraction_flagged": float((adata.obs["pct_counts_mt"] > value).mean()),
            }
            for value in (5, 10, 15, 20, 25, 30)
        },
        "qc_assessment_id": metadata["qc_assessment_id"],
    }
    artifacts = _write_qc_artifacts(adata, context, output_name=output_name, report=report)
    figures = _render_qc_figures(adata, context, thresholds=thresholds)
    artifacts.extend(figures)
    provenance = dict(adata.uns.get("scagent_sdk", {}))
    required_visual_artifacts = [
        f"{context.artifact_relative_path}/{item['relative_path']}"
        for item in figures
    ]
    return {
        "summary": (
            f"Calculated QC for {adata.n_obs:,} cells; "
            f"{flags['any_requested_flag']:,} cells meet at least one requested flag."
        ),
        "details": report,
        "facts_patch": {
            "cell_qc": {
                "status": "assessed",
                "assessment_id": metadata["qc_assessment_id"],
                "artifact_path": final_path,
                "thresholds": thresholds,
                "flag_counts": flags,
                "cell_set_id": provenance.get("cell_set_id"),
                "count_representation_id": provenance.get("count_representation_id"),
                "review_status": "pending",
                "required_visual_artifacts": required_visual_artifacts,
                "shown_visual_artifacts": required_visual_artifacts,
            }
        },
        "artifacts": artifacts,
        "model_media": figures,
    }


def review_qc(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    facts = context.state_facts
    evidence = facts.get("cell_qc")
    if not isinstance(evidence, dict) or evidence.get("status") != "assessed":
        raise ValueError("current cell-QC assessment is absent; run calculate_single_cell_qc")
    assessment_id = str(arguments["assessment_id"])
    if assessment_id != evidence.get("assessment_id"):
        raise ValueError("assessment_id does not match the current cell-QC assessment")
    rationale = str(arguments["rationale"]).strip()
    if not rationale:
        raise ValueError("rationale must not be empty")
    reviewed = {str(value) for value in arguments.get("reviewed_artifacts", [])}
    reviewed.update(str(value) for value in evidence.get("shown_visual_artifacts", []))
    required = {str(value) for value in evidence.get("required_visual_artifacts", [])}
    missing = sorted(required - reviewed)
    if missing:
        raise ValueError(
            "visual review is incomplete; inspect and include every required artifact: "
            + ", ".join(missing)
        )
    decision = str(arguments["decision"])
    resolved = decision in {"keep_all", "not_applicable"}
    review = {
        "status": "resolved" if resolved else "action_required",
        "assessment_id": assessment_id,
        "decision": decision,
        "rationale": rationale,
        "visual_findings": [str(value) for value in arguments.get("visual_findings", [])],
        "reviewed_artifacts": sorted(reviewed),
        "cell_set_id": evidence.get("cell_set_id"),
        "count_representation_id": evidence.get("count_representation_id"),
    }
    return {
        "summary": (
            f"Recorded cell-QC decision {decision!r}; "
            + ("QC review is resolved." if resolved else "follow-up action is required.")
        ),
        "details": review,
        "facts_patch": {"cell_qc": {"review": review}},
        "decisions_patch": {"cell_qc_handling": review},
    }


def filter_cells(arguments: dict[str, Any], context: Any) -> dict[str, Any]:

    if arguments.get("confirm_filtering") is not True:
        raise ValueError("confirm_filtering must be true before changing the cell set")
    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    layer_arg = arguments.get("counts_layer", "auto")
    layer = str(layer_arg) if layer_arg is not None else None
    organism = str(arguments.get("organism", "human"))
    # Deliberately no threshold defaults on the mutating tool: the flag thresholds on
    # calculate_single_cell_qc are instrumentation, and inheriting them here would let a
    # bare call delete the mitochondrial tail as a side effect of a default.
    min_arg = arguments.get("min_genes")
    max_arg = arguments.get("max_genes")
    mito_arg = arguments.get("max_pct_mito")
    if min_arg is None and max_arg is None and mito_arg is None:
        raise ValueError(
            "no threshold requested: state min_genes, max_genes, and/or max_pct_mito "
            "explicitly when removing cells (this tool has no default thresholds)"
        )
    thresholds = {
        "min_genes": int(min_arg) if min_arg is not None else None,
        "max_genes": int(max_arg) if max_arg is not None else None,
        "max_pct_mito": float(mito_arg) if mito_arg is not None else None,
    }
    source = _read_matrix(path)
    layer = _resolve_layer(source, layer)
    assessed, flags = _add_metrics(source, layer=layer, organism=organism, **thresholds)
    before = int(assessed.n_obs)
    filtered = assessed[assessed.obs["qc_pass_requested_thresholds"].to_numpy()].copy()
    if filtered.n_obs == 0:
        raise ValueError("requested thresholds would remove every cell")
    cell_set_id = _identity("cells", sorted(map(str, filtered.obs_names)))
    filtered_counts = _matrix(filtered, layer)
    matrix_id = _count_matrix_identity(
        filtered_counts, filtered.obs_names, filtered.var_names
    )
    count_id = _identity(
        "count-representation",
        {
            "matrix_id": matrix_id,
            "cell_set_id": cell_set_id,
            "operation": "filter-cells",
        },
    )
    metadata = _base_metadata(filtered)
    metadata.update(
        {
            "cell_set_id": cell_set_id,
            "count_representation_id": count_id,
            "count_matrix_id": matrix_id,
            "dataset_revision_id": _identity(
                "dataset-revision",
                {
                    "parent_path": str(path),
                    "cell_set_id": cell_set_id,
                    "count_representation_id": count_id,
                    "operation": "filter-cells",
                },
            ),
        }
    )
    for key in ("representation_id", "clustering_id", "qc_assessment_id"):
        metadata.pop(key, None)
    filtered.uns["scagent_sdk"] = metadata
    output_name = "cells-filtered.zarr"
    report = {
        "operation": "filter_cells",
        "before_cells": before,
        "after_cells": int(filtered.n_obs),
        "removed_cells": before - int(filtered.n_obs),
        "thresholds": thresholds,
        "flag_counts": flags,
        "cell_set_id": cell_set_id,
    }
    artifacts = _write_qc_artifacts(filtered, context, output_name=output_name, report=report)
    return {
        "summary": (
            f"Filtered {before - filtered.n_obs:,} of {before:,} cells; "
            f"{filtered.n_obs:,} remain."
        ),
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "id": metadata["dataset_revision_id"],
                    "n_cells": int(filtered.n_obs),
                    "n_genes": int(filtered.n_vars),
                },
                "cell_set": {"id": cell_set_id, "n_cells": int(filtered.n_obs)},
                "count_representation": {
                    "id": count_id,
                    "matrix_id": matrix_id,
                    "method": "counts-after-cell-filter",
                    "source_layer": layer or "X",
                },
                "representation": None,
                "clustering": None,
            },
            "cell_qc": None,
            "doublets": None,
            "batch": None,
            "cluster_qc": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": artifacts,
    }


def filter_genes(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import numpy as np

    if arguments.get("confirm_filtering") is not True:
        raise ValueError("confirm_filtering must be true before changing the feature set")
    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    layer_arg = arguments.get("counts_layer", "auto")
    layer = str(layer_arg) if layer_arg is not None else None
    min_cells = int(arguments.get("min_cells", 3))
    adata = _read_matrix(path)
    layer = _resolve_layer(adata, layer)
    counts = _matrix(adata, layer)
    detected = np.asarray((counts > 0).sum(axis=0)).ravel()
    before = int(adata.n_vars)
    filtered = adata[:, detected >= min_cells].copy()
    if filtered.n_vars == 0:
        raise ValueError("requested threshold would remove every gene")
    metadata = _base_metadata(filtered)
    cell_set_id = metadata.get("cell_set_id") or _identity(
        "cells", sorted(map(str, filtered.obs_names))
    )
    filtered_counts = _matrix(filtered, layer)
    matrix_id = _count_matrix_identity(
        filtered_counts, filtered.obs_names, filtered.var_names
    )
    count_id = _identity(
        "count-representation",
        {
            "matrix_id": matrix_id,
            "cell_set_id": cell_set_id,
            "operation": "filter-genes",
            "min_cells": min_cells,
        },
    )
    revision_id = _identity(
        "dataset-revision",
        {
            "parent_path": str(path),
            "genes": sorted(map(str, filtered.var_names)),
            "count_representation_id": count_id,
            "operation": "filter-genes",
            "min_cells": min_cells,
        },
    )
    metadata.update(
        {
            "dataset_revision_id": revision_id,
            "cell_set_id": cell_set_id,
            "count_representation_id": count_id,
            "count_matrix_id": matrix_id,
        }
    )
    for key in (
        "representation_id",
        "clustering_id",
        "qc_assessment_id",
    ):
        metadata.pop(key, None)
    filtered.uns["scagent_sdk"] = metadata
    output_name = "genes-filtered.zarr"
    _write_matrix(filtered, context.staging_dir / output_name)
    report = {
        "operation": "filter_genes",
        "before_genes": before,
        "after_genes": int(filtered.n_vars),
        "removed_genes": before - int(filtered.n_vars),
        "min_cells": min_cells,
    }
    (context.staging_dir / "gene-filter-summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "summary": (
            f"Filtered {before - filtered.n_vars:,} of {before:,} genes; "
            f"{filtered.n_vars:,} remain."
        ),
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "id": revision_id,
                    "n_cells": int(filtered.n_obs),
                    "n_genes": int(filtered.n_vars),
                },
                "cell_set": {"id": cell_set_id, "n_cells": int(filtered.n_obs)},
                "count_representation": {
                    "id": count_id,
                    "matrix_id": matrix_id,
                    "method": "counts-after-gene-filter",
                    "source_layer": layer or "X",
                },
                "representation": None,
                "clustering": None,
            },
            "cell_qc": None,
            "batch": None,
            "cluster_qc": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": [
            {
                "name": "gene-filtered-anndata",
                "relative_path": output_name,
                "media_type": "application/vnd.zarr",
            },
            {
                "name": "gene-filter-summary",
                "relative_path": "gene-filter-summary.json",
                "media_type": "application/json",
            },
        ],
    }
