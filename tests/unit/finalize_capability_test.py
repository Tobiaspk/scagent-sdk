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
        if package.manifest.skill_id == "finalize-analysis"
    )


def _handler() -> Any:
    package = _package()
    tool = next(tool for tool in package.manifest.tools if tool.name == "finalize_analysis")
    return package.load_handler(tool)


def _context(tmp_path: Path, clustering_id: str = "clusters-a") -> Any:
    staging = tmp_path / "staging"
    staging.mkdir()
    return SimpleNamespace(
        staging_dir=staging,
        session_dir=tmp_path / "session",
        execution_id="execution-1",
        state_facts={"analysis": {"clustering": {"id": clustering_id}}},
    )


def _valid_contract(clusters: set[str]) -> dict[str, dict[str, str]]:
    return {
        "labels": {cluster: f"type-{cluster}" for cluster in clusters},
        "rationales": {cluster: f"rationale {cluster}" for cluster in clusters},
        "deg_labels": {cluster: f"type-{cluster}" for cluster in clusters},
        "evidence_summaries": {cluster: f"markers {cluster}" for cluster in clusters},
        "confidence": {cluster: "high" for cluster in clusters},
        "overrides": {},
    }


# --- manifest contract ------------------------------------------------------


def test_manifest_is_gpu_routed_ungated_and_labels_only_required() -> None:
    package = _package()
    tool = next(tool for tool in package.manifest.tools if tool.name == "finalize_analysis")
    assert tool.environment == "gpu-singlecell"
    assert tool.floors == ()
    assert tool.input_schema["additionalProperties"] is False
    # ``path`` is the declared matrix input and therefore optional; finalization runs against the
    # active lineage artifact unless one is named explicitly.
    assert tool.primary_matrix_input == "path"
    assert tool.primary_matrix_output == "final-annotated-anndata"
    assert set(tool.input_schema["required"]) == {"labels"}


# --- pure label contract ----------------------------------------------------


def _validator() -> Any:
    return _handler().__globals__["_validate_label_contract"]


def test_complete_matching_maps_pass() -> None:
    clusters = {"0", "1", "2"}
    _validator()(clusters=clusters, **_valid_contract(clusters))


def test_missing_cluster_is_rejected() -> None:
    clusters = {"0", "1", "2"}
    contract = _valid_contract(clusters)
    contract["labels"].pop("2")
    with pytest.raises(ValueError, match="must cover exactly the current clusters"):
        _validator()(clusters=clusters, **contract)


def test_extra_unknown_cluster_is_rejected() -> None:
    clusters = {"0", "1"}
    contract = _valid_contract(clusters)
    for mapping in contract.values():
        if isinstance(mapping, dict) and mapping:
            mapping["9"] = "ghost"
    with pytest.raises(ValueError, match="extra="):
        _validator()(clusters=clusters, **contract)


def test_empty_label_value_is_rejected() -> None:
    clusters = {"0", "1"}
    contract = _valid_contract(clusters)
    contract["labels"]["0"] = "  "
    with pytest.raises(ValueError, match="labels values must not be empty"):
        _validator()(clusters=clusters, **contract)


def test_bad_confidence_enum_is_rejected() -> None:
    clusters = {"0", "1"}
    contract = _valid_contract(clusters)
    contract["confidence"]["0"] = "certain"
    with pytest.raises(ValueError, match="confidence values must be high, medium, or low"):
        _validator()(clusters=clusters, **contract)


def test_final_vs_deg_mismatch_does_not_require_string_override() -> None:
    clusters = {"0", "1"}
    contract = _valid_contract(clusters)
    contract["labels"]["0"] = "pDC"
    contract["deg_labels"]["0"] = "plasma cell"
    _validator()(clusters=clusters, **contract)


def test_override_for_unknown_cluster_is_rejected() -> None:
    clusters = {"0", "1"}
    contract = _valid_contract(clusters)
    contract["overrides"] = {"9": "justification for a cluster that does not exist"}
    with pytest.raises(ValueError, match="overrides contains unknown clusters"):
        _validator()(clusters=clusters, **contract)


