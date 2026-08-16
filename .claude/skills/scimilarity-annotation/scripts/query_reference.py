"""Query the SCimilarity reference cell atlas with centroids built from selected cells.

Per-cell kNN annotation answers "what label do nearby reference cells carry". This answers a
different question: *which* reference cells is this population like — what studies, tissues,
diseases, and annotated types they come from, and how internally coherent the query is. That
evidence characterizes novel or ambiguous populations in a way a single transferred label
cannot.

Cost is the design constraint. Opening the reference index loads a multi-gigabyte nearest-
neighbor structure and the full reference cell metadata into memory, so every input, asset,
gene, organism, and selection check runs first and one index load serves every requested
group. Query counts and result sizes are bounded explicitly rather than truncated silently.

Selection planning, ranking, and bounding are pure functions over plain values so they are
deterministically testable in the control plane, where the scientific stack is absent.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_assets import (  # noqa: E402  (sibling module; path inserted above)
    _best_gene_names,
    _identity,
    _select_counts,
    _validate_counts,
    declared_organism,
    model_fingerprint,
    read_gene_order,
    resolve_model,
    verify_species,
)

# Reference metadata columns worth summarizing, in reporting order. Which exist depends on the
# model release, so presence is measured and absences are reported rather than assumed.
_COMPOSITION_COLUMNS = (
    "celltype_name",
    "tissue_general",
    "tissue",
    "disease",
    "study",
)
_NEIGHBOR_COLUMNS = (
    "index",
    "celltype_name",
    "cell_type_ontology_term_id",
    "tissue",
    "tissue_general",
    "disease",
    "study",
    "sample",
    "prediction",
    "query_nn_dist",
)
_DISTANCE_COLUMN = "query_nn_dist"

# SCimilarity's coherence QC k-means-clusters the query cells, searches each sub-centroid, and
# counts how many of its 100 nearest reference cells also fall in the whole-query centroid's
# k neighbors. Below this many cells the sub-clustering is not meaningful, so QC is skipped and
# reported as skipped instead of producing a number nobody should trust.
_QC_MINIMUM_CELLS = 20
_QC_MAX_CLUSTERS = 10
_QC_NEIGHBORS_PER_SUBCENTROID = 100

_INLINE_QUERY_LIMIT = 25
_INLINE_TOP_N = 5
_REPORT_TOP_N = 10
_NEIGHBOR_ROW_CAP = 200_000
# Two thirds of the executor's 48 KiB inline ceiling, leaving room for the surrounding envelope.
_INLINE_BUDGET_BYTES = 32 * 1024
_CELL_IDS_QUERY_NAME = "selected-cells"

# Per-cell mode costs one atlas search plus one 43M-row metadata join per cell, so the default is
# deliberately tiny and going past the cap is an error rather than a long silent run. Centroid
# mode is the way to ask about a whole population.
_DEFAULT_MAX_QUERY_CELLS = 10
_SAMPLE_LEVELS = ("study", "sample", "tissue", "disease")
_BACKGROUND_COLUMNS = ("disease", "tissue_general")


def _read_matrix(path):
    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)

def _plan_cell_queries(
    obs_names: list[str],
    *,
    cell_ids: list[str] | None,
    group_selected: list[str] | None,
    max_query_cells: int,
) -> list[str]:
    """Resolve per-cell mode to an explicit, bounded list of query cells — never subsampled.

    Which cells were queried has to be reproducible from the recorded inputs, so a selection
    larger than the cap is refused with its size instead of being silently thinned.
    """

    known = set(obs_names)
    if cell_ids:
        requested = list(dict.fromkeys(str(value) for value in cell_ids))
        missing = [value for value in requested if value not in known]
        if missing:
            raise ValueError(
                f"{len(missing):,} of {len(requested):,} cell_ids are absent from this dataset; "
                f"first missing: {', '.join(missing[:5])}"
            )
    elif group_selected is not None:
        requested = list(dict.fromkeys(group_selected))
    else:
        raise ValueError("per-cell mode needs cell_ids or a group selection")
    if not requested:
        raise ValueError("the per-cell selection resolved to no cells")
    if len(requested) > max_query_cells:
        raise ValueError(
            f"per-cell mode would issue {len(requested):,} separate reference searches, above "
            f"max_query_cells={max_query_cells:,}. Each cell costs its own atlas search and "
            "metadata join. Name the cells of interest in cell_ids, raise max_query_cells "
            "deliberately, or use query_mode='centroid' to ask about the population at once."
        )
    return requested


def _exclusion_record(
    *, before: int, after: int, studies: list[str]
) -> dict[str, Any]:
    """Report what study exclusion removed, including when it removed everything."""

    record: dict[str, Any] = {
        "excluded_studies": studies,
        "neighbors_before": before,
        "neighbors_after": after,
        "neighbors_removed": before - after,
    }
    if after == 0 and before > 0:
        record["warning"] = (
            "every retrieved neighbor came from an excluded study, so this query has no "
            "independent reference support at this k; raise k or reconsider the query"
        )
    return record


def _plan_group_queries(
    labels: list[str],
    requested: list[str] | None,
    *,
    group_key: str,
    min_query_cells: int,
    max_queries: int,
) -> tuple[list[tuple[str, int]], list[dict[str, Any]]]:
    """Decide which groups become queries, refusing rather than silently truncating."""

    counts = Counter(labels)
    available = sorted(counts)
    values = [str(value) for value in requested] if requested else available
    unknown = [value for value in values if value not in counts]
    if unknown:
        shown = ", ".join(available[:25])
        suffix = f" (+{len(available) - 25:,} more)" if len(available) > 25 else ""
        raise ValueError(
            f"group_values not present in obs[{group_key!r}]: {', '.join(unknown[:5])}. "
            f"Available: {shown}{suffix}"
        )
    if len(values) > max_queries:
        raise ValueError(
            f"{len(values):,} groups in obs[{group_key!r}] exceeds max_queries={max_queries:,}. "
            "Each group is a separate reference search against one loaded index; name the "
            "groups of interest in group_values, or raise max_queries deliberately."
        )
    kept: list[tuple[str, int]] = []
    skipped: list[dict[str, Any]] = []
    for value in values:
        n_cells = int(counts[value])
        if n_cells < min_query_cells:
            skipped.append({"group": value, "n_cells": n_cells, "reason": "below min_query_cells"})
            continue
        kept.append((value, n_cells))
    if not kept:
        raise ValueError(
            f"every requested group in obs[{group_key!r}] has fewer than min_query_cells="
            f"{min_query_cells:,} cells; lower min_query_cells or select larger groups"
        )
    return kept, skipped


def _plan_cell_ids(
    obs_names: list[str],
    cell_ids: list[str],
    *,
    min_query_cells: int,
) -> int:
    """Validate an explicit cell selection and return how many cells it resolves to."""

    requested = [str(value) for value in cell_ids]
    known = set(map(str, obs_names))
    missing = [value for value in requested if value not in known]
    if missing:
        raise ValueError(
            f"{len(missing):,} of {len(requested):,} cell_ids are absent from this dataset; "
            f"first missing: {', '.join(missing[:5])}"
        )
    selected = len(set(requested))
    if selected < min_query_cells:
        raise ValueError(
            f"cell_ids selects {selected:,} cells, below min_query_cells={min_query_cells:,}; "
            "a centroid over fewer cells is not reliable evidence"
        )
    return selected


def _rank_counts(counts: dict[str, int], *, top_n: int) -> list[dict[str, Any]]:
    """Rank a value-count mapping by descending count, breaking ties by value for stability."""

    total = sum(counts.values())
    ranked = sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))
    return [
        {
            "value": str(value),
            "count": int(count),
            "fraction": round(float(count) / total, 4) if total else 0.0,
        }
        for value, count in ranked[:top_n]
    ]


def _distance_summary(values: Any) -> dict[str, Any] | None:
    import numpy as np

    if values is None:
        return None
    array = np.asarray(values, dtype=float).ravel()
    if array.size == 0:
        return None
    return {
        "minimum": round(float(np.min(array)), 6),
        "median": round(float(np.median(array)), 6),
        "mean": round(float(np.mean(array)), 6),
        "maximum": round(float(np.max(array)), 6),
    }


def _coherence_plan(n_query_cells: int, *, requested: bool) -> tuple[bool, int, str | None]:
    """Whether to measure coherence for this query, with how many sub-centroids, and why not.

    Coherence costs one extra atlas search per sub-centroid — measured at roughly ten times the
    cost of the query itself — so it is skippable, and it is skipped automatically when the
    query is too small for sub-clustering to mean anything.
    """

    if not requested:
        return False, 0, "measure_coherence=false"
    if n_query_cells < _QC_MINIMUM_CELLS:
        return (
            False,
            0,
            f"fewer than {_QC_MINIMUM_CELLS} query cells; sub-centroid coherence would not be "
            "meaningful",
        )
    return True, max(2, min(_QC_MAX_CLUSTERS, n_query_cells // 2)), None


def _coherence_record(
    *,
    value: float | None,
    measured: bool,
    subcentroids: int | None,
    k: int,
    reason: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "measured": measured,
        "value": round(float(value), 2) if measured and value is not None else None,
        "subcentroids": subcentroids if measured else None,
        "neighbors_per_subcentroid": _QC_NEIGHBORS_PER_SUBCENTROID if measured else None,
        "compared_against_k": k,
    }
    if not measured:
        record["reason"] = reason or "not measured"
    return record


def _summarize(
    name: str,
    *,
    n_query_cells: int,
    n_neighbors: int,
    distances: Any,
    composition_counts: dict[str, dict[str, int]],
    coherence: dict[str, Any],
    top_n: int,
    exclusion: dict[str, Any] | None = None,
    samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "query": name,
        "n_query_cells": n_query_cells,
        "n_reference_neighbors": n_neighbors,
        "neighbor_distance": _distance_summary(distances),
        "coherence": coherence,
        "composition": {
            column: _rank_counts(counts, top_n=top_n)
            for column, counts in composition_counts.items()
        },
    }
    if "study" in composition_counts:
        summary["n_reference_studies"] = len(composition_counts["study"])
    if exclusion is not None:
        summary["study_exclusion"] = exclusion
    if samples is not None:
        summary["top_reference_samples"] = samples[:top_n]
    return summary


def _background_comparison(
    label: str,
    query_composition: dict[str, list[dict[str, Any]]],
    background_counts: dict[str, dict[str, int]],
    *,
    top_n: int,
) -> dict[str, Any]:
    """Contrast a query's hit composition with the reference background for the same cell type.

    Without this, "62% of my neighbors are from Crohn's tissue" is uninterpretable — the reference
    itself may be mostly Crohn's for that cell type. Enrichment is the query fraction over the
    background fraction for the same value.
    """

    comparison: dict[str, Any] = {"reference_celltype": label, "axes": {}}
    for column, counts in background_counts.items():
        total = sum(counts.values())
        background = {
            str(value): (count / total if total else 0.0) for value, count in counts.items()
        }
        rows = []
        for entry in query_composition.get(column, [])[:top_n]:
            share = background.get(entry["value"], 0.0)
            rows.append(
                {
                    "value": entry["value"],
                    "query_fraction": entry["fraction"],
                    "reference_fraction": round(share, 4),
                    "enrichment": (
                        round(entry["fraction"] / share, 2) if share > 0 else None
                    ),
                }
            )
        comparison["axes"][column] = rows
        comparison.setdefault("reference_cells", total)
    return comparison


def _summary_row(summary: dict[str, Any]) -> dict[str, Any]:
    types = summary["composition"].get("celltype_name") or []
    leader = types[0] if types else {}
    return {
        "query": summary["query"],
        "n_query_cells": summary["n_query_cells"],
        "n_reference_neighbors": summary["n_reference_neighbors"],
        "top_celltype": leader.get("value"),
        "top_celltype_fraction": leader.get("fraction"),
        "coherence": summary["coherence"]["value"],
        "median_neighbor_distance": (
            summary["neighbor_distance"]["median"] if summary["neighbor_distance"] else None
        ),
    }


def _project_inline(
    summary: dict[str, Any], *, top_n: int, optional: tuple[str, ...]
) -> dict[str, Any]:
    """One query's model-visible projection, carrying the requested optional evidence blocks."""

    projected: dict[str, Any] = {
        key: summary[key]
        for key in (
            "query",
            "n_query_cells",
            "n_reference_neighbors",
            "neighbor_distance",
            "coherence",
        )
    }
    projected["composition"] = {
        column: values[:top_n] for column, values in summary["composition"].items()
    }
    if "study_exclusion" in summary:
        projected["study_exclusion"] = summary["study_exclusion"]
    if "reference_background" in optional and "reference_background" in summary:
        background = summary["reference_background"]
        projected["reference_background"] = {
            "reference_celltype": background.get("reference_celltype"),
            "reference_cells": background.get("reference_cells"),
            "axes": {
                column: rows[:top_n] for column, rows in background.get("axes", {}).items()
            },
        }
    if "top_reference_samples" in optional and summary.get("top_reference_samples"):
        projected["top_reference_samples"] = summary["top_reference_samples"][:top_n]
    return projected


