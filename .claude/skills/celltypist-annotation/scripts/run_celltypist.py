"""Standalone CellTypist inference and optional cluster summarization."""

from __future__ import annotations

import hashlib
import json
import os
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


def _align_var_to_reference(var: Any, reference: Any) -> tuple[Any, str, int]:
    import pandas as pd

    ref_by_upper = {str(gene).upper(): str(gene) for gene in reference}
    original = [str(name) for name in var.index]
    candidates: list[tuple[str, list[str]]] = [("var_names", original)]
    for column in _SYMBOL_COLUMNS:
        if column in var.columns:
            candidates.append((f"var[{column!r}]", [str(value) for value in var[column]]))
    source, names = max(
        candidates,
        key=lambda item: sum(
            1 for name in item[1] if str(name).upper() in ref_by_upper
        ),
    )
    hits = sum(1 for name in names if str(name).upper() in ref_by_upper)
    aligned = pd.Index([ref_by_upper.get(str(name).upper(), str(name)) for name in names])
    return aligned.where(~aligned.duplicated(), pd.Index(original)), source, hits


def _validate_counts(matrix: Any, *, label: str) -> None:
    import numpy as np

    values = np.asarray(matrix.tocsr().data if hasattr(matrix, "tocsr") else matrix).ravel()
    if values.size and (
        not bool(np.all(np.isfinite(values)))
        or not bool(np.all(values >= 0))
        or not bool(np.all(values == np.round(values)))
    ):
        raise ValueError(
            f"{label} is not finite nonnegative integer counts; "
            "CellTypist normalization must begin from raw counts"
        )


def _select_counts(adata: Any, counts_layer: str | None) -> tuple[Any, str]:
    if counts_layer is None or counts_layer == "X":
        return adata.X, "X"
    if counts_layer in adata.layers:
        return adata.layers[counts_layer], f"layer:{counts_layer}"
    raise ValueError(
        f"count layer {counts_layer!r} is absent; use counts_layer=X or null for adata.X"
    )


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import anndata as ad
    import celltypist
    import numpy as np
    import pandas as pd
    import scanpy as sc
    from celltypist import models

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    model = str(arguments["model"])
    counts_arg = arguments.get("counts_layer", "counts")
    counts_layer = str(counts_arg) if counts_arg is not None else None
    output_key = str(arguments.get("output_key", "celltypist_prediction"))
    model_path = (
        Path(model).expanduser() if os.path.isabs(model) else Path(models.models_path) / model
    )
    if not model_path.is_file():
        raise FileNotFoundError(
            f"CellTypist model is not cached: {model_path}. Choose a local model; "
            "downloads are not implicit."
        )
    adata = _read_matrix(path)
    counts, count_source = _select_counts(adata, counts_layer)
    _validate_counts(counts, label=count_source)
    ct_model = models.Model.load(str(model_path))
    names, gene_source, overlap = _align_var_to_reference(adata.var, ct_model.features)
    if overlap == 0:
        raise ValueError("no input genes overlap the selected CellTypist model")
    prepared = ad.AnnData(X=counts.copy(), obs=adata.obs.copy(), var=adata.var.copy())
    prepared.var_names = names
    sc.pp.normalize_total(prepared, target_sum=1e4)
    sc.pp.log1p(prepared)
    result = celltypist.annotate(
        prepared,
        model=ct_model,
        mode="best match",
        majority_voting=False,
    ).to_adata()
    if not result.obs_names.equals(adata.obs_names):
        result = result[adata.obs_names].copy()
    predicted = result.obs["predicted_labels"].astype(str)
    confidence = pd.to_numeric(result.obs["conf_score"], errors="coerce")
    adata.obs[output_key] = predicted.to_numpy()
    adata.obs[f"{output_key}_confidence"] = confidence.to_numpy()
    metadata = dict(adata.uns.get("scagent_sdk", {}))
    cell_set_id = metadata.get("cell_set_id") or _identity(
        "cells", sorted(map(str, adata.obs_names))
    )
    model_fingerprint = _identity(
        "celltypist-model",
        {"path": str(model_path), "size": model_path.stat().st_size},
    )
    run_id = _identity(
        "celltypist-run",
        {
            "cell_set_id": cell_set_id,
            "model_fingerprint": model_fingerprint,
            "count_source": count_source,
            "gene_source": gene_source,
            "overlap": overlap,
            "output_key": output_key,
        },
    )
    metadata.update(
        {
            "cell_set_id": cell_set_id,
            "celltypist_run_id": run_id,
            "celltypist_model_fingerprint": model_fingerprint,
        }
    )
    adata.uns["scagent_sdk"] = metadata
    output_name = "celltypist-annotated.zarr"
    final_path = f"{context.artifact_relative_path}/{output_name}"
    _write_matrix(adata, context.staging_dir / output_name)
    cell_table = pd.DataFrame(
        {
            "cell": adata.obs_names.astype(str),
            "prediction": predicted.to_numpy(),
            "confidence": confidence.to_numpy(),
        }
    )
    cell_table.to_csv(context.staging_dir / "celltypist-cell-predictions.csv", index=False)
    report = {
        "run_id": run_id,
        "model": str(model_path),
        "model_fingerprint": model_fingerprint,
        "count_source": count_source,
        "gene_name_source": gene_source,
        "overlapping_genes": overlap,
        "model_genes": int(len(ct_model.features)),
        "prediction_count": int(predicted.nunique()),
        "median_confidence": float(np.nanmedian(confidence)),
        "output_key": output_key,
    }
    (context.staging_dir / "celltypist-run.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "summary": f"CellTypist predicted {adata.n_obs:,} cells with {model_path.name}.",
        "details": report,
        "facts_patch": {
            "reference_runs": {
                "celltypist": {
                    context.execution_id: {
                        "status": "complete",
                        "run_id": run_id,
                        "cell_set_id": cell_set_id,
                        "model": str(model_path),
                        "model_fingerprint": model_fingerprint,
                        "count_source": count_source,
                        "gene_name_source": gene_source,
                        "overlapping_genes": overlap,
                        "output_key": output_key,
                        "artifact_path": final_path,
                    }
                }
            }
        },
        "artifacts": [
            {
                "name": "celltypist-annotated-anndata",
                "relative_path": output_name,
                "media_type": "application/vnd.zarr",
            },
            {
                "name": "celltypist-cell-predictions",
                "relative_path": "celltypist-cell-predictions.csv",
                "media_type": "text/csv",
            },
            {
                "name": "celltypist-run",
                "relative_path": "celltypist-run.json",
                "media_type": "application/json",
            },
        ],
    }


