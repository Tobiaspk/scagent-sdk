"""Independent PCA, neighbor-graph, and UMAP operations."""

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

def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _scanpy_umap_key(requested_key: str) -> str | None:
    """Use Scanpy's conventional path for X_umap; custom keys are stored verbatim."""

    return None if requested_key == "X_umap" else requested_key


def _load(arguments: dict[str, Any]) -> tuple[Path, Any]:

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    return path, _read_matrix(path)


def _publish(
    adata: Any,
    context: Any,
    *,
    output_name: str,
    report_name: str,
    report: dict[str, Any],
    artifact_name: str,
) -> tuple[str, list[dict[str, str]]]:
    final_path = f"artifacts/capabilities/{context.execution_id}/{output_name}"
    _write_matrix(adata, context.staging_dir / output_name)
    (context.staging_dir / report_name).write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return final_path, [
        {
            "name": artifact_name,
            "relative_path": output_name,
            "media_type": "application/vnd.zarr",
        },
        {
            "name": report_name.removesuffix(".json"),
            "relative_path": report_name,
            "media_type": "application/json",
        },
    ]


def compute_pca(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import matplotlib.pyplot as plt
    import numpy as np
    import scanpy as sc

    path, adata = _load(arguments)
    requested = int(arguments.get("n_components", 50))
    use_hvg = bool(arguments.get("use_highly_variable", True))
    seed = int(arguments.get("random_seed", 0))
    if use_hvg and "highly_variable" not in adata.var:
        raise ValueError(
            "use_highly_variable=true requires var['highly_variable']; "
            "select HVGs first or set use_highly_variable=false"
        )
    available_genes = (
        int(adata.var["highly_variable"].sum()) if use_hvg else int(adata.n_vars)
    )
    n_components = min(requested, int(adata.n_obs) - 1, available_genes - 1)
    if n_components < 2:
        raise ValueError("at least three cells and three eligible genes are required for PCA")
    sc.tl.pca(
        adata,
        n_comps=n_components,
        use_highly_variable=use_hvg,
        random_state=seed,
    )
    metadata = dict(adata.uns.get("scagent_sdk", {}))
    cell_set_id = metadata.get("cell_set_id") or _identity(
        "cells", sorted(map(str, adata.obs_names))
    )
    representation_id = _identity(
        "representation",
        {
            "input_path": str(path),
            "cell_set_id": cell_set_id,
            "method": "pca",
            "n_components": n_components,
            "use_highly_variable": use_hvg,
            "random_seed": seed,
        },
    )
    metadata.update(
        {
            "cell_set_id": cell_set_id,
            "representation_id": representation_id,
            "representation_key": "X_pca",
        }
    )
    metadata.pop("clustering_id", None)
    adata.uns["scagent_sdk"] = metadata
    report = {
        "method": "pca",
        "representation_key": "X_pca",
        "representation_id": representation_id,
        "n_components": n_components,
        "use_highly_variable": use_hvg,
        "random_seed": seed,
    }
    variance_ratio = np.asarray(adata.uns["pca"]["variance_ratio"], dtype=float)
    components = np.arange(1, variance_ratio.size + 1)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    axes[0].plot(components, variance_ratio, marker="o", markersize=3, linewidth=1.2)
    axes[0].set(
        xlabel="Principal component",
        ylabel="Explained variance ratio",
        title="PCA variance by component",
    )
    axes[1].plot(
        components,
        np.cumsum(variance_ratio),
        marker="o",
        markersize=3,
        linewidth=1.2,
    )
    axes[1].set(
        xlabel="Principal component",
        ylabel="Cumulative explained variance",
        title="Cumulative PCA variance",
    )
    axes[1].set_ylim(0, min(1.0, max(0.05, float(np.cumsum(variance_ratio)[-1]) * 1.05)))
    fig.tight_layout()
    variance_figure = "pca/pca-variance-ratio.png"
    (context.staging_dir / "pca").mkdir(parents=True, exist_ok=True)
    fig.savefig(context.staging_dir / variance_figure, dpi=160, bbox_inches="tight")
    plt.close(fig)
    _, artifacts = _publish(
        adata,
        context,
        output_name="pca.zarr",
        report_name="pca.json",
        report=report,
        artifact_name="pca-anndata",
    )
    artifacts.append(
        {
            "name": "pca-variance-ratio",
            "relative_path": variance_figure,
            "media_type": "image/png",
        }
    )
    return {
        "summary": (
            f"Computed {n_components} principal components from "
            f"{available_genes:,} eligible genes."
        ),
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "n_cells": int(adata.n_obs),
                    "n_genes": int(adata.n_vars),
                },
                "cell_set": {"id": cell_set_id, "n_cells": int(adata.n_obs)},
                "representation": {
                    "id": representation_id,
                    "method": "pca",
                    "key": "X_pca",
                    "n_components": n_components,
                },
                "clustering": None,
            },
            "cluster_qc": None,
            "batch": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": artifacts,
        "model_media": [
            {
                "name": "pca-variance-ratio",
                "relative_path": variance_figure,
                "media_type": "image/png",
            }
        ],
    }


