"""Standalone SCimilarity per-cell inference and optional cluster summarization."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_assets import (  # noqa: E402  (sibling module; path inserted above)
    _SYMBOL_COLUMNS,
    _best_gene_names,
    _identity,
    _select_counts,
    _select_gene_names,
    _validate_counts,
    declared_organism,
    model_fingerprint,
    read_gene_order,
    resolve_model,
    validate_target_celltypes,
    verify_species,
)

__all__ = [
    "_SYMBOL_COLUMNS",
    "_best_gene_names",
    "_identity",
    "_select_counts",
    "_select_gene_names",
    "_validate_counts",
    "run",
    "summarize_by_cluster",
]


def _read_matrix(path):
    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)

def _vote_confidence(stats: Any, *, weighting: bool, n_obs: int) -> dict[str, Any]:
    """Per-cell kNN vote margins, named for what they mean rather than for SCimilarity's columns.

    ``vs2nd``/``vsAll`` (or their distance-weighted twins when weighting is on) are the winning
    label's share of the top-two votes and of all k votes. They are the model's own measure of how
    contested a call was, and are dropped on the floor if the stats frame is ignored.
    """

    import numpy as np

    columns = ("vs2nd_weighted", "vsAll_weighted") if weighting else ("vs2nd", "vsAll")
    names = ("vs_second", "vs_all")
    resolved: dict[str, Any] = {}
    for column, name in zip(columns, names, strict=True):
        if stats is None or column not in getattr(stats, "columns", ()):
            continue
        values = np.asarray(stats[column], dtype=float).reshape(-1)
        if values.shape[0] != n_obs:
            continue
        resolved[name] = values
    return resolved


def _confidence_summary(values: Any) -> dict[str, float] | None:
    import numpy as np

    if values is None:
        return None
    array = np.asarray(values, dtype=float).reshape(-1)
    if array.size == 0:
        return None
    return {
        "median": round(float(np.median(array)), 4),
        "minimum": round(float(np.min(array)), 4),
        "fraction_below_0_5": round(float(np.mean(array < 0.5)), 4),
    }


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import anndata as ad
    import numpy as np
    import pandas as pd
    from scimilarity import CellAnnotation
    from scimilarity.utils import align_dataset, lognorm_counts

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    organism = declared_organism(arguments)
    model_path = resolve_model(arguments)
    counts_arg = arguments.get("counts_layer", "counts")
    counts_layer = str(counts_arg) if counts_arg is not None else None
    min_overlap = int(arguments.get("min_gene_overlap", 5000))
    output_key = str(arguments.get("output_key", "scimilarity_prediction"))
    knn_k = int(arguments.get("knn_k", 50))
    weighting = bool(arguments.get("weighting", False))
    requested_celltypes = arguments.get("target_celltypes") or None

    adata = _read_matrix(path)
    counts, count_source = _select_counts(adata, counts_layer)
    _validate_counts(counts, label=count_source)
    species = verify_species(
        adata.var,
        declared=organism,
        allow_mismatch=bool(arguments.get("allow_species_mismatch", False)),
    )
    # Validate against the vocabulary file before constructing the encoder, so a wrong model,
    # organism, or identifier space fails without loading the reference label table.
    gene_order = read_gene_order(model_path)
    names, gene_source, overlap = _best_gene_names(adata.var, gene_order)
    if overlap < min_overlap:
        raise ValueError(
            f"only {overlap:,} input genes overlap the model vocabulary, below "
            f"min_gene_overlap={min_overlap:,}. Use the correct organism/model and gene symbols."
        )
    annotator = CellAnnotation(model_path=str(model_path))

    prepared = ad.AnnData(X=counts.copy(), obs=adata.obs.copy(), var=adata.var.copy())
    prepared.var_names = names
    prepared = align_dataset(
        prepared,
        annotator.gene_order,
        gene_overlap_threshold=min_overlap,
    )
    prepared.layers["counts"] = prepared.X.copy()
    prepared = lognorm_counts(prepared)
    embeddings = np.asarray(annotator.get_embeddings(prepared.X))
    predictions, indices, distances, stats = annotator.get_predictions_knn(
        embeddings, k=knn_k, weighting=weighting
    )
    predicted = pd.Series(list(predictions), index=adata.obs_names, dtype="string").astype(str)
    distance_array = np.asarray(distances)
    min_distance = (
        distance_array.min(axis=1)
        if distance_array.ndim > 1
        else distance_array.reshape(-1)
    )
    if min_distance.shape[0] != adata.n_obs:
        raise ValueError("SCimilarity returned a distance array with the wrong cell dimension")

    adata.obsm["X_scimilarity"] = embeddings
    adata.obs[output_key] = predicted.to_numpy()
    adata.obs[f"{output_key}_min_distance"] = min_distance
    # SCimilarity already computes per-cell vote margins while predicting. Keeping them is the
    # difference between a bare label and a label you can weigh: vs_second is the winner's share
    # of the top-two votes, vs_all its share of all k votes.
    confidence = _vote_confidence(stats, weighting=weighting, n_obs=adata.n_obs)
    for suffix, values in confidence.items():
        adata.obs[f"{output_key}_{suffix}"] = values

    constrained_key = f"{output_key}_constrained"
    constrained: dict[str, Any] | None = None
    if requested_celltypes is not None:
        # Validate before mutating the index: safelisting marks every other label deleted.
        targets = validate_target_celltypes(requested_celltypes, annotator.classes)
        annotator.safelist_celltypes(targets)
        constrained_predictions, _, constrained_distances, constrained_stats = (
            annotator.get_predictions_knn(embeddings, k=knn_k, weighting=weighting)
        )
        annotator.reset_knn()
        constrained_labels = pd.Series(
            list(constrained_predictions), index=adata.obs_names, dtype="string"
        ).astype(str)
        constrained_distance_array = np.asarray(constrained_distances)
        constrained_min = (
            constrained_distance_array.min(axis=1)
            if constrained_distance_array.ndim > 1
            else constrained_distance_array.reshape(-1)
        )
        adata.obs[constrained_key] = constrained_labels.to_numpy()
        adata.obs[f"{constrained_key}_min_distance"] = constrained_min
        for suffix, values in _vote_confidence(
            constrained_stats, weighting=weighting, n_obs=adata.n_obs
        ).items():
            adata.obs[f"{constrained_key}_{suffix}"] = values
        agreement = float((constrained_labels.to_numpy() == predicted.to_numpy()).mean())
        constrained = {
            "target_celltypes": targets,
            "requested": len(requested_celltypes),
            "output_key": constrained_key,
            "label_count": int(constrained_labels.nunique()),
            "agreement_with_unconstrained": round(agreement, 4),
            "reassigned_cells": int(
                (constrained_labels.to_numpy() != predicted.to_numpy()).sum()
            ),
            "labels_absent_from_data": sorted(
                set(targets) - set(constrained_labels.unique().tolist())
            ),
        }
    metadata = dict(adata.uns.get("scagent_sdk", {}))
    cell_set_id = metadata.get("cell_set_id") or _identity(
        "cells", sorted(map(str, adata.obs_names))
    )
    fingerprint = model_fingerprint(model_path)
    run_id = _identity(
        "scimilarity-run",
        {
            "cell_set_id": cell_set_id,
            "model_fingerprint": fingerprint,
            "count_source": count_source,
            "gene_source": gene_source,
            "overlap": overlap,
            "output_key": output_key,
            "knn_k": knn_k,
            "weighting": weighting,
            "target_celltypes": constrained["target_celltypes"] if constrained else None,
        },
    )
    metadata.update(
        {
            "cell_set_id": cell_set_id,
            "scimilarity_run_id": run_id,
            "scimilarity_model_fingerprint": fingerprint,
        }
    )
    adata.uns["scagent_sdk"] = metadata
    output_name = "scimilarity-annotated.h5ad"
    final_path = f"artifacts/capabilities/{context.execution_id}/{output_name}"
    adata.write_h5ad(context.staging_dir / output_name, compression="gzip")
    columns: dict[str, Any] = {
        "cell": adata.obs_names.astype(str),
        "prediction": predicted.to_numpy(),
        "min_neighbor_distance": min_distance,
    }
    for suffix, values in confidence.items():
        columns[suffix] = values
    if constrained is not None:
        columns["constrained_prediction"] = adata.obs[constrained_key].to_numpy()
    cell_table = pd.DataFrame(columns)
    cell_table.to_csv(context.staging_dir / "scimilarity-cell-predictions.csv", index=False)
    distance_summary = {
        "minimum": float(np.min(min_distance)),
        "median": float(np.median(min_distance)),
        "mean": float(np.mean(min_distance)),
        "maximum": float(np.max(min_distance)),
    }
    report = {
        "run_id": run_id,
        "input_path": str(path),
        "model_path": str(model_path),
        "model_fingerprint": fingerprint,
        "organism": organism,
        "species_check": species,
        "count_source": count_source,
        "gene_name_source": gene_source,
        "input_genes": int(adata.n_vars),
        "model_genes": int(len(annotator.gene_order)),
        "overlapping_input_genes": overlap,
        "minimum_required_overlap": min_overlap,
        "embedding_shape": list(map(int, embeddings.shape)),
        "output_key": output_key,
        "knn_k": knn_k,
        "distance_weighted_voting": weighting,
        "prediction_count": int(predicted.nunique()),
        "min_neighbor_distance": distance_summary,
        "vote_confidence": {
            name: _confidence_summary(values) for name, values in confidence.items()
        },
        "constrained_annotation": constrained,
        "neighbor_index_shape": list(map(int, np.asarray(indices).shape)),
    }
    (context.staging_dir / "scimilarity-run.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "summary": (
            f"SCimilarity embedded and predicted {adata.n_obs:,} cells using "
            f"{overlap:,} overlapping genes at k={knn_k}"
            + (
                f"; constrained annotation over {len(constrained['target_celltypes'])} safelisted "
                f"types agreed with {constrained['agreement_with_unconstrained']:.0%} of the "
                "unconstrained calls."
                if constrained
                else "."
            )
        ),
        "details": report,
        "facts_patch": {
            "reference_runs": {
                "scimilarity": {
                    context.execution_id: {
                        "status": "complete",
                        "run_id": run_id,
                        "cell_set_id": cell_set_id,
                        "model_path": str(model_path),
                        "model_fingerprint": fingerprint,
                        "organism": organism,
                        "species_verdict": species.get("verdict"),
                        "count_source": count_source,
                        "gene_name_source": gene_source,
                        "overlapping_input_genes": overlap,
                        "output_key": output_key,
                        "knn_k": knn_k,
                        "distance_weighted_voting": weighting,
                        "constrained_output_key": constrained_key if constrained else None,
                        "target_celltypes": (
                            constrained["target_celltypes"] if constrained else None
                        ),
                        "artifact_path": final_path,
                    }
                }
            }
        },
        "artifacts": [
            {
                "name": "scimilarity-annotated-anndata",
                "relative_path": output_name,
                "media_type": "application/x-hdf5",
            },
            {
                "name": "scimilarity-cell-predictions",
                "relative_path": "scimilarity-cell-predictions.csv",
                "media_type": "text/csv",
            },
            {
                "name": "scimilarity-run",
                "relative_path": "scimilarity-run.json",
                "media_type": "application/json",
            },
        ],
    }


def summarize_by_cluster(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import pandas as pd

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    cluster_key = str(arguments["cluster_key"])
    prediction_key = str(arguments.get("prediction_key", "scimilarity_prediction"))
    adata = _read_matrix(path)
    if cluster_key not in adata.obs:
        raise ValueError(f"cluster key {cluster_key!r} is absent")
    if prediction_key not in adata.obs:
        raise ValueError(f"SCimilarity prediction key {prediction_key!r} is absent")
    cell_table = pd.DataFrame(
        {
            "cell": adata.obs_names.astype(str),
            "cluster": adata.obs[cluster_key].astype(str).to_numpy(),
            "prediction": adata.obs[prediction_key].astype(str).to_numpy(),
        }
    )
    rows: list[dict[str, Any]] = []
    for cluster, frame in cell_table.groupby("cluster", observed=True):
        counts = frame["prediction"].value_counts()
        rows.append(
            {
                "cluster": str(cluster),
                "prediction": str(counts.index[0]),
                "support_fraction": float(counts.iloc[0] / len(frame)),
                "n_cells": int(len(frame)),
                "n_distinct_predictions": int(counts.size),
            }
        )
    cluster_table = pd.DataFrame(rows).sort_values("cluster")
    cluster_table.to_csv(
        context.staging_dir / "scimilarity-cluster-predictions.csv", index=False
    )
    provenance = dict(adata.uns.get("scagent_sdk", {}))
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
    evidence_id = _identity(
        "scimilarity-cluster-evidence",
        {
            "run_id": provenance.get("scimilarity_run_id"),
            "clustering_id": clustering_id,
            "cluster_key": cluster_key,
            "prediction_key": prediction_key,
            "rows": rows,
        },
    )
    predictions = cluster_table.to_dict(orient="records")
    return {
        "summary": (
            f"Summarized SCimilarity predictions across {len(rows)} groups in "
            f"{cluster_key!r}."
        ),
        "details": {
            "evidence_id": evidence_id,
            "clustering_id": clustering_id,
            "cell_set_id": cell_set_id,
            "cluster_key": cluster_key,
            "prediction_key": prediction_key,
            "cluster_predictions": predictions,
        },
        "facts_patch": {
            "annotation": {
                "evidence": {
                    "scimilarity": {
                        "status": "complete",
                        "evidence_id": evidence_id,
                        "clustering_id": clustering_id,
                        "cell_set_id": cell_set_id,
                        "cluster_key": cluster_key,
                        "prediction_key": prediction_key,
                        "cluster_predictions": predictions,
                        "artifact_path": (
                            f"artifacts/capabilities/{context.execution_id}/"
                            "scimilarity-cluster-predictions.csv"
                        ),
                    }
                }
            }
        },
        "artifacts": [
            {
                "name": "scimilarity-cluster-predictions",
                "relative_path": "scimilarity-cluster-predictions.csv",
                "media_type": "text/csv",
            }
        ],
    }
