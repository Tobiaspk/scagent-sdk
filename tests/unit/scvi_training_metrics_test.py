from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from scagent_sdk.capabilities.registry import CapabilityRegistry


class _Frame:
    def __init__(self, name: str, values: list[float], index: list[int]) -> None:
        self.columns = [name]
        self.index = np.asarray(index)
        self._values = np.asarray(values)
        self.empty = not values

    def __getitem__(self, _name: str) -> Any:
        return self._values


def _globals() -> dict[str, Any]:
    root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        package
        for package in CapabilityRegistry(root).discover()
        if package.manifest.skill_id == "scvi-integration"
    )
    tool = next(tool for tool in package.manifest.tools if tool.name == "train_scvi_latent")
    return package.load_handler(tool).__globals__


def test_training_metrics_use_exact_finite_history_values() -> None:
    summarize = _globals()["_training_metric_summary"]
    result = summarize(
        {
            "elbo_train": _Frame("elbo_train", [20.0, np.nan, 12.25], [0, 1, 2]),
            "elbo_validation": _Frame(
                "elbo_validation", [19.0, 11.5, 12.0], [0, 7, 8]
            ),
        }
    )
    assert result == {
        "final_train_elbo": 12.25,
        "final_validation_elbo": 12.0,
        "best_validation_elbo": 11.5,
        "best_validation_epoch": 7,
    }
