"""Validate raw 10x droplets and run CellBender in its locked GPU runtime."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

SAMPLE_BYTES = 1024 * 1024
CELLBENDER_RANDOM_SEED = 1234
LOW_COUNT_UMI = 10
MIN_BARCODES = 1000
MIN_LOW_COUNT_BARCODES = 100
MIN_LOW_COUNT_FRACTION = 0.01
MAX_LOG_WARNINGS = 20


def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _dataset_fingerprint(path: Path, *, mode: str) -> str:
    if mode not in {"sampled", "full"}:
        raise ValueError(f"unsupported recorded fingerprint mode: {mode!r}")
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(f"scagent-dataset-v1\0{size}\0".encode())
    with path.open("rb") as handle:
        if mode == "full":
            for chunk in iter(lambda: handle.read(SAMPLE_BYTES), b""):
                digest.update(chunk)
        else:
            digest.update(handle.read(SAMPLE_BYTES))
            if size > SAMPLE_BYTES:
                handle.seek(max(0, size - SAMPLE_BYTES))
                digest.update(handle.read(SAMPLE_BYTES))
    return f"sha256:{digest.hexdigest()}"


def _active_dataset(context: Any, path: Path) -> dict[str, Any]:
    dataset = context.state_facts.get("dataset")
    if not isinstance(dataset, dict):
        raise ValueError("the session has no active dataset identity")
    recorded_path = dataset.get("path")
    fingerprint = dataset.get("fingerprint")
    mode = dataset.get("fingerprint_mode")
    if not all(isinstance(value, str) and value for value in (recorded_path, fingerprint, mode)):
        raise ValueError("the active dataset identity is incomplete")
    if Path(recorded_path).expanduser().resolve() != path:
        raise ValueError(
            "input path does not match the active dataset identity; run inspect_dataset on this "
            "exact raw input"
        )
    if not path.is_file():
        raise FileNotFoundError(path)
    stat = path.stat()
    if dataset.get("size_bytes") != stat.st_size:
        raise ValueError("the input size changed after dataset inspection")
    recorded_mtime = dataset.get("modified_time_ns")
    if isinstance(recorded_mtime, int) and recorded_mtime != stat.st_mtime_ns:
        raise ValueError("the input modification time changed after dataset inspection")
    observed = _dataset_fingerprint(path, mode=mode)
    if observed != fingerprint:
        raise ValueError("the input fingerprint changed after dataset inspection")
    return dataset


def _decode_values(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, bytes):
            result.append(value.decode("utf-8", errors="replace"))
        else:
            result.append(str(value))
    return result


def _read_10x_h5(path: Path) -> tuple[dict[str, Any], Any]:
    import h5py
    import numpy as np
    from scipy.sparse import csc_matrix

    if path.suffix.lower() not in {".h5", ".hdf5"}:
        raise ValueError("this capability currently supports Cell Ranger-style 10x H5 input only")
    try:
        handle = h5py.File(path, "r")
    except OSError as exc:
        raise ValueError(f"input is not a readable HDF5 file: {exc}") from exc
    with handle:
        root_keys = sorted(map(str, handle.keys()))
        if "matrix" not in handle:
            raise ValueError("H5 input has no /matrix group and is not a supported 10x matrix")
        matrix = handle["matrix"]
        required = {"barcodes", "data", "features", "indices", "indptr", "shape"}
        missing = sorted(required - set(matrix.keys()))
        if missing:
            raise ValueError(f"10x /matrix group is missing required datasets: {missing}")
        shape = tuple(int(value) for value in matrix["shape"][()])
        if len(shape) != 2 or min(shape) <= 0:
            raise ValueError(f"invalid 10x sparse matrix shape: {shape}")
        n_features, n_barcodes = shape
        if len(matrix["barcodes"]) != n_barcodes:
            raise ValueError("10x barcode count disagrees with sparse matrix shape")
        data = matrix["data"][()]
        if not np.issubdtype(data.dtype, np.integer):
            raise ValueError(f"10x count data must be integer-valued, found dtype {data.dtype}")
        if data.size and int(data.min()) < 0:
            raise ValueError("10x count data contains negative values")
        sparse = csc_matrix(
            (data, matrix["indices"][()], matrix["indptr"][()]),
            shape=shape,
        )
        totals = np.asarray(sparse.sum(axis=0)).ravel()
        feature_types: list[str] = []
        features = matrix["features"]
        if "feature_type" in features:
            feature_types = _decode_values(features["feature_type"][()])
        counts = Counter(feature_types)
        title = matrix.attrs.get("TITLE")
        if isinstance(title, bytes):
            title = title.decode("utf-8", errors="replace")
        quantile_values = np.quantile(totals, [0, 0.01, 0.1, 0.5, 0.9, 0.99, 1])
        low_count = int((totals <= LOW_COUNT_UMI).sum())
        summary = {
            "container": "10x-h5",
            "shape": {"features": n_features, "barcodes": n_barcodes},
            "nnz": int(data.size),
            "count_dtype": str(data.dtype),
            "root_keys": root_keys,
            "matrix_title": str(title) if title is not None else None,
            "feature_types": dict(sorted(counts.items())),
            "gene_expression_features": int(counts.get("Gene Expression", n_features)),
            "zero_count_barcodes": int((totals == 0).sum()),
            "nonzero_barcodes": int((totals > 0).sum()),
            "low_count_umi_threshold": LOW_COUNT_UMI,
            "low_count_barcodes": low_count,
            "low_count_fraction": low_count / n_barcodes,
            "umi_quantiles": {
                name: float(value)
                for name, value in zip(
                    ("min", "q01", "q10", "median", "q90", "q99", "max"),
                    quantile_values,
                    strict=True,
                )
            },
        }
    return summary, totals


def _assess_suitability(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []
    name = path.name.lower()
    if "filtered" in name:
        reasons.append("filename indicates a filtered matrix")
    cellbender_keys = {"droplet_latents", "global_latents", "metadata"}
    present = sorted(cellbender_keys.intersection(summary["root_keys"]))
    if present or "background correction" in str(summary.get("matrix_title", "")).lower():
        reasons.append("H5 contains CellBender output metadata/latents")
    n_barcodes = int(summary["shape"]["barcodes"])
    if n_barcodes < MIN_BARCODES:
        reasons.append(f"only {n_barcodes} barcodes are present; raw droplet inference needs more")
    required_low = max(
        MIN_LOW_COUNT_BARCODES,
        math.ceil(MIN_LOW_COUNT_FRACTION * n_barcodes),
    )
    observed_low = int(summary["low_count_barcodes"])
    if observed_low < required_low:
        reasons.append(
            f"only {observed_low} barcodes have <= {LOW_COUNT_UMI} UMIs; at least "
            f"{required_low} are required as empty-droplet evidence"
        )
    if int(summary["gene_expression_features"]) <= 0:
        reasons.append("matrix has no Gene Expression features")
    non_gene_expression = sorted(
        name for name in summary["feature_types"] if name != "Gene Expression"
    )
    if non_gene_expression:
        warnings.append(
            "non-Gene Expression feature types will be excluded from inference and left unchanged: "
            + ", ".join(non_gene_expression)
        )
    return {
        "status": "suitable" if not reasons else "unsuitable",
        "reasons": reasons,
        "warnings": warnings,
        "required_low_count_barcodes": required_low,
        "required_low_count_semantics": (
            "minimum empty-tail evidence for suitability only; not an expected-cell estimate"
        ),
    }


def _write_rank_plot(totals: Any, path: Path, *, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    ranked = np.sort(np.asarray(totals, dtype=float))[::-1]
    ranked = np.maximum(ranked, 0.5)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.loglog(np.arange(1, ranked.size + 1), ranked, linewidth=1.0)
    ax.axhline(LOW_COUNT_UMI, color="tab:red", linestyle="--", linewidth=1)
    ax.set_xlabel("Barcode rank")
    ax.set_ylabel("Total UMIs")
    ax.set_title(title)
    ax.grid(alpha=0.2)
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)


def _write_validation_report(
    path: Path,
    *,
    source: Path,
    assessment: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    reasons = assessment["reasons"] or ["No refusal criteria were triggered."]
    warnings = assessment["warnings"] or ["None."]
    lines = [
        "# CellBender input assessment",
        "",
        f"- Input: `{source}`",
        f"- Status: **{assessment['status']}**",
        f"- Shape: {summary['shape']['features']:,} features × "
        f"{summary['shape']['barcodes']:,} barcodes",
        f"- Median UMIs: {summary['umi_quantiles']['median']:.3g}",
        f"- Barcodes with ≤{LOW_COUNT_UMI} UMIs: "
        f"{summary['low_count_barcodes']:,} ({summary['low_count_fraction']:.1%})",
        f"- Minimum low-count evidence gate: {assessment['required_low_count_barcodes']:,} "
        "barcodes (not a cell-count estimate)",
        "",
        "## Assessment",
        "",
        *(f"- {item}" for item in reasons),
        "",
        "## Warnings",
        "",
        *(f"- {item}" for item in warnings),
        "",
        "A suitable result establishes raw empty-droplet evidence; it does not establish that "
        "ambient correction is necessary for the biological question.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_validate(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    stat = path.stat()
    dataset = {
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_time_ns": stat.st_mtime_ns,
        "fingerprint": _dataset_fingerprint(path, mode="full"),
        "fingerprint_mode": "full",
    }
    summary, totals = _read_10x_h5(path)
    assessment = _assess_suitability(path, summary)
    validation = {
        "schema_version": 1,
        "input_path": str(path),
        "dataset_fingerprint": dataset["fingerprint"],
        "dataset_fingerprint_mode": dataset["fingerprint_mode"],
        "assessment": assessment,
        "matrix": summary,
    }
    (context.staging_dir / "input-validation.json").write_text(
        json.dumps(validation, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    _write_rank_plot(
        totals,
        context.staging_dir / "input-umi-rank.png",
        title="Raw droplet UMI rank for CellBender suitability",
    )
    _write_validation_report(
        context.staging_dir / "input-assessment.md",
        source=path,
        assessment=assessment,
        summary=summary,
    )
    status = assessment["status"]
    if status == "suitable":
        message = (
            f"Validated raw-droplet evidence in {summary['shape']['barcodes']:,} barcodes; "
            f"{summary['low_count_barcodes']:,} have <= {LOW_COUNT_UMI} UMIs."
        )
    else:
        message = "Refused CellBender input: " + "; ".join(assessment["reasons"])
    fact = {
        "status": status,
        "input_path": str(path),
        "dataset_fingerprint": dataset["fingerprint"],
        "matrix_shape": summary["shape"],
        "low_count_barcodes": summary["low_count_barcodes"],
        "low_count_fraction": summary["low_count_fraction"],
        "reasons": assessment["reasons"],
        "warnings": assessment["warnings"],
    }
    artifacts = [
        {
            "name": "cellbender-input-validation",
            "relative_path": "input-validation.json",
            "media_type": "application/json",
        },
        {
            "name": "cellbender-input-assessment",
            "relative_path": "input-assessment.md",
            "media_type": "text/markdown",
        },
        {
            "name": "cellbender-input-umi-rank",
            "relative_path": "input-umi-rank.png",
            "media_type": "image/png",
        },
    ]
    return {
        "summary": message,
        "details": validation,
        "facts_patch": {
            "dataset": dataset,
            "ambient_background": {"input_validation": fact},
        },
        "decisions_patch": {},
        "artifacts": artifacts,
        "model_media": [artifacts[-1]],
    }


def _optional_positive(arguments: dict[str, Any], name: str) -> int | None:
    value = arguments.get(name)
    if value is None:
        return None
    converted = int(value)
    if converted <= 0:
        raise ValueError(f"{name} must be positive")
    return converted


def _parameters(arguments: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    expected_cells = _optional_positive(arguments, "expected_cells")
    total_droplets = _optional_positive(arguments, "total_droplets_included")
    epochs = _optional_positive(arguments, "epochs")
    posterior_batch = _optional_positive(arguments, "posterior_batch_size")
    low_count_raw = arguments.get("low_count_threshold")
    low_count = int(low_count_raw) if low_count_raw is not None else None
    if low_count is not None and low_count < 0:
        raise ValueError("low_count_threshold must be nonnegative")
    n_barcodes = int(summary["shape"]["barcodes"])
    if total_droplets is not None and total_droplets > n_barcodes:
        raise ValueError("total_droplets_included exceeds the input barcode count")
    if expected_cells is not None and expected_cells > n_barcodes:
        raise ValueError("expected_cells exceeds the input barcode count")
    if (
        expected_cells is not None
        and total_droplets is not None
        and total_droplets <= expected_cells
    ):
        raise ValueError("total_droplets_included must be larger than expected_cells")
    fpr = float(arguments.get("fpr", 0.01))
    if not 0 <= fpr < 1:
        raise ValueError("fpr must satisfy 0 <= fpr < 1")
    model = str(arguments.get("model", "full"))
    if model not in {"naive", "simple", "ambient", "swapping", "full"}:
        raise ValueError(f"unsupported CellBender model: {model}")
    selected = str(arguments.get("selected_output", "filtered"))
    if selected not in {"filtered", "full"}:
        raise ValueError("selected_output must be filtered or full")
    timeout = int(arguments.get("timeout_seconds", 21000))
    if not 60 <= timeout <= 21000:
        raise ValueError("timeout_seconds must be between 60 and 21000")
    supplied_exclusions = arguments.get("exclude_feature_types", [])
    if not isinstance(supplied_exclusions, list) or not all(
        isinstance(value, str) and value.strip() for value in supplied_exclusions
    ):
        raise ValueError("exclude_feature_types must contain non-empty strings")
    detected = [name for name in summary["feature_types"] if name != "Gene Expression"]
    exclusions = sorted({value.strip() for value in supplied_exclusions}.union(detected))
    return {
        "expected_cells": expected_cells,
        "total_droplets_included": total_droplets,
        "fpr": fpr,
        "epochs": epochs,
        "model": model,
        "low_count_threshold": low_count,
        "posterior_batch_size": posterior_batch,
        "exclude_feature_types": exclusions,
        "selected_output": selected,
        "timeout_seconds": timeout,
        "random_seed": CELLBENDER_RANDOM_SEED,
    }


def _build_command(
    executable: str,
    *,
    input_path: Path,
    output_path: Path,
    parameters: dict[str, Any],
    checkpoint_path: Path | None,
) -> list[str]:
    command = [
        executable,
        "remove-background",
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--cuda",
        "--model",
        parameters["model"],
        "--fpr",
        str(parameters["fpr"]),
    ]
    names = {
        "expected_cells": "--expected-cells",
        "total_droplets_included": "--total-droplets-included",
        "epochs": "--epochs",
        "low_count_threshold": "--low-count-threshold",
        "posterior_batch_size": "--posterior-batch-size",
    }
    for name, flag in names.items():
        if parameters[name] is not None:
            command.extend([flag, str(parameters[name])])
    if parameters["exclude_feature_types"]:
        command.append("--exclude-feature-types")
        command.extend(parameters["exclude_feature_types"])
    if checkpoint_path is not None:
        command.extend(["--checkpoint", str(checkpoint_path)])
    return command


def _parse_metrics(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    result: dict[str, Any] = {}
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.reader(handle):
            if len(row) < 2 or not row[0]:
                continue
            raw = row[1].strip()
            try:
                result[row[0].strip()] = float(raw)
            except ValueError:
                result[row[0].strip()] = raw
    return result


def _extract_warnings(paths: list[Path]) -> list[str]:
    warnings: list[str] = []
    needles = ("warning", "unable", "failed", "non-converg", "nan")
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            text = line.strip()
            if text and any(needle in text.lower() for needle in needles):
                warnings.append(text[-1000:])
                if len(warnings) >= MAX_LOG_WARNINGS:
                    return warnings
    return warnings


def _artifact(name: str, relative_path: str, media_type: str) -> dict[str, str]:
    return {"name": name, "relative_path": relative_path, "media_type": media_type}


def _existing_artifacts(staging: Path, candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    return [item for item in candidates if (staging / item["relative_path"]).is_file()]


def _barcode_identity(path: Path) -> tuple[str, int]:
    import h5py

    digest = hashlib.sha256()
    with h5py.File(path, "r") as handle:
        barcodes = handle["matrix/barcodes"]
        for value in barcodes:
            encoded = value if isinstance(value, bytes) else str(value).encode()
            digest.update(encoded)
            digest.update(b"\0")
        count = len(barcodes)
    return f"barcodes:sha256:{digest.hexdigest()}", count


def _write_comparison_plot(raw_totals: Any, corrected_totals: Any, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig, ax = plt.subplots(figsize=(8, 5))
    series = (("raw input", raw_totals), ("selected corrected output", corrected_totals))
    for label, values in series:
        ranked = np.sort(np.asarray(values, dtype=float))[::-1]
        ranked = np.maximum(ranked, 0.5)
        ax.loglog(np.arange(1, ranked.size + 1), ranked, label=label, linewidth=1)
    ax.set_xlabel("Barcode rank")
    ax.set_ylabel("Total UMIs")
    ax.set_title("Raw and CellBender-corrected barcode ranks")
    ax.legend()
    ax.grid(alpha=0.2)
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)


def _write_run_report(
    path: Path,
    *,
    status: str,
    runtime_seconds: float,
    parameters: dict[str, Any],
    metrics: dict[str, Any],
    warnings: list[str],
    failure: str | None,
    selected_summary: dict[str, Any] | None,
) -> None:
    lines = [
        "# CellBender ambient-background removal",
        "",
        f"- Status: **{status}**",
        f"- Runtime: {runtime_seconds:.1f} seconds",
        f"- Fixed random seed: {CELLBENDER_RANDOM_SEED}",
        f"- Selected output: {parameters['selected_output']}",
        "",
        "## Parameters",
        "",
        "```json",
        json.dumps(parameters, indent=2, sort_keys=True),
        "```",
    ]
    if failure:
        lines.extend(["", "## Failure", "", failure])
    if selected_summary:
        lines.extend(
            [
                "",
                "## Selected output",
                "",
                f"- Features: {selected_summary['shape']['features']:,}",
                f"- Barcodes: {selected_summary['shape']['barcodes']:,}",
                f"- Nonzero entries: {selected_summary['nnz']:,}",
            ]
        )
    lines.extend(["", "## Metrics", ""])
    if metrics:
        lines.extend(f"- `{name}`: {value}" for name, value in sorted(metrics.items()))
    else:
        lines.append("- No metrics CSV was produced.")
    lines.extend(["", "## Log warnings", ""])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- No warning-pattern lines were detected; inspect the complete log anyway.")
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "Container integrity and process completion do not prove biological improvement. "
            "Review the CellBender PDF/report, priors, cell probabilities, convergence evidence, "
            "counts removed, and gene-level effects before trusting the correction. Continue with "
            "ordinary "
            "cell QC, doublet review, normalization, clustering, and cluster QC.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _failed_result(
    *,
    context: Any,
    dataset: dict[str, Any],
    parameters: dict[str, Any],
    command: list[str],
    runtime_seconds: float,
    reason: str,
    candidates: list[dict[str, str]],
    warnings: list[str],
) -> dict[str, Any]:
    _write_run_report(
        context.staging_dir / "cellbender-interpretation.md",
        status="failed",
        runtime_seconds=runtime_seconds,
        parameters=parameters,
        metrics={},
        warnings=warnings,
        failure=reason,
        selected_summary=None,
    )
    artifacts = _existing_artifacts(context.staging_dir, candidates)
    checkpoint = next(
        (item for item in artifacts if item["name"] == "cellbender-checkpoint"),
        None,
    )
    checkpoint_path = None
    if checkpoint:
        checkpoint_path = (
            f"{context.artifact_relative_path}/{checkpoint['relative_path']}"
        )
    return {
        "summary": f"CellBender did not complete; active data were unchanged. {reason}",
        "details": {
            "status": "failed",
            "reason": reason,
            "runtime_seconds": runtime_seconds,
            "parameters": parameters,
            "command_argv": command,
            "warnings": warnings,
            "checkpoint_path": checkpoint_path,
        },
        "facts_patch": {
            "ambient_background": {
                "last_run": {
                    "status": "failed",
                    "input_path": dataset["path"],
                    "dataset_fingerprint": dataset["fingerprint"],
                    "reason": reason,
                    "checkpoint_path": checkpoint_path,
                }
            }
        },
        "decisions_patch": {},
        "artifacts": artifacts,
    }


def run_remove(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    path = Path(str(arguments["path"])).expanduser().resolve()
    dataset = _active_dataset(context, path)
    ambient = context.state_facts.get("ambient_background")
    validation = ambient.get("input_validation") if isinstance(ambient, dict) else None
    if not isinstance(validation, dict) or validation.get("status") != "suitable":
        raise ValueError("current CellBender input validation is absent or unsuitable")
    if validation.get("dataset_fingerprint") != dataset["fingerprint"]:
        raise ValueError("CellBender input validation is stale for the active dataset")
    if Path(str(validation.get("input_path", ""))).expanduser().resolve() != path:
        raise ValueError("CellBender input validation was recorded for a different path")

    raw_summary, raw_totals = _read_10x_h5(path)
    assessment = _assess_suitability(path, raw_summary)
    if assessment["status"] != "suitable":
        raise ValueError(
            "CellBender input is no longer suitable: " + "; ".join(assessment["reasons"])
        )
    parameters = _parameters(arguments, raw_summary)
    executable = shutil.which("cellbender")
    if executable is None:
        raise RuntimeError("cellbender executable is absent from the declared runtime")

    staging = context.staging_dir
    output = staging / "cellbender-output.h5"
    if output.exists():
        raise FileExistsError("staging output already exists; refusing to overwrite it")
    checkpoint_argument: Path | None = None
    source_checkpoint_raw = arguments.get("checkpoint_path")
    if source_checkpoint_raw:
        source_checkpoint = Path(str(source_checkpoint_raw)).expanduser().resolve()
        if not source_checkpoint.is_file():
            raise FileNotFoundError(f"CellBender checkpoint not found: {source_checkpoint}")
        checkpoint_argument = staging / "ckpt.tar.gz"
        shutil.copy2(source_checkpoint, checkpoint_argument)

    command = _build_command(
        executable,
        input_path=path,
        output_path=output,
        parameters=parameters,
        checkpoint_path=checkpoint_argument,
    )
    lineage = {
        "schema_version": 1,
        "source": {
            "path": str(path),
            "size_bytes": dataset["size_bytes"],
            "fingerprint": dataset["fingerprint"],
            "fingerprint_mode": dataset["fingerprint_mode"],
        },
        "source_is_read_only": True,
        "selected_output": parameters["selected_output"],
    }
    (staging / "input-lineage.json").write_text(
        json.dumps(lineage, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    command_record = {
        "schema_version": 1,
        "command_argv": command,
        "parameters": parameters,
        "cellbender_random_seed": CELLBENDER_RANDOM_SEED,
        "gpu_required": True,
    }
    (staging / "cellbender-command.json").write_text(
        json.dumps(command_record, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    stdout_path = staging / "cellbender.stdout.log"
    stderr_path = staging / "cellbender.stderr.log"
    started = time.monotonic()
    timed_out = False
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        try:
            completed = subprocess.run(
                command,
                cwd=staging,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                timeout=parameters["timeout_seconds"],
                check=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = -1
    runtime_seconds = time.monotonic() - started

    related = {
        "filtered": staging / "cellbender-output_filtered.h5",
        "posterior": staging / "cellbender-output_posterior.h5",
        "metrics": staging / "cellbender-output_metrics.csv",
        "barcodes": staging / "cellbender-output_cell_barcodes.csv",
        "pdf": staging / "cellbender-output.pdf",
        "html": staging / "cellbender-output_report.html",
        "log": staging / "cellbender-output.log",
        "checkpoint": staging / "ckpt.tar.gz",
    }
    candidates = [
        _artifact("cellbender-input-lineage", "input-lineage.json", "application/json"),
        _artifact("cellbender-command", "cellbender-command.json", "application/json"),
        _artifact("cellbender-stdout", "cellbender.stdout.log", "text/plain"),
        _artifact("cellbender-stderr", "cellbender.stderr.log", "text/plain"),
        _artifact("cellbender-log", "cellbender-output.log", "text/plain"),
        _artifact("cellbender-checkpoint", "ckpt.tar.gz", "application/gzip"),
        _artifact("cellbender-full-output", "cellbender-output.h5", "application/x-hdf5"),
        _artifact(
            "cellbender-filtered-output",
            "cellbender-output_filtered.h5",
            "application/x-hdf5",
        ),
        _artifact("cellbender-posterior", "cellbender-output_posterior.h5", "application/x-hdf5"),
        _artifact("cellbender-metrics", "cellbender-output_metrics.csv", "text/csv"),
        _artifact("cellbender-cell-barcodes", "cellbender-output_cell_barcodes.csv", "text/csv"),
        _artifact("cellbender-summary-pdf", "cellbender-output.pdf", "application/pdf"),
        _artifact("cellbender-html-report", "cellbender-output_report.html", "text/html"),
        _artifact(
            "cellbender-count-comparison",
            "cellbender-count-comparison.png",
            "image/png",
        ),
        _artifact(
            "cellbender-interpretation",
            "cellbender-interpretation.md",
            "text/markdown",
        ),
    ]
    warnings = _extract_warnings([related["log"], stdout_path, stderr_path])
    if timed_out:
        return _failed_result(
            context=context,
            dataset=dataset,
            parameters=parameters,
            command=command,
            runtime_seconds=runtime_seconds,
            reason=f"inner timeout reached after {parameters['timeout_seconds']} seconds",
            candidates=candidates,
            warnings=warnings,
        )
    if returncode != 0:
        return _failed_result(
            context=context,
            dataset=dataset,
            parameters=parameters,
            command=command,
            runtime_seconds=runtime_seconds,
            reason=f"CellBender exited with return code {returncode}",
            candidates=candidates,
            warnings=warnings,
        )
    required_outputs = [output, related["filtered"], related["metrics"]]
    missing = [
        item.name
        for item in required_outputs
        if not item.is_file() or item.stat().st_size == 0
    ]
    if missing:
        return _failed_result(
            context=context,
            dataset=dataset,
            parameters=parameters,
            command=command,
            runtime_seconds=runtime_seconds,
            reason=(
                "CellBender returned success but required outputs are missing: "
                + ", ".join(missing)
            ),
            candidates=candidates,
            warnings=warnings,
        )

    selected_path = related["filtered"] if parameters["selected_output"] == "filtered" else output
    selected_summary, corrected_totals = _read_10x_h5(selected_path)
    output_fingerprint = _dataset_fingerprint(selected_path, mode="full")
    barcode_set_id, barcode_count = _barcode_identity(selected_path)
    count_representation_id = _identity(
        "counts",
        {
            "parent_fingerprint": dataset["fingerprint"],
            "output_fingerprint": output_fingerprint,
            "parameters": parameters,
        },
    )
    dataset_revision_id = _identity(
        "dataset-revision",
        {
            "parent_fingerprint": dataset["fingerprint"],
            "output_fingerprint": output_fingerprint,
            "selected_output": parameters["selected_output"],
        },
    )
    _write_comparison_plot(
        raw_totals,
        corrected_totals,
        staging / "cellbender-count-comparison.png",
    )
    metrics = _parse_metrics(related["metrics"])
    _write_run_report(
        staging / "cellbender-interpretation.md",
        status="complete",
        runtime_seconds=runtime_seconds,
        parameters=parameters,
        metrics=metrics,
        warnings=warnings,
        failure=None,
        selected_summary=selected_summary,
    )
    artifacts = _existing_artifacts(staging, candidates)
    selected_relative = selected_path.name
    selected_final = (
        context.session_dir
        / "artifacts"
        / "capabilities"
        / context.execution_id
        / selected_relative
    )
    stat = selected_path.stat()
    dataset_fact = {
        "path": str(selected_final),
        "size_bytes": stat.st_size,
        "modified_time_ns": stat.st_mtime_ns,
        "fingerprint": output_fingerprint,
        "fingerprint_mode": "full",
        "format": {
            "extension": "h5",
            "suffixes": [".h5"],
            "byte_signature": "hdf5",
            "extension_signature_consistent": True,
        },
        "lineage": {
            "parent_path": str(path),
            "parent_fingerprint": dataset["fingerprint"],
            "transformation": "cellbender-remove-background",
            "selected_output": parameters["selected_output"],
        },
    }
    completion = {
        "status": "complete",
        "input_path": str(path),
        "parent_dataset_fingerprint": dataset["fingerprint"],
        "selected_output": parameters["selected_output"],
        "output_path": str(selected_final),
        "output_fingerprint": output_fingerprint,
        "dataset_revision_id": dataset_revision_id,
        "barcode_set_id": barcode_set_id,
        "count_representation_id": count_representation_id,
        "parameters": parameters,
        "metrics": metrics,
        "warnings": warnings,
    }
    selected_kind = "cell-set" if parameters["selected_output"] == "filtered" else "droplet-set"
    return {
        "summary": (
            f"CellBender completed on GPU and selected the {parameters['selected_output']} output: "
            f"{barcode_count:,} barcodes × {selected_summary['shape']['features']:,} features."
        ),
        "details": completion,
        "facts_patch": {
            "dataset": dataset_fact,
            "ambient_background": {
                "input_validation": None,
                "last_run": completion,
                "status": "complete",
            },
            "analysis": {
                "dataset_revision": {
                    "id": dataset_revision_id,
                    "parent_fingerprint": dataset["fingerprint"],
                    "corrected_path": str(selected_final),
                    "selected_output": parameters["selected_output"],
                },
                "cell_set": {
                    "id": barcode_set_id,
                    "n_cells": barcode_count,
                    "kind": selected_kind,
                },
                "count_representation": {
                    "id": count_representation_id,
                    "method": "cellbender-remove-background",
                    "cellbender_version": "0.3.2",
                },
                "representation": None,
                "clustering": None,
            },
            "cell_qc": None,
            "cluster_qc": None,
            "batch": None,
            "annotation": None,
            "finalization": None,
        },
        "decisions_patch": {
            "preprocessing": {
                "ambient_background": "cellbender",
                "selected_output": parameters["selected_output"],
                "parameters": parameters,
            },
            "batch_handling": None,
            "integration": None,
            "final_labels": None,
        },
        "artifacts": artifacts,
        "model_media": [
            _artifact(
                "cellbender-count-comparison",
                "cellbender-count-comparison.png",
                "image/png",
            )
        ],
    }
