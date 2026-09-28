"""Foundation model embedding and input checking, through biorun's typed client.

Both tools run in the `biorun` runtime, which owns torch, the model weights and
the transform layer. Neither one reimplements any preprocessing here: the point
of the package behind this skill is that a model's own published pipeline is
reproduced once, tested against the authors' code, and versioned, so a skill
never has to guess what "normalized" means for a given checkpoint.

Two contracts this file keeps deliberately:

* **The baseline is not optional by accident.** Every embedding call computes
  PCA-50 on the same cells and scores both with the same metrics; turning it off
  takes an explicit `baseline="none"`, and the result says so in the summary.
* **A refusal is an answer.** When a model's validator rejects the dataset, the
  tool returns the named check and the fix rather than an exception trace, so the
  agent can report it or switch models instead of retrying blindly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EMBEDDING_KEY_TEMPLATE = "X_{family}"
#: biorun also serves protein language models, which embed sequences rather than
#: cells. They are registered for the "embed" task too, so they have to be named
#: rather than inferred out.
PROTEIN_FAMILIES = frozenset({"esm2", "esmc"})


def _read_matrix(path: Path):
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)


def _write_matrix(adata, path: Path) -> None:
    import anndata as ad

    if str(path).endswith(".zarr"):
        ad.settings.zarr_write_format = 2
        adata.write_zarr(path)
    else:
        adata.write_h5ad(path, compression="gzip")


def _refusal(exc: Any, model: str) -> dict[str, Any]:
    """An InputValidationError, rendered as a result rather than a crash."""
    return {
        "model": model,
        "accepted": False,
        "check": getattr(exc, "check", "unknown"),
        "detail": getattr(exc, "detail", str(exc)),
        "fix": getattr(exc, "fix", None),
    }


def _subsample(adata, max_cells: int | None, label_key: str | None, seed: int):
    """Seeded, stratified when a label column exists, so small types survive."""
    import numpy as np

    if max_cells is None or adata.n_obs <= max_cells:
        return adata, None
    rng = np.random.RandomState(seed)
    if label_key and label_key in adata.obs:
        labels = adata.obs[label_key].astype(str).to_numpy()
        keep: list[int] = []
        for value in np.unique(labels):
            rows = np.flatnonzero(labels == value)
            share = max(1, int(round(max_cells * rows.size / labels.size)))
            keep.extend(rng.choice(rows, size=min(share, rows.size), replace=False).tolist())
        chosen = np.sort(np.asarray(keep))[:max_cells]
        strategy = f"stratified on {label_key!r}"
    else:
        chosen = np.sort(rng.choice(adata.n_obs, size=max_cells, replace=False))
        strategy = "uniform"
    return adata[chosen].copy(), {
        "requested": int(max_cells),
        "kept": int(len(chosen)),
        "of": int(adata.n_obs),
        "strategy": strategy,
        "seed": int(seed),
    }


# --------------------------------------------------------------------- check


def check(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    from biorun.errors import (
        InputValidationError,
        OptionalDependencyError,
        WeightsUnavailableError,
    )
    from biorun.models import load_transform
    from biorun.registry import get as get_card
    from biorun.registry import list_cards

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    requested = str(arguments.get("model", "all"))
    counts_layer = arguments.get("counts_layer")
    layer = None if counts_layer in (None, "", "X") else str(counts_layer)

    if requested == "all":
        cards = [
            card
            for card in list_cards(status="implemented")
            if "embed" in card.tasks
            and card.family != "baseline"
            and card.family not in PROTEIN_FAMILIES
        ]
    else:
        cards = [get_card(requested)]

    adata = _read_matrix(path)
    verdicts = []
    for card in cards:
        try:
            transform = load_transform(card, seed=0)
            summary = transform.validate(adata, layer=layer)
            verdicts.append(
                {
                    "model": card.id,
                    "accepted": True,
                    "input_expectation": card.input_expectation,
                    "summary": {k: v for k, v in summary.items() if k != "matrix"},
                }
            )
        except InputValidationError as exc:
            verdicts.append({**_refusal(exc, card.id), "input_expectation": card.input_expectation})
        except (WeightsUnavailableError, OptionalDependencyError) as exc:
            verdicts.append(
                {
                    "model": card.id,
                    "accepted": None,
                    "check": "assets_unavailable",
                    "detail": str(exc).splitlines()[0],
                }
            )

    accepted = [v["model"] for v in verdicts if v["accepted"] is True]
    refused = [v for v in verdicts if v["accepted"] is False]
    unavailable = [v["model"] for v in verdicts if v["accepted"] is None]

    report = {
        "input_path": str(path),
        "counts_layer": counts_layer,
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "verdicts": verdicts,
    }
    (context.staging_dir / "fm-input-check.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )

    parts = [f"{len(accepted)} of {len(verdicts)} foundation models accept this dataset"]
    if accepted:
        parts.append(f"accepted: {', '.join(accepted)}")
    for verdict in refused:
        parts.append(f"{verdict['model']} refused [{verdict['check']}]")
    if unavailable:
        parts.append(f"not installed or not downloaded here: {', '.join(unavailable)}")
    return {
        "summary": "; ".join(parts) + ".",
        "details": report,
        "facts_patch": {
            "reference_runs": {
                "foundation_model_check": {
                    context.execution_id: {
                        "status": "complete",
                        "input_path": str(path),
                        "accepted_models": accepted,
                        "refusals": {v["model"]: v["check"] for v in refused},
                        "unavailable_models": unavailable,
                    }
                }
            }
        },
        "artifacts": [
            {
                "name": "fm-input-check",
                "relative_path": "fm-input-check.json",
                "media_type": "application/json",
            }
        ],
    }


# --------------------------------------------------------------------- embed


def embed(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import numpy as np
    from biorun import Client
    from biorun.errors import InputValidationError
    from biorun.registry import get as get_card

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    model_id = str(arguments["model"])
    card = get_card(model_id)
    counts_layer = arguments.get("counts_layer")
    layer_key = None if counts_layer in (None, "", "X") else str(counts_layer)
    label_key = arguments.get("label_key") or None
    batch_key = arguments.get("batch_key") or None
    baseline = str(arguments.get("baseline", "pca-50"))
    seed = int(arguments.get("seed", 0))
    pooling = arguments.get("pooling") or _default_pooling(card)
    layer = arguments.get("layer", "last")
    max_cells = arguments.get("max_cells")

    adata = _read_matrix(path)
    adata, subsampling = _subsample(
        adata, int(max_cells) if max_cells else None, label_key, seed
    )

    client = Client(backend="local")
    job = client.embed(
        model=model_id,
        cells=adata,
        layer=layer,
        pooling=pooling,
        baseline=None if baseline == "none" else "pca-50",
        label_key=label_key,
        batch_key=batch_key,
        batch_size=int(arguments.get("batch_size", 8)),
        seed=seed,
    )
    try:
        result = job.result()
    except InputValidationError as exc:
        refusal = _refusal(exc, model_id)
        (context.staging_dir / "fm-embedding-refusal.json").write_text(
            json.dumps(refusal, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return {
            "summary": (
                f"{model_id} refused this dataset [{refusal['check']}]: {refusal['detail']} "
                f"{refusal['fix'] or ''}".strip()
            ),
            "details": refusal,
            "artifacts": [
                {
                    "name": "fm-embedding-refusal",
                    "relative_path": "fm-embedding-refusal.json",
                    "media_type": "application/json",
                }
            ],
        }

    embedding_key = EMBEDDING_KEY_TEMPLATE.format(family=card.family.replace("-", "_"))
    adata.obsm[embedding_key] = np.asarray(result.embeddings, dtype=np.float32)
    if result.baseline is not None and result.baseline.embeddings is not None:
        adata.obsm["X_pca50_baseline"] = np.asarray(
            result.baseline.embeddings, dtype=np.float32
        )
    output_name = f"{path.stem}-{card.family}-embedded.h5ad"
    _write_matrix(adata, context.staging_dir / output_name)

    comparison = result.comparison()
    provenance = json.loads(result.provenance.model_dump_json())
    report = {
        "model": model_id,
        "input_path": str(path),
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "embedding_key": embedding_key,
        "embedding_dim": int(np.asarray(result.embeddings).shape[1]),
        "layer": layer,
        "pooling": pooling,
        "pooling_source": "model's published convention"
        if not arguments.get("pooling")
        else "caller override",
        "label_key": result.extra.get("label_key"),
        "batch_key": result.extra.get("batch_key"),
        "subsampling": subsampling,
        "metrics_model": result.metrics,
        # biorun's Result holds a named collection of baselines (one for embed,
        # two for perturbation prediction). Keep the flat key for the primary
        # one, which is what the facts patch and the summary quote.
        "metrics_baseline": (
            result.baseline.metrics if result.baseline is not None else None
        ),
        "metrics_baselines": {
            name: entry.metrics for name, entry in result.baselines.items()
        },
        "comparison": comparison,
        "provenance": provenance,
        "eval_status_on_card": card.eval_status,
        "weights_license": card.weights_license,
    }
    (context.staging_dir / "fm-embedding-run.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )

    summary = _summarize(model_id, adata.n_obs, report, comparison, baseline)
    final_path = f"{context.artifact_relative_path}/{output_name}"
    return {
        "summary": summary,
        "details": report,
        "facts_patch": {
            "reference_runs": {
                "foundation_model_embedding": {
                    context.execution_id: {
                        "status": "complete",
                        "model": model_id,
                        "embedding_key": embedding_key,
                        "embedding_dim": report["embedding_dim"],
                        "label_key": report["label_key"],
                        "baseline": None if baseline == "none" else "pca-50",
                        "metrics_model": result.metrics,
                        "metrics_baseline": report["metrics_baseline"],
                        "transform_input_hash": provenance.get("transform_input_hash"),
                        "weights_sha256": provenance.get("weights_sha256"),
                        "artifact_path": final_path,
                    }
                }
            }
        },
        "artifacts": [
            {
                "name": "fm-embedded-anndata",
                "relative_path": output_name,
                "media_type": "application/x-hdf5",
            },
            {
                "name": "fm-embedding-run",
                "relative_path": "fm-embedding-run.json",
                "media_type": "application/json",
            },
        ],
    }


def _default_pooling(card: Any) -> str:
    """Each model's own published convention, so a caller need not know it."""
    return {
        "scgpt": "cls",
        "uce": "cls",
        "state-se": "cls",
    }.get(card.family, "mean")