def build_neighbors(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scanpy as sc

    path, adata = _load(arguments)
    representation_key = str(arguments.get("representation_key", "X_pca"))
    neighbors_key = str(arguments.get("neighbors_key", "neighbors"))
    n_neighbors = min(int(arguments.get("n_neighbors", 15)), int(adata.n_obs) - 1)
    n_pcs_arg = arguments.get("n_pcs")
    n_pcs = int(n_pcs_arg) if n_pcs_arg is not None else None
    seed = int(arguments.get("random_seed", 0))
    if representation_key not in adata.obsm:
        raise ValueError(f"representation {representation_key!r} is absent from obsm")
    if n_neighbors < 2:
        raise ValueError("at least three cells are required for a neighbor graph")
    sc.pp.neighbors(
        adata,
        n_neighbors=n_neighbors,
        n_pcs=n_pcs,
        use_rep=representation_key,
        key_added=None if neighbors_key == "neighbors" else neighbors_key,
        random_state=seed,
    )
    metadata = dict(adata.uns.get("scagent_sdk", {}))
    source_id = metadata.get("representation_id") or _identity(
        "representation-source",
        {
            "input_path": str(path),
            "key": representation_key,
            "shape": list(map(int, adata.obsm[representation_key].shape)),
        },
    )
    graph_id = _identity(
        "neighbor-graph",
        {
            "source_id": source_id,
            "representation_key": representation_key,
            "neighbors_key": neighbors_key,
            "n_neighbors": n_neighbors,
            "n_pcs": n_pcs,
            "random_seed": seed,
        },
    )
    metadata.update({"representation_id": source_id, "neighbor_graph_id": graph_id})
    metadata.pop("clustering_id", None)
    adata.uns["scagent_sdk"] = metadata
    report = {
        "representation_key": representation_key,
        "representation_id": source_id,
        "neighbors_key": neighbors_key,
        "neighbor_graph_id": graph_id,
        "n_neighbors": n_neighbors,
        "n_pcs": n_pcs,
        "random_seed": seed,
    }
    _, artifacts = _publish(
        adata,
        context,
        output_name="neighbors.zarr",
        report_name="neighbors.json",
        report=report,
        artifact_name="neighbors-anndata",
    )
    return {
        "summary": (
            f"Built a {n_neighbors}-neighbor graph from {representation_key!r} "
            f"as {neighbors_key!r}."
        ),
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "n_cells": int(adata.n_obs),
                    "n_genes": int(adata.n_vars),
                },
                "representation": {
                    "id": source_id,
                    "key": representation_key,
                    "neighbor_graph_id": graph_id,
                    "neighbors_key": neighbors_key,
                },
                "clustering": None,
            },
            "cluster_qc": None,
            "batch": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": artifacts,
    }


def compute_umap(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scanpy as sc

    path, adata = _load(arguments)
    neighbors_key = str(arguments.get("neighbors_key", "neighbors"))
    umap_key = str(arguments.get("umap_key", "X_umap"))
    min_dist = float(arguments.get("min_dist", 0.5))
    spread = float(arguments.get("spread", 1.0))
    seed = int(arguments.get("random_seed", 0))
    if neighbors_key not in adata.uns:
        raise ValueError(f"neighbor graph {neighbors_key!r} is absent")
    scanpy_key = _scanpy_umap_key(umap_key)
    sc.tl.umap(
        adata,
        neighbors_key=None if neighbors_key == "neighbors" else neighbors_key,
        min_dist=min_dist,
        spread=spread,
        random_state=seed,
        key_added=scanpy_key,
    )
    actual_key = "X_umap" if scanpy_key is None else scanpy_key
    if actual_key not in adata.obsm:
        raise RuntimeError(
            f"Scanpy did not create requested UMAP key {actual_key!r}; "
            f"available embeddings: {', '.join(map(str, adata.obsm.keys())) or 'none'}"
        )
    layout_id = _identity(
        "umap",
        {
            "input_path": str(path),
            "neighbors_key": neighbors_key,
            "umap_key": actual_key,
            "min_dist": min_dist,
            "spread": spread,
            "random_seed": seed,
        },
    )
    metadata = dict(adata.uns.get("scagent_sdk", {}))
    metadata.update({"umap_id": layout_id, "umap_key": actual_key})
    adata.uns["scagent_sdk"] = metadata
    report = {
        "neighbors_key": neighbors_key,
        "umap_key": actual_key,
        "umap_id": layout_id,
        "min_dist": min_dist,
        "spread": spread,
        "random_seed": seed,
    }
    _, artifacts = _publish(
        adata,
        context,
        output_name="umap.zarr",
        report_name="umap.json",
        report=report,
        artifact_name="umap-anndata",
    )
    return {
        "summary": f"Computed {actual_key!r} from neighbor graph {neighbors_key!r}.",
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "n_cells": int(adata.n_obs),
                    "n_genes": int(adata.n_vars),
                },
                "umap": {
                    "id": layout_id,
                    "key": actual_key,
                    "neighbors_key": neighbors_key,
                },
            }
        },
        "artifacts": artifacts,
    }
