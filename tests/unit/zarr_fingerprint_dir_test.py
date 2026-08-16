"""Skill dataset-fingerprint helpers must handle a .zarr store (a directory), not only a file.

A .zarr artifact is a directory (ADR 0011). A fingerprint that does ``path.open("rb")`` raises
IsADirectoryError on it -- a latent crash whenever the skill fingerprints a .zarr input or output.
This exercises the real helpers on a directory: no crash, deterministic, change-sensitive, and the
plain-file path still works.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

SKILLS = pathlib.Path(__file__).parents[2] / ".claude" / "skills"
FINGERPRINT_SKILLS = {
    "doublets": "doublet-evidence/scripts/doublets.py",
    "cluster_qc_evaluate": "cluster-qc/scripts/evaluate.py",
}


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, SKILLS / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_store(root: pathlib.Path, extra: bytes = b"") -> None:
    (root / "X").mkdir(parents=True)
    (root / ".zgroup").write_bytes(b'{"zarr_format":2}')
    (root / "X" / "0.0").write_bytes(b"chunk" * 1000 + extra)


@pytest.mark.parametrize("name,rel", FINGERPRINT_SKILLS.items())
def test_dataset_fingerprint_handles_a_zarr_store_directory(
    name: str, rel: str, tmp_path: pathlib.Path
) -> None:
    module = _load(name, rel)
    fingerprint = module._dataset_fingerprint

    same_a, same_b, changed = tmp_path / "a.zarr", tmp_path / "b.zarr", tmp_path / "c.zarr"
    _make_store(same_a)
    _make_store(same_b)
    _make_store(changed, extra=b"x")

    hash_a = fingerprint(same_a)  # must not raise IsADirectoryError
    assert hash_a.startswith("sha256:")
    assert fingerprint(same_b) == hash_a  # identical stores -> identical fingerprint
    assert fingerprint(changed) != hash_a  # content change -> different fingerprint

    plain = tmp_path / "plain.h5ad"  # plain-file path still works
    plain.write_bytes(b"\x89HDF\r\n\x1a\n" + b"data" * 100)
    assert fingerprint(plain).startswith("sha256:")
