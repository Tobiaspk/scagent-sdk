"""Cross-skill interoperability for .zarr artifacts, and the serialization of non-finite stats.

Every in-session artifact is a ``.zarr`` store -- a directory (ADR 0011). Two whole classes of
failure came from that not being universally true of the *consumers*:

* a skill that guarded its input with ``is_file()`` or matched ``is_dir()`` before the suffix
  rejected (or misread) every artifact the pipeline itself produced, leaving the original input
  file as the only readable path and silently resetting analysis lineage;
* Scanpy's infinite log fold-change reached the executor, which serializes durable state with
  ``allow_nan=False``, and failed the capability outright.

The heavier round trips need ``anndata`` and are skipped where it is absent (the SDK venv);
everything else here is pure-stdlib/numpy and always runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry

SKILLS = Path(__file__).parents[2] / ".claude" / "skills"


def _globals(skill_id: str, tool_name: str) -> dict[str, Any]:
    package = next(
        package
        for package in CapabilityRegistry(SKILLS).discover()
        if package.manifest.skill_id == skill_id
    )
    tool = next(tool for tool in package.manifest.tools if tool.name == tool_name)
    return package.load_handler(tool).__globals__


def _context(tmp_path: Path) -> Any:
    return SimpleNamespace(
        staging_dir=tmp_path, session_dir=tmp_path, artifact_relative_path="artifacts/x"
    )


def _store(root: Path, *, chunk: bytes = b"a" * 4096, big: bytes = b"b" * 65536) -> Path:
    """A minimal zarr-v2-shaped directory store: metadata dotfiles plus data members."""

    root.mkdir(parents=True, exist_ok=True)
    (root / ".zgroup").write_text('{\n    "zarr_format": 2\n}', encoding="utf-8")
    (root / ".zattrs").write_text("{}", encoding="utf-8")
    (root / "X").mkdir(exist_ok=True)
    (root / "X" / "0.0").write_bytes(chunk)
    (root / "X" / "0.1").write_bytes(big)  # the largest data member
    return root


# --------------------------------------------------------------------------------------------
# single-cell-counts: a .zarr store must not be mistaken for a 10x Matrix Market directory
# --------------------------------------------------------------------------------------------


def test_counts_reader_routes_a_zarr_store_to_the_anndata_reader() -> None:
    scope = _globals("single-cell-counts", "materialize_count_matrix")
    sentinel = object()
    original = scope["_read_matrix"]
    scope["_read_matrix"] = lambda path: sentinel

    class _NoMtx:
        def read_10x_mtx(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("a .zarr store must never reach the 10x Matrix Market reader")

    try:
        assert scope["_read"](Path("/nowhere/doublet-annotated.zarr"), _NoMtx()) is sentinel
    finally:
        scope["_read_matrix"] = original


def test_counts_reader_still_treats_a_plain_directory_as_10x_mtx(tmp_path: Path) -> None:
    scope = _globals("single-cell-counts", "materialize_count_matrix")
    plain = tmp_path / "filtered_feature_bc_matrix"
    plain.mkdir()
    marker = object()

    class _Mtx:
        def read_10x_mtx(self, *args: Any, **kwargs: Any) -> Any:
            return marker

    assert scope["_read"](plain, _Mtx()) is marker


# --------------------------------------------------------------------------------------------
# expression-preprocessing: 'auto' resolves counts in X, an explicit layer is never substituted
# --------------------------------------------------------------------------------------------


def _adata(layers: dict[str, Any], x: Any) -> Any:
    return SimpleNamespace(layers=layers, X=x)


def test_resolve_layer_prefers_counts_then_falls_back_to_x() -> None:
    scope = _globals("expression-preprocessing", "normalize_single_cell_expression")
    resolve = scope["_resolve_layer"]
    assert resolve(_adata({"counts": None}, None), "auto") == "counts"
    assert resolve(_adata({}, None), "auto") is None
    assert resolve(_adata({}, None), None) is None
    assert resolve(_adata({}, None), "raw_counts") == "raw_counts"


def test_count_matrix_accepts_counts_in_x_under_auto() -> None:
    scope = _globals("expression-preprocessing", "normalize_single_cell_expression")
    counts = np.array([[0.0, 3.0], [5.0, 1.0]], dtype="float32")
    assert scope["_count_matrix"](_adata({}, counts), "auto") is counts


def test_count_matrix_still_fails_on_an_explicitly_named_missing_layer() -> None:
    scope = _globals("expression-preprocessing", "normalize_single_cell_expression")
    with pytest.raises(ValueError, match="absent"):
        scope["_count_matrix"](_adata({}, np.zeros((2, 2), dtype="float32")), "counts")


def test_count_matrix_rejects_non_count_values_in_x() -> None:
    scope = _globals("expression-preprocessing", "normalize_single_cell_expression")
    logged = np.array([[0.0, 1.5]], dtype="float32")
    with pytest.raises(ValueError, match="X is not finite nonnegative integer counts"):
        scope["_count_matrix"](_adata({}, logged), "auto")


# --------------------------------------------------------------------------------------------
# marker-annotation: an infinite log fold-change must not fail the capability
# --------------------------------------------------------------------------------------------


def test_json_number_nulls_non_finite_statistics() -> None:
    scope = _globals("marker-annotation", "evaluate_marker_evidence")
    to_number = scope["_json_number"]
    assert to_number(np.float32(2.5)) == pytest.approx(2.5)
    assert to_number(float("inf")) is None
    assert to_number(float("-inf")) is None
    assert to_number(float("nan")) is None


def test_marker_deg_payload_with_infinite_logfc_serializes_like_the_executor() -> None:
    # The executor writes results with json.dumps(..., allow_nan=False); an untreated inf raised
    # "Out of range float values are not JSON compliant" and lost the whole marker evidence step.
    scope = _globals("marker-annotation", "evaluate_marker_evidence")
    to_number = scope["_json_number"]
    payload = {
        "deg_summary": {
            "4": {
                "n_significant_positive_degs": 12,
                "top_degs": [
                    {
                        "gene": "SFTPC",
                        "score": to_number(np.float64(31.2)),
                        "logfoldchange": to_number(np.float64("inf")),
                        "adjusted_pvalue": to_number(np.float64(0.0)),
                    }
                ],
            }
        }
    }
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
    assert '"logfoldchange": null' in encoded


# --------------------------------------------------------------------------------------------
# inspect-dataset: directory stores, with the sampled/full hashing contract preserved
# --------------------------------------------------------------------------------------------


def test_store_fingerprint_is_deterministic(tmp_path: Path) -> None:
    scope = _globals("inspect-dataset", "inspect_dataset")
    store = _store(tmp_path / "a.zarr")
    members = scope["_store_members"](store)
    size = sum(member.stat().st_size for member in members)
    first = scope["_fingerprint_store"](store, mode="sampled", members=members, size=size)
    second = scope["_fingerprint_store"](store, mode="sampled", members=members, size=size)
    assert first == second


def test_store_fingerprint_changes_with_content_and_with_structure(tmp_path: Path) -> None:
    scope = _globals("inspect-dataset", "inspect_dataset")

    def fingerprint(store: Path, mode: str = "sampled") -> str:
        members = scope["_store_members"](store)
        size = sum(member.stat().st_size for member in members)
        return scope["_fingerprint_store"](store, mode=mode, members=members, size=size)

    base = fingerprint(_store(tmp_path / "base.zarr"))
    # Same layout, different bytes in the largest data member.
    assert fingerprint(_store(tmp_path / "content.zarr", big=b"c" * 65536)) != base
    # Same bytes, different member size -> the manifest moves.
    assert fingerprint(_store(tmp_path / "size.zarr", chunk=b"a" * 8192)) != base
    # An added member changes the manifest too.
    extra = _store(tmp_path / "extra.zarr")
    (extra / "X" / "1.0").write_bytes(b"a" * 4096)
    assert fingerprint(extra) != base


def test_sampled_store_hashing_covers_more_than_the_largest_member(
    tmp_path: Path,
) -> None:
    # A bounded sample is not a full integrity hash, but it must not reduce a multi-array store to
    # one largest chunk. This same-length edit changes a smaller representative member.
    scope = _globals("inspect-dataset", "inspect_dataset")

    def fingerprint(store: Path, mode: str) -> str:
        members = scope["_store_members"](store)
        size = sum(member.stat().st_size for member in members)
        return scope["_fingerprint_store"](store, mode=mode, members=members, size=size)

    base = _store(tmp_path / "base.zarr")
    edited = _store(tmp_path / "edited.zarr", chunk=b"z" * 4096)
    assert fingerprint(base, "sampled") != fingerprint(edited, "sampled")
    assert fingerprint(base, "full") != fingerprint(edited, "full")


def test_inspect_dataset_reports_identity_for_a_zarr_store(tmp_path: Path) -> None:
    scope = _globals("inspect-dataset", "inspect_dataset")
    store = _store(tmp_path / "qc-assessed.zarr")
    staging = tmp_path / "staging"
    staging.mkdir()
    result = scope["run"]({"path": str(store)}, _context(staging))
    identity = result["details"]
    assert identity["size_bytes"] == sum(
        member.stat().st_size for member in store.rglob("*") if member.is_file()
    )
    assert identity["format"]["byte_signature"] == "zarr"
    assert identity["format"]["extension"] == "zarr"
    assert identity["format"]["extension_signature_consistent"] is True
    assert identity["fingerprint_mode"] == "sampled"
    # The executor serializes this fact patch; it must be clean JSON.
    json.dumps(result["facts_patch"], allow_nan=False)


def test_inspect_dataset_still_fingerprints_a_plain_file(tmp_path: Path) -> None:
    scope = _globals("inspect-dataset", "inspect_dataset")
    dataset = tmp_path / "input.h5ad"
    dataset.write_bytes(b"\x89HDF\r\n\x1a\n" + b"0" * 4096)
    staging = tmp_path / "staging"
    staging.mkdir()
    identity = scope["run"]({"path": str(dataset)}, _context(staging))["details"]
    assert identity["format"]["byte_signature"] == "hdf5"
    assert identity["size_bytes"] == dataset.stat().st_size


def test_inspect_dataset_rejects_a_missing_path(tmp_path: Path) -> None:
    scope = _globals("inspect-dataset", "inspect_dataset")
    with pytest.raises(FileNotFoundError):
        scope["run"]({"path": str(tmp_path / "absent.zarr")}, _context(tmp_path))


def test_inspect_dataset_rejects_an_arbitrary_directory(tmp_path: Path) -> None:
    scope = _globals("inspect-dataset", "inspect_dataset")
    directory = tmp_path / "not-a-dataset"
    directory.mkdir()
    (directory / "notes.txt").write_text("private unrelated content", encoding="utf-8")
    with pytest.raises(ValueError, match="directory inputs must be .zarr stores"):
        scope["run"]({"path": str(directory)}, _context(tmp_path))


def test_inspect_dataset_rejects_a_fake_zarr_directory(tmp_path: Path) -> None:
    scope = _globals("inspect-dataset", "inspect_dataset")
    directory = tmp_path / "fake.zarr"
    directory.mkdir()
    (directory / "notes.txt").write_text("not zarr", encoding="utf-8")
    with pytest.raises(ValueError, match="no root Zarr metadata marker"):
        scope["run"]({"path": str(directory)}, _context(tmp_path))


# --------------------------------------------------------------------------------------------
# finalize-analysis: publish only over an adjudicated clustering, and never overstate the review
# --------------------------------------------------------------------------------------------

CLUSTERING = "clustering:sha256:current"


def _finalize_scope() -> dict[str, Any]:
    return _globals("finalize-analysis", "finalize_analysis")


def test_finalize_allows_an_analysis_that_never_ran_cluster_qc() -> None:
    # Targeted analyses must stay possible: no cluster_qc fact means nothing to adjudicate.
    validate = _finalize_scope()["_validate_cluster_qc_resolved"]
    validate({}, CLUSTERING, "leiden")
    validate({"cluster_qc": None}, CLUSTERING, "leiden")


def test_finalize_ignores_non_attested_cluster_qc_state() -> None:
    validate = _finalize_scope()["_validate_cluster_qc_resolved"]
    validate(
        {"cluster_qc": {"status": "not_applicable", "clustering_id": CLUSTERING}},
        CLUSTERING,
        "leiden",
    )


def test_finalize_refuses_cluster_qc_evaluated_but_never_reviewed() -> None:
    validate = _finalize_scope()["_validate_cluster_qc_resolved"]
    facts = {
        "cluster_qc": {
            "status": "attested",
            "clustering_id": CLUSTERING,
            "review_status": "pending",
        }
    }
    with pytest.raises(ValueError, match="never reviewed"):
        validate(facts, CLUSTERING, "leiden_res_1_0")


def test_finalize_ignores_cluster_qc_recorded_for_another_clustering() -> None:
    validate = _finalize_scope()["_validate_cluster_qc_resolved"]
    facts = {"cluster_qc": {"status": "attested", "clustering_id": "clustering:sha256:other"}}
    validate(facts, CLUSTERING, "leiden")


def test_finalize_accepts_a_resolved_cluster_qc_review() -> None:
    validate = _finalize_scope()["_validate_cluster_qc_resolved"]
    facts = {
        "cluster_qc": {
            "status": "attested",
            "evidence_id": "cluster-qc-evidence:current",
            "clustering_id": CLUSTERING,
            "review": {
                "status": "resolved",
                "evidence_id": "cluster-qc-evidence:current",
                "clustering_id": CLUSTERING,
            },
        }
    }
    validate(facts, CLUSTERING, "leiden")


def test_finalize_refuses_a_review_with_unresolved_cluster_actions() -> None:
    validate = _finalize_scope()["_validate_cluster_qc_resolved"]
    facts = {
        "cluster_qc": {
            "status": "attested",
            "evidence_id": "cluster-qc-evidence:current",
            "clustering_id": CLUSTERING,
            "review": {
                "status": "action_required",
                "evidence_id": "cluster-qc-evidence:current",
                "clustering_id": CLUSTERING,
                "unresolved_clusters": ["7"],
            },
        }
    }
    with pytest.raises(ValueError, match="unresolved cluster"):
        validate(facts, CLUSTERING, "leiden")


def test_finalize_refuses_a_stale_review_for_the_same_clustering() -> None:
    validate = _finalize_scope()["_validate_cluster_qc_resolved"]
    facts = {
        "cluster_qc": {
            "status": "attested",
            "evidence_id": "cluster-qc-evidence:new",
            "clustering_id": CLUSTERING,
            "review": {
                "status": "resolved",
                "evidence_id": "cluster-qc-evidence:old",
                "clustering_id": CLUSTERING,
            },
        }
    }
    with pytest.raises(ValueError, match="review.*stale"):
        validate(facts, CLUSTERING, "leiden")


def test_caveats_do_not_claim_a_review_that_never_happened() -> None:
    caveats = _finalize_scope()["_auto_caveats"](
        {"cluster_qc": {"warnings": ["doublet signal is absent"]}}, {}
    )
    joined = " ".join(caveats)
    assert "visually reviewed" not in joined
    assert "no current resolved review is on record" in joined


def test_caveats_flag_reference_runs_that_never_became_evidence() -> None:
    caveats = _finalize_scope()["_auto_caveats"](
        {
            "reference_runs": {"scimilarity": {"abc": {"status": "complete"}}, "celltypist": {}},
            "annotation": {"evidence": {"celltypist": {"status": "complete"}}},
        },
        {},
    )
    joined = " ".join(caveats)
    assert "scimilarity" in joined
    assert "never registered" in joined


def test_caveats_ignore_stale_or_incomplete_reference_runs() -> None:
    caveats = _finalize_scope()["_auto_caveats"](
        {
            "analysis": {"cell_set": {"id": "cells:current"}},
            "reference_runs": {
                "scimilarity": {
                    "old": {"status": "complete", "cell_set_id": "cells:old"},
                    "failed": {"status": "failed", "cell_set_id": "cells:current"},
                }
            },
        },
        {},
    )
    assert not any("scimilarity" in caveat for caveat in caveats)


def test_caveats_do_not_treat_atlas_queries_as_annotation_runs() -> None:
    caveats = _finalize_scope()["_auto_caveats"](
        {
            "reference_runs": {
                "scimilarity_query": {"query": {"status": "complete"}}
            }
        },
        {},
    )
    assert not any("scimilarity_query" in caveat for caveat in caveats)


def test_caveats_flag_labels_reviewed_without_marker_evidence() -> None:
    caveats = _finalize_scope()["_auto_caveats"](
        {"annotation": {"review": {"deg_primary": False, "methods_reviewed": ["celltypist"]}}},
        {},
    )
    assert any("without registered marker/DEG evidence" in caveat for caveat in caveats)


def test_caveats_stay_quiet_when_marker_evidence_was_reviewed() -> None:
    caveats = _finalize_scope()["_auto_caveats"](
        {"annotation": {"review": {"deg_primary": True, "methods_reviewed": ["markers"]}}}, {}
    )
    assert not any("without registered marker/DEG evidence" in caveat for caveat in caveats)


# --------------------------------------------------------------------------------------------
# Real round trip: only runs where anndata is installed (the compute environment).
# --------------------------------------------------------------------------------------------


def test_a_real_zarr_artifact_survives_the_readers_that_consume_it(tmp_path: Path) -> None:
    ad = pytest.importorskip("anndata")  # written/read in the compute env; venv may lack it
    pytest.importorskip("zarr")
    import pandas as pd

    counts = np.array([[0, 3, 1], [5, 0, 2], [1, 1, 0]], dtype="float32")
    adata = ad.AnnData(
        X=counts,
        obs=pd.DataFrame(
            {"predicted_doublet": [False, True, False], "donor": ["d1", "d1", "d2"]},
            index=["c1", "c2", "c3"],
        ),
        var=pd.DataFrame(index=["GENE1", "GENE2", "MT-CO1"]),
    )
    store = tmp_path / "doublet-annotated.zarr"
    ad.settings.zarr_write_format = 2
    adata.write_zarr(store)

    # single-cell-counts must read the store rather than hand it to the 10x reader.
    counts_scope = _globals("single-cell-counts", "materialize_count_matrix")
    import scanpy as sc

    round_tripped = counts_scope["_read"](store, sc)
    assert "predicted_doublet" in round_tripped.obs.columns

    # describe_dataset must open the directory and report its annotations.
    staging = tmp_path / "staging"
    staging.mkdir()
    describe_scope = _globals("inspect-dataset", "describe_dataset")
    sheet = describe_scope["run"]({"path": str(store)}, _context(staging))["details"]
    assert sheet["read_mode"] == "lazy"
    assert sheet["backed"] is False
    assert "predicted_doublet" in sheet["obs_columns"]
    assert sheet["shape"] == {"n_obs": 3, "n_vars": 3}
    json.dumps(sheet, allow_nan=False, default=str)
