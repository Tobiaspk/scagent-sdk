from __future__ import annotations

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
        if package.manifest.skill_id == "doublet-evidence"
    )


def _handlers() -> tuple[Any, Any]:
    package = _package()
    tools = {tool.name: tool for tool in package.manifest.tools}
    return (
        package.load_handler(tools["evaluate_doublet_evidence"]),
        package.load_handler(tools["review_doublet_evidence"]),
    )


def _context(tmp_path: Path) -> Any:
    staging = tmp_path / "staging"
    staging.mkdir()
    return SimpleNamespace(
        staging_dir=staging,
        session_dir=tmp_path / "session",
        execution_id="execution-1",
        state_facts={
            "analysis": {
                "dataset_revision": {
                    "id": "revision-a",
                    "prepared_path": "/data/prepared.h5ad",
                },
                "cell_set": {"id": "cells-a", "n_cells": 1000},
                "count_representation": {"id": "counts-a"},
                "clustering": {"id": "clusters-a", "key": "leiden"},
            },
            "doublets": {
                "evidence": {
                    "status": "complete",
                    "evidence_id": "doublets-a",
                    "cell_set_id": "cells-a",
                    "count_representation_id": "counts-a",
                    "annotated_path": "/data/doublet-annotated.h5ad",
                }
            },
        },
    )


def test_doublet_manifest_is_strict_gpu_routed_and_review_floored() -> None:
    package = _package()
    tools = {tool.name: tool for tool in package.manifest.tools}

    assert set(tools) == {"evaluate_doublet_evidence", "review_doublet_evidence"}
    assert tools["evaluate_doublet_evidence"].environment == "gpu-singlecell"
    review = tools["review_doublet_evidence"]
    assert review.environment == "gpu-singlecell"
    assert review.floors == ("current_doublet_evidence",)
    assert review.input_schema["additionalProperties"] is False
    assert "score_cutoff" not in review.input_schema["properties"]


def test_parameter_contract_preserves_legacy_defaults_and_bounds() -> None:
    evaluate, _review = _handlers()
    parameters = evaluate.__globals__["_parameters"]({})

    assert parameters["expected_doublet_rate"] == 0.06
    assert parameters["sim_doublet_ratio"] == 2.0
    assert parameters["requested_n_prin_comps"] == 30
    assert parameters["random_seed"] == 0
    with pytest.raises(ValueError, match="expected_doublet_rate"):
        evaluate.__globals__["_parameters"]({"expected_doublet_rate": 0.6})


def test_artifact_contract_uses_portable_provenance_without_session_state() -> None:
    evaluate, _review = _handlers()
    provenance = {
        "dataset_revision_id": "revision-a",
        "cell_set_id": "cells-a",
        "count_representation_id": "counts-a",
    }
    adata = SimpleNamespace(obs_names=["cell-1", "cell-2"])

    contract = evaluate.__globals__["_artifact_contract"](
        Path("/data/prepared.h5ad"),
        provenance,
        adata,
        "counts:matrix-a",
        "layers/counts",
    )

    assert contract["cell_set_id"] == "cells-a"
    assert contract["count_representation_id"] == "counts-a"
    assert contract["dataset_revision_id"] == "revision-a"


def test_evidence_patch_keeps_cells_and_invalidates_qc_not_clustering(
    tmp_path: Path, monkeypatch: Any
) -> None:
    evaluate, _review = _handlers()
    context = _context(tmp_path)
    evidence = {
        "status": "complete",
        "evidence_id": "doublets-new",
        "cell_set_id": "cells-a",
        "count_representation_id": "counts-a",
        "count_matrix_id": "counts:matrix-a",
        "count_source": "layers/counts",
        "n_cells": 1000,
        "predicted_doublets": 60,
        "predicted_rate": 0.06,
        "annotated_path": "artifacts/capabilities/execution-1/doublet-annotated.h5ad",
    }
    monkeypatch.setitem(
        evaluate.__globals__,
        "_execute_evidence",
        lambda _arguments, _context: {
            "details": evidence,
            "evidence": evidence,
            "dataset_revision_patch": {
                "prepared_path": evidence["annotated_path"],
                "observation_revision_id": "observation-a",
            },
            "artifacts": [],
            "model_media": [],
        },
    )

    result = evaluate({"path": "/data/prepared.h5ad", "batch_key": "library"}, context)

    assert result["facts_patch"]["analysis"]["cell_set"] == {
        "id": "cells-a",
        "n_cells": 1000,
    }
    assert result["facts_patch"]["analysis"]["count_representation"]["id"] == "counts-a"
    assert "clustering" not in result["facts_patch"]["analysis"]
    assert result["facts_patch"]["cluster_qc"] is None
    assert result["facts_patch"]["finalization"] is None
    assert "for review" in result["summary"]


