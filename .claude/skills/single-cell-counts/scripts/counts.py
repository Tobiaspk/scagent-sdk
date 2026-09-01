"""Resolve one raw-count representation without running downstream analysis."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# h5py defaults gzip to level 4, which dominated this capability's runtime. Nearly all of the
# ratio is available for far less CPU. Measured on one 36,419-cell input (100M stored values),
# writing the identical trimmed payload:
#
#     lzf            34.6 s   1.09 GB
#     gzip level 1   41.8 s   0.52 GB
#     gzip level 2   37.7 s   0.51 GB
#     gzip level 4   55.4 s   0.50 GB   <- the previous default
#
# Level 2 costs ~2% size against level 4 for ~32% less time, matches lzf's speed at half its
# size, and stays gzip, whose filter ships with every HDF5 distribution — lzf is an h5py filter
# that other readers (R's rhdf5) may not have.
INTERMEDIATE_COMPRESSION = "gzip"
INTERMEDIATE_COMPRESSION_OPTS = 2


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


def _redundant_payload(
    selected: str, *, layer_names: list[str], raw_present: bool
) -> list[str]:
    """Payload this tool is about to duplicate, and can therefore drop from its own artifact.

    The output carries the selected counts in both ``X`` and ``layers['counts']``. An aligned
    ``.raw`` and the source layer are then a third and fourth copy of the same values, which
    made the count-ready artifact larger than the input it came from. Only what this tool
    duplicated is dropped: unrelated layers (spliced/unspliced, velocity) are preserved, and
    the immutable input keeps everything regardless. `doublet-evidence` and `cluster-qc`
    already clear ``.raw`` the same way when they emit a filtered artifact.

    ``.raw`` is dropped whether or not it aligned to ``var_names``. An unaligned ``.raw`` is not
    strictly a duplicate, but no capability in this project reads ``.raw``, this artifact's
    contract is one validated count representation, and the source file is immutable and
    recorded in ``dataset_revision.source_path``, so nothing becomes unrecoverable.
    """
    dropped = []
    if raw_present:
        dropped.append("raw")
    if selected.startswith("layer:"):
        source_layer = selected.split(":", 1)[1]
        if source_layer != "counts" and source_layer in layer_names:
            dropped.append(selected)
    return dropped


def _count_matrix_identity(matrix: Any, obs_names: Any, var_names: Any) -> str:
    import numpy as np

    try:
        from scipy import sparse
    except ImportError:  # keeps this recipe callable from the control plane for drift tests
        sparse = None  # type: ignore[assignment]

    digest = hashlib.sha256()
    digest.update(b"scagent-count-matrix-v1\0")
    digest.update(str(tuple(map(int, matrix.shape))).encode() + b"\0")
    if sparse is not None and sparse.issparse(matrix):
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


def _read(path: Path, sc: Any) -> Any:
    if path.is_dir():
        return sc.read_10x_mtx(path, var_names="gene_symbols", cache=False)
    if path.suffix.lower() == ".h5ad":
        return _read_matrix(path)
    if path.suffix.lower() in {".h5", ".hdf5"}:
        return sc.read_10x_h5(path)
    raise ValueError("supported inputs are H5AD, 10x H5, or a 10x Matrix Market directory")


def _stored_values(matrix: Any) -> Any:
    import numpy as np

    if hasattr(matrix, "tocsr"):
        return np.asarray(matrix.tocsr().data)
    return np.asarray(matrix).ravel()


def _inspect_matrix(matrix: Any) -> dict[str, Any]:
    import numpy as np

    values = _stored_values(matrix)
    if values.size == 0:
        return {
            "count_like": True,
            "min": 0.0,
            "max": 0.0,
            "all_integer": True,
            "finite": True,
            "n_stored": 0,
        }
    finite = bool(np.all(np.isfinite(values)))
    nonnegative = bool(finite and np.all(values >= 0))
    all_integer = bool(finite and np.all(values == np.round(values)))
    return {
        "count_like": bool(finite and nonnegative and all_integer),
        "min": float(np.min(values)) if finite else None,
        "max": float(np.max(values)) if finite else None,
        "all_integer": all_integer,
        "finite": finite,
        "n_stored": int(values.size),
    }


def _choose_count_source(
    inspections: dict[str, dict[str, Any]],
    *,
    counts_source: str,
    counts_layer: str | None,
) -> tuple[str, str]:
    def require(label: str, absent: str) -> None:
        stats = inspections.get(label)
        if stats is None:
            raise ValueError(absent)
        if not stats["count_like"]:
            raise ValueError(
                f"{label} is not finite nonnegative integer counts "
                f"(min={stats['min']}, max={stats['max']}, all_integer={stats['all_integer']})"
            )

    if counts_source == "X":
        require("X", "X is unavailable")
        return "X", "explicit X selection validated as count-like"
    if counts_source == "raw":
        require("raw", "counts_source='raw' but no aligned count-like .raw is available")
        return "raw", "explicit .raw selection validated as count-like"
    if counts_source == "layer":
        if not counts_layer:
            raise ValueError("counts_source='layer' requires counts_layer")
        label = f"layer:{counts_layer}"
        require(label, f"counts layer {counts_layer!r} is absent")
        return label, f"explicit layer {counts_layer!r} selection validated as count-like"
    if counts_source != "auto":
        raise ValueError(f"unknown counts_source {counts_source!r}")
    if inspections.get("X", {}).get("count_like"):
        return "X", "X already holds count-like values"
    alternatives = sorted(
        label for label, stats in inspections.items() if label != "X" and stats["count_like"]
    )
    if len(alternatives) == 1:
        return alternatives[0], f"selected sole count-like alternative {alternatives[0]!r}"
    if not alternatives:
        raise ValueError(
            "no finite nonnegative integer count source was found in X, aligned .raw, or layers"
        )
    raise ValueError(
        "multiple count-like alternatives found ("
        + ", ".join(alternatives)
        + "); choose counts_source/counts_layer explicitly"
    )


def _candidate_sources(adata: Any) -> tuple[dict[str, Any], bool, bool]:
    candidates: dict[str, Any] = {"X": adata.X}
    for name in list(adata.layers.keys()):
        candidates[f"layer:{name}"] = adata.layers[name]
    raw_present = adata.raw is not None
    raw_usable = False
    if raw_present:
        raw_vars = set(map(str, adata.raw.var_names))
        if set(map(str, adata.var_names)).issubset(raw_vars):
            candidates["raw"] = adata.raw[:, adata.var_names].X
            raw_usable = True
    return candidates, raw_present, raw_usable


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scanpy as sc

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    source = str(arguments.get("counts_source", "auto"))
    layer_arg = arguments.get("counts_layer")
    layer = str(layer_arg) if layer_arg is not None else None

    adata = _read(path, sc)
    adata.var_names_make_unique()
    if adata.n_obs == 0 or adata.n_vars == 0:
        raise ValueError("the input matrix has zero cells or zero genes")
    candidates, raw_present, raw_usable = _candidate_sources(adata)
    inspections = {name: _inspect_matrix(matrix) for name, matrix in candidates.items()}
    selected, reason = _choose_count_source(
        inspections, counts_source=source, counts_layer=layer
    )
    # One copy out of the candidate (which may be a view into `.raw` or a layer dropped below),
    # then one more so X and the counts layer stay independent objects. The third copy the
    # previous version made was pure overhead.
    counts = candidates[selected].copy()
    adata.X = counts
    adata.layers["counts"] = counts.copy()

    dropped_payload = _redundant_payload(
        selected, layer_names=list(adata.layers.keys()), raw_present=raw_present
    )
    for item in dropped_payload:
        if item == "raw":
            adata.raw = None
        else:
            del adata.layers[item.split(":", 1)[1]]

    cell_set_id = _identity("cells", sorted(map(str, adata.obs_names)))
    matrix_id = _count_matrix_identity(counts, adata.obs_names, adata.var_names)
    count_id = _identity(
        "count-representation",
        {"matrix_id": matrix_id, "cell_set_id": cell_set_id, "selected_source": selected},
    )
    revision_id = _identity(
        "dataset-revision",
        {"input_path": str(path), "cell_set_id": cell_set_id, "count_id": count_id},
    )
    metadata = dict(adata.uns.get("scagent_sdk", {}))
    metadata.update(
        {
            "schema_version": 1,
            "source_path": str(path),
            "dataset_revision_id": revision_id,
            "cell_set_id": cell_set_id,
            "count_representation_id": count_id,
            "count_matrix_id": matrix_id,
            "count_source": selected,
        }
    )
    adata.uns["scagent_sdk"] = metadata
    output_name = "counts-ready.zarr"
    _write_matrix(adata, context.staging_dir / output_name)

    report = {
        "requested_source": source,
        "requested_layer": layer,
        "selected_source": selected,
        "selection_reason": reason,
        "raw_present": raw_present,
        "raw_usable": raw_usable,
        "dropped_payload": dropped_payload,
        "compression": f"{INTERMEDIATE_COMPRESSION}:{INTERMEDIATE_COMPRESSION_OPTS}",
        "inspections": inspections,
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "identities": {
            "dataset_revision_id": revision_id,
            "cell_set_id": cell_set_id,
            "count_representation_id": count_id,
            "count_matrix_id": matrix_id,
        },
    }
    (context.staging_dir / "count-source-selection.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "summary": (
            f"Materialized {selected} as raw counts for "
            f"{adata.n_obs:,} cells × {adata.n_vars:,} genes."
            + (
                f" Dropped redundant {', '.join(dropped_payload)} from the artifact; "
                "the source file retains them."
                if dropped_payload
                else ""
            )
        ),
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "id": revision_id,
                    "source_path": str(path),
                    "n_cells": int(adata.n_obs),
                    "n_genes": int(adata.n_vars),
                },
                "cell_set": {"id": cell_set_id, "n_cells": int(adata.n_obs)},
                "count_representation": {
                    "id": count_id,
                    "matrix_id": matrix_id,
                    "method": "validated-source-counts",
                    "count_source": selected,
                    "source_layer": "counts",
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
                "name": "count-ready-anndata",
                "relative_path": output_name,
                "media_type": "application/vnd.zarr",
            },
            {
                "name": "count-source-selection",
                "relative_path": "count-source-selection.json",
                "media_type": "application/json",
            },
        ],
    }
