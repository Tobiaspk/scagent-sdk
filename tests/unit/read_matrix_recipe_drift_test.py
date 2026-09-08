"""Guard the duplicated `_read_matrix` recipe against drift.

Skill packages cannot import one another (each entrypoint only puts its own ``scripts`` directory
on ``sys.path``), so the artifact reader that tolerates both ``.h5ad`` files and ``.zarr`` stores
(ADR 0011) is duplicated per skill. This asserts every copy is byte-identical, matching the
precedent in ``inspect-dataset/scripts/identity.py``.
"""
from __future__ import annotations

import pathlib

SKILLS = pathlib.Path(__file__).parents[2] / ".claude" / "skills"

CANON = (
    'def _read_matrix(path):\n'
    '    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""\n'
    '    import anndata as ad\n'
    '\n'
    '    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)'
)

CANON_WRITE = (
    'def _write_matrix(adata, path):\n'
    '    """Write an AnnData artifact: a .zarr store (blosc) or a gzipped .h5ad file (ADR 0011)."""\n'  # noqa: E501
    '    import anndata as ad\n'
    '\n'
    '    if str(path).endswith(".zarr"):\n'
    '        ad.settings.zarr_write_format = 2  # v2 until the v3 sharding/dedup design lands\n'
    '        adata.write_zarr(path)\n'
    '    else:\n'
    '        adata.write_h5ad(path, compression="gzip")'
)


def test_read_matrix_helper_is_byte_identical_across_skills() -> None:
    holders = sorted(
        p
        for p in SKILLS.glob("*/scripts/*.py")
        if "_read_matrix" in p.read_text(encoding="utf-8")
    )
    # Every matrix-reading skill carries the recipe; keep this from silently shrinking.
    assert len(holders) >= 15, f"expected the recipe in >=15 skills, found {len(holders)}"
    drifted = [str(p) for p in holders if CANON not in p.read_text(encoding="utf-8")]
    assert not drifted, f"_read_matrix drifted from canonical form in: {drifted}"


def test_matrix_reading_skills_accept_directory_stores_not_only_files() -> None:
    # A .zarr artifact is a directory, so a skill that reads a matrix must guard its input with
    # path.exists(), never path.is_file() (which is False for a store and raises FileNotFoundError
    # before the read). Regression for the run where read-only skills (visualize, batch) crashed on
    # a .zarr input because their guards were never relaxed alongside the writer flip.
    offenders = [
        str(p)
        for p in SKILLS.glob("*/scripts/*.py")
        if "_read_matrix" in (text := p.read_text(encoding="utf-8"))
        and "if not path.is_file()" in text
    ]
    assert not offenders, f"matrix-reading skills reject .zarr directory inputs: {offenders}"


# These open a user- or artifact-supplied dataset path but carry no `_read_matrix`, so the scan
# above never reached them. That is exactly how they stayed .h5ad/is_file()-only through the zarr
# flip, leaving every in-session artifact uninspectable and undescribable; guard them by name.
DATASET_ENTRY_SCRIPTS = (
    "inspect-dataset/scripts/inspect_dataset.py",
    "inspect-dataset/scripts/describe.py",
    "inspect-dataset/scripts/convert.py",
)


def test_dataset_entry_points_accept_directory_stores() -> None:
    offenders = [
        rel
        for rel in DATASET_ENTRY_SCRIPTS
        if "if not path.is_file()" in (SKILLS / rel).read_text(encoding="utf-8")
    ]
    assert not offenders, f"dataset entry points reject .zarr directory inputs: {offenders}"


def test_zarr_writer_skills_do_not_declare_an_h5ad_matrix_artifact() -> None:
    # A skill that writes a .zarr store must declare that store as its artifact, not a stale .h5ad
    # path -- otherwise the executor looks for a file that was never written ("declared artifact
    # does not exist"). Regression for scvi-integration, where the write flipped to .zarr but the
    # inline relative_path literal stayed .h5ad. finalize/convert legitimately write .h5ad and
    # never call _write_matrix with a .zarr path, so they are not caught here.
    offenders = []
    for p in SKILLS.glob("*/scripts/*.py"):
        text = p.read_text(encoding="utf-8")
        writes_zarr_store = "_write_matrix(" in text and '.zarr")' in text
        declares_h5ad = any(
            '"relative_path":' in line and '.h5ad"' in line for line in text.splitlines()
        )
        if writes_zarr_store and declares_h5ad:
            offenders.append(str(p))
    assert not offenders, f"skills write .zarr but declare a .h5ad matrix artifact: {offenders}"


def test_write_matrix_helper_is_byte_identical_across_skills() -> None:
    holders = sorted(
        p
        for p in SKILLS.glob("*/scripts/*.py")
        if "_write_matrix" in p.read_text(encoding="utf-8")
    )
    assert holders, "no skill carries the _write_matrix recipe"
    drifted = [str(p) for p in holders if CANON_WRITE not in p.read_text(encoding="utf-8")]
    assert not drifted, f"_write_matrix drifted from canonical form in: {drifted}"
