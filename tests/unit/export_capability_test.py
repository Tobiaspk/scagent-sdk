from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _package() -> Any:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "export-dataset"
    )


def _handler() -> Any:
    package = _package()
    return package.load_handler(package.manifest.tools[0])


class _Index:
    def __init__(self, values: list[str]):
        self.values = values

    def equals(self, other: Any) -> bool:
        return isinstance(other, _Index) and self.values == other.values


class _Frame:
    def __init__(self, columns: list[str], index: list[str]):
        self.columns = columns
        self.index = _Index(index)


class _File:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _AnnData:
    def __init__(self, *, omit_graph: bool = False, backed: bool = False):
        self.n_obs = 2
        self.n_vars = 3
        self.obs = _Frame(["donor"], ["cell-a", "cell-b"])
        self.var = _Frame(["feature_type"], ["g1", "g2", "g3"])
        self.layers = {"counts": object()}
        self.obsm = {"X_scVI": object(), "X_umap": object()}
        self.varm: dict[str, object] = {}
        self.obsp = {} if omit_graph else {"connectivities": object()}
        self.varp: dict[str, object] = {}
        self.uns = {"neighbors": {"connectivities_key": "connectivities"}}
        self.raw = SimpleNamespace(n_obs=2, n_vars=3)
        self.file = _File() if backed else None
        self.write_arguments: dict[str, Any] | None = None

    @property
    def obs_names(self) -> _Index:
        return self.obs.index

    @property
    def var_names(self) -> _Index:
        return self.var.index

    def write_h5ad(self, path: Path, **kwargs: Any) -> None:
        self.write_arguments = kwargs
        path.write_bytes(b"H5AD")


def _install_fake_anndata(
    monkeypatch: pytest.MonkeyPatch, *, omit_graph_after_round_trip: bool = False
) -> tuple[_AnnData, _AnnData]:
    source = _AnnData()
    exported = _AnnData(omit_graph=omit_graph_after_round_trip, backed=True)
    module = SimpleNamespace(
        read_zarr=lambda _path: source,
        read_h5ad=lambda _path, backed=None: exported,
    )
    monkeypatch.setitem(sys.modules, "anndata", module)
    return source, exported


def _source_store(tmp_path: Path) -> Path:
    source_path = tmp_path / "enriched.zarr"
    source_path.mkdir()
    return source_path


def test_manifest_declares_format_only_ungated_lineage_consumption() -> None:
    tool = _package().manifest.tools[0]

    assert tool.name == "export_anndata"
    assert tool.environment == "anndata-io"
    assert tool.floors == ()
    assert tool.primary_matrix_input == "path"
    assert tool.primary_matrix_output == "portable-anndata"
    assert tool.advances_lineage is False
    assert "path" not in tool.input_schema.get("required", [])


def test_export_preserves_structure_registers_h5ad_and_verifies_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, exported = _install_fake_anndata(monkeypatch)
    source_path = _source_store(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()

    result = _handler()(
        {"path": str(source_path), "filename": "analysis-result.h5ad"},
        SimpleNamespace(staging_dir=staging),
    )

    assert (staging / "analysis-result.h5ad").read_bytes() == b"H5AD"
    assert source.write_arguments == {"compression": "gzip", "compression_opts": 2}
    assert exported.file.closed is True
    assert result["facts_patch"] == {}
    assert result["details"]["round_trip_verified"] is True
    assert result["details"]["obsm"] == ["X_scVI", "X_umap"]
    assert result["details"]["obsp"] == ["connectivities"]
    assert result["artifacts"] == [
        {
            "name": "portable-anndata",
            "relative_path": "analysis-result.h5ad",
            "media_type": "application/x-h5ad",
        }
    ]


def test_export_refuses_a_round_trip_that_drops_graph_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, exported = _install_fake_anndata(monkeypatch, omit_graph_after_round_trip=True)
    source_path = _source_store(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(ValueError, match="changed the AnnData structure"):
        _handler()(
            {"path": str(source_path), "filename": "analysis-result.h5ad"},
            SimpleNamespace(staging_dir=staging),
        )
    assert exported.file.closed is True


@pytest.mark.parametrize("filename", ["../escape.h5ad", "result.zarr", "/tmp/result.h5ad"])
def test_export_rejects_nonlocal_or_non_h5ad_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    _install_fake_anndata(monkeypatch)
    source_path = _source_store(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(ValueError, match="basename ending in .h5ad"):
        _handler()(
            {"path": str(source_path), "filename": filename},
            SimpleNamespace(staging_dir=staging),
        )
