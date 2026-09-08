"""Publish a complete label mapping for the current clustered artifact."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recipe  # noqa: E402  (sibling module; path inserted above)


def _read_matrix(path):
    """Read an AnnData artifact, tolerating both .h5ad files and .zarr stores (ADR 0011)."""
    import anndata as ad

    return ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)


def _exact_mapping(mapping: dict[str, str], clusters: set[str], name: str) -> None:
    if set(mapping) != clusters:
        missing = sorted(clusters - set(mapping))
        extra = sorted(set(mapping) - clusters)
        raise ValueError(
            f"{name} must cover exactly the current clusters; "
            f"missing={missing}, extra={extra}"
        )
    if not all(value.strip() for value in mapping.values()):
        raise ValueError(f"{name} values must not be empty")


def _validate_label_contract(
    *,
    clusters: set[str],
    labels: dict[str, str],
    rationales: dict[str, str],
    deg_labels: dict[str, str],
    evidence_summaries: dict[str, str],
    confidence: dict[str, str],
    overrides: dict[str, str],
) -> None:
    _exact_mapping(labels, clusters, "labels")
    for name, mapping in (
        ("rationales", rationales),
        ("deg_labels", deg_labels),
        ("evidence_summaries", evidence_summaries),
        ("confidence", confidence),
        ("overrides", overrides),
    ):
        unknown = sorted(set(mapping) - clusters)
        if unknown:
            raise ValueError(f"{name} contains unknown clusters: {unknown}")
        if not all(value.strip() for value in mapping.values()):
            raise ValueError(f"{name} values must not be empty")
    if not set(confidence.values()).issubset({"high", "medium", "low"}):
        raise ValueError("confidence values must be high, medium, or low")


def _resolve_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize and range-check the label contract without touching the AnnData."""

    cluster_key = str(arguments.get("cluster_key", "leiden"))
    label_key = str(arguments.get("label_key", "cell_type"))
    labels = {str(key): str(value).strip() for key, value in arguments["labels"].items()}
    rationales = {
        str(key): str(value).strip()
        for key, value in arguments.get("rationales", {}).items()
    }
    deg_labels = {
        str(key): str(value).strip()
        for key, value in arguments.get("deg_labels", {}).items()
    }
    evidence_summaries = {
        str(key): str(value).strip()
        for key, value in arguments.get("evidence_summaries", {}).items()
    }
    confidence = {
        str(key): str(value).strip()
        for key, value in arguments.get("confidence", {}).items()
    }
    overrides = {
        str(key): str(value).strip() for key, value in arguments.get("overrides", {}).items()
    }
    summary = str(arguments.get("analysis_summary", "Finalized cluster annotation.")).strip()
    caveats = [str(item) for item in arguments.get("caveats", [])]
    if not summary:
        summary = "Finalized cluster annotation."
    return {
        "cluster_key": cluster_key,
        "label_key": label_key,
        "labels": labels,
        "rationales": rationales,
        "deg_labels": deg_labels,
        "evidence_summaries": evidence_summaries,
        "confidence": confidence,
        "overrides": overrides,
        "summary": summary,
        "caveats": caveats,
    }


def _validate_inputs(
    *,
    parsed: dict[str, Any],
    clusters: set[str],
    existing_columns: set[str],
    input_clustering_id: Any,
    current_clustering_id: Any,
) -> None:
    """All non-AnnData-writing validation: presence, non-overwrite, labels, and identity."""

    cluster_key = parsed["cluster_key"]
    label_key = parsed["label_key"]
    if cluster_key not in existing_columns:
        raise ValueError(f"cluster key {cluster_key!r} is absent")
    if label_key in existing_columns:
        raise ValueError(f"refusing to overwrite existing observation column {label_key!r}")
    _validate_label_contract(
        clusters=clusters,
        labels=parsed["labels"],
        rationales=parsed["rationales"],
        deg_labels=parsed["deg_labels"],
        evidence_summaries=parsed["evidence_summaries"],
        confidence=parsed["confidence"],
        overrides=parsed["overrides"],
    )
    if not current_clustering_id or input_clustering_id != current_clustering_id:
        raise ValueError(
            "input AnnData clustering identity is stale or does not match durable state: "
            f"input={input_clustering_id!r}, current={current_clustering_id!r}"
        )