def test_remove_predicted_requires_explicit_confirmation_and_nonempty_rationale() -> None:
    _evaluate, review = _handlers()
    validate = review.__globals__["_review_parameters"]

    with pytest.raises(ValueError, match="confirm_filtering"):
        validate({"decision": "remove_predicted", "rationale": "reviewed"})
    with pytest.raises(ValueError, match="rationale"):
        validate({"decision": "keep_all", "rationale": " "})


def test_review_only_records_decision_without_cell_set_change(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _evaluate, review = _handlers()
    context = _context(tmp_path)
    payload = {
        "status": "complete",
        "evidence_id": "doublets-a",
        "cell_set_id": "cells-a",
        "decision": "retain_for_cluster_review",
        "rationale": "inspect cluster context",
        "n_cells": 1000,
        "predicted_doublets": 60,
        "predicted_fraction": 0.06,
        "filtered_output_path": None,
    }
    monkeypatch.setitem(
        review.__globals__,
        "_execute_review",
        lambda _arguments, _context: {
            "review": payload,
            "filter_payload": None,
            "artifacts": [],
        },
    )

    result = review(
        {
            "path": "/data/doublet-annotated.h5ad",
            "decision": "retain_for_cluster_review",
            "rationale": "inspect cluster context",
        },
        context,
    )

    assert set(result["facts_patch"]) == {"doublets"}
    assert result["decisions_patch"]["doublet_handling"]["decision"] == (
        "retain_for_cluster_review"
    )


def test_confirmed_filter_issues_new_dataset_and_invalidates_downstream(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _evaluate, review = _handlers()
    context = _context(tmp_path)
    payload = {
        "status": "complete",
        "evidence_id": "doublets-a",
        "cell_set_id": "cells-a",
        "decision": "remove_predicted",
        "rationale": "reviewed calls",
        "n_cells": 1000,
        "predicted_doublets": 60,
        "predicted_fraction": 0.06,
        "filtered_output_path": "artifacts/capabilities/execution-1/filtered.h5ad",
    }
    filter_payload = {
        "dataset": {"fingerprint": "sha256:new"},
        "analysis": {
            "dataset_revision": {"id": "revision-b"},
            "cell_set": {"id": "cells-b"},
            "count_representation": {"id": "counts-b"},
            "representation": None,
            "clustering": None,
        },
        "cell_qc": None,
        "cluster_qc": None,
        "batch": None,
        "annotation": None,
        "finalization": None,
    }
    monkeypatch.setitem(
        review.__globals__,
        "_execute_review",
        lambda _arguments, _context: {
            "review": payload,
            "filter_payload": filter_payload,
            "artifacts": [],
        },
    )

    result = review(
        {
            "path": "/data/doublet-annotated.h5ad",
            "decision": "remove_predicted",
            "rationale": "reviewed calls",
            "confirm_filtering": True,
        },
        context,
    )

    assert result["facts_patch"]["analysis"]["cell_set"]["id"] == "cells-b"
    assert result["facts_patch"]["analysis"]["clustering"] is None
    assert result["facts_patch"]["annotation"] is None
    assert result["facts_patch"]["doublets"]["evidence"] is None
    assert result["decisions_patch"]["final_labels"] is None


def test_current_evidence_accepts_a_review_path_under_the_committed_action_dir(
    tmp_path: Path,
) -> None:
    """Regression: committed artifact dirs are named ``<action>--<execution_id>`` (aa8492c), so a
    review path resolved from that directory must match the recorded ``annotated_path``. When the
    skill recorded the path from the bare execution_id instead, every review failed with
    "review path does not match the current doublet evidence artifact"."""

    _evaluate, review = _handlers()
    current_evidence = review.__globals__["_current_evidence"]
    committed = "artifacts/capabilities/evaluate-doublet-evidence--exec-1/doublet-annotated.zarr"
    session_dir = tmp_path / "session"
    context = SimpleNamespace(
        session_dir=session_dir,
        state_facts={
            "analysis": {"cell_set": {"id": "cells-a"}},
            "doublets": {
                "evidence": {
                    "status": "complete",
                    "evidence_id": "doublets-a",
                    "cell_set_id": "cells-a",
                    "annotated_path": committed,
                }
            },
        },
    )
    review_path = (session_dir / committed).resolve()
    provenance = {"doublet_evidence_id": "doublets-a"}

    assert current_evidence(context, review_path, provenance)["evidence_id"] == "doublets-a"

    stale = (session_dir / "artifacts/capabilities/exec-1/doublet-annotated.zarr").resolve()
    with pytest.raises(ValueError, match="does not match the current doublet evidence"):
        current_evidence(context, stale, provenance)
