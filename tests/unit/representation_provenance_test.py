from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _clustering_globals() -> dict[str, Any]:
    root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        package
        for package in CapabilityRegistry(root).discover()
        if package.manifest.skill_id == "single-cell-clustering"
    )
    tool = next(tool for tool in package.manifest.tools if tool.name == "cluster_single_cells")
    return package.load_handler(tool).__globals__


def test_clustering_uses_graph_representation_not_latest_embedding() -> None:
    resolve = _clustering_globals()["_neighbor_graph_source"]
    adata = SimpleNamespace(
        uns={"neighbors": {"params": {"use_rep": "X_scVI"}}},
        obsm={"X_scVI": np.zeros((3, 2)), "X_pca": np.zeros((3, 2))},
    )
    graph_id, representation_id, representation_key = resolve(
        adata,
        {
            "neighbor_graph_id": "graph-a",
            "representation_id": "pca-latest",
            "representation_key": "X_pca",
            "representations": {
                "X_pca": {"id": "pca-latest"},
                "X_scVI": {"id": "scvi-source"},
            },
        },
        Path("/tmp/example.zarr"),
        "neighbors",
    )
    assert graph_id == "graph-a"
    assert representation_id == "scvi-source"
    assert representation_key == "X_scVI"