def _capability_history(session_dir: Path) -> list[dict[str, Any]]:
    state_path = session_dir / "state.json"
    if not state_path.is_file():
        return []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    artifacts = state.get("artifacts", {})
    if not isinstance(artifacts, dict):
        return []
    sequences: dict[str, int] = {}
    events_path = session_dir / "events.jsonl"
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("kind") == "capability.result_committed":
                execution_id = event.get("payload", {}).get("execution_id")
                if isinstance(execution_id, str):
                    sequences[execution_id] = int(event.get("sequence", 0))
    history: list[dict[str, Any]] = []
    for execution_id, raw in artifacts.items():
        if not isinstance(raw, dict):
            continue
        history.append(
            {
                "sequence": sequences.get(str(execution_id), 0),
                "execution_id": str(execution_id),
                "skill_id": raw.get("skill_id"),
                "skill_version": raw.get("skill_version"),
                "tool_name": raw.get("tool_name"),
                "arguments": raw.get("arguments", {}),
                "summary": raw.get("summary", ""),
                "files": raw.get("files", []),
            }
        )
    return sorted(history, key=lambda item: (item["sequence"], item["execution_id"]))


def _auto_caveats(facts: dict[str, Any], confidence: dict[str, str]) -> list[str]:
    caveats: list[str] = []
    cell_qc = facts.get("cell_qc")
    if isinstance(cell_qc, dict):
        flags = cell_qc.get("flag_counts", {})
        flagged = flags.get("any_requested_flag") if isinstance(flags, dict) else None
        if isinstance(flagged, int) and flagged:
            caveats.append(
                f"Cell-level QC flagged {flagged:,} cells at the recorded thresholds; "
                "the saved QC review explains why the retained cell set was accepted."
            )
    cluster_qc = facts.get("cluster_qc")
    if isinstance(cluster_qc, dict) and cluster_qc.get("warnings"):
        # Only claim a review happened when it resolves this exact evidence object.
        review = cluster_qc.get("review")
        review_is_current = (
            isinstance(review, dict)
            and review.get("status") == "resolved"
            and review.get("evidence_id") == cluster_qc.get("evidence_id")
            and review.get("clustering_id") == cluster_qc.get("clustering_id")
        )
        if review_is_current:
            caveats.append(
                "Cluster QC emitted warnings that were visually reviewed; consult the cluster-QC "
                "report and review rationales before reusing fine-grained labels."
            )
        else:
            caveats.append(
                "Cluster QC emitted warnings and no current resolved review is on record for "
                "this clustering; consult the cluster-QC report before reusing these labels."
            )
    annotation = facts.get("annotation")
    review = annotation.get("review") if isinstance(annotation, dict) else None
    if isinstance(review, dict):
        if review.get("reference_waiver"):
            caveats.append(
                "Only one independent annotation reference was used: "
                + str(review["reference_waiver"])
            )
        elif not review.get("deg_primary"):
            # Not a floor -- the marker skill allows marker-free review -- but the reader must be
            # told the labels rest on reference inference with no registered DEG evidence.
            caveats.append(
                "Final labels were reviewed without registered marker/DEG evidence and without a "
                "recorded reference_waiver; the label basis is reference inference alone. "
                "Reviewed methods: "
                + (", ".join(str(m) for m in review.get("methods_reviewed", [])) or "none")
                + "."
            )
    evidence = annotation.get("evidence") if isinstance(annotation, dict) else None
    analysis = facts.get("analysis")
    cell_set = analysis.get("cell_set") if isinstance(analysis, dict) else None
    clustering = analysis.get("clustering") if isinstance(analysis, dict) else None
    current_cell_set_id = cell_set.get("id") if isinstance(cell_set, dict) else None
    current_clustering_id = clustering.get("id") if isinstance(clustering, dict) else None
    evidence_items = evidence.items() if isinstance(evidence, dict) else ()
    registered_methods = {
        str(method)
        for method, item in evidence_items
        if isinstance(item, dict)
        and item.get("status") == "complete"
        and (current_cell_set_id is None or item.get("cell_set_id") == current_cell_set_id)
        and (
            current_clustering_id is None
            or item.get("clustering_id") == current_clustering_id
        )
    }
    reference_runs = facts.get("reference_runs")
    if isinstance(reference_runs, dict):
        unregistered = sorted(
            str(method)
            for method, runs in reference_runs.items()
            if str(method) in {"celltypist", "scimilarity"}
            and isinstance(runs, dict)
            and any(
                isinstance(run, dict)
                and run.get("status") == "complete"
                and (
                    current_cell_set_id is None
                    or run.get("cell_set_id") == current_cell_set_id
                )
                for run in runs.values()
            )
            and str(method) not in registered_methods
        )
        if unregistered:
            caveats.append(
                "Reference method(s) completed but were never registered as cluster-level "
                "annotation evidence, so they did not inform the recorded review: "
                + ", ".join(unregistered)
                + ". Their per-cell calls are on the artifact; summarize them by cluster to "
                "register that evidence."
            )
    low = sorted(cluster for cluster, value in confidence.items() if value == "low")
    if low:
        caveats.append("Low-confidence final labels remain for clusters: " + ", ".join(low))
    return caveats


