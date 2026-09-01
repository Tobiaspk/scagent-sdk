"""Deterministic single-cell figures: QC, embeddings, composition, agreement, markers.

Each tool reads one H5AD artifact, produces figures plus the table behind them, and returns the
figures as model media so the plot is actually inspected rather than merely saved. Decision
logic — key resolution, layout choice, category selection, agreement arithmetic — is kept in
plain-Python helpers so it is testable without the compute environment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIGURE_DPI = 160
# Above this many categories a legend stops being readable and the figure switches strategy.
LEGEND_CARDINALITY_LIMIT = 12
MAX_PANEL_COLUMNS = 2
PANEL_BASE_WIDTH = 6.0
PANEL_HEIGHT = 5.6
MAX_CATEGORIES = 30
LEGEND_FONT_SIZE = 7
# Inches one legend character occupies at LEGEND_FONT_SIZE, plus the marker and padding each
# entry carries. Used to size the figure around the legend it will actually draw.
LEGEND_CHAR_WIDTH = 0.055
LEGEND_ENTRY_PADDING = 0.45
LEGEND_MIN_LABEL_CHARS = 16
MAX_PANEL_WIDTH = 22.0
# Distinct colors available for categorical overlays: tab20 + tab20b + tab20c, none repeated.
CATEGORY_PALETTE_MAPS = ("tab20", "tab20b", "tab20c")
CATEGORY_PALETTE_SIZE = 60
MAX_GENES = 60
MITO_PREFIXES = ("MT-", "mt-", "Mt-")
RIBO_PREFIXES = ("RPS", "RPL")
HISTOGRAM_BINS = 60
# Per-cell points drawn over a violin body. Every cell is informative up to the point where the
# strip is solid ink; past that the scatter only costs render time and file size.
VIOLIN_JITTER_CELLS = 20000
VIOLIN_JITTER_WIDTH = 0.2
# A highlight grid is worth drawing when a single overlaid view cannot separate the categories,
# and stops being worth it when the individual panels are too small to read.
GRID_MIN_CATEGORIES = 2
GRID_MAX_CATEGORIES = 150
# Companion grids per `plot_embedding` call. Nine categorical panels would otherwise return nine
# extra full figures, which buries the primary figure it was meant to support.
MAX_GRID_COMPANIONS = 4
GRID_BACKGROUND_COLOR = "#cccccc"


# --- pure helpers -------------------------------------------------------------


def _read_matrix(path):
    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)

def resolve_value_key(key: str, obs_columns: list[str], var_names: list[str]) -> tuple[str, str]:
    """Resolve a requested color/value key to an obs column or a gene, or explain the failure."""

    if key in obs_columns:
        return "obs", key
    if key in var_names:
        return "gene", key
    obs_hint = ", ".join(sorted(obs_columns)[:12]) or "none"
    raise ValueError(
        f"{key!r} is neither an obs column nor a gene in this artifact. "
        f"Available obs columns include: {obs_hint}."
    )


def resolve_embedding_key(key: str | None, obsm_keys: list[str]) -> str:
    """Pick the embedding to plot, preferring an explicit key then a conventional one."""

    if key:
        if key in obsm_keys:
            return key
        available = ", ".join(sorted(obsm_keys)) or "none"
        raise ValueError(f"embedding {key!r} is absent; available embeddings: {available}")
    for candidate in ("X_umap", "umap", "X_tsne", "X_scimilarity", "X_scVI", "X_pca"):
        if candidate in obsm_keys:
            return candidate
    raise ValueError(
        "this artifact has no embedding to plot; compute UMAP (or pass an embedding_key such as "
        "X_pca or X_scimilarity) first"
    )


def panel_grid(n_panels: int) -> tuple[int, int]:
    """Rows and columns for n panels, capped at MAX_PANEL_COLUMNS per row."""

    if n_panels < 1:
        raise ValueError("at least one panel is required")
    columns = min(n_panels, MAX_PANEL_COLUMNS)
    rows = -(-n_panels // columns)
    return rows, columns


def legend_columns(legend_entries: int) -> int:
    """Columns the legend will be drawn in, matching what `plot_embedding` requests."""

    return 1 if legend_entries <= LEGEND_CARDINALITY_LIMIT else 2


def panel_width(legend_entries: int, longest_label: int = 0) -> float:
    """Width in inches for one embedding panel including the space its legend needs.

    A legend drawn outside the axes takes its width from the axes unless the figure budgets for
    it, which is how scatter panels end up as unreadable slivers. Reserving a *constant* was not
    enough: legend width is set by the longest label, not the entry count, and reference-model
    vocabularies routinely emit 50-60 character Cell Ontology names such as "effector memory
    CD8-positive, alpha-beta T cell, terminally differentiated". Measure it instead.
    """

    if legend_entries <= 0:
        return PANEL_BASE_WIDTH + 1.1  # colorbar
    characters = max(longest_label, LEGEND_MIN_LABEL_CHARS)
    per_column = LEGEND_ENTRY_PADDING + LEGEND_CHAR_WIDTH * characters
    reserved = legend_columns(legend_entries) * per_column
    return min(PANEL_BASE_WIDTH + reserved, MAX_PANEL_WIDTH)


def figure_layout(n_groups: int, n_classes: int) -> dict[str, Any]:
    """Choose a readable composition layout; many categories become a heatmap, not a legend."""

    if n_classes > LEGEND_CARDINALITY_LIMIT:
        width = float(max(6.0, min(0.35 * n_classes + 3.0, 26.0)))
        height = float(max(4.0, min(0.30 * n_groups + 2.0, 22.0)))
        return {
            "mode": "heatmap",
            "figsize": (width, height),
            "annotate": n_classes <= 30 and n_groups <= 30,
            "tick_fontsize": 7 if max(n_classes, n_groups) > 20 else 8,
        }
    width = float(max(7.0, min(0.5 * n_groups + 2.0, 26.0)))
    return {
        "mode": "bar",
        "figsize": (width, 5.0),
        "legend_ncol": 1 if n_classes <= 8 else 2,
        "rotate_xticks": n_groups > 12,
    }


def category_color_limit(requested: int) -> int:
    """Cap kept categories at the number of distinct colors that exist.

    Cycling a twenty-color map over thirty labels hands two different cell types the same color,
    which makes the figure wrong rather than merely ugly. Categories past the palette collapse
    into the explicit `other` entry instead.
    """

    if requested < 1:
        raise ValueError("at least one category must be kept")
    return min(requested, CATEGORY_PALETTE_SIZE)


def stable_color_slots(
    labels: list[str], palette_size: int = CATEGORY_PALETTE_SIZE
) -> dict[str, int]:
    """Map each label to a palette slot from a stable ordering of the labels, never from counts.

    Assigning colors in count order repaints a figure whenever the counts move — after cell
    filtering, at a different `max_categories`, or when the same annotation is plotted on two
    embeddings — so the same population is blue in one view and orange in the next and two figures
    of one dataset cannot be compared. Comparing views is the normal activity here.

    Slots are taken in sorted label order, so they depend only on *which labels are present*, not
    on how many cells each holds. Consecutive slots are used, which keeps the well-separated front
    of the palette in play: measured minimum pairwise separation at thirty categories is CIE76
    dE 9.3, against 3.6 for a digest-scattered assignment and 0.0 for the cycled twenty-color map
    this replaced.

    Residual limitation: a label entering or leaving shifts the labels ordered after it. Full
    stability across a changing label set needs a palette persisted in scientific state and
    handed back through `CapabilityContext.state_facts`; that is deliberately not done here.
    """

    if palette_size < 1:
        raise ValueError("palette_size must be positive")
    unique = sorted(set(labels))
    if len(unique) > palette_size:
        raise ValueError(
            f"{len(unique)} labels exceed the {palette_size} distinct colors available"
        )
    return {label: index for index, label in enumerate(unique)}


def positive_span(values: list[float]) -> tuple[float, float, int]:
    """Smallest and largest strictly positive value, and how many values were not positive.

    A log axis cannot show a zero, so the count of dropped cells is returned rather than
    discarded: "1,204 cells have zero counts" is QC evidence, and silently omitting them from a
    library-size histogram is how an empty-droplet population disappears from the figure that was
    supposed to reveal it.
    """

    positive = [float(value) for value in values if float(value) > 0.0]
    dropped = len(values) - len(positive)
    if not positive:
        raise ValueError("no positive values to plot on a log scale")
    return min(positive), max(positive), dropped


def log_spaced_bins(minimum: float, maximum: float, count: int = HISTOGRAM_BINS) -> list[float]:
    """Bin edges uniform in log space, for a metric drawn on a log axis.

    Linear bins under a log axis is the standard mistake, and the original scagent run makes it:
    the leftmost bar spans hundreds of counts while the rightmost spans tens of thousands, so the
    low end of the distribution is compressed into two or three blocks and the shape that decides
    a threshold cannot be read. Uniform-in-log edges give every bar the same width on the drawn
    axis.
    """

    import math

    if count < 1:
        raise ValueError("count must be at least 1")
    if minimum <= 0.0:
        raise ValueError("log-spaced bins need a positive minimum; drop non-positive values first")
    if maximum <= minimum:
        # A degenerate span still has to produce usable edges rather than a zero-width axis.
        maximum = minimum * 10.0
    low, high = math.log10(minimum), math.log10(maximum)
    step = (high - low) / count
    return [10.0 ** (low + step * index) for index in range(count + 1)]


def highlight_grid_layout(n_categories: int) -> dict[str, Any]:
    """Rows, columns, and figure size for a one-panel-per-category highlight grid."""

    if n_categories < 1:
        raise ValueError("a highlight grid needs at least one category")
    columns = 6 if n_categories > 30 else min(5, n_categories)
    rows = -(-n_categories // columns)
    return {
        "rows": rows,
        "columns": columns,
        "figsize": (2.6 * columns, 2.75 * rows),
    }


def grid_point_sizes(n_cells: int) -> tuple[float, float]:
    """Background and foreground marker areas for a highlight-grid panel.

    Panels are small, so the sizes that work on a full-size embedding do not transfer: the
    background must stay faint enough to read as context and the highlighted cells must stay
    visible when a category holds a few dozen cells.
    """

    if n_cells < 1:
        raise ValueError("point sizes need at least one cell")
    background = max(0.4, min(4.0, 80_000.0 / n_cells))
    foreground = max(0.8, min(7.0, 120_000.0 / n_cells))
    return background, foreground


def should_render_grid(n_categories: int) -> bool:
    """Whether a per-category grid adds anything over the single overlaid panel."""

    return GRID_MIN_CATEGORIES <= n_categories <= GRID_MAX_CATEGORIES


def figure_slug(key: str) -> str:
    """Filesystem-safe fragment naming a figure after the obs column it shows."""

    cleaned = "".join(character if character.isalnum() else "-" for character in str(key))
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-").lower() or "panel"


def natural_order(names: list[str]) -> list[str]:
    """Sort labels the way a reader looks them up: numerically where they are numbers.

    `select_top_categories` orders by size because size is the *selection* criterion, but size
    order is the wrong *display* order — finding cluster 24 in a grid laid out 3, 1, 5, 8, 4, 18
    means scanning every panel, and two figures of the same clustering shuffle whenever a filter
    moves the counts. Plain string sort is no better ("10" before "2"), so digit runs are
    compared as integers.
    """

    def key(name: str) -> tuple[tuple[int, int, str], ...]:
        parts: list[tuple[int, int, str]] = []
        current = ""
        for character in str(name) + "\0":
            if character.isdigit() == current.isdigit() and character != "\0":
                current += character
                continue
            if current:
                parts.append(
                    (1, int(current), "") if current.isdigit() else (0, 0, current.lower())
                )
            current = character if character != "\0" else ""
        return tuple(parts)

    return sorted(names, key=key)


def compact_tick(value: float) -> str:
    """Short axis-tick text for count-scale numbers.

    Spelled-out thousands ("10,000", "20,000", "50,000") overrun each other on a five-panel row,
    which reintroduces the collision the 1/2/5 locator was chosen to avoid.
    """

    magnitude = abs(float(value))
    if magnitude >= 1_000_000:
        return f"{float(value) / 1_000_000:g}M"
    if magnitude >= 1_000:
        return f"{float(value) / 1_000:g}k"
    return f"{float(value):g}"


def is_ribo(symbol: str) -> bool:
    return str(symbol).upper().startswith(RIBO_PREFIXES)


def select_top_categories(counts: dict[str, int], top_n: int) -> tuple[list[str], int]:
    """Keep the largest categories in descending size, reporting how many were collapsed."""

    if top_n < 1:
        raise ValueError("top_n must be at least 1")
    ordered = sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))
    kept = [str(name) for name, _ in ordered[:top_n]]
    return kept, max(0, len(ordered) - len(kept))


def agreement_summary(first: list[str], second: list[str]) -> dict[str, Any]:
    """Exact-match agreement plus the most frequent disagreements between two label columns."""

    if len(first) != len(second):
        raise ValueError("label columns must describe the same cells")
    if not first:
        raise ValueError("no cells to compare")
    matches = sum(1 for a, b in zip(first, second, strict=True) if str(a) == str(b))
    disagreements: dict[tuple[str, str], int] = {}
    for a, b in zip(first, second, strict=True):
        if str(a) != str(b):
            key = (str(a), str(b))
            disagreements[key] = disagreements.get(key, 0) + 1
    top = sorted(disagreements.items(), key=lambda item: (-item[1], item[0]))[:10]
    return {
        "n_cells": len(first),
        "exact_agreement": matches / len(first),
        "top_disagreements": [
            {"first": pair[0], "second": pair[1], "cells": count} for pair, count in top
        ],
    }


def looks_like_counts(maximum: float, has_fractional: bool) -> bool:
    """Raw counts are nonnegative integers with a large dynamic range; log data are not."""

    return not has_fractional and maximum >= 20


def mito_mask_source(var_names: list[str], symbol_column: list[str] | None) -> list[str]:
    """Gene symbols to test for mitochondrial prefixes, preferring an explicit symbol column."""

    if symbol_column is not None and len(symbol_column) == len(var_names):
        return [str(item) for item in symbol_column]
    return [str(item) for item in var_names]


def is_mito(symbol: str) -> bool:
    return str(symbol).upper().startswith("MT-")


# --- shared plumbing ----------------------------------------------------------


def _load(arguments: dict[str, Any]) -> tuple[Path, Any]:

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    return path, _read_matrix(path)


def _figure(context: Any, name: str, title: str) -> dict[str, str]:
    return {"name": name, "relative_path": f"{name}.png", "media_type": "image/png"}


def _save(figure: Any, context: Any, name: str) -> None:
    figure.savefig(
        context.staging_dir / f"{name}.png", dpi=FIGURE_DPI, bbox_inches="tight"
    )


def _write_json(context: Any, name: str, value: Any) -> dict[str, str]:
    (context.staging_dir / f"{name}.json").write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return {
        "name": name,
        "relative_path": f"{name}.json",
        "media_type": "application/json",
    }


def _write_csv(context: Any, name: str, frame: Any) -> dict[str, str]:
    frame.to_csv(context.staging_dir / f"{name}.csv")
    return {"name": name, "relative_path": f"{name}.csv", "media_type": "text/csv"}


def _matrix(adata: Any, layer: str | None) -> Any:
    if layer in (None, "X"):
        return adata.X
    if layer not in adata.layers:
        available = ", ".join(sorted(adata.layers)) or "none"
        raise ValueError(f"layer {layer!r} is absent; available layers: {available}")
    return adata.layers[layer]


def _dense_column(matrix: Any, index: int) -> Any:
    import numpy as np

    column = matrix[:, index]
    if hasattr(column, "toarray"):
        column = column.toarray()
    return np.asarray(column).ravel()


def _prepare_expression(adata: Any, layer: str | None) -> tuple[Any, bool]:
    """Return a log-normalized view for expression plots, normalizing raw counts when needed."""

    import numpy as np
    import scanpy as sc

    matrix = _matrix(adata, layer)
    sample = matrix[: min(200, adata.n_obs)]
    if hasattr(sample, "toarray"):
        sample = sample.toarray()
    sample = np.asarray(sample, dtype=float)
    maximum = float(sample.max()) if sample.size else 0.0
    fractional = bool(sample.size and not np.allclose(sample, np.rint(sample)))
    if not looks_like_counts(maximum, fractional):
        return matrix, False
    prepared = adata.copy()
    if layer not in (None, "X"):
        prepared.X = prepared.layers[layer].copy()
    sc.pp.normalize_total(prepared, target_sum=1e4)
    sc.pp.log1p(prepared)
    return prepared.X, True


def _distinct_colors(count: int) -> list[Any]:
    """`count` colors with no repeats, drawn from the concatenated qualitative maps."""

    import matplotlib.pyplot as plt

    colors: list[Any] = []
    for name in CATEGORY_PALETTE_MAPS:
        colors.extend(plt.get_cmap(name).colors)
    if count > len(colors):
        raise ValueError(
            f"{count} categories exceed the {len(colors)} distinct colors available; "
            "lower max_categories so the remainder collapses into 'other'"
        )
    return colors[:count]


def _categorical(adata: Any, key: str) -> Any:
    if key not in adata.obs:
        available = ", ".join(sorted(map(str, adata.obs.columns))[:15]) or "none"
        raise ValueError(f"obs column {key!r} is absent; available columns include: {available}")
    return adata.obs[key].astype(str)


def _plain_log_ticks(axis: Any, which: str = "x") -> None:
    """Label a log axis with plain numbers at 1/2/5 per decade instead of mantissa notation.

    Matplotlib's default over a two-decade range labels the minor ticks as "2 x 10^2", "3 x 10^2",
    ... which run together into an unreadable smear — visible on the genes-per-cell panel of the
    original scagent run. Decade-only labels are legible but too sparse to read a threshold off,
    so 1/2/5 per decade is used and written out in full.
    """

    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

    target = axis.xaxis if which == "x" else axis.yaxis
    target.set_major_locator(LogLocator(base=10.0, subs=(1.0, 2.0, 5.0)))
    target.set_major_formatter(FuncFormatter(lambda value, _: compact_tick(value)))
    target.set_minor_formatter(NullFormatter())


def _violin_panel(axis: Any, values: Any, label: str, rng: Any) -> int:
    """Draw one violin body with the per-cell points jittered over it; return points drawn.

    The body alone is a smoothed density and hides how many cells produced it — a violin over
    forty cells and a violin over forty thousand look identical. The original scagent figures
    overlay the cells (`sc.pl.violin(..., jitter=0.2)`) for exactly that reason, so the reader
    sees the sample as well as its shape.
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