def summarize_by_cluster(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import numpy as np
    import pandas as pd

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    cluster_key = str(arguments["cluster_key"])
    prediction_key = str(arguments.get("prediction_key", "celltypist_prediction"))
    confidence_key = str(
        arguments.get("confidence_key", "celltypist_prediction_confidence")
    )
    adata = _read_matrix(path)
    for key in (cluster_key, prediction_key, confidence_key):
        if key not in adata.obs:
            raise ValueError(f"obs key {key!r} is absent")
    table = pd.DataFrame(
        {
            "cell": adata.obs_names.astype(str),
            "cluster": adata.obs[cluster_key].astype(str).to_numpy(),
            "prediction": adata.obs[prediction_key].astype(str).to_numpy(),
            "confidence": pd.to_numeric(adata.obs[confidence_key], errors="coerce").to_numpy(),
        }
    )
    rows: list[dict[str, Any]] = []
    for cluster, frame in table.groupby("cluster", observed=True):
        counts = frame["prediction"].value_counts()
        rows.append(
            {
                "cluster": str(cluster),
                "prediction": str(counts.index[0]),
                "support_fraction": float(counts.iloc[0] / len(frame)),
                "median_confidence": float(np.nanmedian(frame["confidence"])),
                "n_cells": int(len(frame)),
                "n_distinct_predictions": int(counts.size),
            }
        )
    result = pd.DataFrame(rows).sort_values("cluster")
    result.to_csv(context.staging_dir / "celltypist-cluster-predictions.csv", index=False)
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
        "celltypist-cluster-evidence",
        {
            "run_id": provenance.get("celltypist_run_id"),
            "clustering_id": clustering_id,
            "cluster_key": cluster_key,
            "prediction_key": prediction_key,
            "rows": rows,
        },
    )
    predictions = result.to_dict(orient="records")
    return {
        "summary": (
            f"Summarized CellTypist predictions across {len(rows)} groups in "
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
                    "celltypist": {
                        "status": "complete",
                        "evidence_id": evidence_id,
                        "clustering_id": clustering_id,
                        "cell_set_id": cell_set_id,
                        "cluster_key": cluster_key,
                        "prediction_key": prediction_key,
                        "cluster_predictions": predictions,
                        "artifact_path": (
                            f"{context.artifact_relative_path}/"
                            "celltypist-cluster-predictions.csv"
                        ),
                    }
                }
            }
        },
        "artifacts": [
            {
                "name": "celltypist-cluster-predictions",
                "relative_path": "celltypist-cluster-predictions.csv",
                "media_type": "text/csv",
            }
        ],
    }