def _durable_decision_provenance(
    decision: dict[str, Any], analysis: dict[str, Any]
) -> str:
    """Describe identity currency without pretending an unbound decision is current."""

    bindings = (
        ("cell_set_id", "cell_set"),
        ("count_representation_id", "count_representation"),
    )
    compared = 0
    for decision_key, analysis_key in bindings:
        recorded = decision.get(decision_key)
        current_node = analysis.get(analysis_key)
        current = current_node.get("id") if isinstance(current_node, dict) else None
        if recorded is None or current is None:
            continue
        compared += 1
        if recorded != current:
            return "durable decision; stale for current lineage"
    if compared:
        return "durable decision; identity binding matches current lineage"
    return "durable decision; current-lineage evidence binding unavailable"


def _graph_provenance(analysis: dict[str, Any], adata: Any) -> dict[str, Any]:
    """Resolve the graph source separately from the latest computed embedding."""

    representation = analysis.get("representation", {})
    clustering = analysis.get("clustering", {})
    use_rep = None
    graph_id = None
    if isinstance(clustering, dict):
        use_rep = clustering.get("representation_key")
        graph_id = clustering.get("neighbor_graph_id")
    if isinstance(representation, dict):
        use_rep = use_rep or representation.get("neighbor_graph_representation_key")
        graph_id = graph_id or representation.get("neighbor_graph_id")
    neighbors = adata.uns.get("neighbors", {}) if hasattr(adata, "uns") else {}
    params = neighbors.get("params", {}) if isinstance(neighbors, dict) else {}
    if isinstance(params, dict):
        use_rep = use_rep or params.get("use_rep")
    return {"neighbor_graph_id": graph_id, "representation_key": use_rep}


def _format_metric(value: Any) -> str:
    return "not recorded" if value is None else f"{float(value):.6f}"


def _validate_summary_metrics(summary: str, facts: dict[str, Any]) -> None:
    """Keep model narrative from duplicating authoritative structured training numbers."""

    analysis = facts.get("analysis", {})
    if (
        isinstance(analysis, dict)
        and isinstance(analysis.get("scvi_training"), dict)
        and "elbo" in summary.casefold()
    ):
        raise ValueError(
            "analysis_summary must not manually transcribe ELBO values; finalization renders "
            "the exact structured scVI training metrics automatically"
        )


