"""Standalone scVI latent-model training across an isolated environment boundary."""

from __future__ import annotations

import hashlib
import json
import shutil
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

def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scvi

    path = Path(str(arguments["path"])).expanduser().resolve()
    batch_key = str(arguments["batch_key"])
    n_latent = int(arguments.get("n_latent", 30))
    n_layers = int(arguments.get("n_layers", 2))
    max_epochs = int(arguments.get("max_epochs", 200))
    seed = int(arguments.get("random_seed", 0))
    adata = _read_matrix(path)
    if batch_key not in adata.obs:
        raise ValueError(f"batch key {batch_key!r} is absent")
    if "counts" not in adata.layers:
        raise ValueError("scVI requires raw counts in layers['counts']")
    old = adata.uns.get("scagent_sdk", {})
    cell_set_id = old.get("cell_set_id") or _identity(
        "cells", sorted(map(str, adata.obs_names))
    )
    scvi.settings.seed = seed
    scvi.model.SCVI.setup_anndata(adata, layer="counts", batch_key=batch_key)
    model = scvi.model.SCVI(adata, n_latent=n_latent, n_layers=n_layers)
    model.train(
        max_epochs=max_epochs,
        accelerator="auto",
        devices="auto",
        early_stopping=True,
        enable_progress_bar=False,
    )
    adata.obsm["X_scVI"] = model.get_latent_representation()
    representation_id = _identity(
        "representation",
        {
            "cell_set_id": cell_set_id,
            "method": "scvi",
            "batch_key": batch_key,
            "n_latent": n_latent,
            "n_layers": n_layers,
            "max_epochs": max_epochs,
            "seed": seed,
        },
    )
    new_metadata = {
        **old,
        "cell_set_id": cell_set_id,
        "representation_id": representation_id,
        "representation_key": "X_scVI",
        "scvi": {
            "method": "scvi",
            "batch_key": batch_key,
            "n_latent": n_latent,
            "n_layers": n_layers,
            "max_epochs": max_epochs,
        },
    }
    new_metadata.pop("clustering_id", None)
    adata.uns["scagent_sdk"] = new_metadata
    _write_matrix(adata, context.staging_dir / "scvi-latent.zarr")
    model_dir = context.staging_dir / "scvi-model"
    model.save(model_dir, overwrite=True)
    shutil.make_archive(str(context.staging_dir / "scvi-model"), "zip", model_dir)
    shutil.rmtree(model_dir)
    history = model.history
    if history:
        history_frame = next(iter(history.values())).copy()
        history_frame.to_csv(context.staging_dir / "scvi-training-history.csv")
    else:
        (context.staging_dir / "scvi-training-history.csv").write_text("\n", encoding="utf-8")
    return {
        "summary": (
            f"Trained scVI and saved a {n_latent}-dimensional X_scVI representation "
            f"for {adata.n_obs:,} cells; no graph, UMAP, or clusters were created."
        ),
        "details": {
            "batch_key": batch_key,
            "n_latent": n_latent,
            "n_layers": n_layers,
            "max_epochs": max_epochs,
            "representation_id": representation_id,
            "representation_key": "X_scVI",
        },
        "facts_patch": {
            "analysis": {
                "representation": {
                    "id": representation_id,
                    "method": "scvi",
                    "batch_key": batch_key,
                    "key": "X_scVI",
                },
                "clustering": None,
            },
            "cluster_qc": None,
            "batch": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": [
            {
                "name": "scvi-latent-anndata",
                "relative_path": "scvi-latent.zarr",
                "media_type": "application/vnd.zarr",
            },
            {
                "name": "scvi-model",
                "relative_path": "scvi-model.zip",
                "media_type": "application/zip",
            },
            {
                "name": "scvi-training-history",
                "relative_path": "scvi-training-history.csv",
                "media_type": "text/csv",
            }
        ],
    }
