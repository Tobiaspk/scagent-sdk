from __future__ import annotations

from pathlib import Path

from scagent_sdk.floors import FloorEvaluator
from scagent_sdk.session import AnalysisSession


def _attested_cluster_qc(clustering_id: str) -> dict[str, object]:
    return {
        "status": "attested",
        "evidence_schema_version": 3,
        "evidence_id": "cluster-qc-evidence:sha256:abc",
        "clustering_id": clustering_id,
        "cell_set_id": "cells-a",
        "representation_id": "rep-a",
        "count_representation_id": "counts-a",
        "review": {
            "status": "resolved",
            "evidence_id": "cluster-qc-evidence:sha256:abc",
            "clustering_id": clustering_id,
            "unresolved_clusters": [],
        },
    }


def _seed_cluster_qc_analysis() -> dict[str, object]:
    return {
        "cell_set": {"id": "cells-a"},
        "count_representation": {"id": "counts-a"},
        "representation": {"id": "rep-a"},
        "clustering": {"id": "cluster-a"},
    }


def test_current_cluster_qc_refires_after_reclustering(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="floor")
    session.store.record(
        "test.seed",
        state_patch={
            "facts": {
                "analysis": _seed_cluster_qc_analysis(),
                "cluster_qc": _attested_cluster_qc("cluster-a"),
            }
        },
    )
    evaluator = FloorEvaluator()

    assert evaluator.evaluate(session.store.state, "current_cluster_qc") is None

    session.store.record(
        "test.recluster",
        state_patch={"facts": {"analysis": {"clustering": {"id": "cluster-b"}}}},
    )

    failure = evaluator.evaluate(session.store.state, "current_cluster_qc")
    assert failure is not None
    assert "stale" in failure.reason