def _render_report(
    *,
    summary: str,
    facts: dict[str, Any],
    decisions: dict[str, Any],
    history: list[dict[str, Any]],
    path: Path,
    adata: Any,
    cluster_key: str,
    label_key: str,
    clustering_id: str,
    table: Any,
    rationales: dict[str, str],
    evidence_summaries: dict[str, str],
    caveats: list[str],
) -> str:
    dataset = facts.get("dataset", {})
    analysis = facts.get("analysis", {})
    clustering = analysis.get("clustering", {}) if isinstance(analysis, dict) else {}
    cell_qc = facts.get("cell_qc", {})
    doublets = facts.get("doublets", {})
    cluster_qc = facts.get("cluster_qc", {})
    batch = facts.get("batch", {})
    annotation = facts.get("annotation", {})
    graph = _graph_provenance(analysis, adata)
    scvi_training = analysis.get("scvi_training", {}) if isinstance(analysis, dict) else {}
    lines = [
        "# Comprehensive single-cell analysis report",
        "",
        "## Executive summary",
        "",
        summary,
        "",
        "## Dataset and final analysis object",
        "",
        f"- Source dataset: `{dataset.get('path', path)}`",
        f"- Final matrix: {adata.n_obs:,} cells × {adata.n_vars:,} genes",
        f"- Dataset fingerprint: `{dataset.get('fingerprint', 'not recorded')}`",
        f"- Final clustering: `{cluster_key}` at resolution "
        f"`{clustering.get('resolution', 'not recorded')}` ({len(table):,} cluster-label rows)",
        f"- Final annotation column: `{label_key}`",
        "",
        "## Reproducible workflow and parameters",
        "",
        "| # | Capability | Version | Parameters | Result |",
        "|---:|---|---|---|---|",
    ]
    for index, item in enumerate(history, 1):
        arguments = json.dumps(item.get("arguments", {}), sort_keys=True, default=str)
        if len(arguments) > 500:
            arguments = arguments[:497] + "..."
        arguments_cell = arguments.replace("|", "\\|")
        summary_cell = str(item.get("summary", "")).replace("|", "\\|")
        lines.append(
            f"| {index} | `{item.get('tool_name')}` | `{item.get('skill_version')}` | "
            f"`{arguments_cell}` | {summary_cell} |"
        )
    if not history:
        lines.append("| — | No committed capability history was readable | — | — | — |")

    qc_review = cell_qc.get("review", {}) if isinstance(cell_qc, dict) else {}
    qc_decision_source = "current lineage facts"
    if not isinstance(qc_review, dict) or not qc_review.get("decision"):
        durable_qc = decisions.get("cell_qc_handling")
        if isinstance(durable_qc, dict) and durable_qc.get("decision"):
            qc_review = durable_qc
            qc_decision_source = _durable_decision_provenance(durable_qc, analysis)
        else:
            qc_review = {}
            qc_decision_source = "not recorded"
    lines.extend(
        [
            "",
            "## Cell-level quality control",
            "",
            f"- Thresholds: `{json.dumps(cell_qc.get('thresholds', {}), sort_keys=True)}`",
            f"- Flag counts: `{json.dumps(cell_qc.get('flag_counts', {}), sort_keys=True)}`",
            f"- QC decision: `{qc_review.get('decision', 'not recorded')}`",
            f"- QC rationale: {qc_review.get('rationale', 'not recorded')}",
            f"- QC decision provenance: `{qc_decision_source}`",
        ]
    )
    for finding in qc_review.get("visual_findings", []) if isinstance(qc_review, dict) else []:
        lines.append(f"- Visual finding: {finding}")
    doublet_evidence = doublets.get("evidence", {}) if isinstance(doublets, dict) else {}
    doublet_decision = doublets.get("decision", {}) if isinstance(doublets, dict) else {}
    doublet_decision_source = "current lineage facts"
    if not isinstance(doublet_decision, dict) or not doublet_decision.get("decision"):
        durable_doublet = decisions.get("doublet_handling")
        if isinstance(durable_doublet, dict) and durable_doublet.get("decision"):
            doublet_decision = durable_doublet
            doublet_decision_source = _durable_decision_provenance(
                durable_doublet, analysis
            )
        else:
            doublet_decision = {}
            doublet_decision_source = "not recorded"
    lines.extend(
        [
            f"- Doublet evidence: `{doublet_evidence.get('status', 'not recorded')}`",
            f"- Doublet handling: `{doublet_decision.get('decision', 'not recorded')}`",
            f"- Doublet decision provenance: `{doublet_decision_source}`",
            "",
            "## Representation and clustering",
            "",
            "- Representation: `"
            + json.dumps(analysis.get("representation", {}), sort_keys=True, default=str)
            + "`",
            "- Neighbor graph representation: `"
            + str(graph.get("representation_key") or "not recorded")
            + "`",
            "- Neighbor graph identity: `"
            + str(graph.get("neighbor_graph_id") or "not recorded")
            + "`",
            f"- UMAP: `{json.dumps(analysis.get('umap', {}), sort_keys=True, default=str)}`",
            f"- Current clustering identity: `{clustering_id}`",
        ]
    )
    if isinstance(scvi_training, dict) and scvi_training:
        lines.extend(
            [
                "",
                "### scVI training diagnostics",
                "",
                "- Epochs trained: `"
                + str(scvi_training.get("epochs_trained", "not recorded"))
                + "` (cap `"
                + str(scvi_training.get("max_epochs", "not recorded"))
                + "`)",
                "- Early stopping: enabled `"
                + str(scvi_training.get("early_stopping_enabled", "not recorded"))
                + "`, triggered `"
                + str(scvi_training.get("early_stopped", "not recorded"))
                + "`",
                "- Final train ELBO: `"
                + _format_metric(scvi_training.get("final_train_elbo"))
                + "`",
                "- Final validation ELBO: `"
                + _format_metric(scvi_training.get("final_validation_elbo"))
                + "`",
                "- Best validation ELBO: `"
                + _format_metric(scvi_training.get("best_validation_elbo"))
                + "` at epoch `"
                + str(scvi_training.get("best_validation_epoch", "not recorded"))
                + "`",
            ]
        )
    lines.extend(
        [
            "",
            "## Cluster-level QC",
            "",
            f"- Evidence status: `{cluster_qc.get('status', 'not recorded')}`",
            f"- Evidence id: `{cluster_qc.get('evidence_id', 'not recorded')}`",
            f"- Clusters requiring review: `{cluster_qc.get('review_clusters', [])}`",
        ]
    )
    cluster_review = cluster_qc.get("review", {}) if isinstance(cluster_qc, dict) else {}
    cluster_findings = (
        cluster_review.get("visual_findings", [])
        if isinstance(cluster_review, dict)
        else []
    )
    for finding in cluster_findings:
        lines.append(f"- Visual finding: {finding}")
    for cluster, review in (
        cluster_review.get("cluster_reviews", {}).items()
        if isinstance(cluster_review, dict)
        else []
    ):
        lines.append(
            f"- Cluster {cluster}: `{review.get('disposition')}` — {review.get('rationale')}"
        )

    batch_evidence = batch.get("evidence", {}) if isinstance(batch, dict) else {}
    batch_decision = batch.get("decision", {}) if isinstance(batch, dict) else {}
    integration_score = (
        batch.get("integration_score", {}) if isinstance(batch, dict) else {}
    )
    batch_decision_source = "current lineage facts"
    if not isinstance(batch_decision, dict) or not batch_decision.get("decision"):
        durable_batch = decisions.get("batch_handling")
        if isinstance(durable_batch, dict) and durable_batch.get("decision"):
            batch_decision = durable_batch
            batch_decision_source = _durable_decision_provenance(durable_batch, analysis)
        else:
            batch_decision = {}
            batch_decision_source = "not recorded"
    recorded_batch_decision = batch_decision.get("decision", "not recorded")
    if batch_evidence.get("status") == "not_applicable":
        recorded_batch_decision = "not_applicable"
    lines.extend(
        [
            "",
            "## Batch-effect investigation",
            "",
            f"- Evidence status: `{batch_evidence.get('status', 'not recorded')}`",
            f"- Recommendation: `{batch_evidence.get('recommendation', 'not recorded')}`",
            f"- Decision: `{recorded_batch_decision}`",
            f"- Rationale: {batch_decision.get('rationale', 'not recorded')}",
            f"- Decision provenance: `{batch_decision_source}`",
            "- Integration comparison status: `"
            + str(integration_score.get("comparison_status", "not recorded"))
            + "`",
            "- Integration interpretation: "
            + str(integration_score.get("interpretation", "not recorded")),
            "",
            "## Annotation evidence and adjudication",
            "",
        ]
    )
    annotation_review = annotation.get("review", {}) if isinstance(annotation, dict) else {}
    lines.append(
        "- Current evidence methods: "
        + ", ".join(annotation_review.get("methods_reviewed", []))
    )
    lines.append("- DEGs were primary: " + str(annotation_review.get("deg_primary", False)))
    for finding in annotation_review.get("agreement_findings", []):
        lines.append(f"- Agreement finding: {finding}")
    if annotation_review.get("reference_waiver"):
        lines.append(f"- Reference-method waiver: {annotation_review['reference_waiver']}")

    lines.extend(
        [
            "",
            "## Final cluster labels",
            "",
            "| Cluster | DEG label | Final label | Confidence | Cells | Evidence and rationale |",
            "|---|---|---|---|---:|---|",
        ]
    )
    for row in table.sort_values("cluster").itertuples(index=False):
        evidence_parts = []
        if evidence_summaries[row.cluster]:
            evidence_parts.append(evidence_summaries[row.cluster])
        if rationales[row.cluster]:
            evidence_parts.append("Rationale: " + rationales[row.cluster])
        if row.override_justification:
            evidence_parts.append("Earlier-hypothesis note: " + row.override_justification)
        evidence = (" ".join(evidence_parts) or "Not separately recorded.").replace(
            "|", "\\|"
        ).replace("\n", " ")
        lines.append(
            f"| {row.cluster} | {row.deg_label} | {row.cell_type} | {row.confidence} | "
            f"{row.n_cells} | {evidence} |"
        )
    lines.extend(["", "## Caveats and limitations", ""])
    lines.extend(f"- {item}" for item in caveats)
    lines.extend(
        [
            "",
            "## Provenance and deliverables",
            "",
            f"- Source artifact used for finalization: `{path}`",
            f"- Clustering identity: `{clustering_id}`",
            "- Exact committed capability calls are saved in `code/analysis-recipe.py`.",
            "- For a step-by-step reading version, with each step's arguments, decision "
            "rationale, and figures inline, ask for an analysis notebook; it can be rebuilt at "
            "any time and will include any work done after this report.",
            "- Human-facing figures, reports, tables, and datasets are projected into their "
            "named session folders; canonical provenance remains under `artifacts/capabilities/`.",
        ]
    )
    return "\n".join(lines) + "\n"


