"""Evaluate independent scientific floors against durable session facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scagent_sdk.contracts.state import SessionState

# The restored three-axis cluster-QC evidence schema. Pre-restoration attestations lack this
# version (and the representation/evidence binding) and therefore fail the current-QC floor,
# forcing a rerun of the strengthened cluster QC before annotation or finalization.
CLUSTER_QC_EVIDENCE_SCHEMA = 3

# The gene-first batch-evidence schema. Legacy-shaped batch facts lack it and fail closed.
BATCH_EVIDENCE_SCHEMA = 1

@dataclass(frozen=True)
class FloorFailure:
    floor: str
    reason: str
    remediation: str


class FloorEvaluator:
    def evaluate(self, state: SessionState, floor: str) -> FloorFailure | None:
        facts = state.facts
        if floor == "dataset_identity":
            dataset = facts.get("dataset")
            if isinstance(dataset, dict) and dataset.get("fingerprint"):
                return None
            return FloorFailure(
                floor,
                "The input dataset has no durable byte-level identity.",
                "Run inspect_dataset on the active input first.",
            )
        if floor == "cellbender_input_suitable":
            dataset = facts.get("dataset")
            ambient = facts.get("ambient_background")
            validation = (
                ambient.get("input_validation") if isinstance(ambient, dict) else None
            )
            if (
                isinstance(dataset, dict)
                and isinstance(validation, dict)
                and validation.get("status") == "suitable"
                and validation.get("dataset_fingerprint") == dataset.get("fingerprint")
                and validation.get("input_path") == dataset.get("path")
            ):
                return None
            return FloorFailure(
                floor,
                "CellBender input suitability is absent or stale for the active dataset.",
                "Run validate_cellbender_input on the current raw droplet matrix.",
            )
        if floor == "current_doublet_evidence":
            analysis = facts.get("analysis")
            cell_set = analysis.get("cell_set") if isinstance(analysis, dict) else None
            count_representation = (
                analysis.get("count_representation") if isinstance(analysis, dict) else None
            )
            doublets = facts.get("doublets")
            evidence = doublets.get("evidence") if isinstance(doublets, dict) else None
            if (
                isinstance(cell_set, dict)
                and isinstance(count_representation, dict)
                and isinstance(evidence, dict)
                and evidence.get("status") == "complete"
                and evidence.get("cell_set_id") == cell_set.get("id")
                and evidence.get("count_representation_id") == count_representation.get("id")
                and evidence.get("evidence_id")
                and evidence.get("annotated_path")
            ):
                return None
            return FloorFailure(
                floor,
                "Doublet evidence is absent or stale for the current cells/counts.",
                "Run evaluate_doublet_evidence on the current prepared raw-count artifact.",
            )
        if floor == "current_cell_qc_review":
            cell_set_id = self._analysis_id(facts, "cell_set")
            count_representation_id = self._analysis_id(facts, "count_representation")
            evidence = facts.get("cell_qc")
            review = evidence.get("review") if isinstance(evidence, dict) else None
            if (
                cell_set_id
                and count_representation_id
                and isinstance(evidence, dict)
                and evidence.get("status") == "assessed"
                and evidence.get("assessment_id")
                and evidence.get("cell_set_id") == cell_set_id
                and evidence.get("count_representation_id") == count_representation_id
                and isinstance(review, dict)
                and review.get("status") == "resolved"
                and review.get("assessment_id") == evidence.get("assessment_id")
                and review.get("cell_set_id") == cell_set_id
                and review.get("count_representation_id") == count_representation_id
                and review.get("decision") in {"keep_all", "not_applicable"}
            ):
                return None
            return FloorFailure(
                floor,
                "Cell-level QC is absent, visually unreviewed, unresolved, or stale for the "
                "current cells and count representation.",
                "Run calculate_single_cell_qc, inspect every returned QC figure, then call "
                "review_single_cell_qc. If filtering is selected, filter, recalculate, and "
                "review QC on the retained cells.",
            )
        if floor == "current_cluster_qc":
            clustering_id = self._clustering_id(facts)
            cell_set_id = self._analysis_id(facts, "cell_set")
            representation_id = self._analysis_id(facts, "representation")
            count_representation_id = self._analysis_id(facts, "count_representation")
            evidence = facts.get("cluster_qc")
            review = evidence.get("review") if isinstance(evidence, dict) else None
            if (
                clustering_id
                and isinstance(evidence, dict)
                and evidence.get("status") == "attested"
                and evidence.get("evidence_schema_version") == CLUSTER_QC_EVIDENCE_SCHEMA
                and evidence.get("evidence_id")
                and evidence.get("clustering_id") == clustering_id
                and evidence.get("cell_set_id") == cell_set_id
                and evidence.get("representation_id") == representation_id
                and evidence.get("count_representation_id") == count_representation_id
                and isinstance(review, dict)
                and review.get("status") == "resolved"
                and review.get("evidence_id") == evidence.get("evidence_id")
                and review.get("clustering_id") == clustering_id
                and not review.get("unresolved_clusters")
            ):
                return None
            return FloorFailure(
                floor,
                "Three-axis cluster QC is absent, stale, visually unreviewed, or has unresolved "
                "cluster actions for the current analysis identities.",
                "Run evaluate_cluster_qc, inspect every required figure, then call "
                "review_cluster_qc. Apply any requested cleanup/reclustering before finalization.",
            )
        if floor == "current_batch_evidence":
            evidence = self._batch_evidence(facts)
            if (
                isinstance(evidence, dict)
                and evidence.get("status") in ("complete", "not_applicable")
                and evidence.get("schema_version") == BATCH_EVIDENCE_SCHEMA
                and evidence.get("evidence_id")
                and self._batch_identities_current(facts, evidence)
            ):
                return None
            return FloorFailure(
                floor,
                "Batch evidence is absent or stale for the current cells and counts.",
                "Run investigate_batch on the current clustering before deciding batch handling.",
            )
        if floor == "batch_decision":
            evidence = self._batch_evidence(facts)
            decision = self._batch_decision(facts)
            evidence_current = (
                isinstance(evidence, dict)
                and evidence.get("schema_version") == BATCH_EVIDENCE_SCHEMA
                and evidence.get("evidence_id")
                and self._batch_identities_current(facts, evidence)
            )
            if evidence_current and evidence.get("status") == "not_applicable":
                return None
            if (
                isinstance(decision, dict)
                and evidence_current
                and decision.get("decision")
                and decision.get("evidence_id") == evidence.get("evidence_id")
            ):
                return None
            return FloorFailure(
                floor,
                "No explicit batch-handling decision is recorded against current batch evidence "
                "for the active cells and counts.",
                "Run investigate_batch, ask the user once, then record keep_uncorrected, "
                "integrate, or separate. Not-applicable evidence needs no second decision call.",
            )
        if floor == "current_annotation_evidence":
            clustering_id = self._clustering_id(facts)
            annotation = facts.get("annotation")
            evidence = annotation.get("evidence") if isinstance(annotation, dict) else None
            review = annotation.get("review") if isinstance(annotation, dict) else None
            valid: dict[str, dict[str, Any]] = {}
            if isinstance(evidence, dict):
                valid = {
                    str(name): value
                    for name, value in evidence.items()
                    if isinstance(value, dict)
                    and value.get("clustering_id") == clustering_id
                    and value.get("status") == "complete"
                }
            independent = set(valid) - {"markers"}
            if (
                clustering_id
                and "markers" in valid
                and independent
                and isinstance(review, dict)
                and review.get("status") == "resolved"
                and review.get("clustering_id") == clustering_id
                and review.get("deg_primary") is True
                and set(review.get("methods_reviewed", [])) <= set(valid)
                and all(
                    review.get("evidence_ids", {}).get(method)
                    == valid[method].get("evidence_id")
                    for method in review.get("methods_reviewed", [])
                )
                and not review.get("unresolved_clusters")
            ):
                return None
            return FloorFailure(
                floor,
                "Current annotation needs DEG-primary evidence review, current independent "
                "reference evidence, and no unresolved clusters.",
                "Run marker evidence and suitable reference methods, inspect their agreement, "
                "then call review_annotation_evidence. Use two references when available or "
                "record a specific waiver.",
            )
        return FloorFailure(
            floor,
            f"Unknown scientific floor {floor!r}; failing closed.",
            "Correct the capability manifest or add a tested floor evaluator.",
        )

    def failures(self, state: SessionState, floors: tuple[str, ...]) -> tuple[FloorFailure, ...]:
        return tuple(
            failure for floor in floors if (failure := self.evaluate(state, floor)) is not None
        )

    @staticmethod
    def _clustering_id(facts: dict[str, Any]) -> str | None:
        return FloorEvaluator._analysis_id(facts, "clustering")

    @staticmethod
    def _analysis_id(facts: dict[str, Any], node: str) -> str | None:
        analysis = facts.get("analysis")
        entry = analysis.get(node) if isinstance(analysis, dict) else None
        value = entry.get("id") if isinstance(entry, dict) else None
        return str(value) if value else None

    @staticmethod
    def _batch_evidence(facts: dict[str, Any]) -> Any:
        batch = facts.get("batch")
        return batch.get("evidence") if isinstance(batch, dict) else None

    @staticmethod
    def _batch_decision(facts: dict[str, Any]) -> Any:
        batch = facts.get("batch")
        return batch.get("decision") if isinstance(batch, dict) else None

    @staticmethod
    def _batch_identities_current(facts: dict[str, Any], node: dict[str, Any]) -> bool:
        """A batch evidence/decision object is current only if the CELLS and COUNTS it was
        computed against still match the active analysis identities.

        Currency is deliberately keyed to the cell set and count representation, NOT to the
        downstream ``representation``/``clustering``. The batch judgement — whether these samples
        should be integrated — is a property of the cells and their counts, established once on the
        uncorrected pass. Integrating (which changes ``representation_id`` from X_pca to X_scVI) and
        re-clustering (at the annotation resolution) are *consequences* of that decision and must
        not invalidate it — otherwise integration would erase its own justification and force an
        expensive re-run of the gene-first diagnostic. A genuine change to the cell set (a cleanup
        removal) or to the counts still stales the evidence, as it should. Post-integration mixing
        is verified separately by ``score_integration``, not by re-deciding batch handling."""

        analysis = facts.get("analysis")
        if not isinstance(analysis, dict):
            return False
        for fact_key, id_key in (
            ("cell_set", "cell_set_id"),
            ("count_representation", "count_representation_id"),
        ):
            entry = analysis.get(fact_key)
            current = entry.get("id") if isinstance(entry, dict) else None
            recorded = node.get(id_key)
            if not current or not recorded or current != recorded:
                return False
        return True
