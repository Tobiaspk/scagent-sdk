"""Deterministic import and minimal execution checks for physical compute runtimes."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import sys
from typing import Any


def _version(module: Any, distribution: str) -> str | None:
    direct = getattr(module, "__version__", None)
    if direct is not None:
        return str(direct)
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _rapids() -> dict[str, Any]:
    modules = {
        name: importlib.import_module(name)
        for name in (
            "torch",
            "numpy",
            "anndata",
            "scanpy",
            "rapids_singlecell",
            "cupy",
            "scvi",
            "celltypist",
            "scimilarity",
        )
    }
    cupy = modules["cupy"]
    vector = cupy.arange(16, dtype=cupy.float32)
    total = float(cupy.asnumpy(vector.sum()))
    if total != 120.0:
        raise RuntimeError(f"unexpected CuPy result: {total}")
    return {
        "python": sys.version.split()[0],
        "versions": {
            name: _version(module, "scvi-tools" if name == "scvi" else name)
            for name, module in modules.items()
        },
        "gpu_devices": cupy.cuda.runtime.getDeviceCount(),
        "cuda_runtime_version": cupy.cuda.runtime.runtimeGetVersion(),
        "cupy_sum": total,
    }


def _cellbender() -> dict[str, Any]:
    cellbender = importlib.import_module("cellbender")
    torch = importlib.import_module("torch")
    return {
        "python": sys.version.split()[0],
        "versions": {
            "cellbender": _version(cellbender, "cellbender"),
            "torch": _version(torch, "torch"),
        },
        "gpu_devices": torch.cuda.device_count(),
    }


def _diffxpy() -> dict[str, Any]:
    modules = {
        name: importlib.import_module(name)
        for name in ("numpy", "scipy", "pandas", "anndata", "tensorflow", "diffxpy")
    }
    return {
        "python": sys.version.split()[0],
        "versions": {
            name: _version(module, "tensorflow-cpu" if name == "tensorflow" else name)
            for name, module in modules.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", choices=("rapids", "cellbender", "diffxpy"))
    args = parser.parse_args()
    checks = {"rapids": _rapids, "cellbender": _cellbender, "diffxpy": _diffxpy}
    result = {"runtime": args.runtime, "status": "pass", **checks[args.runtime]()}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