def test_empty_override_justification_is_rejected() -> None:
    clusters = {"0", "1"}
    contract = _valid_contract(clusters)
    contract["labels"]["0"] = "pDC"
    contract["deg_labels"]["0"] = "plasma cell"
    contract["overrides"] = {"0": "   "}
    with pytest.raises(ValueError, match="overrides values must not be empty"):
        _validator()(clusters=clusters, **contract)


# --- pure input-state contract (non-overwrite + staleness) ------------------


def _input_validator() -> Any:
    return _handler().__globals__["_validate_inputs"]


def _parsed(clusters: set[str]) -> dict[str, Any]:
    contract = _valid_contract(clusters)
    return {
        "cluster_key": "leiden",
        "label_key": "cell_type",
        "summary": "summary",
        "caveats": [],
        **contract,
    }


def test_absent_cluster_key_is_rejected() -> None:
    clusters = {"0", "1"}
    with pytest.raises(ValueError, match="cluster key 'leiden' is absent"):
        _input_validator()(
            parsed=_parsed(clusters),
            clusters=clusters,
            existing_columns={"total_counts"},
            input_clustering_id="clusters-a",
            current_clustering_id="clusters-a",
        )


def test_existing_label_key_is_not_overwritten() -> None:
    clusters = {"0", "1"}
    with pytest.raises(ValueError, match="refusing to overwrite"):
        _input_validator()(
            parsed=_parsed(clusters),
            clusters=clusters,
            existing_columns={"leiden", "cell_type"},
            input_clustering_id="clusters-a",
            current_clustering_id="clusters-a",
        )


def test_stale_clustering_identity_is_rejected() -> None:
    clusters = {"0", "1"}
    with pytest.raises(ValueError, match="clustering identity is stale"):
        _input_validator()(
            parsed=_parsed(clusters),
            clusters=clusters,
            existing_columns={"leiden"},
            input_clustering_id="clusters-OLD",
            current_clustering_id="clusters-a",
        )


def test_missing_current_clustering_identity_is_rejected() -> None:
    clusters = {"0", "1"}
    with pytest.raises(ValueError, match="clustering identity is stale"):
        _input_validator()(
            parsed=_parsed(clusters),
            clusters=clusters,
            existing_columns={"leiden"},
            input_clustering_id=None,
            current_clustering_id=None,
        )


def test_fresh_and_complete_inputs_pass() -> None:
    clusters = {"0", "1"}
    _input_validator()(
        parsed=_parsed(clusters),
        clusters=clusters,
        existing_columns={"leiden", "total_counts"},
        input_clustering_id="clusters-a",
        current_clustering_id="clusters-a",
    )


def test_labels_only_contract_gets_lightweight_defaults() -> None:
    resolve = _handler().__globals__["_resolve_arguments"]
    parsed = resolve({"labels": {"0": "T cell", "1": "unknown"}})
    assert parsed["summary"] == "Finalized cluster annotation."
    assert parsed["rationales"] == {}
    assert parsed["confidence"] == {}


def test_durable_decision_provenance_distinguishes_current_stale_and_unbound() -> None:
    provenance = _handler().__globals__["_durable_decision_provenance"]
    analysis = {
        "cell_set": {"id": "cells-a"},
        "count_representation": {"id": "counts-a"},
    }
    assert "matches" in provenance(
        {"cell_set_id": "cells-a", "count_representation_id": "counts-a"}, analysis
    )
    assert "stale" in provenance({"cell_set_id": "cells-old"}, analysis)
    assert "unavailable" in provenance({"decision": "integrate"}, analysis)


def test_graph_provenance_uses_graph_source_not_latest_embedding() -> None:
    graph_provenance = _handler().__globals__["_graph_provenance"]
    adata = SimpleNamespace(uns={"neighbors": {"params": {"use_rep": "X_scVI"}}})
    result = graph_provenance(
        {
            "representation": {
                "id": "pca-latest",
                "key": "X_pca",
                "neighbor_graph_id": "graph-a",
            }
        },
        adata,
    )
    assert result == {"neighbor_graph_id": "graph-a", "representation_key": "X_scVI"}


def test_summary_cannot_restate_structured_elbo_values() -> None:
    validate = _handler().__globals__["_validate_summary_metrics"]
    facts = {"analysis": {"scvi_training": {"final_train_elbo": 12.5}}}
    with pytest.raises(ValueError, match="must not manually transcribe ELBO"):
        validate("Final train ELBO 13", facts)
    validate("The scVI curves stabilized without obvious divergence.", facts)


