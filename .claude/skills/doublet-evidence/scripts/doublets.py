"""Generate, review, and explicitly filter identity-bound Scrublet evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SAMPLE_BYTES = 1024 * 1024


def _read_matrix(path):
    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)

def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _dataset_fingerprint(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(f"scagent-dataset-v1\0{size}\0".encode())
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(SAMPLE_BYTES), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _scientific_modules() -> tuple[Any, Any, Any, Any, Any, Any]:
    import anndata as ad
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import rapids_singlecell as rsc
    import scanpy as sc

    return ad, plt, np, pd, rsc, sc


def _recorded_path(context: Any, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = context.session_dir / path
    return path.resolve()


def _artifact_contract(
    path: Path,
    provenance: dict[str, Any],
    adata: Any,
    observed_count_id: str,
    count_source: str,
) -> dict[str, Any]:
    """Resolve portable identities from the input artifact, not session-global state."""

    cell_set_id = provenance.get("cell_set_id")
    if not isinstance(cell_set_id, str):
        cell_set_id = _identity("cells", sorted(map(str, adata.obs_names)))
    count_representation_id = provenance.get("count_representation_id")
    if not isinstance(count_representation_id, str):
        count_representation_id = _identity(
            "count-representation",
            {"count_matrix_id": observed_count_id, "source": count_source},
        )
    dataset_revision_id = provenance.get("dataset_revision_id")
    if not isinstance(dataset_revision_id, str):
        dataset_revision_id = _identity(
            "dataset-revision",
            {"source_fingerprint": _dataset_fingerprint(path), "cell_set_id": cell_set_id},
        )
    return {
        "dataset_revision_id": dataset_revision_id,
        "cell_set_id": cell_set_id,
        "count_representation_id": count_representation_id,
        "clustering_id": provenance.get("clustering_id"),
        "cluster_key": provenance.get("cluster_key"),
    }


def _parameters(arguments: dict[str, Any]) -> dict[str, Any]:
    expected_rate = float(arguments.get("expected_doublet_rate", 0.06))
    sim_ratio = float(arguments.get("sim_doublet_ratio", 2.0))
    requested_pcs = int(arguments.get("n_prin_comps", 30))
    min_cells = int(arguments.get("min_cells_per_library", 100))
    threshold_raw = arguments.get("threshold")
    threshold = float(threshold_raw) if threshold_raw is not None else None
    seed = int(arguments.get("random_seed", 0))
    if not 0 < expected_rate < 0.5:
        raise ValueError("expected_doublet_rate must satisfy 0 < rate < 0.5")
    if not 1 <= sim_ratio <= 10:
        raise ValueError("sim_doublet_ratio must be between 1 and 10")
    if not 2 <= requested_pcs <= 100:
        raise ValueError("n_prin_comps must be between 2 and 100")
    if min_cells < 20:
        raise ValueError("min_cells_per_library must be at least 20")
    if threshold is not None and threshold < 0:
        raise ValueError("threshold must be nonnegative")
    return {
        "expected_doublet_rate": expected_rate,
        "sim_doublet_ratio": sim_ratio,
        "requested_n_prin_comps": requested_pcs,
        "min_cells_per_library": min_cells,
        "threshold": threshold,
        "random_seed": seed,
    }


def _select_counts(adata: Any, np: Any) -> tuple[str, Any]:
    from scipy import sparse

    if "counts" in adata.layers:
        source = "layers/counts"
        matrix = adata.layers["counts"]
    else:
        source = "X"
        matrix = adata.X
    values = matrix.data if sparse.issparse(matrix) else np.asarray(matrix).ravel()
    if values.size and not bool(np.isfinite(values).all()):
        raise ValueError(f"{source} contains non-finite values")
    if values.size and float(values.min()) < 0:
        raise ValueError(f"{source} contains negative values")
    if values.size and not bool(np.allclose(values, np.rint(values), atol=1e-6, rtol=0)):
        raise ValueError(f"{source} is not integer-valued raw counts")
    if matrix.shape != (adata.n_obs, adata.n_vars):
        raise ValueError(f"{source} shape disagrees with AnnData")
    return source, matrix.copy()


def _matrix_identity(matrix: Any, obs_names: Any, var_names: Any, np: Any) -> str:
    from scipy import sparse

    digest = hashlib.sha256()
    digest.update(b"scagent-count-matrix-v1\0")
    digest.update(str(tuple(map(int, matrix.shape))).encode() + b"\0")
    if sparse.issparse(matrix):
        value = matrix.tocsr()
        for array in (value.data, value.indices, value.indptr):
            contiguous = np.ascontiguousarray(array)
            digest.update(str(contiguous.dtype).encode() + b"\0")
            digest.update(memoryview(contiguous).cast("B"))
    else:
        contiguous = np.ascontiguousarray(matrix)
        digest.update(str(contiguous.dtype).encode() + b"\0")
        digest.update(memoryview(contiguous).cast("B"))
    for names in (obs_names, var_names):
        for name in names:
            digest.update(str(name).encode("utf-8", errors="replace") + b"\0")
    return f"counts:sha256:{digest.hexdigest()}"


def _library_labels(adata: Any, batch_key: str | None, confirm_unstratified: bool) -> Any:
    if batch_key is None:
        if not confirm_unstratified:
            raise ValueError(
                "batch_key=null requires confirm_unstratified=true for a known single library"
            )
        return None
    if batch_key not in adata.obs:
        raise ValueError(f"library column {batch_key!r} is absent from obs")
    if bool(adata.obs[batch_key].isna().any()):
        raise ValueError(f"library column {batch_key!r} contains missing values")
    return adata.obs[batch_key].astype(str)


def _thresholds(scrublet: Any, libraries: list[str]) -> dict[str, float | None]:
    if not isinstance(scrublet, dict):
        return {library: None for library in libraries}
    batches = scrublet.get("batches")
    if isinstance(batches, dict):
        return {
            library: (
                float(batches[library]["threshold"])
                if library in batches
                and isinstance(batches[library], dict)
                and batches[library].get("threshold") is not None
                else None
            )
            for library in libraries
        }
    threshold = scrublet.get("threshold")
    value = float(threshold) if threshold is not None else None
    return {library: value for library in libraries}


def _simulated_scores(scrublet: Any) -> list[float]:
    if not isinstance(scrublet, dict):
        return []
    batches = scrublet.get("batches")
    values: list[float] = []
    if isinstance(batches, dict):
        for result in batches.values():
            if isinstance(result, dict) and result.get("doublet_scores_sim") is not None:
                values.extend(float(value) for value in result["doublet_scores_sim"])
        return values
    raw = scrublet.get("doublet_scores_sim")
    return [float(value) for value in raw] if raw is not None else []


def _write_report(
    path: Path,
    *,
    batch_key: str | None,
    count_source: str,
    parameters: dict[str, Any],
    rows: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    lines = [
        "# Scrublet doublet evidence",
        "",
        f"- Raw-count source: `{count_source}`",
        f"- Library key: `{batch_key}`" if batch_key else "- Library key: unstratified (confirmed)",
        f"- Libraries: {len(rows)}",
        f"- Random seed: {parameters['random_seed']}",
        f"- Expected rate prior: {parameters['expected_doublet_rate']:.3f}",
        "",
        "## Per-library calls",
        "",
    ]
    for row in rows:
        lines.append(
            f"- `{row['library']}`: {row['predicted_doublets']:,}/{row['n_cells']:,} "
            f"({row['predicted_rate']:.2%}), score p95={row['score_p95']:.4f}, "
            f"threshold={row['threshold']}"
        )
    lines.extend(["", "## Warnings", ""])
    lines.extend(
        f"- {warning}"
        for warning in warnings or ["No automated warning thresholds fired."]
    )
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "Scrublet predictions are review evidence, not ground truth. Heterotypic doublets are "
            "easier to identify than homotypic doublets, and coherent high-RNA singlets may score "
            "highly. Review cluster, marker, library, and QC context before filtering. Use the "
            "recorded `predicted_doublet` call; do not invent a score cutoff.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _execute_evidence(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    ad, plt, np, pd, rsc, sc = _scientific_modules()
    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".h5ad":
        raise ValueError("doublet evidence requires an H5AD file")
    adata = _read_matrix(path)
    raw_provenance = adata.uns.get("scagent_sdk")
    provenance = dict(raw_provenance) if isinstance(raw_provenance, dict) else {}
    if not bool(arguments.get("overwrite_existing_predictions", False)) and any(
        column in adata.obs for column in ("doublet_score", "predicted_doublet")
    ):
        raise ValueError(
            "doublet predictions already exist; refuse to overwrite without explicit confirmation"
        )
    parameters = _parameters(arguments)
    batch_key_raw = arguments.get("batch_key")
    batch_key = str(batch_key_raw) if batch_key_raw is not None else None
    labels = _library_labels(
        adata,
        batch_key,
        bool(arguments.get("confirm_unstratified", False)),
    )
    count_source, counts = _select_counts(adata, np)
    observed_count_id = _matrix_identity(counts, adata.obs_names, adata.var_names, np)
    contract = _artifact_contract(path, provenance, adata, observed_count_id, count_source)
    recorded_matrix_id = provenance.get("count_matrix_id")
    if recorded_matrix_id is not None and recorded_matrix_id != observed_count_id:
        raise ValueError("raw-count matrix identity disagrees with H5AD provenance")

    if labels is None:
        library_series = pd.Series(["__single_library__"] * adata.n_obs, index=adata.obs_names)
    else:
        library_series = labels
    sizes = library_series.value_counts(sort=False)
    undersized = sizes[sizes < parameters["min_cells_per_library"]]
    if not undersized.empty:
        detail = ", ".join(f"{name}={int(size)}" for name, size in undersized.items())
        raise ValueError(
            "libraries below min_cells_per_library must be resolved rather than scored as "
            f"singlets: {detail}"
        )
    safe_pcs = min(
        parameters["requested_n_prin_comps"],
        int(sizes.min()) - 1,
        int(adata.n_vars) - 1,
    )
    if safe_pcs < 2:
        raise ValueError("too few cells or genes for Scrublet PCA")
    parameters["n_prin_comps_used"] = safe_pcs
    parameters["batch_key"] = batch_key

    work = ad.AnnData(X=counts, obs=adata.obs.copy(), var=adata.var.copy())
    if batch_key is not None:
        work.obs[batch_key] = library_series
    for column in ("doublet_score", "predicted_doublet"):
        if column in work.obs:
            del work.obs[column]
    rsc.get.anndata_to_GPU(work)
    rsc.pp.scrublet(
        work,
        batch_key=batch_key,
        sim_doublet_ratio=parameters["sim_doublet_ratio"],
        expected_doublet_rate=parameters["expected_doublet_rate"],
        n_prin_comps=safe_pcs,
        threshold=parameters["threshold"],
        random_state=parameters["random_seed"],
        log_transform=False,
        verbose=True,
    )
    rsc.get.anndata_to_CPU(work, convert_all=True)
    if not {"doublet_score", "predicted_doublet"}.issubset(work.obs.columns):
        raise RuntimeError("rapids-singlecell returned without required Scrublet calls")
    scores = work.obs["doublet_score"].reindex(adata.obs_names).astype(float)
    predicted = work.obs["predicted_doublet"].reindex(adata.obs_names).astype(bool)
    if bool(scores.isna().any()) or not bool(np.isfinite(scores.to_numpy()).all()):
        raise RuntimeError("Scrublet produced missing or non-finite scores")
    adata.obs["doublet_score"] = scores
    adata.obs["predicted_doublet"] = predicted
    adata.uns["scrublet"] = work.uns.get("scrublet", {})
    adata.uns["scrublet_parameters"] = parameters

    libraries = [str(value) for value in sizes.index]
    thresholds = _thresholds(adata.uns["scrublet"], libraries)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for library in libraries:
        mask = library_series == library
        library_scores = scores.loc[mask]
        library_calls = predicted.loc[mask]
        n_cells = int(mask.sum())
        n_predicted = int(library_calls.sum())
        rate = n_predicted / n_cells
        row = {
            "library": library,
            "n_cells": n_cells,
            "predicted_doublets": n_predicted,
            "predicted_rate": rate,
            "score_median": float(library_scores.median()),
            "score_p95": float(library_scores.quantile(0.95)),
            "score_max": float(library_scores.max()),
            "threshold": thresholds.get(library),
        }
        rows.append(row)
        if n_predicted == 0:
            warnings.append(f"library {library!r} has zero predicted doublets")
        if rate > max(0.2, parameters["expected_doublet_rate"] * 2.5):
            warnings.append(
                f"library {library!r} predicted rate {rate:.1%} is unusually high; inspect "
                "threshold, score distribution, and biological composition"
            )
        if thresholds.get(library) is None and parameters["threshold"] is None:
            warnings.append(f"library {library!r} has no recorded automatic threshold")

    calls = pd.DataFrame(
        {
            "cell_id": list(map(str, adata.obs_names)),
            "library": library_series.to_numpy(),
            "doublet_score": scores.to_numpy(),
            "predicted_doublet": predicted.to_numpy(),
        }
    )
    calls.to_csv(context.staging_dir / "doublet-calls.csv", index=False)
    pd.DataFrame(rows).to_csv(context.staging_dir / "doublet-library-summary.csv", index=False)

    simulated = _simulated_scores(adata.uns["scrublet"])
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(scores.to_numpy(), bins=60, alpha=0.7, label="observed cells")
    if simulated:
        ax.hist(simulated, bins=60, alpha=0.45, label="simulated doublets")
    ax.set_xlabel("Scrublet doublet score")
    ax.set_ylabel("Count")
    ax.set_title("Observed and simulated Scrublet scores")
    ax.legend()
    fig.savefig(
        context.staging_dir / "doublet-score-distribution.png",
        dpi=160,
        bbox_inches="tight",
    )
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(max(7, len(rows) * 0.6), 5))
    ax.bar([row["library"] for row in rows], [row["predicted_rate"] for row in rows])
    ax.axhline(
        parameters["expected_doublet_rate"],
        color="tab:red",
        linestyle="--",
        label="expected-rate prior",
    )
    ax.set_ylabel("Predicted doublet fraction")
    ax.set_title("Scrublet calls by library")
    ax.tick_params(axis="x", rotation=45)
    ax.legend()
    fig.tight_layout()
    fig.savefig(context.staging_dir / "doublet-rate-by-library.png", dpi=160)
    plt.close(fig)

    figure_artifacts = [
        {
            "name": "doublet-score-distribution",
            "relative_path": "doublet-score-distribution.png",
            "media_type": "image/png",
        },
        {
            "name": "doublet-rate-by-library",
            "relative_path": "doublet-rate-by-library.png",
            "media_type": "image/png",
        },
    ]
    if "X_umap" in adata.obsm:
        sc.pl.umap(adata, color=["doublet_score", "predicted_doublet"], show=False)
        plt.savefig(context.staging_dir / "doublet-umap.png", dpi=160, bbox_inches="tight")
        plt.close("all")
        figure_artifacts.append(
            {
                "name": "doublet-umap",
                "relative_path": "doublet-umap.png",
                "media_type": "image/png",
            }
        )

    evidence_id = _identity(
        "doublet-evidence",
        {
            **contract,
            "count_matrix_id": observed_count_id,
            "parameters": parameters,
            "calls": list(
                zip(
                    map(str, adata.obs_names),
                    (round(float(value), 12) for value in scores),
                    map(bool, predicted),
                    strict=True,
                )
            ),
        },
    )
    output_relative = "doublet-annotated.h5ad"
    output_path = context.staging_dir / output_relative
    final_path = f"artifacts/capabilities/{context.execution_id}/{output_relative}"
    provenance = dict(provenance)
    provenance.update(
        {
            "count_matrix_id": observed_count_id,
            "doublet_evidence_id": evidence_id,
            "doublet_parameters": parameters,
        }
    )
    adata.uns["scagent_sdk"] = provenance
    adata.write_h5ad(output_path, compression="gzip")
    _write_report(
        context.staging_dir / "doublet-evidence.md",
        batch_key=batch_key,
        count_source=count_source,
        parameters=parameters,
        rows=rows,
        warnings=warnings,
    )
    details = {
        "status": "complete",
        "evidence_id": evidence_id,
        "cell_set_id": contract["cell_set_id"],
        "count_representation_id": contract["count_representation_id"],
        "count_matrix_id": observed_count_id,
        "count_source": count_source,
        "batch_key": batch_key,
        "n_libraries": len(rows),
        "n_cells": int(adata.n_obs),
        "predicted_doublets": int(predicted.sum()),
        "predicted_rate": float(predicted.mean()),
        "parameters": parameters,
        "execution_device": "gpu",
        "library_summary": rows,
        "warnings": warnings,
        "annotated_path": final_path,
    }
    (context.staging_dir / "doublet-evidence.json").write_text(
        json.dumps(details, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        "details": details,
        "evidence": details,
        "dataset_revision_patch": {
            "id": contract["dataset_revision_id"],
            "observation_revision_id": _identity(
                "observation-revision",
                {"parent": contract["dataset_revision_id"], "doublet_evidence_id": evidence_id},
            ),
        },
        "artifacts": [
            {
                "name": "doublet-annotated-anndata",
                "relative_path": output_relative,
                "media_type": "application/x-hdf5",
            },
            {
                "name": "doublet-calls",
                "relative_path": "doublet-calls.csv",
                "media_type": "text/csv",
            },
            {
                "name": "doublet-library-summary",
                "relative_path": "doublet-library-summary.csv",
                "media_type": "text/csv",
            },
            {
                "name": "doublet-evidence-json",
                "relative_path": "doublet-evidence.json",
                "media_type": "application/json",
            },
            {
                "name": "doublet-evidence-report",
                "relative_path": "doublet-evidence.md",
                "media_type": "text/markdown",
            },
            *figure_artifacts,
        ],
        "model_media": figure_artifacts,
    }


def run_evaluate(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    result = _execute_evidence(arguments, context)
    details = result["details"]
    return {
        "summary": (
            f"Generated per-library Scrublet evidence for {details['n_cells']:,} cells; "
            f"{details['predicted_doublets']:,} ({details['predicted_rate']:.2%}) were predicted "
            "doublets for review."
        ),
        "details": details,
        "facts_patch": {
            "analysis": {
                "dataset_revision": result["dataset_revision_patch"],
                "cell_set": {
                    "id": details["cell_set_id"],
                    "n_cells": details["n_cells"],
                },
                "count_representation": {
                    "id": details["count_representation_id"],
                    "count_matrix_id": details["count_matrix_id"],
                    "source": details["count_source"],
                },
            },
            "doublets": {"evidence": result["evidence"], "review": None},
            "cluster_qc": None,
            "finalization": None,
        },
        "decisions_patch": {"doublet_handling": None, "final_labels": None},
        "artifacts": result["artifacts"],
        "model_media": result["model_media"],
    }


def _current_evidence(context: Any, path: Path, provenance: Any) -> dict[str, Any]:
    doublets = context.state_facts.get("doublets")
    evidence = doublets.get("evidence") if isinstance(doublets, dict) else None
    if not isinstance(evidence, dict) or evidence.get("status") != "complete":
        raise ValueError("current doublet evidence is absent")
    analysis = context.state_facts.get("analysis")
    cell_set = analysis.get("cell_set") if isinstance(analysis, dict) else None
    if not isinstance(cell_set, dict) or evidence.get("cell_set_id") != cell_set.get("id"):
        raise ValueError("doublet evidence is stale for the current cell set")
    annotated = evidence.get("annotated_path")
    if not isinstance(annotated, str) or _recorded_path(context, annotated) != path:
        raise ValueError("review path does not match the current doublet evidence artifact")
    if not isinstance(provenance, dict) or provenance.get("doublet_evidence_id") != evidence.get(
        "evidence_id"
    ):
        raise ValueError("H5AD doublet evidence identity is absent or stale")
    return evidence


def _review_parameters(arguments: dict[str, Any]) -> tuple[str, str, bool, float]:
    decision = str(arguments["decision"])
    allowed = {"retain_for_cluster_review", "keep_all", "remove_predicted", "request_guidance"}
    if decision not in allowed:
        raise ValueError(f"unsupported doublet review decision: {decision}")
    rationale = str(arguments["rationale"]).strip()
    if not rationale:
        raise ValueError("doublet review rationale must not be empty")
    confirmed = bool(arguments.get("confirm_filtering", False))
    maximum = float(arguments.get("max_removal_fraction", 0.2))
    if not 0 < maximum <= 0.5:
        raise ValueError("max_removal_fraction must satisfy 0 < value <= 0.5")
    if decision == "remove_predicted" and not confirmed:
        raise ValueError("remove_predicted requires confirm_filtering=true")
    return decision, rationale, confirmed, maximum


def _write_review_report(
    path: Path,
    *,
    decision: str,
    rationale: str,
    n_cells: int,
    n_predicted: int,
    output_path: str | None,
) -> None:
    path.write_text(
        "# Doublet evidence review\n\n"
        f"- Decision: **{decision}**\n"
        f"- Current cells: {n_cells:,}\n"
        f"- Scrublet predicted calls: {n_predicted:,}\n"
        f"- Filtered raw-count artifact: `{output_path}`\n\n"
        "## Rationale\n\n"
        f"{rationale}\n\n"
        "Filtering, when selected, used only `predicted_doublet == true`; no custom score cutoff "
        "was applied. A filtered cell set requires fresh preparation and downstream evidence.\n",
        encoding="utf-8",
    )


def _execute_review(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    _ad, _plt, np, _pd, _rsc, sc = _scientific_modules()
    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    decision, rationale, _confirmed, maximum = _review_parameters(arguments)
    adata = _read_matrix(path)
    provenance = adata.uns.get("scagent_sdk")
    evidence = _current_evidence(context, path, provenance)
    if "predicted_doublet" not in adata.obs:
        raise ValueError("annotated H5AD lacks predicted_doublet")
    raw_calls = adata.obs["predicted_doublet"]
    if bool(raw_calls.isna().any()):
        raise ValueError("predicted_doublet contains missing values")
    predicted = raw_calls.astype(bool)
    n_cells = int(adata.n_obs)
    n_predicted = int(predicted.sum())
    fraction = n_predicted / n_cells
    final_path: str | None = None
    filter_payload: dict[str, Any] | None = None
    artifacts: list[dict[str, str]] = []
    if decision == "remove_predicted":
        if n_predicted == 0:
            raise ValueError("no predicted doublets are available to remove")
        if fraction > maximum:
            raise ValueError(
                f"predicted removal fraction {fraction:.1%} exceeds the confirmed maximum "
                f"{maximum:.1%}; review the evidence before changing the cell set"
            )
        count_source, counts = _select_counts(adata, np)
        adata.X = counts
        filtered = adata[~predicted.to_numpy()].copy()
        filtered.raw = None
        filtered.layers.clear()
        filtered.layers["counts"] = filtered.X.copy()
        filtered.obsm.clear()
        filtered.obsp.clear()
        filtered.varm.clear()
        analysis = context.state_facts.get("analysis")
        clustering = analysis.get("clustering") if isinstance(analysis, dict) else None
        cluster_key = clustering.get("key") if isinstance(clustering, dict) else None
        if isinstance(cluster_key, str) and cluster_key in filtered.obs:
            del filtered.obs[cluster_key]
        new_cell_set_id = _identity("cells", sorted(map(str, filtered.obs_names)))
        new_count_id = _identity(
            "counts",
            {
                "parent": evidence["count_representation_id"],
                "cell_set_id": new_cell_set_id,
                "filter": "scrublet-predicted-doublet",
            },
        )
        new_revision_id = _identity(
            "dataset-revision",
            {
                "parent": evidence.get("evidence_id"),
                "cell_set_id": new_cell_set_id,
                "removed_cells": n_predicted,
            },
        )
        output_relative = "doublet-filtered-raw-counts.h5ad"
        output_path = context.staging_dir / output_relative
        final_path = f"artifacts/capabilities/{context.execution_id}/{output_relative}"
        filtered.uns = {
            "scagent_sdk": {
                "schema_version": 1,
                "source_path": str(path),
                "dataset_revision_id": new_revision_id,
                "cell_set_id": new_cell_set_id,
                "count_representation_id": new_count_id,
                "count_matrix_id": _matrix_identity(
                    filtered.X, filtered.obs_names, filtered.var_names, np
                ),
            },
            "doublet_filtering": {
                "parent_evidence_id": evidence["evidence_id"],
                "decision": decision,
                "rationale": rationale,
                "removed_predicted_doublets": n_predicted,
                "source_count_location": count_source,
            },
        }
        filtered.write_h5ad(output_path, compression="gzip")
        stat = output_path.stat()
        output_fingerprint = _dataset_fingerprint(output_path)
        filter_payload = {
            "dataset": {
                "path": str(
                    context.session_dir
                    / "artifacts"
                    / "capabilities"
                    / context.execution_id
                    / output_relative
                ),
                "size_bytes": stat.st_size,
                "modified_time_ns": stat.st_mtime_ns,
                "fingerprint": output_fingerprint,
                "fingerprint_mode": "full",
                "format": {
                    "extension": "h5ad",
                    "suffixes": [".h5ad"],
                    "byte_signature": "hdf5",
                    "extension_signature_consistent": True,
                },
                "lineage": {
                    "parent_path": str(path),
                    "parent_evidence_id": evidence["evidence_id"],
                    "transformation": "remove-scrublet-predicted-doublets",
                },
            },
            "analysis": {
                "dataset_revision": {
                    "id": new_revision_id,
                    "source_path": str(path),
                    "n_cells": int(filtered.n_obs),
                    "n_genes": int(filtered.n_vars),
                },
                "cell_set": {"id": new_cell_set_id, "n_cells": int(filtered.n_obs)},
                "count_representation": {
                    "id": new_count_id,
                    "method": "scrublet-predicted-doublet-filter",
                    "parent_id": evidence["count_representation_id"],
                },
                "representation": None,
                "clustering": None,
            },
            "cell_qc": None,
            "cluster_qc": None,
            "batch": None,
            "annotation": None,
            "finalization": None,
        }
        artifacts.append(
            {
                "name": "doublet-filtered-raw-counts",
                "relative_path": output_relative,
                "media_type": "application/x-hdf5",
            }
        )
    review = {
        "status": "complete",
        "evidence_id": evidence["evidence_id"],
        "cell_set_id": evidence["cell_set_id"],
        "decision": decision,
        "rationale": rationale,
        "n_cells": n_cells,
        "predicted_doublets": n_predicted,
        "predicted_fraction": fraction,
        "filtered_output_path": final_path,
    }
    _write_review_report(
        context.staging_dir / "doublet-review.md",
        decision=decision,
        rationale=rationale,
        n_cells=n_cells,
        n_predicted=n_predicted,
        output_path=final_path,
    )
    (context.staging_dir / "doublet-review.json").write_text(
        json.dumps(review, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    artifacts.extend(
        [
            {
                "name": "doublet-review-report",
                "relative_path": "doublet-review.md",
                "media_type": "text/markdown",
            },
            {
                "name": "doublet-review-json",
                "relative_path": "doublet-review.json",
                "media_type": "application/json",
            },
        ]
    )
    return {
        "review": review,
        "filter_payload": filter_payload,
        "artifacts": artifacts,
    }


def run_review(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    result = _execute_review(arguments, context)
    review = result["review"]
    filtered = review["decision"] == "remove_predicted"
    if filtered:
        facts_patch = {
            **result["filter_payload"],
            "doublets": {"evidence": None, "review": review},
        }
        decisions_patch = {
            "doublet_handling": {
                "decision": review["decision"],
                "rationale": review["rationale"],
            },
            "batch_handling": None,
            "integration": None,
            "final_labels": None,
        }
        summary = (
            f"Removed exactly {review['predicted_doublets']:,} Scrublet predicted doublets; "
            "the new raw-count cell set requires fresh preprocessing and downstream evidence."
        )
    else:
        facts_patch = {"doublets": {"review": review}}
        decisions_patch = {
            "doublet_handling": {
                "decision": review["decision"],
                "rationale": review["rationale"],
            }
        }
        summary = (
            f"Recorded doublet review decision {review['decision']!r}; no cells were removed."
        )
    return {
        "summary": summary,
        "details": review,
        "facts_patch": facts_patch,
        "decisions_patch": decisions_patch,
        "artifacts": result["artifacts"],
    }