def _highlight_grid_figure(
    plt: Any,
    np: Any,
    coordinates: Any,
    labels: Any,
    categories: list[str],
    colors: list[Any],
    title: str,
) -> Any:
    """One small embedding panel per category: that category colored, every other cell grey.

    A single overlaid scatter stops resolving categories long before it stops drawing them —
    thirty clusters over one UMAP is thirty colors competing for the same pixels, and a small or
    spatially split population is simply unfindable in it. Splitting into panels trades color
    discrimination for position, which the eye reads reliably at any cardinality.
    """

    layout = highlight_grid_layout(len(categories))
    rows, columns = int(layout["rows"]), int(layout["columns"])
    figure, axes = plt.subplots(rows, columns, figsize=layout["figsize"], squeeze=False)
    background_size, foreground_size = grid_point_sizes(int(coordinates.shape[0]))
    # Panels are only comparable if they share limits; autoscaling each one would silently
    # rescale a panel whose category sits in a corner.
    x_low, x_high = float(coordinates[:, 0].min()), float(coordinates[:, 0].max())
    y_low, y_high = float(coordinates[:, 1].min()), float(coordinates[:, 1].max())
    x_pad = max((x_high - x_low) * 0.03, 1e-6)
    y_pad = max((y_high - y_low) * 0.03, 1e-6)
    for index, category in enumerate(categories):
        axis = axes[index // columns][index % columns]
        selected = np.asarray(labels == category)
        other = ~selected
        if other.any():
            axis.scatter(
                coordinates[other, 0],
                coordinates[other, 1],
                c=GRID_BACKGROUND_COLOR,
                s=background_size,
                alpha=0.25,
                linewidths=0,
                rasterized=True,
            )
        axis.scatter(
            coordinates[selected, 0],
            coordinates[selected, 1],
            c=[colors[index]],
            s=foreground_size,
            alpha=0.9,
            linewidths=0,
            rasterized=True,
        )
        axis.set_title(f"{category}  ({int(selected.sum()):,})", fontsize=8, pad=3)
        axis.set_xlim(x_low - x_pad, x_high + x_pad)
        axis.set_ylim(y_low - y_pad, y_high + y_pad)
        axis.set_aspect("equal", adjustable="box")
        axis.set_axis_off()
    for index in range(len(categories), rows * columns):
        axes[index // columns][index % columns].set_visible(False)
    figure.suptitle(title, fontsize=11)
    figure.subplots_adjust(hspace=0.28, wspace=0.04)
    return figure


def _envelope(
    summary: str,
    details: dict[str, Any],
    artifacts: list[dict[str, str]],
    media: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "summary": summary,
        "details": details,
        "artifacts": artifacts,
        "model_media": media,
    }


# --- tools --------------------------------------------------------------------


def plot_qc_distributions(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Library size, detected genes, mitochondrial fraction, and the UMI-rank knee."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    path, adata = _load(arguments)
    layer = arguments.get("counts_layer")
    layer = str(layer) if layer is not None else None
    symbol_key = arguments.get("gene_symbol_column")
    group_key = arguments.get("group_key")
    matrix = _matrix(adata, layer)
    totals = np.asarray(matrix.sum(axis=1)).ravel()
    if hasattr(matrix, "getnnz"):
        detected = matrix.getnnz(axis=1)
    else:
        detected = (np.asarray(matrix) > 0).sum(axis=1)
    detected = np.asarray(detected).ravel()
    symbols = mito_mask_source(
        [str(name) for name in adata.var_names],
        [str(item) for item in adata.var[symbol_key]] if symbol_key in adata.var else None,
    )
    def _fraction(indices: list[int]) -> Any:
        if not indices:
            return np.zeros(adata.n_obs, dtype=float)
        subtotal = np.asarray(matrix[:, indices].sum(axis=1)).ravel()
        return np.divide(
            subtotal * 100.0, totals, out=np.zeros_like(totals, dtype=float), where=totals > 0
        )

    def _class_percent(indices: list[int], obs_column: str) -> tuple[Any, str]:
        """Percent of counts from a gene class, preferring the value QC already recorded.

        Recomputing from the matrix is wrong on any artifact whose genes have been filtered:
        this tool measured 0% ribosomal on an annotated object whose RPS/RPL genes had been
        dropped downstream, while the correct 21% was sitting in `pct_counts_ribo`. The gene
        class can be removed from the matrix; the fraction it contributed to the original
        library cannot be recovered from what is left. Prefer the recorded column, fall back to
        computing, and report which was used so the number is never ambiguous.
        """

        if obs_column in adata.obs:
            recorded = np.asarray(adata.obs[obs_column], dtype=float)
            if np.isfinite(recorded).any():
                return recorded, f"obs:{obs_column}"
        if not indices:
            return np.zeros(adata.n_obs, dtype=float), "absent"
        return _fraction(indices), f"computed from {len(indices)} genes"

    mito_index = [index for index, symbol in enumerate(symbols) if is_mito(symbol)]
    ribo_index = [index for index, symbol in enumerate(symbols) if is_ribo(symbol)]
    mito_pct, mito_source = _class_percent(mito_index, "pct_counts_mt")
    ribo_pct, ribo_source = _class_percent(ribo_index, "pct_counts_ribo")
    rng = np.random.default_rng(0)
    artifacts: list[dict[str, str]] = []
    media: list[dict[str, str]] = []
    dropped_note: dict[str, int] = {}

    def _log_histogram(axis: Any, values: Any, label: str) -> None:
        """Histogram on a log axis with bins that are uniform *on that axis*."""

        low, high, dropped = positive_span([float(item) for item in values.tolist()])
        if dropped:
            dropped_note[label] = dropped
        axis.hist(
            values[values > 0],
            bins=log_spaced_bins(low, high),
            color="#4c72b0",
            edgecolor="#22405f",
            linewidth=0.3,
        )
        axis.set_xscale("log")
        _plain_log_ticks(axis)
        axis.set_xlabel(f"{label} (log scale)")
        axis.set_ylabel("cells")
        title = label if not dropped else f"{label} ({dropped:,} non-positive omitted)"
        axis.set_title(title)

    # --- distributions: knee, the two heavy-tailed size metrics, and the two fraction metrics ---
    figure, axes = plt.subplots(1, 5, figsize=(23.0, 4.2))
    ranked = np.sort(totals)[::-1]
    axes[0].plot(np.arange(1, ranked.size + 1), np.maximum(ranked, 1e-9), color="#1f77b4")
    axes[0].set_xscale("log")
    axes[0].set_yscale("log")
    axes[0].set_xlabel("cell rank")
    axes[0].set_ylabel("total counts")
    axes[0].set_title("UMI rank (knee)")
    _log_histogram(axes[1], totals, "total counts per cell")
    _log_histogram(axes[2], detected.astype(float), "genes detected per cell")
    for axis, values, label, source in (
        (axes[3], mito_pct, "mitochondrial percent", mito_source),
        (axes[4], ribo_pct, "ribosomal percent", ribo_source),
    ):
        axis.hist(values, bins=HISTOGRAM_BINS, color="#4c72b0", edgecolor="#22405f", linewidth=0.3)
        axis.set_xlabel(label)
        axis.set_ylabel("cells")
        axis.set_title(
            f"{label} (no matching genes found)" if source == "absent" else f"{label}\n[{source}]",
            fontsize=10,
        )
    figure.suptitle(f"QC distributions — {path.name} ({adata.n_obs:,} cells)")
    figure.tight_layout()
    _save(figure, context, "qc-distributions")
    plt.close(figure)
    artifacts.append(_figure(context, "qc-distributions", "QC distributions"))
    media.append(dict(artifacts[-1]))

    # --- violins: log1p for the size metrics so the body is not one spike against a long tail ---
    violin_panels = [
        (np.log1p(totals), "log1p_total_counts"),
        (np.log1p(detected.astype(float)), "log1p_n_genes_by_counts"),
        (mito_pct, "pct_counts_mt"),
        (ribo_pct, "pct_counts_ribo"),
    ]
    figure, axes = plt.subplots(1, len(violin_panels), figsize=(4.2 * len(violin_panels), 4.6))
    jitter_shown = 0
    for axis, (values, label) in zip(axes, violin_panels, strict=True):
        jitter_shown = _violin_panel(axis, values, label, rng)
    figure.suptitle(f"QC metric distributions — {adata.n_obs:,} cells")
    figure.tight_layout()
    _save(figure, context, "qc-violins")
    plt.close(figure)
    artifacts.append(_figure(context, "qc-violins", "QC violins"))
    media.append(dict(artifacts[-1]))

    # --- scatters: the joint structure the marginal histograms cannot show ---
    figure, axes = plt.subplots(1, 3, figsize=(16.5, 4.8))
    positive = totals > 0
    axes[0].scatter(
        totals[positive], np.maximum(detected[positive], 1), s=3, alpha=0.35, linewidths=0,
        color="#555555", rasterized=True,
    )
    axes[0].set(xscale="log", yscale="log", xlabel="total counts", ylabel="genes detected")
    _plain_log_ticks(axes[0], "x")
    _plain_log_ticks(axes[0], "y")
    axes[0].set_title("genes versus counts (log-log)")
    axes[1].scatter(
        totals[positive], mito_pct[positive], s=3, alpha=0.35, linewidths=0, color="#555555",
        rasterized=True,
    )
    axes[1].set(xscale="log", xlabel="total counts", ylabel="mitochondrial percent")
    _plain_log_ticks(axes[1], "x")
    axes[1].set_title("mitochondrial percent versus counts")
    axes[2].scatter(
        mito_pct, ribo_pct, s=3, alpha=0.35, linewidths=0, color="#555555", rasterized=True
    )
    axes[2].set(xlabel="mitochondrial percent", ylabel="ribosomal percent")
    axes[2].set_title("ribosomal versus mitochondrial")
    figure.suptitle("QC joint distributions")
    figure.tight_layout()
    _save(figure, context, "qc-scatter")
    plt.close(figure)
    artifacts.append(_figure(context, "qc-scatter", "QC scatter"))
    media.append(dict(artifacts[-1]))

    # --- doublets: only when a doublet call already exists; this tool never invents one ---
    doublet_column = next(
        (name for name in ("doublet_score", "scrublet_score") if name in adata.obs), None
    )
    if doublet_column:
        scores = np.asarray(adata.obs[doublet_column], dtype=float)
        figure, axes = plt.subplots(1, 2, figsize=(11.0, 4.4))
        _violin_panel(axes[0], scores, doublet_column, rng)
        axes[1].hist(
            scores[np.isfinite(scores)], bins=HISTOGRAM_BINS, color="#4c72b0",
            edgecolor="#22405f", linewidth=0.3,
        )
        axes[1].set(xlabel=doublet_column, ylabel="cells", title="doublet score histogram")
        figure.suptitle("Doublet evidence already present on this artifact")
        figure.tight_layout()
        _save(figure, context, "qc-doublets")
        plt.close(figure)
        artifacts.append(_figure(context, "qc-doublets", "Doublet scores"))
        media.append(dict(artifacts[-1]))

    if group_key:
        groups = _categorical(adata, str(group_key))
        names, collapsed = select_top_categories(
            groups.value_counts().to_dict(), int(arguments.get("max_groups", MAX_CATEGORIES))
        )
        # Largest groups are kept, but drawn in label order so a cluster can be found by name.
        names = natural_order(names)
        membership = [np.asarray(groups == name) for name in names]
        panels = (
            (totals.astype(float), "total counts", True),
            (detected.astype(float), "genes detected", True),
            (mito_pct, "mitochondrial percent", False),
            (ribo_pct, "ribosomal percent", False),
        )
        figure, axes = plt.subplots(
            len(panels), 1, figsize=(max(11.0, 0.55 * len(names) + 4.0), 3.1 * len(panels))
        )
        for axis, (values, label, log_scale) in zip(axes, panels, strict=True):
            grouped = [values[selected] for selected in membership]
            axis.boxplot(grouped, tick_labels=names, showfliers=False, patch_artist=True)
            # Library size and gene counts span orders of magnitude across cell types; on a
            # linear axis every low-complexity cluster collapses onto the floor and the
            # difference that distinguishes debris from a small cell is not visible.
            if log_scale and all(bool((group > 0).all()) for group in grouped if group.size):
                axis.set_yscale("log")
                _plain_log_ticks(axis, "y")
            axis.set_ylabel(label)
            axis.grid(axis="y", alpha=0.2)
            plt.setp(axis.get_xticklabels(), rotation=90, fontsize=7)
        axes[-1].set_xlabel(str(group_key))
        figure.suptitle(f"QC metrics by {group_key}")
        figure.tight_layout()
        _save(figure, context, "qc-by-group")
        plt.close(figure)
        artifacts.append(_figure(context, "qc-by-group", "QC by group"))
        media.append(dict(artifacts[-1]))
        if collapsed:
            artifacts.append(
                _write_json(context, "qc-group-note", {"collapsed_groups": collapsed})
            )

    details = {
        "input_path": str(path),
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "counts_layer": layer or "X",
        "mitochondrial_genes": len(mito_index),
        "ribosomal_genes": len(ribo_index),
        "mito_percent_source": mito_source,
        "ribo_percent_source": ribo_source,
        "median_total_counts": float(np.median(totals)),
        "median_genes_detected": float(np.median(detected)),
        "median_mito_percent": float(np.median(mito_pct)),
        "median_ribo_percent": float(np.median(ribo_pct)),
        "doublet_column": doublet_column,
        "log_scaled_panels": ["total counts per cell", "genes detected per cell"],
        "cells_omitted_from_log_panels": dropped_note,
        "violin_points_per_panel": jitter_shown,
        "figures": [item["name"] for item in artifacts if item["media_type"] == "image/png"],
    }
    artifacts.append(_write_json(context, "qc-distribution-facts", details))
    doublets = f" Doublet evidence plotted from {doublet_column}." if doublet_column else ""
    return _envelope(
        f"Plotted {len(media)} QC figures for {adata.n_obs:,} cells "
        f"(median {np.median(totals):,.0f} counts, {np.median(detected):,.0f} genes, "
        f"{np.median(mito_pct):.1f}% mitochondrial, {np.median(ribo_pct):.1f}% ribosomal). "
        "Library size and detected genes are drawn on a log axis with log-spaced bins."
        f"{doublets}",
        details,
        artifacts,
        media,
    )


def plot_embedding(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Scatter an existing embedding, one panel per requested obs column or gene."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    path, adata = _load(arguments)
    embedding_key = resolve_embedding_key(
        str(arguments["embedding_key"]) if arguments.get("embedding_key") else None,
        list(adata.obsm),
    )
    coordinates = np.asarray(adata.obsm[embedding_key])[:, :2]
    color_keys = [str(item) for item in (arguments.get("color_keys") or [])]
    if not color_keys:
        raise ValueError("color_keys must name at least one obs column or gene to color by")
    if len(color_keys) > 9:
        raise ValueError("plot at most nine panels at once so each stays legible")
    layer = arguments.get("expression_layer")
    layer = str(layer) if layer is not None else None
    point_size = float(arguments.get("point_size", 0.0)) or max(
        2.0, min(24.0, 12000.0 / max(adata.n_obs, 1))
    )
    obs_columns = [str(column) for column in adata.obs.columns]
    var_names = [str(name) for name in adata.var_names]
    resolved = [resolve_value_key(key, obs_columns, var_names) for key in color_keys]
    expression = None
    if any(kind == "gene" for kind, _ in resolved):
        expression, normalized = _prepare_expression(adata, layer)
    else:
        normalized = False

    # Decide each panel's kind before sizing: the legend a categorical panel needs is what makes
    # the figure wide enough to stay readable.
    max_categories = category_color_limit(int(arguments.get("max_categories", MAX_CATEGORIES)))
    panels: list[dict[str, Any]] = []
    for key, (kind, name) in zip(color_keys, resolved, strict=True):
        if kind == "gene":
            panels.append({"key": key, "kind": "gene", "name": name, "entries": 0})
            continue
        series = adata.obs[name]
        if series.dtype.kind in "fiu" and series.nunique() > LEGEND_CARDINALITY_LIMIT:
            panels.append({"key": key, "kind": "continuous", "name": name, "entries": 0})
            continue
        kept, collapsed = select_top_categories(
            series.astype(str).value_counts().to_dict(), max_categories
        )
        labels = [str(item) for item in kept] + ([f"other ({collapsed})"] if collapsed else [])
        panels.append(
            {
                "key": key,
                "kind": "categorical",
                "name": name,
                "kept": kept,
                "collapsed": collapsed,
                "entries": len(labels),
                "longest_label": max((len(label) for label in labels), default=0),
            }
        )

    rows, columns = panel_grid(len(color_keys))
    widths = [
        max(
            panel_width(
                int(panels[index]["entries"]),
                int(panels[index].get("longest_label", 0)),
            )
            for index in range(column, len(panels), columns)
        )
        for column in range(columns)
    ]
    figure, axes = plt.subplots(
        rows,
        columns,
        figsize=(sum(widths), PANEL_HEIGHT * rows),
        squeeze=False,
        gridspec_kw={"width_ratios": widths},
        layout="constrained",
    )
    rng = np.random.default_rng(int(arguments.get("random_seed", 0)))
    order = rng.permutation(coordinates.shape[0])
    legends: dict[str, Any] = {}
    for index, panel in enumerate(panels):
        key, kind, name = str(panel["key"]), str(panel["kind"]), str(panel["name"])
        axis = axes[index // columns][index % columns]
        if kind == "gene":
            assert expression is not None
            values = _dense_column(expression, var_names.index(name))[order]
            points = axis.scatter(
                coordinates[order, 0],
                coordinates[order, 1],
                c=values,
                s=point_size,
                cmap="viridis",
                linewidths=0,
            )
            figure.colorbar(points, ax=axis, label="log-normalized expression")
        elif kind == "continuous":
            values = np.asarray(adata.obs[name], dtype=float)[order]
            points = axis.scatter(
                coordinates[order, 0],
                coordinates[order, 1],
                c=values,
                s=point_size,
                cmap="viridis",
                linewidths=0,
            )
            figure.colorbar(points, ax=axis, label=name)
        else:
            kept = [str(item) for item in panel["kept"]]
            collapsed = int(panel["collapsed"])
            legends[name] = {"shown": len(kept), "collapsed": collapsed}
            palette = _distinct_colors(CATEGORY_PALETTE_SIZE)
            slots = stable_color_slots([str(item) for item in kept])
            mapping = {label: palette[slots[str(label)]] for label in kept}
            # The companion grid must use these exact colors: a population that is green in the
            # overlaid panel and orange in its own grid panel cannot be tracked between them.
            panel["mapping"] = mapping
            shuffled = adata.obs[name].astype(str).to_numpy()[order]
            colors = [mapping.get(label, (0.82, 0.82, 0.82, 1.0)) for label in shuffled]
            axis.scatter(
                coordinates[order, 0],
                coordinates[order, 1],
                c=colors,
                s=point_size,
                linewidths=0,
            )
            handles = [
                plt.Line2D([], [], marker="o", linestyle="", color=mapping[label], label=label)
                for label in kept
            ]
            if collapsed:
                handles.append(
                    plt.Line2D(
                        [],
                        [],
                        marker="o",
                        linestyle="",
                        color=(0.82, 0.82, 0.82, 1.0),
                        label=f"other ({collapsed})",
                    )
                )
            axis.legend(
                handles=handles,
                loc="center left",
                bbox_to_anchor=(1.01, 0.5),
                fontsize=LEGEND_FONT_SIZE,
                frameon=False,
                ncol=legend_columns(len(handles)),
                borderaxespad=0.0,
                handletextpad=0.4,
                labelspacing=0.35,
            )
        # An embedding is a 2D geometry; a panel squeezed into a tall strip misrepresents cluster
        # shape and separation, which is what the plot is read for. Forcing a square data area
        # also bounds the damage when a legend still wants more width than was reserved: the axes
        # shrinks evenly instead of collapsing into a sliver.
        axis.set_box_aspect(1)
        axis.set_title(key)
        axis.set_xlabel(f"{embedding_key}[0]")
        axis.set_ylabel(f"{embedding_key}[1]")
        axis.set_xticks([])
        axis.set_yticks([])
    for empty in range(len(color_keys), rows * columns):
        axes[empty // columns][empty % columns].axis("off")
    figure.suptitle(f"{embedding_key} — {path.name} ({adata.n_obs:,} cells)")
    _save(figure, context, "embedding")
    plt.close(figure)

    artifacts = [_figure(context, "embedding", "Embedding")]
    media = [dict(artifacts[0])]

    # A companion grid per categorical panel. This is emitted automatically rather than on
    # request because the overlaid panel is at its least readable exactly when nobody thinks to
    # ask for the grid — many clusters, similar colors, small populations.
    grids: dict[str, str] = {}
    grids_skipped: dict[str, str] = {}
    include_grids = bool(arguments.get("include_highlight_grids", True))
    categorical = (
        [panel for panel in panels if panel["kind"] == "categorical"] if include_grids else []
    )
    for panel in categorical:
        name = str(panel["name"])
        kept = [str(item) for item in panel["kept"]]
        if not should_render_grid(len(kept)):
            grids_skipped[name] = (
                f"{len(kept)} categories is outside the {GRID_MIN_CATEGORIES}"
                f"-{GRID_MAX_CATEGORIES} range where a grid is more readable than the overlay"
            )
            continue
        if len(grids) >= MAX_GRID_COMPANIONS:
            grids_skipped[name] = (
                f"at most {MAX_GRID_COMPANIONS} companion grids are returned per call; "
                "plot this key on its own to get its grid"
            )
            continue
        mapping = panel["mapping"]
        slug = figure_slug(name)
        # Panels are laid out in reading order, but each keeps the color it was given in the
        # overlaid panel, so the two figures stay cross-referenceable.
        ordered = natural_order(kept)
        grid_figure = _highlight_grid_figure(
            plt,
            np,
            coordinates,
            adata.obs[name].astype(str).to_numpy(),
            ordered,
            [mapping[label] for label in ordered],
            f"{name} on {embedding_key} — one panel per category "
            f"({len(kept)} shown, {adata.n_obs:,} cells)",
        )
        _save(grid_figure, context, f"embedding-grid-{slug}")
        plt.close(grid_figure)
        grids[name] = f"embedding-grid-{slug}.png"
        artifacts.append(_figure(context, f"embedding-grid-{slug}", f"{name} highlight grid"))
        media.append(dict(artifacts[-1]))

    details = {
        "input_path": str(path),
        "embedding_key": embedding_key,
        "color_keys": color_keys,
        "n_cells": int(adata.n_obs),
        "expression_normalized_internally": normalized,
        "legends": legends,
        "highlight_grids": grids,
        "highlight_grids_skipped": grids_skipped,
    }
    artifacts.append(_write_json(context, "embedding-facts", details))
    grid_note = (
        f" Per-category highlight grid{'s' if len(grids) > 1 else ''} also returned for "
        f"{', '.join(sorted(grids))}."
        if grids
        else ""
    )
    return _envelope(
        f"Plotted {embedding_key} for {adata.n_obs:,} cells colored by "
        f"{', '.join(color_keys)}.{grid_note}",
        details,
        artifacts,
        media,
    )


def plot_qc_embedding(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Paint every available standard QC metric and flag on an existing embedding."""

    _, adata = _load(arguments)
    preferred = (
        "total_counts",
        "n_genes_by_counts",
        "pct_counts_mt",
        "pct_counts_ribo",
        "doublet_score",
        "predicted_doublet",
    )
    keys = [column for column in preferred if column in adata.obs]
    keys.extend(
        column
        for column in map(str, adata.obs.columns)
        if column.startswith("qc_flag_") and column not in keys
    )
    keys = keys[:9]
    if not keys:
        raise ValueError(
            "no standard QC metrics or qc_flag_* columns are present; calculate QC first"
        )
    delegated = plot_embedding(
        {
            "path": arguments["path"],
            "color_keys": keys,
            "embedding_key": arguments.get("embedding_key"),
            "point_size": arguments.get("point_size", 0),
            "max_categories": arguments.get("max_categories", MAX_CATEGORIES),
            "include_highlight_grids": arguments.get("include_highlight_grids", True),
            "random_seed": arguments.get("random_seed", 0),
        },
        context,
    )
    # Every artifact this produced is a QC view, so the whole set is renamed rather than two
    # known filenames — the delegate now also emits one grid per categorical flag, and a
    # hard-coded rename map would leave those under a name that claims they are generic.
    for collection in ("artifacts", "model_media"):
        for item in delegated.get(collection, []):
            name = str(item.get("name", ""))
            if not name.startswith("embedding"):
                continue
            renamed = f"qc-{name}"
            old_path = str(item.get("relative_path", ""))
            suffix = Path(old_path).suffix
            new_path = f"{renamed}{suffix}"
            source = context.staging_dir / old_path
            if source.exists():
                source.replace(context.staging_dir / new_path)
            item["name"] = renamed
            item["relative_path"] = new_path
    delegated["summary"] = (
        f"Plotted {delegated['details']['embedding_key']} with {len(keys)} QC overlays: "
        + ", ".join(keys)
        + "."
    )
    delegated["details"]["qc_keys"] = keys
    return delegated


def plot_group_composition(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Composition of one categorical column within another, as fractions."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    path, adata = _load(arguments)
    group_key = str(arguments["group_key"])
    class_key = str(arguments["class_key"])
    groups = _categorical(adata, group_key)
    classes = _categorical(adata, class_key)
    kept_classes, collapsed_classes = select_top_categories(
        classes.value_counts().to_dict(), int(arguments.get("max_classes", MAX_CATEGORIES))
    )
    classes = classes.where(classes.isin(kept_classes), other="other")
    table = pd.crosstab(groups, classes)
    fractions = table.div(table.sum(axis=1), axis=0)
    layout = figure_layout(int(fractions.shape[0]), int(fractions.shape[1]))

    if layout["mode"] == "heatmap":
        figure, axis = plt.subplots(figsize=layout["figsize"])
        image = axis.imshow(
            fractions.to_numpy(), aspect="auto", cmap="viridis", vmin=0.0, vmax=1.0
        )
        axis.set_xticks(range(fractions.shape[1]))
        axis.set_xticklabels(
            [str(item) for item in fractions.columns],
            rotation=90,
            fontsize=layout["tick_fontsize"],
        )
        axis.set_yticks(range(fractions.shape[0]))
        axis.set_yticklabels(
            [str(item) for item in fractions.index], fontsize=layout["tick_fontsize"]
        )
        figure.colorbar(image, ax=axis, label=f"fraction of {group_key}")
    else:
        figure, axis = plt.subplots(figsize=layout["figsize"])
        fractions.plot(kind="bar", stacked=True, ax=axis, colormap="tab20", width=0.85)
        axis.set_ylabel(f"fraction within {group_key}")
        axis.legend(
            title=class_key,
            bbox_to_anchor=(1.02, 1.0),
            loc="upper left",
            ncol=layout["legend_ncol"],
            fontsize=8,
            frameon=False,
        )
        if layout["rotate_xticks"]:
            plt.setp(axis.get_xticklabels(), rotation=90, fontsize=8)
    axis.set_title(f"{class_key} composition within {group_key}")
    figure.tight_layout()
    _save(figure, context, "group-composition")
    plt.close(figure)

    details = {
        "input_path": str(path),
        "group_key": group_key,
        "class_key": class_key,
        "n_groups": int(fractions.shape[0]),
        "n_classes": int(fractions.shape[1]),
        "collapsed_classes": collapsed_classes,
        "layout": layout["mode"],
    }
    artifacts = [
        _figure(context, "group-composition", "Group composition"),
        _write_csv(context, "group-composition-fractions", fractions),
        _write_csv(context, "group-composition-counts", table),
        _write_json(context, "group-composition-facts", details),
    ]
    return _envelope(
        f"Plotted {class_key} composition across {fractions.shape[0]} {group_key} groups.",
        details,
        artifacts,
        [dict(artifacts[0])],
    )


def plot_label_agreement(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Cross-tabulate two label columns to show where independent annotations agree."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    path, adata = _load(arguments)
    first_key = str(arguments["first_key"])
    second_key = str(arguments["second_key"])
    if first_key == second_key:
        raise ValueError("compare two different label columns")
    first = _categorical(adata, first_key)
    second = _categorical(adata, second_key)
    max_labels = int(arguments.get("max_labels", 25))
    kept_first, collapsed_first = select_top_categories(first.value_counts().to_dict(), max_labels)
    kept_second, collapsed_second = select_top_categories(
        second.value_counts().to_dict(), max_labels
    )
    first = first.where(first.isin(kept_first), other="other")
    second = second.where(second.isin(kept_second), other="other")
    table = pd.crosstab(first, second)
    fractions = table.div(table.sum(axis=1).replace(0, 1), axis=0)
    summary = agreement_summary(
        [str(item) for item in adata.obs[first_key]],
        [str(item) for item in adata.obs[second_key]],
    )

    width = float(max(7.0, min(0.45 * fractions.shape[1] + 4.0, 24.0)))
    height = float(max(5.0, min(0.42 * fractions.shape[0] + 3.0, 22.0)))
    figure, axis = plt.subplots(figsize=(width, height))
    image = axis.imshow(fractions.to_numpy(), aspect="auto", cmap="magma", vmin=0.0, vmax=1.0)
    axis.set_xticks(range(fractions.shape[1]))
    axis.set_xticklabels([str(item) for item in fractions.columns], rotation=90, fontsize=7)
    axis.set_yticks(range(fractions.shape[0]))
    axis.set_yticklabels([str(item) for item in fractions.index], fontsize=7)
    axis.set_xlabel(second_key)
    axis.set_ylabel(first_key)
    axis.set_title(
        f"{first_key} vs {second_key} — {summary['exact_agreement']:.1%} exact label agreement"
    )
    figure.colorbar(image, ax=axis, label=f"fraction of each {first_key} label")
    figure.tight_layout()
    _save(figure, context, "label-agreement")
    plt.close(figure)

    details = {
        "input_path": str(path),
        "first_key": first_key,
        "second_key": second_key,
        "collapsed_first": collapsed_first,
        "collapsed_second": collapsed_second,
        **summary,
    }
    artifacts = [
        _figure(context, "label-agreement", "Label agreement"),
        _write_csv(context, "label-agreement-counts", table),
        _write_json(context, "label-agreement-facts", details),
    ]
    return _envelope(
        f"Compared {first_key} and {second_key} across {summary['n_cells']:,} cells: "
        f"{summary['exact_agreement']:.1%} exact label agreement. Exact string agreement "
        "understates concordance when the two vocabularies differ.",
        details,
        artifacts,
        [dict(artifacts[0])],
    )


def plot_marker_expression(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Dot plot of mean expression and detection fraction for genes across groups."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    path, adata = _load(arguments)
    group_key = str(arguments["group_key"])
    genes = [str(item) for item in (arguments.get("genes") or [])]
    if not genes:
        raise ValueError("genes must name at least one gene to plot")
    if len(genes) > MAX_GENES:
        raise ValueError(f"plot at most {MAX_GENES} genes at once so labels stay legible")
    var_names = [str(name) for name in adata.var_names]
    present = [gene for gene in genes if gene in var_names]
    missing = [gene for gene in genes if gene not in var_names]
    if not present:
        raise ValueError(
            f"none of the requested genes exist in this artifact: {', '.join(missing)}. "
            "Check the gene identifier type (symbols vs Ensembl IDs)."
        )
    # A gene absent after filtering is a fact about the data, not a reason to fail the figure —
    # but it must never be dropped silently, so it is reported in the summary and the facts.
    genes = present
    groups = _categorical(adata, group_key)
    kept, collapsed = select_top_categories(
        groups.value_counts().to_dict(), int(arguments.get("max_groups", MAX_CATEGORIES))
    )
    layer = arguments.get("expression_layer")
    expression, normalized = _prepare_expression(adata, str(layer) if layer is not None else None)

    means = np.zeros((len(kept), len(genes)), dtype=float)
    fractions = np.zeros_like(means)
    membership = {name: np.asarray(groups == name) for name in kept}
    for column, gene in enumerate(genes):
        values = _dense_column(expression, var_names.index(gene))
        for row, name in enumerate(kept):
            selected = values[membership[name]]
            if selected.size:
                means[row, column] = float(selected.mean())
                fractions[row, column] = float((selected > 0).mean())
    scaled = means.copy()
    spread = scaled.max(axis=0) - scaled.min(axis=0)
    safe = np.where(spread > 0, spread, 1.0)
    scaled = (scaled - scaled.min(axis=0)) / safe

    figure, axis = plt.subplots(
        figsize=(max(6.0, 0.5 * len(genes) + 4.0), max(4.0, 0.42 * len(kept) + 2.5))
    )
    grid_x, grid_y = np.meshgrid(np.arange(len(genes)), np.arange(len(kept)))
    points = axis.scatter(
        grid_x.ravel(),
        grid_y.ravel(),
        s=fractions.ravel() * 220.0 + 4.0,
        c=scaled.ravel(),
        cmap="Reds",
        edgecolors="#444444",
        linewidths=0.3,
    )
    axis.set_xticks(range(len(genes)))
    axis.set_xticklabels(genes, rotation=90, fontsize=8)
    axis.set_yticks(range(len(kept)))
    axis.set_yticklabels(kept, fontsize=8)
    axis.set_xlabel("gene")
    axis.set_ylabel(group_key)
    axis.set_title(f"Marker expression by {group_key}")
    figure.colorbar(points, ax=axis, label="mean expression (scaled per gene)")
    handles = [
        plt.Line2D(
            [], [], marker="o", linestyle="", markersize=size, color="#888888",
            label=f"{fraction:.0%} of cells",
        )
        for fraction, size in ((0.25, 5), (0.5, 8), (1.0, 11))
    ]
    axis.legend(
        handles=handles,
        loc="center left",
        bbox_to_anchor=(1.16, 0.5),
        frameon=False,
        fontsize=7,
        title="detected in",
        title_fontsize=7,
    )
    figure.tight_layout()
    _save(figure, context, "marker-expression")
    plt.close(figure)

    details = {
        "input_path": str(path),
        "group_key": group_key,
        "genes": genes,
        "genes_absent": missing,
        "groups_shown": kept,
        "collapsed_groups": collapsed,
        "expression_normalized_internally": normalized,
    }
    artifacts = [
        _figure(context, "marker-expression", "Marker expression"),
        _write_json(context, "marker-expression-facts", details),
    ]
    absent = f" {len(missing)} requested gene(s) absent: {', '.join(missing)}." if missing else ""
    return _envelope(
        f"Plotted {len(genes)} genes across {len(kept)} {group_key} groups "
        f"({'normalized internally' if normalized else 'using supplied values'}).{absent}",
        details,
        artifacts,
        [dict(artifacts[0])],
    )