def test_report_recovers_recorded_qc_and_batch_decisions_from_durable_state() -> None:
    render = _handler().__globals__["_render_report"]
    row = SimpleNamespace(
        cluster="0",
        deg_label="T cell",
        cell_type="T cell",
        confidence="high",
        n_cells=10,
        override_justification="",
    )

    class _Table:
        def __len__(self) -> int:
            return 1

        def sort_values(self, _key: str) -> Any:
            return self

        def itertuples(self, *, index: bool) -> list[Any]:
            assert index is False
            return [row]

    adata = SimpleNamespace(
        n_obs=10,
        n_vars=20,
        uns={"neighbors": {"params": {"use_rep": "X_scVI"}}},
    )
    report = render(
        summary="Summary.",
        facts={
            "analysis": {
                "cell_set": {"id": "cells-a"},
                "count_representation": {"id": "counts-a"},
                "clustering": {"id": "clusters-a", "resolution": 1.0},
                "scvi_training": {
                    "epochs_trained": 8,
                    "max_epochs": 10,
                    "early_stopping_enabled": True,
                    "early_stopped": True,
                    "final_train_elbo": 12.25,
                    "final_validation_elbo": 12.0,
                    "best_validation_elbo": 11.5,
                    "best_validation_epoch": 7,
                },
            }
        },
        decisions={
            "cell_qc_handling": {
                "decision": "keep_all",
                "rationale": "Reviewed distributions.",
                "cell_set_id": "cells-a",
                "count_representation_id": "counts-a",
            },
            "batch_handling": {
                "decision": "integrate",
                "rationale": "User selected correction.",
                "cell_set_id": "cells-a",
                "count_representation_id": "counts-a",
            },
        },
        history=[],
        path=Path("/tmp/input.zarr"),
        adata=adata,
        cluster_key="leiden",
        label_key="cell_type",
        clustering_id="clusters-a",
        table=_Table(),
        rationales={"0": ""},
        evidence_summaries={"0": ""},
        caveats=[],
    )
    assert "QC decision: `keep_all`" in report
    assert "Decision: `integrate`" in report
    assert report.count("identity binding matches current lineage") == 2
    assert "Final train ELBO: `12.250000`" in report
    assert "Best validation ELBO: `11.500000` at epoch `7`" in report


# --- envelope assembly ------------------------------------------------------


def test_successful_envelope_emits_state_and_artifacts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    handler = _handler()
    context = _context(tmp_path)
    labels = {"0": "T cell", "1": "pDC"}
    payload = {
        "clustering_id": "clusters-a",
        "cluster_key": "leiden",
        "label_key": "cell_type",
        "labels": labels,
        "rationales": {"0": "CD3D", "1": "LILRA4"},
        "deg_labels": {"0": "T cell", "1": "pDC"},
        "evidence_summaries": {"0": "markers", "1": "markers"},
        "confidence": {"0": "high", "1": "medium"},
        "overrides": {},
        "caveats": [],
        "final_path": "artifacts/capabilities/execution-1/final-annotated.h5ad",
        "report_path": "artifacts/capabilities/execution-1/analysis-report.md",
        "n_obs": 1000,
        "n_clusters": 2,
        "artifacts": [
            {
                "name": "final-annotated-anndata",
                "relative_path": "final-annotated.h5ad",
                "media_type": "application/x-hdf5",
            }
        ],
        "model_media": [],
    }
    monkeypatch.setitem(
        handler.__globals__, "_execute_finalization", lambda _arguments, _context: payload
    )

    result = handler({"path": "/data/prepared.h5ad", "labels": labels}, context)

    final = result["facts_patch"]["finalization"]
    assert final["status"] == "complete"
    assert final["clustering_id"] == "clusters-a"
    assert result["facts_patch"]["annotation"]["final"]["labels"] == labels
    assert result["decisions_patch"]["final_labels"] == labels
    assert result["artifacts"][0]["relative_path"] == "final-annotated.h5ad"
    assert "Finalized 2 cluster labels across 1,000 cells." == result["summary"]
