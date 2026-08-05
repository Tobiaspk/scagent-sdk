"""Independent Leiden clustering and group-wise gene ranking."""

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


def _load(arguments: dict[str, Any]) -> tuple[Path, Any]:

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    return path, _read_matrix(path)


def cluster_cells(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scanpy as sc

    path, adata = _load(arguments)
    neighbors_key = str(arguments.get("neighbors_key", "neighbors"))
    cluster_key = str(arguments.get("cluster_key", "leiden"))
    resolution = float(arguments.get("resolution", 0.8))
    seed = int(arguments.get("random_seed", 0))
    if neighbors_key not in adata.uns:
        raise ValueError(f"neighbor graph {neighbors_key!r} is absent")
    if cluster_key in adata.obs:
        raise ValueError(
            f"obs column {cluster_key!r} already exists; choose a new cluster_key "
            "to avoid overwriting labels"
        )
    sc.tl.leiden(
        adata,
        resolution=resolution,
        key_added=cluster_key,
        neighbors_key=None if neighbors_key == "neighbors" else neighbors_key,
        random_state=seed,
        flavor="igraph",
        n_iterations=2,
    )
    labels = adata.obs[cluster_key].astype(str)
    metadata = dict(adata.uns.get("scagent_sdk", {}))
    representation_id = metadata.get("representation_id") or metadata.get("neighbor_graph_id")
    if not representation_id:
        representation_id = _identity(
            "representation",
            {"input_path": str(path), "neighbors_key": neighbors_key},
        )
    cell_set_id = metadata.get("cell_set_id") or _identity(
        "cells", sorted(map(str, adata.obs_names))
    )
    clustering_id = _identity(
        "clustering",
        {
            "representation_id": representation_id,
            "cell_set_id": cell_set_id,
            "cluster_key": cluster_key,
            "resolution": resolution,
            "random_seed": seed,
            "labels": sorted(
                zip(map(str, adata.obs_names), map(str, labels), strict=True)
            ),
        },
    )
    metadata.update(
        {
            "cell_set_id": cell_set_id,
            "representation_id": representation_id,
            "clustering_id": clustering_id,
            "clustering_key": cluster_key,
        }
    )
    adata.uns["scagent_sdk"] = metadata
    output_name = "clustered.zarr"
    _write_matrix(adata, context.staging_dir / output_name)
    sizes = labels.value_counts().sort_index()
    sizes.rename_axis("cluster").rename("n_cells").to_csv(
        context.staging_dir / "cluster-sizes.csv"
    )
    return {
        "summary": (
            f"Created {sizes.size} Leiden groups in obs[{cluster_key!r}] from "
            f"neighbor graph {neighbors_key!r}."
        ),
        "details": {
            "neighbors_key": neighbors_key,
            "cluster_key": cluster_key,
            "resolution": resolution,
            "random_seed": seed,
            "n_clusters": int(sizes.size),
            "cluster_sizes": {str(key): int(value) for key, value in sizes.items()},
            "clustering_id": clustering_id,
        },
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "n_cells": int(adata.n_obs),
                    "n_genes": int(adata.n_vars),
                },
                "cell_set": {"id": cell_set_id, "n_cells": int(adata.n_obs)},
                "representation": {"id": representation_id},
                "clustering": {
                    "id": clustering_id,
                    "key": cluster_key,
                    "resolution": resolution,
                    "n_clusters": int(sizes.size),
                },
            },
            "cluster_qc": None,
            "batch": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": [
            {
                "name": "clustered-anndata",
                "relative_path": output_name,
                "media_type": "application/vnd.zarr",
            },
            {
                "name": "cluster-sizes",
                "relative_path": "cluster-sizes.csv",
                "media_type": "text/csv",
            },
        ],
    }


def rank_groups(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scanpy as sc

    path, adata = _load(arguments)
    group_key = str(arguments["group_key"])
    method = str(arguments.get("method", "wilcoxon"))
    layer_arg = arguments.get("layer")
    layer = str(layer_arg) if layer_arg is not None else None
    use_raw = bool(arguments.get("use_raw", False))
    reference = str(arguments.get("reference", "rest"))
    if group_key not in adata.obs:
        raise ValueError(f"group key {group_key!r} is absent")
    if adata.obs[group_key].astype(str).nunique() < 2:
        raise ValueError("at least two groups are required for gene ranking")
    if layer is not None and layer not in adata.layers:
        raise ValueError(f"layer {layer!r} is absent")
    sc.tl.rank_genes_groups(
        adata,
        groupby=group_key,
        method=method,
        layer=layer,
        use_raw=use_raw,
        reference=reference,
        pts=True,
        key_added=f"rank_genes_{group_key}",
    )
    table = sc.get.rank_genes_groups_df(
        adata, group=None, key=f"rank_genes_{group_key}"
    )
    table.to_csv(context.staging_dir / "ranked-genes.csv", index=False)
    output_name = "ranked-groups.zarr"
    final_path = f"artifacts/capabilities/{context.execution_id}/{output_name}"
    _write_matrix(adata, context.staging_dir / output_name)
    evidence_id = _identity(
        "ranked-genes",
        {
            "input_path": str(path),
            "group_key": group_key,
            "method": method,
            "layer": layer,
            "use_raw": use_raw,
            "reference": reference,
            "n_rows": int(len(table)),
        },
    )
    return {
        "summary": (
            f"Ranked genes for {adata.obs[group_key].astype(str).nunique()} groups "
            f"in {group_key!r} using {method}."
        ),
        "details": {
            "group_key": group_key,
            "method": method,
            "layer": layer,
            "use_raw": use_raw,
            "reference": reference,
            "n_rows": int(len(table)),
            "evidence_id": evidence_id,
        },
        "facts_patch": {
            "group_gene_ranking": {
                context.execution_id: {
                    "id": evidence_id,
                    "group_key": group_key,
                    "method": method,
                    "artifact_path": (
                        f"artifacts/capabilities/{context.execution_id}/ranked-genes.csv"
                    ),
                    "annotated_path": final_path,
                }
            }
        },
        "artifacts": [
            {
                "name": "ranked-groups-anndata",
                "relative_path": output_name,
                "media_type": "application/vnd.zarr",
            },
            {
                "name": "ranked-genes",
                "relative_path": "ranked-genes.csv",
                "media_type": "text/csv",
            },
        ],
    }