def _inline_view(
    summaries: list[dict[str, Any]], *, budget_bytes: int = _INLINE_BUDGET_BYTES
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fit the per-query evidence into the model-visible budget, reporting what was dropped.

    The executor drops `details` wholesale when it exceeds its inline limit, so an over-budget
    result would cost the model *everything* rather than the least useful part. Trimming therefore
    proceeds in a fixed order — narrower value lists, then sample enrichment, then background, then
    fewer queries — and what it gave up is stated rather than left to be inferred.
    """

    plans: list[tuple[int, int, tuple[str, ...]]] = []
    both = ("reference_background", "top_reference_samples")
    for query_limit in (_INLINE_QUERY_LIMIT, 12, 6, 3, 1):
        for top_n in (_INLINE_TOP_N, 3):
            for optional in (both, ("reference_background",), ()):
                plans.append((query_limit, top_n, optional))
    fitted: list[dict[str, Any]] = []
    chosen = plans[-1]
    for query_limit, top_n, optional in plans:
        candidate = [
            _project_inline(summary, top_n=top_n, optional=optional)
            for summary in summaries[:query_limit]
        ]
        if len(json.dumps(candidate, default=str).encode()) <= budget_bytes:
            fitted, chosen = candidate, (query_limit, top_n, optional)
            break
    else:  # pragma: no cover - the narrowest plan is one query with no optional blocks
        fitted = [
            _project_inline(summary, top_n=3, optional=()) for summary in summaries[:1]
        ]
    query_limit, top_n, optional = chosen
    bounding = {
        "queries_shown": len(fitted),
        "queries_total": len(summaries),
        "queries_truncated": len(summaries) > len(fitted),
        "values_per_column": top_n,
        "omitted_blocks": [name for name in both if name not in optional],
        "note": (
            "the full per-query report is written as an artifact; read it when the inline view is "
            "truncated"
        ),
    }
    return fitted, bounding


def _neighbor_budget(
    row_counts: list[int], *, cap: int = _NEIGHBOR_ROW_CAP
) -> tuple[int | None, bool]:
    """Rows to keep per query so the neighbor artifact stays bounded; None means keep all."""

    if not row_counts or sum(row_counts) <= cap:
        return None, False
    return max(1, cap // len(row_counts)), True


def _resolve_queries(
    adata: Any,
    arguments: dict[str, Any],
    *,
    min_query_cells: int,
    max_queries: int,
    query_mode: str = "centroid",
    max_query_cells: int = _DEFAULT_MAX_QUERY_CELLS,
) -> tuple[list[tuple[str, Any]], dict[str, Any]]:
    """Resolve an explicit cell selection into named boolean masks over the input cells.

    In ``centroid`` mode each group (or the whole explicit cell set) becomes one averaged query.
    In ``cells`` mode every selected cell is its own query, which is what the SCimilarity search
    tutorials do from a single cell of interest.
    """

    import numpy as np

    cell_ids = arguments.get("cell_ids") or None
    group_key = arguments.get("group_key") or None
    if cell_ids and group_key:
        raise ValueError(
            "provide either cell_ids or group_key, not both: they are two ways of naming the "
            "same query selection"
        )
    if not cell_ids and not group_key:
        raise ValueError(
            "a query selection is required: pass group_key (with optional group_values) to "
            "query one centroid per group, or cell_ids to query one centroid built from an "
            "explicit set of cells"
        )

    if query_mode == "cells":
        obs_names = [str(name) for name in adata.obs_names]
        group_selected = None
        if group_key:
            if group_key not in adata.obs:
                raise ValueError(f"group_key {group_key!r} is absent from obs")
            labels = [str(value) for value in adata.obs[group_key]]
            wanted = (
                {str(value) for value in arguments.get("group_values")}
                if arguments.get("group_values")
                else set(labels)
            )
            unknown = wanted - set(labels)
            if unknown:
                raise ValueError(
                    f"group_values not present in obs[{group_key!r}]: "
                    f"{', '.join(sorted(unknown)[:5])}"
                )
            group_selected = [
                name for name, label in zip(obs_names, labels, strict=True) if label in wanted
            ]
        selected = _plan_cell_queries(
            obs_names,
            cell_ids=[str(value) for value in cell_ids] if cell_ids else None,
            group_selected=group_selected,
            max_query_cells=max_query_cells,
        )
        position = {name: index for index, name in enumerate(obs_names)}
        queries = []
        for name in selected:
            mask = np.zeros(len(obs_names), dtype=bool)
            mask[position[name]] = True
            queries.append((name, mask))
        selection = {
            "kind": "cells",
            "group_key": str(group_key) if group_key else None,
            "requested_cells": len(selected),
            "max_query_cells": max_query_cells,
            "skipped_groups": [],
        }
        return queries, selection

    if cell_ids:
        obs_names = [str(name) for name in adata.obs_names]
        selected = _plan_cell_ids(obs_names, list(cell_ids), min_query_cells=min_query_cells)
        wanted = {str(value) for value in cell_ids}
        mask = np.asarray([name in wanted for name in obs_names])
        selection = {
            "kind": "cell_ids",
            "group_key": None,
            "requested_cells": selected,
            "skipped_groups": [],
        }
        return [(_CELL_IDS_QUERY_NAME, mask)], selection

    if group_key not in adata.obs:
        raise ValueError(f"group_key {group_key!r} is absent from obs")
    labels = np.asarray([str(value) for value in adata.obs[group_key]])
    kept, skipped = _plan_group_queries(
        labels.tolist(),
        arguments.get("group_values") or None,
        group_key=str(group_key),
        min_query_cells=min_query_cells,
        max_queries=max_queries,
    )
    queries = [(value, labels == value) for value, _ in kept]
    selection = {
        "kind": "group",
        "group_key": str(group_key),
        "requested_groups": len(kept) + len(skipped),
        "skipped_groups": skipped,
    }
    return queries, selection


def _kept_hits(nn_idxs: Any, positions: Any) -> Any:
    """Reference hit ids surviving a row filter over the concatenated neighbor metadata.

    `search_nearest` returns one index array per query vector and a metadata frame that is those
    arrays concatenated with a positional index, so filtering the frame and reusing its surviving
    positions keeps downstream evidence scoped to the same cells.
    """

    import numpy as np

    flat = np.concatenate([np.asarray(block).reshape(-1) for block in nn_idxs])
    return flat[np.asarray(positions, dtype=int)]


def _sample_enrichment(reference: Any, nn_idxs: Any, *, top_n: int) -> list[dict[str, Any]]:
    """Which reference studies and samples the hits came from, as a fraction of each sample.

    SCimilarity's `compile_sample_metadata` answers "is this population a scattering of cells
    across many samples, or most of one specific sample" — the question a single cell-type label
    cannot. Ranked by that fraction so a small, highly matched sample is not buried.
    """

    try:
        frame = reference.compile_sample_metadata(nn_idxs, levels=list(_SAMPLE_LEVELS))
    except Exception as error:  # noqa: BLE001 - optional evidence must not fail a query
        return [{"unavailable": str(error)[:200]}]
    if frame is None or not len(frame):
        return []
    sort_column = "fraction" if "fraction" in frame.columns else "cells"
    ranked = frame.sort_values(sort_column, ascending=False).head(top_n)
    rows = []
    for _, row in ranked.iterrows():
        entry = {level: str(row[level]) for level in _SAMPLE_LEVELS if level in frame.columns}
        entry["cells"] = int(row["cells"]) if "cells" in frame.columns else None
        if "fraction" in frame.columns:
            entry["fraction_of_sample"] = round(float(row["fraction"]), 6)
        if "total" in frame.columns:
            entry["sample_size"] = int(row["total"])
        rows.append(entry)
    return rows


def _attach_background(reference: Any, summaries: list[dict[str, Any]], *, top_n: int) -> None:
    """Add a reference-background comparison for each query's leading reference cell type."""

    metadata = getattr(reference, "cell_metadata", None)
    if metadata is None or "celltype_name" not in getattr(metadata, "columns", ()):
        return
    labels = {
        summary["composition"]["celltype_name"][0]["value"]
        for summary in summaries
        if summary["composition"].get("celltype_name")
    }
    cache: dict[str, dict[str, dict[str, int]]] = {}
    for label in labels:
        subset = metadata[metadata["celltype_name"].astype(str) == label]
        cache[label] = {
            column: {
                str(value): int(count)
                for value, count in subset[column].astype(str).value_counts().items()
            }
            for column in _BACKGROUND_COLUMNS
            if column in subset.columns
        }
    for summary in summaries:
        leading = summary["composition"].get("celltype_name")
        if not leading:
            continue
        label = leading[0]["value"]
        if label in cache:
            summary["reference_background"] = _background_comparison(
                label, summary["composition"], cache[label], top_n=top_n
            )


def query(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import anndata as ad
    import pandas as pd
    from scimilarity import CellQuery
    from scimilarity.utils import align_dataset, lognorm_counts
    from scipy.sparse import csr_matrix

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    organism = declared_organism(arguments)
    model_path = resolve_model(arguments, require_cellsearch=True)
    counts_arg = arguments.get("counts_layer", "counts")
    counts_layer = str(counts_arg) if counts_arg is not None else None
    min_overlap = int(arguments.get("min_gene_overlap", 5000))
    k = int(arguments.get("k", 100))
    raw_max_dist = arguments.get("max_dist")
    max_dist = float(raw_max_dist) if raw_max_dist is not None else None
    min_query_cells = int(arguments.get("min_query_cells", 10))
    max_queries = int(arguments.get("max_queries", 40))
    measure_coherence = bool(arguments.get("measure_coherence", True))
    query_mode = str(arguments.get("query_mode", "centroid"))
    if query_mode not in {"centroid", "cells"}:
        raise ValueError("query_mode must be 'centroid' or 'cells'")
    max_query_cells = int(arguments.get("max_query_cells", _DEFAULT_MAX_QUERY_CELLS))
    excluded_studies = {str(value) for value in (arguments.get("exclude_studies") or [])}
    summarize_samples = bool(arguments.get("summarize_samples", True))
    compare_background = bool(arguments.get("compare_to_reference_background", True))

    adata = _read_matrix(path)
    counts, count_source = _select_counts(adata, counts_layer)
    _validate_counts(counts, label=count_source)
    species = verify_species(
        adata.var,
        declared=organism,
        allow_mismatch=bool(arguments.get("allow_species_mismatch", False)),
    )
    gene_order = read_gene_order(model_path)
    names, gene_source, overlap = _best_gene_names(adata.var, gene_order)
    if overlap < min_overlap:
        raise ValueError(
            f"only {overlap:,} input genes overlap the model vocabulary, below "
            f"min_gene_overlap={min_overlap:,}. Use the correct organism/model and gene symbols."
        )
    queries, selection = _resolve_queries(
        adata,
        arguments,
        min_query_cells=min_query_cells,
        max_queries=max_queries,
        query_mode=query_mode,
        max_query_cells=max_query_cells,
    )

    prepared = ad.AnnData(X=counts.copy(), obs=adata.obs.copy(), var=adata.var.copy())
    prepared.var_names = names
    prepared = align_dataset(prepared, gene_order, gene_overlap_threshold=min_overlap)
    # SCimilarity's centroid builder calls scipy's np.matrix-era `.A` on this layer, so keep it
    # a classic csr_matrix regardless of what the reader produced.
    prepared.layers["counts"] = csr_matrix(prepared.X)
    prepared = lognorm_counts(prepared)

    load_started = time.monotonic()
    reference = CellQuery(model_path=str(model_path))
    index_load_seconds = round(time.monotonic() - load_started, 2)
    reference_cells = int(len(reference.cell_metadata))

    centroid_key = "__scagent_query__"
    summaries: list[dict[str, Any]] = []
    neighbor_frames: list[Any] = []
    absent_columns: list[str] | None = None
    search_started = time.monotonic()
    for name, mask in queries:
        n_query_cells = int(mask.sum())
        if query_mode == "cells":
            # One query vector per cell, as in SCimilarity's own search tutorials.
            embeddings = reference.get_embeddings(prepared[mask].X)
            nn_idxs, _, metadata = reference.search_nearest(
                embeddings, k=k, max_dist=max_dist
            )
            qc_stats: dict[str, Any] = {}
            measured, k_clusters, skip_reason = (
                False,
                0,
                "per-cell queries have no sub-centroid structure to score",
            )
        else:
            prepared.obs[centroid_key] = mask.astype(int)
            measured, k_clusters, skip_reason = _coherence_plan(
                n_query_cells, requested=measure_coherence
            )
            _, nn_idxs, _, metadata, qc_stats = reference.search_centroid_nearest(
                prepared,
                centroid_key=centroid_key,
                k=k,
                max_dist=max_dist,
                qc=measured,
                qc_params={"k_clusters": max(2, k_clusters)},
            )
        if absent_columns is None:
            absent_columns = [
                column for column in _COMPOSITION_COLUMNS if column not in metadata.columns
            ]
        exclusion = None
        if excluded_studies and "study" in metadata.columns:
            before = int(len(metadata))
            metadata = metadata[~metadata["study"].astype(str).isin(excluded_studies)]
            # metadata keeps its original positions, which index the concatenated neighbor hits,
            # so sample enrichment can be scoped to the same surviving cells.
            nn_idxs = [_kept_hits(nn_idxs, metadata.index.to_numpy())]
            exclusion = _exclusion_record(
                before=before, after=int(len(metadata)), studies=sorted(excluded_studies)
            )
        composition_counts = {
            column: {
                str(value): int(count)
                for value, count in metadata[column].astype(str).value_counts().items()
            }
            for column in _COMPOSITION_COLUMNS
            if column in metadata.columns
        }
        samples = None
        if summarize_samples and len(metadata):
            samples = _sample_enrichment(reference, nn_idxs, top_n=_REPORT_TOP_N)
        summaries.append(
            _summarize(
                name,
                n_query_cells=n_query_cells,
                n_neighbors=int(len(metadata)),
                distances=(
                    metadata[_DISTANCE_COLUMN].to_numpy()
                    if _DISTANCE_COLUMN in metadata.columns
                    else None
                ),
                composition_counts=composition_counts,
                coherence=_coherence_record(
                    value=qc_stats.get("query_coherence"),
                    measured=measured,
                    subcentroids=k_clusters,
                    k=k,
                    reason=skip_reason,
                ),
                top_n=_REPORT_TOP_N,
                exclusion=exclusion,
                samples=samples,
            )
        )
        keep = [column for column in _NEIGHBOR_COLUMNS if column in metadata.columns]
        frame = metadata[keep].copy()
        frame.insert(0, "query", name)
        neighbor_frames.append(frame)
    search_seconds = round(time.monotonic() - search_started, 2)
    if centroid_key in prepared.obs:
        prepared.obs.drop(columns=[centroid_key], inplace=True)

    if compare_background:
        background_started = time.monotonic()
        _attach_background(reference, summaries, top_n=_INLINE_TOP_N)
        background_seconds = round(time.monotonic() - background_started, 2)
    else:
        background_seconds = None

    per_query_rows, neighbor_truncated = _neighbor_budget(
        [len(frame) for frame in neighbor_frames]
    )
    neighbors = pd.concat(neighbor_frames, ignore_index=True)
    if per_query_rows is not None:
        neighbors = (
            neighbors.sort_values(_DISTANCE_COLUMN)
            .groupby("query", observed=True, sort=False)
            .head(per_query_rows)
            .reset_index(drop=True)
            if _DISTANCE_COLUMN in neighbors.columns
            else neighbors.head(_NEIGHBOR_ROW_CAP).reset_index(drop=True)
        )
    neighbors_name = "scimilarity-reference-neighbors.csv.gz"
    neighbors.to_csv(context.staging_dir / neighbors_name, index=False, compression="gzip")

    summary_rows = [_summary_row(summary) for summary in summaries]
    summary_name = "scimilarity-reference-query-summary.csv"
    pd.DataFrame(summary_rows).to_csv(context.staging_dir / summary_name, index=False)

    provenance = dict(adata.uns.get("scagent_sdk", {}))
    cell_set_id = provenance.get("cell_set_id") or _identity(
        "cells", sorted(map(str, adata.obs_names))
    )
    fingerprint = model_fingerprint(model_path, include_cellsearch=True)
    query_id = _identity(
        "scimilarity-reference-query",
        {
            "cell_set_id": cell_set_id,
            "model_fingerprint": fingerprint,
            "count_source": count_source,
            "gene_source": gene_source,
            "selection": selection,
            "queries": [name for name, _ in queries],
            "k": k,
            "max_dist": max_dist,
            "measure_coherence": measure_coherence,
            "query_mode": query_mode,
            "excluded_studies": sorted(excluded_studies),
        },
    )
    report_name = "scimilarity-reference-query.json"
    report = {
        "query_id": query_id,
        "input_path": str(path),
        "model_path": str(model_path),
        "model_fingerprint": fingerprint,
        "organism": organism,
        "species_check": species,
        "count_source": count_source,
        "gene_name_source": gene_source,
        "overlapping_input_genes": overlap,
        "reference_cells_indexed": reference_cells,
        "k": k,
        "max_dist": max_dist,
        "query_mode": query_mode,
        "measure_coherence": measure_coherence,
        "excluded_studies": sorted(excluded_studies),
        "reference_background_compared": compare_background,
        "background_seconds": background_seconds,
        "selection": selection,
        "composition_columns_absent": absent_columns or [],
        "neighbor_table_truncated": neighbor_truncated,
        "index_load_seconds": index_load_seconds,
        "search_seconds": search_seconds,
        "queries": summaries,
    }
    (context.staging_dir / report_name).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    inline_queries, inline_bounding = _inline_view(summaries)
    leader = summary_rows[0] if summary_rows else {}
    headline = (
        f"; nearest reference type for {leader['query']!r} is {leader['top_celltype']}"
        if leader.get("top_celltype")
        else ""
    )
    return {
        "summary": (
            f"Queried {len(summaries)} "
            f"{'cell(s)' if query_mode == 'cells' else 'centroid(s)'} against "
            f"{reference_cells:,} {organism} reference cells at k={k:,}{headline}."
        ),
        "details": {
            "query_id": query_id,
            "model_path": str(model_path),
            "organism": organism,
            "species_check": species,
            "k": k,
            "max_dist": max_dist,
            "query_mode": query_mode,
            "measure_coherence": measure_coherence,
            "excluded_studies": sorted(excluded_studies),
            "reference_background_compared": compare_background,
            "background_seconds": background_seconds,
            "reference_cells_indexed": reference_cells,
            "selection": selection,
            "composition_columns_absent": absent_columns or [],
            "neighbor_table_truncated": neighbor_truncated,
            "index_load_seconds": index_load_seconds,
            "search_seconds": search_seconds,
            "inline_view": inline_bounding,
            "full_report_artifact": (
                f"artifacts/capabilities/{context.execution_id}/{report_name}"
            ),
            "queries": inline_queries,
        },
        "facts_patch": {
            "reference_runs": {
                "scimilarity_query": {
                    context.execution_id: {
                        "status": "complete",
                        "query_id": query_id,
                        "cell_set_id": cell_set_id,
                        "model_path": str(model_path),
                        "model_fingerprint": fingerprint,
                        "organism": organism,
                        "species_verdict": species.get("verdict"),
                        "selection": selection,
                        "k": k,
                        "max_dist": max_dist,
                        "query_mode": query_mode,
                        "excluded_studies": sorted(excluded_studies),
                        "queries": summary_rows,
                        "artifact_path": (
                            f"artifacts/capabilities/{context.execution_id}/{report_name}"
                        ),
                    }
                }
            }
        },
        "artifacts": [
            {
                "name": "scimilarity-reference-query",
                "relative_path": report_name,
                "media_type": "application/json",
            },
            {
                "name": "scimilarity-reference-query-summary",
                "relative_path": summary_name,
                "media_type": "text/csv",
            },
            {
                "name": "scimilarity-reference-neighbors",
                "relative_path": neighbors_name,
                "media_type": "application/gzip",
            },
        ],
    }
