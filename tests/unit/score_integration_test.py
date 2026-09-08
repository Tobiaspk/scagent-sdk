"""Unit tests for the score_integration mixing math and manifest wiring.

The neighbour search itself runs in the compute env (sklearn); here we exercise the pure entropy
math and the plain-language thresholds, which must be testable in the control-plane venv.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _package() -> Any:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "scvi-integration"
    )


def _score_globals() -> dict[str, Any]:
    package = _package()
    tool = next(t for t in package.manifest.tools if t.name == "score_integration")
    return package.load_handler(tool).__globals__


def test_manifest_exposes_score_integration_read_only() -> None:
    package = _package()
    names = {tool.name for tool in package.manifest.tools}
    assert names == {"train_scvi_latent", "score_integration"}
    tool = next(t for t in package.manifest.tools if t.name == "score_integration")
    # primary_matrix_input must be omittable (resolved to the lineage head by the executor).
    assert "path" not in tool.input_schema.get("required", [])
    assert tool.primary_matrix_input == "path"
    assert "baseline_path" in tool.input_schema["properties"]


def test_perfectly_mixed_neighbourhood_scores_near_one() -> None:
    entropy_from_neighbor_labels = _score_globals()["entropy_from_neighbor_labels"]
    # Two batches, every neighbourhood evenly split -> maximal (normalized) entropy = 1.0.
    neighbor_codes = np.array([[0, 1, 0, 1]] * 6)
    per_cell = entropy_from_neighbor_labels(neighbor_codes, n_batches=2)
    assert np.allclose(per_cell, 1.0)


def test_segregated_neighbourhood_scores_zero() -> None:
    entropy_from_neighbor_labels = _score_globals()["entropy_from_neighbor_labels"]
    # Every neighbour shares the cell's own batch -> no mixing -> entropy 0.
    neighbor_codes = np.array([[0, 0, 0], [1, 1, 1], [0, 0, 0]])
    per_cell = entropy_from_neighbor_labels(neighbor_codes, n_batches=2)
    assert np.allclose(per_cell, 0.0)


def test_interpret_mixing_thresholds() -> None:
    interpret = _score_globals()["interpret_mixing"]
    assert interpret(0.9).startswith("Excellent")
    assert interpret(0.65).startswith("Good")
    assert interpret(0.5).startswith("Moderate")
    assert interpret(0.2).startswith("Poor")


def test_missing_baseline_does_not_claim_good_integration() -> None:
    interpret = _score_globals()["interpret_integration"]
    incomplete = interpret(0.9, None)
    assert "improvement is unknown" in incomplete
    assert "Excellent" not in incomplete
    complete = interpret(0.9, {"entropy_mean": 0.2})
    assert complete.startswith("Excellent")
    assert "change versus X_pca baseline +0.7000" in complete