def _summarize(
    model_id: str, n_cells: int, report: dict[str, Any], comparison: dict[str, Any], baseline: str
) -> str:
    if baseline == "none":
        return (
            f"{model_id} embedded {n_cells:,} cells into "
            f"{report['embedding_dim']} dimensions with NO baseline, at the caller's explicit "
            "request. There is nothing here to say whether the model beat PCA."
        )
    parts = [
        f"{model_id} embedded {n_cells:,} cells into {report['embedding_dim']} dimensions "
        f"(pooling={report['pooling']})."
    ]
    label = report.get("label_key")
    if label:
        for name in ("knn_accuracy", "knn_macro_f1", "silhouette"):
            row = comparison.get(name)
            if not row or row.get("model") is None:
                continue
            # comparison() rows are {"model": v, "baselines": {name: v}, "deltas": {...}}
            value = (row.get("baselines") or {}).get("pca-50")
            if value is None:
                continue
            delta = row["model"] - value
            direction = "above" if delta >= 0 else "below"
            parts.append(
                f"{name}: model {row['model']:.3f} vs PCA-50 baseline {value:.3f} "
                f"({abs(delta):.3f} {direction} baseline)."
            )
        parts.append(f"Both scored on obs[{label!r}] with identical metric code.")
    else:
        parts.append(
            "No label column was found, so only unsupervised metrics were computed and there "
            "is no accuracy comparison to report."
        )
    return " ".join(parts)