def _validate_cluster_qc_resolved(
    facts: dict[str, Any], clustering_id: Any, cluster_key: str
) -> None:
    """Refuse to publish labels over a clustering whose recorded QC was never adjudicated.

    This is an integrity check, not a mandatory pipeline step: it fires only when cluster QC was
    actually evaluated *for the clustering being finalized* and then left unresolved. A targeted
    analysis that never ran cluster QC records no ``cluster_qc`` fact and is unaffected, which
    keeps the "guided, not gated" stance intact.

    ``evaluate_cluster_qc`` writes a new ``evidence_id`` and ``review_cluster_qc`` binds its review
    to that exact ID. Checking both identities prevents a stale review for the same clustering key
    from certifying newly evaluated evidence, without requiring that QC exist in the first place.
    """

    cluster_qc = facts.get("cluster_qc")
    if not isinstance(cluster_qc, dict) or cluster_qc.get("status") != "attested":
        return
    if clustering_id is None or cluster_qc.get("clustering_id") != clustering_id:
        return
    review = cluster_qc.get("review")
    if not isinstance(review, dict) or review.get("clustering_id") != clustering_id:
        raise ValueError(
            f"cluster QC was evaluated for the current clustering {cluster_key!r} but never "
            "reviewed (no cluster_qc.review for this clustering); call review_cluster_qc before "
            "finalizing so the recorded labels rest on an adjudicated clustering."
        )
    evidence_id = cluster_qc.get("evidence_id")
    if not evidence_id or review.get("evidence_id") != evidence_id:
        raise ValueError(
            "cluster QC review for the current clustering is stale: its evidence_id does not "
            "match the current cluster-QC evidence. Re-review the current evidence before "
            "finalizing."
        )
    if review.get("status") != "resolved":
        unresolved = ", ".join(str(value) for value in review.get("unresolved_clusters", []))
        raise ValueError(
            "cluster QC review for the current clustering still has unresolved cluster "
            f"action(s): {unresolved or 'unspecified'}. Resolve or re-review them before "
            "finalizing."
        )


