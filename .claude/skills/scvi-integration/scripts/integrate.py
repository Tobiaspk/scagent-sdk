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


def _plot_convergence(history, out_path, *, epochs_trained: int, early_stopped: bool):
    """Save an scVI training-convergence figure and return its staging-relative name.

    Prefers the ELBO curves (train + validation); falls back to the first logged metric so a
    plot always exists. Returns None only if nothing plottable is present. matplotlib runs
    headless (the scientific runtime sets MPLBACKEND=Agg; we also force it defensively).
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def _series(name):
        frame = history.get(name)
        if frame is None or getattr(frame, "empty", True):
            return None
        return frame[frame.columns[0]]

    curves = []
    for label, name, style in (
        ("ELBO (train)", "elbo_train", {"linewidth": 1.4}),
        ("ELBO (validation)", "elbo_validation", {"linewidth": 1.4, "linestyle": "--"}),
    ):
        series = _series(name)
        if series is not None:
            curves.append((label, series, style))
    if not curves:
        first = next(iter(history), None)
        series = _series(first) if first is not None else None
        if series is None:
            return None
        curves.append((str(first), series, {"linewidth": 1.4}))

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for label, series, style in curves:
        ax.plot(series.index.to_numpy(), series.to_numpy(), label=label, **style)
    title = f"scVI training convergence — {epochs_trained} epochs"
    if early_stopped:
        title += " (early stopped)"
    ax.set(xlabel="Epoch", ylabel="ELBO / loss", title=title)
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out_path.name


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scvi

    path = Path(str(arguments["path"])).expanduser().resolve()
    batch_key = str(arguments["batch_key"])
    n_latent = int(arguments.get("n_latent", 30))
    n_layers = int(arguments.get("n_layers", 2))
    seed = int(arguments.get("random_seed", 0))
    adata = _read_matrix(path)
    if batch_key not in adata.obs:
        raise ValueError(f"batch key {batch_key!r} is absent")
    if "counts" not in adata.layers:
        raise ValueError("scVI requires raw counts in layers['counts']")
    # Epoch cap: honor an explicit request, otherwise defer to scVI's own cell-count heuristic
    # (fewer epochs for larger datasets) rather than a flat constant — this restores the legacy
    # behavior that scaled epochs with n_obs. Early stopping applies either way.
    max_epochs_arg = arguments.get("max_epochs")
    if max_epochs_arg is None:
        from scvi.model._utils import get_max_epochs_heuristic

        max_epochs = int(get_max_epochs_heuristic(adata.n_obs))
        epochs_source = "cell-count heuristic"
    else:
        max_epochs = int(max_epochs_arg)
        epochs_source = "explicit"
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
        # Emit the Lightning/tqdm epoch bar so the broker can stream it to the terminal as live
        # progress ("Epoch X/N") during a multi-minute train, the way the legacy run showed it.
        enable_progress_bar=True,
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
    epochs_trained: int | None = None
    early_stopped = False
    convergence_figure: str | None = None
    if history:
        import pandas as pd

        # Save the FULL history — every logged metric aligned on the epoch index — not just the
        # first metric (which is kl_weight, the KL warm-up schedule, not the training loss).
        full_history = pd.concat(list(history.values()), axis=1)
        if full_history.index.name is None:
            full_history.index.name = "epoch"
        full_history.to_csv(context.staging_dir / "scvi-training-history.csv")
        epochs_trained = int(full_history.shape[0])
        early_stopped = epochs_trained < max_epochs
        convergence_figure = _plot_convergence(
            history,
            context.staging_dir / "scvi-training-history.png",
            epochs_trained=epochs_trained,
            early_stopped=early_stopped,
        )
    else:
        (context.staging_dir / "scvi-training-history.csv").write_text("\n", encoding="utf-8")
    return {
        "summary": (
            f"Trained scVI for {adata.n_obs:,} cells "
            + (
                f"({epochs_trained} epochs, early-stopped before the {max_epochs}-epoch "
                f"{epochs_source} cap)"
                if early_stopped
                else f"({epochs_trained if epochs_trained is not None else max_epochs} epochs, "
                f"{epochs_source} cap {max_epochs})"
            )
            + f" and saved a {n_latent}-dimensional X_scVI representation; a training-convergence "
            "plot is attached. No graph, UMAP, or clusters were created."
        ),
        "details": {
            "batch_key": batch_key,
            "n_latent": n_latent,
            "n_layers": n_layers,
            "max_epochs": max_epochs,
            "max_epochs_source": epochs_source,
            "epochs_trained": epochs_trained,
            "early_stopped": early_stopped,
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
            # The batch fact is preserved: the integrate/keep decision was made once on the
            # uncorrected pass and authorized this training; integration is its consequence, not a
            # trigger to re-decide. Post-integration mixing is verified by score_integration.
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
            },
            *(
                [
                    {
                        "name": "scvi-training-convergence",
                        "relative_path": convergence_figure,
                        "media_type": "image/png",
                    }
                ]
                if convergence_figure
                else []
            ),
        ],
        # Attach the convergence plot so the model actually sees training quality (and must
        # interpret it) instead of the user having to hand-roll a loss curve afterward.
        "model_media": (
            [
                {
                    "name": "scvi-training-convergence",
                    "relative_path": convergence_figure,
                    "media_type": "image/png",
                }
            ]
            if convergence_figure
            else []
        ),
    }
