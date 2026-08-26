"""Lightweight post-integration mixing score (neighborhood batch entropy).

This is the SDK counterpart of the legacy ``score_integration`` tool. After an
integration model has been trained, it answers a single, cheap question — did the
correction actually mix the batches? — without re-running the expensive gene-first
``investigate_batch`` diagnostic, which was only ever meant to run once on the
uncorrected pass. The gene diagnostic decides *whether* to integrate (on the
uncorrected representation); this scores *how well* an integration mixed samples.

For each cell we take its k nearest neighbours in a low-dimensional embedding and
compute the Shannon entropy of the batch labels among them, normalized by
``log2(n_batches)`` so it lands in [0, 1] (1 = perfectly mixed). Scoring the
corrected latent (``X_scVI``) against the uncorrected baseline (``X_pca``) on
comparable latent spaces makes the improvement attributable to the integration —
never compare against ``X_umap``, a distorting 2-D projection.

Pure entropy math lives at module scope so it is unit-testable without AnnData.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

CORRECTED_DEFAULT_REP = "X_scVI"
BASELINE_REP = "X_pca"
DEFAULT_N_NEIGHBORS = 50


def entropy_from_neighbor_labels(
    neighbor_codes: np.ndarray, n_batches: int
) -> np.ndarray:
    """Per-cell normalized Shannon entropy of batch labels over a neighbour-code matrix.

    ``neighbor_codes`` is (n_cells, k) of integer batch labels in ``[0, n_batches)`` for each
    cell's neighbours. Returns a per-cell array in [0, 1] where 1 means the neighbourhood is as
    mixed as ``log2(n_batches)`` allows. Pure numpy — the neighbour search is factored out so this
    math is unit-testable outside the compute runtime (no sklearn/AnnData)."""
    max_entropy = np.log2(n_batches)
    n_cells = neighbor_codes.shape[0]
    per_cell = np.empty(n_cells, dtype=float)
    for i in range(n_cells):
        counts = np.bincount(neighbor_codes[i], minlength=n_batches).astype(float)
        probs = counts / counts.sum()
        probs = probs[probs > 0]
        per_cell[i] = float(-np.sum(probs * np.log2(probs)) / max_entropy)
    return per_cell


def normalized_neighborhood_entropy(
    embedding: np.ndarray, batch_codes: np.ndarray, n_batches: int, n_neighbors: int
) -> np.ndarray:
    """Per-cell normalized batch entropy among k nearest neighbours in ``embedding``.

    ``embedding`` is (n_cells, n_dims); ``batch_codes`` is an integer label per cell. The kNN
    search runs through sklearn (compute runtime only); the entropy is delegated to the pure
    ``entropy_from_neighbor_labels``."""
    from sklearn.neighbors import NearestNeighbors

    n_cells = embedding.shape[0]
    k = min(n_neighbors, n_cells - 1)
    nbrs = NearestNeighbors(n_neighbors=k, algorithm="auto", n_jobs=-1).fit(embedding)
    _, indices = nbrs.kneighbors(embedding)
    return entropy_from_neighbor_labels(batch_codes[indices], n_batches)


def interpret_mixing(entropy_mean: float) -> str:
    """Plain-language reading of a mean neighborhood-mixing entropy (legacy thresholds)."""
    if entropy_mean >= 0.8:
        return "Excellent mixing — batches are well integrated."
    if entropy_mean >= 0.6:
        return "Good mixing — minor batch structure may remain."
    if entropy_mean >= 0.4:
        return "Moderate mixing — a stronger correction may help."
    return "Poor mixing — strong batch structure persists; check the batch key or method."


def _read_matrix(path):
    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)


def _score_representation(
    adata, use_rep: str, batch_codes: np.ndarray, n_batches: int, unique_batches, n_neighbors: int
) -> dict[str, Any] | None:
    """Score one embedding, or return None when it is absent from ``obsm``."""
    if use_rep not in adata.obsm:
        return None
    embedding = np.asarray(adata.obsm[use_rep], dtype=float)
    per_cell = normalized_neighborhood_entropy(embedding, batch_codes, n_batches, n_neighbors)
    per_batch = {
        str(b): round(float(np.mean(per_cell[batch_codes == i])), 4)
        for i, b in enumerate(unique_batches)
    }
    return {
        "use_rep": use_rep,
        "entropy_mean": round(float(np.mean(per_cell)), 4),
        "entropy_median": round(float(np.median(per_cell)), 4),
        "entropy_per_batch": per_batch,
    }


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    path = Path(str(arguments["path"])).expanduser().resolve()
    n_neighbors = int(arguments.get("n_neighbors", DEFAULT_N_NEIGHBORS))
    corrected_rep = str(arguments.get("use_rep", CORRECTED_DEFAULT_REP))
    adata = _read_matrix(path)

    # Resolve the batch key: the explicit argument wins, else the one scVI conditioned on.
    batch_key = arguments.get("batch_key")
    if not batch_key:
        meta = adata.uns.get("scagent_sdk", {}) if hasattr(adata, "uns") else {}
        scvi_meta = meta.get("scvi", {}) if isinstance(meta, dict) else {}
        batch_key = scvi_meta.get("batch_key")
    if not batch_key or batch_key not in adata.obs:
        raise ValueError(
            f"batch key {batch_key!r} is absent; pass batch_key explicitly for scoring"
        )
    if corrected_rep not in adata.obsm:
        raise ValueError(
            f"corrected representation {corrected_rep!r} is absent; train integration first"
        )

    batch_labels = adata.obs[batch_key].astype(str).to_numpy()
    unique_batches = np.unique(batch_labels)
    n_batches = int(len(unique_batches))
    if n_batches < 2:
        raise ValueError(f"batch key {batch_key!r} has fewer than two groups")
    code = {b: i for i, b in enumerate(unique_batches)}
    batch_codes = np.array([code[b] for b in batch_labels])

    corrected = _score_representation(
        adata, corrected_rep, batch_codes, n_batches, unique_batches, n_neighbors
    )
    baseline = _score_representation(
        adata, BASELINE_REP, batch_codes, n_batches, unique_batches, n_neighbors
    )

    corrected_mean = corrected["entropy_mean"]
    improvement = (
        round(corrected_mean - baseline["entropy_mean"], 4) if baseline is not None else None
    )
    score = {
        "schema_version": 1,
        "batch_key": str(batch_key),
        "n_batches": n_batches,
        "n_neighbors": min(n_neighbors, adata.n_obs - 1),
        "corrected": corrected,
        "baseline": baseline,
        "mixing_improvement": improvement,
        "interpretation": interpret_mixing(corrected_mean),
        "note": (
            "Neighborhood batch-mixing entropy in [0,1] (1 = perfectly mixed). Corrected and "
            "baseline are scored on comparable latent spaces; this describes mixing only and is "
            "not a benchmark score. Sample structure can also reflect real per-donor biology."
        ),
    }
    (context.staging_dir / "integration-score.json").write_text(
        json.dumps(score, indent=2), encoding="utf-8"
    )

    if improvement is None:
        summary = (
            f"Scored {corrected_rep} mixing on {batch_key!r}: mean neighborhood batch entropy "
            f"{corrected_mean:.3f} ({score['interpretation'].split(' — ')[0]}); "
            f"no {BASELINE_REP} baseline present to compare against."
        )
    else:
        summary = (
            f"Scored integration mixing on {batch_key!r}: {corrected_rep} mean batch entropy "
            f"{corrected_mean:.3f} vs {BASELINE_REP} baseline {baseline['entropy_mean']:.3f} "
            f"(improvement {improvement:+.3f}); {score['interpretation'].split(' — ')[0]}."
        )

    return {
        "summary": summary,
        "details": score,
        # Provenance only — no floor reads this; the batch decision, made once on the
        # uncorrected pass, remains authoritative and is preserved here by omission.
        "facts_patch": {"batch": {"integration_score": score}},
        "artifacts": [
            {
                "name": "integration-score",
                "relative_path": "integration-score.json",
                "media_type": "application/json",
            }
        ],
    }
