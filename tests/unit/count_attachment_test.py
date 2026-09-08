from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry


class _Names(list[str]):
    @property
    def is_unique(self) -> bool:
        return len(self) == len(set(self))


class _FakeAnnData:
    def __init__(self, cells: list[str], genes: list[str]) -> None:
        self.obs_names = _Names(cells)
        self.var_names = _Names(genes)
        self.last_slice: tuple[list[str], list[str]] | None = None

    def __getitem__(self, value: tuple[Any, Any]) -> _FakeAnnData:
        cells, genes = value
        self.last_slice = (list(cells), list(genes))
        return self

    def copy(self) -> _FakeAnnData:
        return self


def _globals() -> dict[str, Any]:
    root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        package
        for package in CapabilityRegistry(root).discover()
        if package.manifest.skill_id == "single-cell-counts"
    )
    tool = package.manifest.tools[0]
    return package.load_handler(tool).__globals__


def test_manifest_exposes_additive_count_attachment() -> None:
    root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        package
        for package in CapabilityRegistry(root).discover()
        if package.manifest.skill_id == "single-cell-counts"
    )
    assert "counts_from" in package.manifest.tools[0].input_schema["properties"]


def test_count_source_is_aligned_to_target_order() -> None:
    align = _globals()["_align_count_source"]
    source = _FakeAnnData(["c2", "c1"], ["g2", "g1"])
    target = _FakeAnnData(["c1", "c2"], ["g1", "g2"])
    align(source, target)
    assert source.last_slice == (["c1", "c2"], ["g1", "g2"])


def test_count_attachment_refuses_identity_mismatch() -> None:
    align = _globals()["_align_count_source"]
    with pytest.raises(ValueError, match="same cells"):
        align(
            _FakeAnnData(["c1", "different"], ["g1"]),
            _FakeAnnData(["c1", "c2"], ["g1"]),
        )
    with pytest.raises(ValueError, match="same genes"):
        align(
            _FakeAnnData(["c1"], ["g1", "different"]),
            _FakeAnnData(["c1"], ["g1", "g2"]),
        )


def test_attachment_facts_do_not_clear_existing_analysis_domains() -> None:
    facts_patch = _globals()["_count_facts_patch"](
        revision_id="revision-a",
        source_path="processed.zarr",
        counts_source_path="raw.h5ad",
        n_cells=2,
        n_genes=3,
        cell_set_id="cells-a",
        count_id="counts-a",
        matrix_id="matrix-a",
        selected="X",
        attachment_mode=True,
    )
    assert set(facts_patch) == {"analysis"}
    assert "representation" not in facts_patch["analysis"]
    assert "clustering" not in facts_patch["analysis"]
