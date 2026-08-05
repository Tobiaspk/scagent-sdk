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


def test_read_matrix_helper_is_byte_identical_across_skills() -> None:
    holders = sorted(
        p
        for p in SKILLS.glob("*/scripts/*.py")
        if "_read_matrix" in p.read_text(encoding="utf-8")
    )
    # Every pipeline skill that reads a matrix carries the recipe; keep this from silently shrinking.
    assert len(holders) >= 15, f"expected the recipe in >=15 skills, found {len(holders)}"
    drifted = [str(p) for p in holders if CANON not in p.read_text(encoding="utf-8")]
    assert not drifted, f"_read_matrix drifted from canonical form in: {drifted}"