def _execute_finalization(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd
    import scanpy as sc

    parsed = _resolve_arguments(arguments)
    cluster_key = parsed["cluster_key"]
    label_key = parsed["label_key"]
    labels = parsed["labels"]
    rationales = parsed["rationales"]
    deg_labels = parsed["deg_labels"]
    evidence_summaries = parsed["evidence_summaries"]
    confidence = parsed["confidence"]
    overrides = parsed["overrides"]
    caveats = list(parsed["caveats"])
    summary = parsed["summary"]
    _validate_summary_metrics(summary, context.state_facts)
    current_analysis = context.state_facts.get("analysis", {})

    path = Path(str(arguments["path"])).expanduser().resolve()
    adata = _read_matrix(path)
    existing_columns = set(map(str, adata.obs.columns))
    clusters = (
        set(map(str, adata.obs[cluster_key].astype(str).unique()))
        if cluster_key in existing_columns
        else set()
    )
    provenance = dict(adata.uns.get("scagent_sdk", {}))
    input_clustering_id = provenance.get("clustering_id")
    current_clustering = (
        current_analysis.get("clustering", {}) if isinstance(current_analysis, dict) else {}
    )
    current_clustering_id = (
        current_clustering.get("id") if isinstance(current_clustering, dict) else None
    )
    _validate_inputs(
        parsed=parsed,
        clusters=clusters,
        existing_columns=existing_columns,
        input_clustering_id=input_clustering_id,
        current_clustering_id=current_clustering_id,
    )
    _validate_cluster_qc_resolved(context.state_facts, current_clustering_id, cluster_key)

    # Only labels are required. Optional evidence fields remain useful in reports when supplied,
    # but missing entries do not turn publication into a second annotation-review workflow.
    rationales = {cluster: rationales.get(cluster, "") for cluster in clusters}
    deg_labels = {cluster: deg_labels.get(cluster, "") for cluster in clusters}
    evidence_summaries = {
        cluster: evidence_summaries.get(cluster, "") for cluster in clusters
    }
    confidence = {cluster: confidence.get(cluster, "not specified") for cluster in clusters}

    for caveat in _auto_caveats(context.state_facts, confidence):
        if caveat not in caveats:
            caveats.append(caveat)
    if not caveats:
        caveats.append("No additional limitations were identified beyond method assumptions.")
    adata.obs[label_key] = adata.obs[cluster_key].astype(str).map(labels).astype("category")
    provenance["final_annotation"] = {
        "cluster_key": cluster_key,
        "label_key": label_key,
        "clustering_id": input_clustering_id,
        "labels": labels,
        "rationales": rationales,
        "deg_labels": deg_labels,
        "evidence_summaries": evidence_summaries,
        "confidence": confidence,
        "overrides": overrides,
        "caveats": caveats,
    }
    umap_key = "X_umap" if "X_umap" in adata.obsm else provenance.get("umap_key")
    if umap_key not in adata.obsm and "umap" in adata.obsm:
        umap_key = "umap"
    if isinstance(umap_key, str) and umap_key in adata.obsm and umap_key != "X_umap":
        adata.obsm["X_umap"] = adata.obsm[umap_key]
        provenance["umap_key"] = "X_umap"
    adata.uns["scagent_sdk"] = provenance
    adata.write_h5ad(context.staging_dir / "final-annotated.h5ad", compression="gzip")
    table = (
        pd.DataFrame(
            {
                "cluster": adata.obs[cluster_key].astype(str),
                "cell_type": adata.obs[label_key].astype(str),
            }
        )
        .value_counts()
        .rename("n_cells")
        .reset_index()
    )
    table["rationale"] = table.cluster.map(rationales)
    table["deg_label"] = table.cluster.map(deg_labels)
    table["evidence_summary"] = table.cluster.map(evidence_summaries)
    table["confidence"] = table.cluster.map(confidence)
    table["override_justification"] = table.cluster.map(overrides).fillna("")
    table.to_csv(context.staging_dir / "final-labels.csv", index=False)
    history = _capability_history(context.session_dir)
    recipe_calls = [
        {
            "tool": item.get("tool_name"),
            "skill": item.get("skill_id"),
            "skill_version": item.get("skill_version"),
            "arguments": item.get("arguments", {}),
        }
        for item in history
    ]
    recipe_calls.append(
        {
            "tool": "finalize_analysis",
            "skill": "finalize-analysis",
            "skill_version": getattr(context, "skill_version", None) or "0.6.0",
            "arguments": arguments,
        }
    )
    (context.staging_dir / "analysis-recipe.py").write_text(
        recipe.render_capability_recipe(recipe_calls), encoding="utf-8"
    )
    report = _render_report(
        summary=summary,
        facts=context.state_facts,
        decisions=getattr(context, "state_decisions", {}),
        history=history,
        path=path,
        adata=adata,
        cluster_key=cluster_key,
        label_key=label_key,
        clustering_id=input_clustering_id,
        table=table,
        rationales=rationales,
        evidence_summaries=evidence_summaries,
        caveats=caveats,
    )
    (context.staging_dir / "analysis-report.md").write_text(
        report, encoding="utf-8"
    )
    figures: list[dict[str, str]] = []
    if "X_umap" in adata.obsm:
        sc.pl.umap(adata, color=[cluster_key, label_key], show=False, legend_loc="right margin")
        (context.staging_dir / "final").mkdir(parents=True, exist_ok=True)
        plt.savefig(
            context.staging_dir / "final/final-annotation-umap.png",
            dpi=180,
            bbox_inches="tight",
        )
        plt.close("all")
        figures.append(
            {
                "name": "final-annotation-umap",
                "relative_path": "final/final-annotation-umap.png",
                "media_type": "image/png",
            }
        )
    counts = adata.obs[label_key].astype(str).value_counts().sort_values()
    fig, axis = plt.subplots(figsize=(9, max(4, 0.35 * len(counts))))
    axis.barh(counts.index, counts.values, color="#4c78a8")
    axis.set(xlabel="Cells", ylabel="Final cell type", title="Final annotation composition")
    for position, value in enumerate(counts.values):
        axis.text(value, position, f" {int(value):,}", va="center")
    fig.tight_layout()
    (context.staging_dir / "final").mkdir(parents=True, exist_ok=True)
    fig.savefig(
        context.staging_dir / "final/final-label-counts.png",
        dpi=160,
        bbox_inches="tight",
    )
    plt.close(fig)
    figures.append(
        {
            "name": "final-label-counts",
            "relative_path": "final/final-label-counts.png",
            "media_type": "image/png",
        }
    )
    final_path = f"{context.artifact_relative_path}/final-annotated.h5ad"
    report_path = f"{context.artifact_relative_path}/analysis-report.md"
    artifacts = [
        {
            "name": "final-annotated-anndata",
            "relative_path": "final-annotated.h5ad",
            "media_type": "application/x-hdf5",
        },
        {"name": "final-labels", "relative_path": "final-labels.csv", "media_type": "text/csv"},
        {
            "name": "analysis-report",
            "relative_path": "analysis-report.md",
            "media_type": "text/markdown",
        },
        {
            "name": "analysis-recipe",
            "relative_path": "analysis-recipe.py",
            "media_type": "text/x-python",
        },
        *figures,
    ]
    return {
        "clustering_id": input_clustering_id,
        "cluster_key": cluster_key,
        "label_key": label_key,
        "labels": labels,
        "rationales": rationales,
        "deg_labels": deg_labels,
        "evidence_summaries": evidence_summaries,
        "confidence": confidence,
        "overrides": overrides,
        "caveats": caveats,
        "final_path": final_path,
        "report_path": report_path,
        "n_obs": int(adata.n_obs),
        "n_clusters": len(clusters),
        "artifacts": artifacts,
        "model_media": figures,
    }


def _result_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    labels = payload["labels"]
    clustering_id = payload["clustering_id"]
    cluster_key = payload["cluster_key"]
    label_key = payload["label_key"]
    return {
        "summary": (
            f"Finalized {payload['n_clusters']} cluster labels across {payload['n_obs']:,} cells."
        ),
        "details": {
            "clustering_id": clustering_id,
            "cluster_key": cluster_key,
            "label_key": label_key,
            "labels": labels,
            "rationales": payload["rationales"],
            "deg_labels": payload["deg_labels"],
            "evidence_summaries": payload["evidence_summaries"],
            "confidence": payload["confidence"],
            "overrides": payload["overrides"],
            "caveats": payload["caveats"],
            "final_path": payload["final_path"],
        },
        "facts_patch": {
            "annotation": {
                "final": {
                    "status": "complete",
                    "clustering_id": clustering_id,
                    "cluster_key": cluster_key,
                    "label_key": label_key,
                    "labels": labels,
                    "rationales": payload["rationales"],
                    "deg_labels": payload["deg_labels"],
                    "evidence_summaries": payload["evidence_summaries"],
                    "confidence": payload["confidence"],
                    "overrides": payload["overrides"],
                }
            },
            "finalization": {
                "status": "complete",
                "clustering_id": clustering_id,
                "dataset_path": payload["final_path"],
                "report_path": payload["report_path"],
            },
        },
        "decisions_patch": {"final_labels": labels},
        "artifacts": payload["artifacts"],
        "model_media": payload["model_media"],
    }


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    payload = _execute_finalization(arguments, context)
    return _result_envelope(payload)