def test_current_cluster_qc_stales_when_count_representation_changes(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="count-rep floor")
    session.store.record(
        "test.seed",
        state_patch={
            "facts": {
                "analysis": _seed_cluster_qc_analysis(),
                "cluster_qc": _attested_cluster_qc("cluster-a"),
            }
        },
    )
    evaluator = FloorEvaluator()
    assert evaluator.evaluate(session.store.state, "current_cluster_qc") is None

    session.store.record(
        "test.recount",
        state_patch={"facts": {"analysis": {"count_representation": {"id": "counts-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "current_cluster_qc") is not None


def test_current_cluster_qc_rejects_cleanup_applied_status(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="cleanup-applied floor")
    session.store.record(
        "test.seed",
        state_patch={
            "facts": {
                "analysis": _seed_cluster_qc_analysis(),
                # After a cleanup the fact is cleanup_applied (not attested): the floor must not
                # be satisfied — the model has to re-prepare, recluster, and re-run cluster QC.
                "cluster_qc": {
                    "status": "cleanup_applied",
                    "evidence_schema_version": 2,
                    "evidence_id": "cluster-qc-evidence:sha256:abc",
                },
            }
        },
    )
    assert FloorEvaluator().evaluate(session.store.state, "current_cluster_qc") is not None


def test_current_cluster_qc_rejects_pre_restoration_attestation(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="legacy attestation")
    session.store.record(
        "test.legacy",
        state_patch={
            "facts": {
                "analysis": {
                    "cell_set": {"id": "cells-a"},
                    "representation": {"id": "rep-a"},
                    "clustering": {"id": "cluster-a"},
                },
                # Old shape: no evidence_schema_version, representation binding, or evidence_id.
                "cluster_qc": {"status": "attested", "clustering_id": "cluster-a"},
            }
        },
    )
    assert FloorEvaluator().evaluate(session.store.state, "current_cluster_qc") is not None


def test_cellbender_suitability_is_bound_to_current_dataset_identity(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="cellbender floor")
    evaluator = FloorEvaluator()
    session.store.record(
        "test.validation",
        state_patch={
            "facts": {
                "dataset": {"path": "/data/raw.h5", "fingerprint": "sha256:raw-a"},
                "ambient_background": {
                    "input_validation": {
                        "status": "suitable",
                        "input_path": "/data/raw.h5",
                        "dataset_fingerprint": "sha256:raw-a",
                    }
                },
            }
        },
    )

    assert evaluator.evaluate(session.store.state, "cellbender_input_suitable") is None

    session.store.record(
        "test.dataset_changed",
        state_patch={"facts": {"dataset": {"fingerprint": "sha256:raw-b"}}},
    )

    failure = evaluator.evaluate(session.store.state, "cellbender_input_suitable")
    assert failure is not None
    assert "stale" in failure.reason


def test_cellbender_suitability_rejects_different_path_and_unsuitable_status(
    tmp_path: Path,
) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="cellbender refusal")
    evaluator = FloorEvaluator()
    session.store.record(
        "test.validation",
        state_patch={
            "facts": {
                "dataset": {"path": "/data/raw.h5", "fingerprint": "sha256:raw"},
                "ambient_background": {
                    "input_validation": {
                        "status": "unsuitable",
                        "input_path": "/data/filtered.h5",
                        "dataset_fingerprint": "sha256:raw",
                    }
                },
            }
        },
    )

    assert evaluator.evaluate(session.store.state, "cellbender_input_suitable") is not None


def test_doublet_evidence_is_bound_to_current_cells_and_counts(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="doublet floor")
    evaluator = FloorEvaluator()
    session.store.record(
        "test.doublets",
        state_patch={
            "facts": {
                "analysis": {
                    "cell_set": {"id": "cells-a"},
                    "count_representation": {"id": "counts-a"},
                },
                "doublets": {
                    "evidence": {
                        "status": "complete",
                        "evidence_id": "doublets-a",
                        "cell_set_id": "cells-a",
                        "count_representation_id": "counts-a",
                        "annotated_path": "/data/doublets.h5ad",
                    }
                },
            }
        },
    )

    assert evaluator.evaluate(session.store.state, "current_doublet_evidence") is None
    session.store.record(
        "test.new_cells",
        state_patch={"facts": {"analysis": {"cell_set": {"id": "cells-b"}}}},
    )
    failure = evaluator.evaluate(session.store.state, "current_doublet_evidence")
    assert failure is not None
    assert "stale" in failure.reason


def test_annotation_requires_markers_and_independent_current_evidence(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="annotation floor")
    evaluator = FloorEvaluator()
    session.store.record(
        "test.markers",
        state_patch={
            "facts": {
                "analysis": {"clustering": {"id": "cluster-a"}},
                "annotation": {
                    "evidence": {
                        "markers": {
                            "status": "complete",
                            "clustering_id": "cluster-a",
                            "evidence_id": "markers-a",
                        }
                    }
                },
            }
        },
    )
    assert evaluator.evaluate(session.store.state, "current_annotation_evidence") is not None

    session.store.record(
        "test.reference",
        state_patch={
            "facts": {
                "annotation": {
                    "evidence": {
                        "celltypist": {
                            "status": "complete",
                            "clustering_id": "cluster-a",
                            "evidence_id": "celltypist-a",
                        }
                    }
                }
            }
        },
    )
    assert evaluator.evaluate(session.store.state, "current_annotation_evidence") is not None

    session.store.record(
        "test.annotation_review",
        state_patch={
            "facts": {
                "annotation": {
                    "review": {
                        "status": "resolved",
                        "clustering_id": "cluster-a",
                        "deg_primary": True,
                        "methods_reviewed": ["markers", "celltypist"],
                        "evidence_ids": {
                            "markers": "markers-a",
                            "celltypist": "celltypist-a",
                        },
                        "unresolved_clusters": [],
                    }
                }
            }
        },
    )
    assert evaluator.evaluate(session.store.state, "current_annotation_evidence") is None


def test_cell_qc_review_is_bound_to_current_cells_and_counts(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="cell qc review")
    session.store.record(
        "test.qc",
        state_patch={
            "facts": {
                "analysis": {
                    "cell_set": {"id": "cells-a"},
                    "count_representation": {"id": "counts-a"},
                },
                "cell_qc": {
                    "status": "assessed",
                    "assessment_id": "qc-a",
                    "cell_set_id": "cells-a",
                    "count_representation_id": "counts-a",
                    "review": {
                        "status": "resolved",
                        "assessment_id": "qc-a",
                        "decision": "keep_all",
                        "cell_set_id": "cells-a",
                        "count_representation_id": "counts-a",
                    },
                },
            }
        },
    )
    evaluator = FloorEvaluator()
    assert evaluator.evaluate(session.store.state, "current_cell_qc_review") is None
    session.store.record(
        "test.recount",
        state_patch={
            "facts": {"analysis": {"count_representation": {"id": "counts-b"}}}
        },
    )
    assert evaluator.evaluate(session.store.state, "current_cell_qc_review") is not None


def _seed_analysis_identities(session: AnalysisSession, *, clustering_id: str) -> None:
    session.store.record(
        "test.analysis",
        state_patch={
            "facts": {
                "analysis": {
                    "cell_set": {"id": "cells-a"},
                    "count_representation": {"id": "counts-a"},
                    "representation": {"id": "rep-a"},
                    "clustering": {"id": clustering_id},
                }
            }
        },
    )


def _batch_ids(clustering_id: str) -> dict[str, str]:
    return {
        "cell_set_id": "cells-a",
        "count_representation_id": "counts-a",
        "representation_id": "rep-a",
        "clustering_id": clustering_id,
    }


def _record_batch(
    session: AnalysisSession,
    *,
    decision: str | None,
    clustering_id: str,
    evidence_id: str = "batch-evidence:e1",
    decision_evidence_id: str | None = None,
    status: str = "complete",
    recommendation: str = "do_not_integrate_based_on_current_evidence",
) -> None:
    ids = _batch_ids(clustering_id)
    evidence = {
        "schema_version": 1,
        "status": status,
        "evidence_id": evidence_id,
        "recommendation": recommendation,
        **ids,
    }
    decision_fact = (
        {
            "decision": decision,
            "evidence_id": decision_evidence_id or evidence_id,
            "rationale": "test choice",
        }
        if decision is not None
        else None
    )
    session.store.record(
        "test.batch",
        state_patch={"facts": {"batch": {"evidence": evidence, "decision": decision_fact}}},
    )


def test_current_batch_evidence_survives_reclustering_and_stales_on_cell_set(
    tmp_path: Path,
) -> None:
    # The batch judgement is keyed to the cells/counts, not the downstream representation or
    # clustering: re-clustering (or integrating) must NOT stale it, but a cell-set change must.
    session = AnalysisSession.create(tmp_path / "sessions", title="batch evidence floor")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    _record_batch(session, decision=None, clustering_id="cluster-a")
    assert evaluator.evaluate(session.store.state, "current_batch_evidence") is None

    session.store.record(
        "test.recluster",
        state_patch={"facts": {"analysis": {"clustering": {"id": "cluster-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "current_batch_evidence") is None

    session.store.record(
        "test.integrate",
        state_patch={"facts": {"analysis": {"representation": {"id": "rep-scvi"}}}},
    )
    assert evaluator.evaluate(session.store.state, "current_batch_evidence") is None

    session.store.record(
        "test.cleanup",
        state_patch={"facts": {"analysis": {"cell_set": {"id": "cells-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "current_batch_evidence") is not None


def test_batch_decision_survives_integration_and_reclustering(
    tmp_path: Path,
) -> None:
    # A once-made integrate/keep decision must carry through integration (representation change)
    # and the annotation re-clustering to finalization, without a forced re-decision.
    session = AnalysisSession.create(tmp_path / "sessions", title="batch decision floor")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    _record_batch(session, decision="keep_uncorrected", clustering_id="cluster-a")

    assert evaluator.evaluate(session.store.state, "batch_decision") is None

    session.store.record(
        "test.integrate",
        state_patch={"facts": {"analysis": {"representation": {"id": "rep-scvi"}}}},
    )
    session.store.record(
        "test.recluster",
        state_patch={"facts": {"analysis": {"clustering": {"id": "cluster-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "batch_decision") is None


def test_current_batch_evidence_rejects_legacy_schema(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="legacy batch evidence")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    session.store.record(
        "test.legacy_batch",
        state_patch={
            "facts": {
                "batch": {
                    # Pre-restoration shape: no schema_version.
                    "evidence": {
                        "status": "complete",
                        "evidence_id": "batch-evidence:e1",
                        **_batch_ids("cluster-a"),
                    },
                    "decision": None,
                }
            }
        },
    )
    assert evaluator.evaluate(session.store.state, "current_batch_evidence") is not None


def test_batch_decision_requires_matching_evidence_id(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="batch evidence-id floor")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    # Decision references a different evidence id than the current evidence.
    _record_batch(
        session,
        decision="keep_uncorrected",
        clustering_id="cluster-a",
        evidence_id="batch-evidence:e1",
        decision_evidence_id="batch-evidence:OLD",
    )
    assert evaluator.evaluate(session.store.state, "batch_decision") is not None


def test_batch_decision_stales_when_cell_set_changes(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="batch cell-set floor")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    _record_batch(session, decision="separate", clustering_id="cluster-a")
    assert evaluator.evaluate(session.store.state, "batch_decision") is None

    session.store.record(
        "test.filter",
        state_patch={"facts": {"analysis": {"cell_set": {"id": "cells-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "batch_decision") is not None


def test_not_applicable_current_evidence_needs_no_second_decision(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="batch not-applicable floor")
    evaluator = FloorEvaluator()
    # A bare identity-free claim still fails closed.
    session.store.record(
        "test.na_bare",
        state_patch={"facts": {"batch": {"evidence": {"status": "not_applicable"}}}},
    )
    assert evaluator.evaluate(session.store.state, "batch_decision") is not None

    _seed_analysis_identities(session, clustering_id="cluster-a")
    _record_batch(session, decision=None, clustering_id="cluster-a", status="not_applicable")
    assert evaluator.evaluate(session.store.state, "batch_decision") is None


def test_integration_authorized_requires_current_integrate_choice(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="integration floor")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")

    _record_batch(session, decision="keep_uncorrected", clustering_id="cluster-a")
    assert evaluator.evaluate(session.store.state, "integration_authorized") is not None

    _record_batch(session, decision="integrate", clustering_id="cluster-a")
    assert evaluator.evaluate(session.store.state, "integration_authorized") is None

    # Re-clustering on the (integrated) representation does not revoke the authorization: it is
    # keyed to the cells/counts, not the clustering. A cell-set change (a cleanup) does.
    session.store.record(
        "test.recluster",
        state_patch={"facts": {"analysis": {"clustering": {"id": "cluster-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "integration_authorized") is None

    session.store.record(
        "test.cleanup",
        state_patch={"facts": {"analysis": {"cell_set": {"id": "cells-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "integration_authorized") is not None
