"""Independent expression normalization and HVG selection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


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


def _to_gpu(adata: Any) -> None:
    from rapids_singlecell.get import anndata_to_GPU

    anndata_to_GPU(adata, convert_all=True)


def _to_cpu(adata: Any) -> None:
    from rapids_singlecell.get import anndata_to_CPU

    anndata_to_CPU(adata, convert_all=True)


def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _count_matrix(adata: Any, layer: str) -> Any:
    import numpy as np

    if layer not in adata.layers:
        raise ValueError(f"raw-count layer {layer!r} is absent")
    matrix = adata.layers[layer]
    values = np.asarray(matrix.tocsr().data if hasattr(matrix, "tocsr") else matrix).ravel()
    if values.size and (
        not bool(np.all(np.isfinite(values)))
        or not bool(np.all(values >= 0))
        or not bool(np.all(values == np.round(values)))
    ):
        raise ValueError(f"layer {layer!r} is not finite nonnegative integer counts")
    return matrix


def normalize_expression(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import rapids_singlecell as rsc

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    layer = str(arguments.get("counts_layer", "counts"))
    target_sum = float(arguments.get("target_sum", 10000))
    adata = _read_matrix(path)
    adata.X = _count_matrix(adata, layer).copy()
    _to_gpu(adata)
    rsc.pp.normalize_total(adata, target_sum=target_sum)
    rsc.pp.log1p(adata)
    _to_cpu(adata)
    metadata = dict(adata.uns.get("scagent_sdk", {}))
    cell_set_id = metadata.get("cell_set_id") or _identity(
        "cells", sorted(map(str, adata.obs_names))
    )
    representation_id = _identity(
        "expression",
        {
            "cell_set_id": cell_set_id,
            "method": "normalize-total-log1p",
            "target_sum": target_sum,
            "counts_layer": layer,
            "compute_backend": "rapids_singlecell",
        },
    )
    metadata.update(
        {
            "cell_set_id": cell_set_id,
            "expression_representation_id": representation_id,
            "expression_method": "normalize-total-log1p",
        }
    )
    adata.uns["scagent_sdk"] = metadata
    output_name = "log-normalized.zarr"
    _write_matrix(adata, context.staging_dir / output_name)
    report = {
        "method": "normalize_total+log1p",
        "target_sum": target_sum,
        "counts_layer": layer,
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "expression_representation_id": representation_id,
        "compute_backend": "rapids_singlecell",
    }
    (context.staging_dir / "normalization.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "summary": (
            f"Normalized and log1p-transformed {adata.n_obs:,} cells from layer {layer!r}; "
            "no cells or genes were removed."
        ),
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "n_cells": int(adata.n_obs),
                    "n_genes": int(adata.n_vars),
                },
                "expression": {
                    "id": representation_id,
                    "method": "normalize-total-log1p",
                    "target_sum": target_sum,
                    "counts_layer": layer,
                },
                "representation": None,
                "clustering": None,
            },
            "batch": None,
            "cluster_qc": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": [
            {
                "name": "log-normalized-anndata",
                "relative_path": output_name,
                "media_type": "application/vnd.zarr",
            },
            {
                "name": "normalization-report",
                "relative_path": "normalization.json",
                "media_type": "application/json",
            },
        ],
    }


def select_hvg(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import rapids_singlecell as rsc

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    n_top = int(arguments.get("n_top_genes", 3000))
    flavor = str(arguments.get("flavor", "seurat"))
    layer_arg = arguments.get("layer")
    layer = str(layer_arg) if layer_arg is not None else None
    batch_arg = arguments.get("batch_key")
    batch_key = str(batch_arg) if batch_arg is not None else None
    adata = _read_matrix(path)
    if layer is not None and layer not in adata.layers:
        raise ValueError(f"layer {layer!r} is absent")
    if batch_key is not None and batch_key not in adata.obs:
        raise ValueError(f"batch key {batch_key!r} is absent")
    _to_gpu(adata)
    rsc.pp.highly_variable_genes(
        adata,
        n_top_genes=min(n_top, adata.n_vars),
        flavor=flavor,
        layer=layer,
        batch_key=batch_key,
    )
    _to_cpu(adata)
    selected = int(adata.var["highly_variable"].sum())
    if selected < 2:
        raise ValueError("fewer than two highly variable genes were selected")
    metadata = dict(adata.uns.get("scagent_sdk", {}))
    hvg_id = _identity(
        "hvg",
        {
            "path": str(path),
            "genes": sorted(
                map(str, adata.var_names[adata.var["highly_variable"].to_numpy()])
            ),
            "flavor": flavor,
            "layer": layer,
            "batch_key": batch_key,
            "compute_backend": "rapids_singlecell",
        },
    )
    metadata["hvg_id"] = hvg_id
    adata.uns["scagent_sdk"] = metadata
    output_name = "hvg-selected.zarr"
    _write_matrix(adata, context.staging_dir / output_name)
    table = adata.var.loc[adata.var["highly_variable"]].copy()
    table.insert(0, "gene", table.index.astype(str))
    table.to_csv(context.staging_dir / "highly-variable-genes.csv", index=False)
    return {
        "summary": (
            f"Flagged {selected:,} highly variable genes using {flavor} in "
            "var['highly_variable'] (all genes retained)."
        ),
        "details": {
            "n_highly_variable": selected,
            "n_genes": int(adata.n_vars),
            "flavor": flavor,
            "layer": layer,
            "batch_key": batch_key,
            "hvg_id": hvg_id,
            "compute_backend": "rapids_singlecell",
        },
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "n_cells": int(adata.n_obs),
                    "n_genes": int(adata.n_vars),
                },
                "hvg": {
                    "id": hvg_id,
                    "n_genes": selected,
                    "flavor": flavor,
                    "layer": layer,
                    "batch_key": batch_key,
                },
                "representation": None,
                "clustering": None,
            },
            "cluster_qc": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": [
            {
                "name": "hvg-anndata",
                "relative_path": output_name,
                "media_type": "application/vnd.zarr",
            },
            {
                "name": "highly-variable-genes",
                "relative_path": "highly-variable-genes.csv",
                "media_type": "text/csv",
            },
        ],
    }
