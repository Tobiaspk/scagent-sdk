"""Publish one AnnData artifact as a portable H5AD without scientific mutation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

COMPRESSION = "gzip"
COMPRESSION_OPTS = 2


def _read_matrix(path):
    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)


def _output_filename(path: Path, requested: Any) -> str:
    filename = str(requested).strip() if requested is not None else f"{path.stem}.h5ad"
    candidate = Path(filename)
    if candidate.name != filename or candidate.suffix.lower() != ".h5ad":
        raise ValueError("filename must be a basename ending in .h5ad")
    return filename


def _keys(value: Any) -> list[str]:
    return sorted(map(str, value.keys()))


def _signature(adata: Any) -> dict[str, Any]:
    return {
        "shape": [int(adata.n_obs), int(adata.n_vars)],
        "obs_columns": list(map(str, adata.obs.columns)),
        "var_columns": list(map(str, adata.var.columns)),
        "layers": _keys(adata.layers),
        "obsm": _keys(adata.obsm),
        "varm": _keys(adata.varm),
        "obsp": _keys(adata.obsp),
        "varp": _keys(adata.varp),
        "uns": _keys(adata.uns),
        "raw_present": adata.raw is not None,
        "raw_shape": (
            [int(adata.raw.n_obs), int(adata.raw.n_vars)] if adata.raw is not None else None
        ),
    }


def _verify_round_trip(source: Any, output_path: Path) -> dict[str, Any]:
    import anndata as ad

    exported = ad.read_h5ad(output_path, backed="r")
    try:
        source_signature = _signature(source)
        exported_signature = _signature(exported)
        if exported_signature != source_signature:
            raise ValueError(
                "H5AD round-trip changed the AnnData structure: "
                f"source={source_signature}, exported={exported_signature}"
            )
        if not exported.obs_names.equals(source.obs_names):
            raise ValueError("H5AD round-trip changed obs_names")
        if not exported.var_names.equals(source.var_names):
            raise ValueError("H5AD round-trip changed var_names")
        return exported_signature
    finally:
        if exported.file is not None:
            exported.file.close()


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    path = Path(str(arguments["path"])).expanduser().resolve()
    if path.suffix.lower() not in {".h5ad", ".zarr"}:
        raise ValueError("export_anndata supports only .h5ad files and .zarr stores")
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".h5ad" and not path.is_file():
        raise ValueError("an .h5ad input must be a file")
    if path.suffix.lower() == ".zarr" and not path.is_dir():
        raise ValueError("a .zarr input must be a directory store")

    filename = _output_filename(path, arguments.get("filename"))
    output_path = context.staging_dir / filename
    adata = _read_matrix(path)
    adata.write_h5ad(
        output_path,
        compression=COMPRESSION,
        compression_opts=COMPRESSION_OPTS,
    )
    signature = _verify_round_trip(adata, output_path)

    return {
        "summary": (
            f"Exported {path.name} as the verified portable H5AD {filename} "
            f"({signature['shape'][0]:,} cells x {signature['shape'][1]:,} variables)."
        ),
        "details": {
            "source_path": str(path),
            "filename": filename,
            "compression": COMPRESSION,
            "compression_opts": COMPRESSION_OPTS,
            "round_trip_verified": True,
            **signature,
        },
        "facts_patch": {},
        "artifacts": [
            {
                "name": "portable-anndata",
                "relative_path": filename,
                "media_type": "application/x-h5ad",
            }
        ],
    }
