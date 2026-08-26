---
name: orchestrate-single-cell
description: Orchestrate an evidence-driven single-cell RNA-seq analysis across focused skills while preserving scientific state, provenance, and resumability. Use for complete or multi-step analyses, deciding what should happen next, resuming prior work, coordinating inspection, QC, preprocessing, clustering, batch investigation, integration, annotation, differential expression, pathways, visualization, and reporting, or explaining why a scientific floor blocks finalization.
---

# Orchestrate Single-Cell

Drive the analysis toward the user's biological goal without turning the workflow into a fixed pipeline. Consult the durable session facts and artifacts, then select the smallest focused skill that can resolve the next scientific uncertainty.

## Comprehensive end-to-end default

When the user asks to analyze a regular raw or minimally processed dataset end to end, treat
“complete” as a comprehensive evidence standard, not merely a final H5AD. This is a default
playbook, not a runtime DAG: the user may change parameters, omit inapplicable branches, or begin
from an already processed artifact, and the observed data may require replanning.

1. Inspect and describe the input, establish byte identity, resolve raw counts, and convert gene
   identifiers before QC when symbols are available.
2. Calculate cell QC with `counts_layer="auto"`. Inspect every returned standard figure, evaluate
   doublets when raw counts permit it, and call `review_single_cell_qc` with a concrete keep/filter
   rationale. If cells or genes are removed, recalculate and review QC on the retained artifact.
3. Normalize, select HVGs, compute PCA, inspect the PCA variance figure, build neighbors, compute
   UMAP, and call `plot_qc_embedding`. Explain where quality signals localize; distributions alone
   do not show whether a signal is a coherent population.
4. Cluster first at exploratory Leiden **2.0** to expose small low-quality populations. Run
   `evaluate_cluster_qc` in report-only mode, inspect its compact evidence and the attached standard
   figures, open a covariance heatmap only for a genuinely ambiguous cluster, and call
   `review_cluster_qc`. If the review confirms removal, apply it, then re-normalize, re-select HVGs,
   recompute PCA/neighbors/UMAP, and repeat exploratory QC on the retained cells. Removing cells
   changes the variance landscape; do not reuse the old HVG mask or embedding.

   If exploratory QC finds no population that should be removed, **stop the cleanup loop**. Do not
   mechanically run 1.5 and 1.0 plus full cluster-QC reports before investigating batch. Resolution
   1.5 is an optional refinement when 2.0 exposes a real merge/split ambiguity, not a mandatory
   toll gate. The ordinary path is one exploratory QC round, batch investigation/decision, then one
   annotation clustering at 1.0. Do not carry an unresolved remove/merge/split/recluster disposition
   into annotation.
5. With a clean exploratory representation, investigate batch structure when meaningful batch
   metadata exists. Use the bounded profile-nomination investigation, present its compact evidence
   to the user, and stop for an explicit handling choice before recording a decision. Record
   `not_applicable` when no defensible batch unit exists. Investigate batch **once**, on the
   uncorrected pass — it is the expensive gene-first diagnostic and answers only *whether* to
   integrate. If integration is chosen, rebuild the neighbors/UMAP from the integrated
   representation, then verify the correction with **`score_integration`** (X_scVI mixing vs the
   X_pca baseline) — do **not** re-run `investigate_batch` to check integration; the once-made
   batch decision carries through integration and re-clustering to finalization. A recurring
   sample-linked program that persists in the gene evidence after scVI is expected donor biology,
   not proof the integration failed; judge success from mixing improvement, not from gene programs.
6. Create the annotation clustering at **1.0 by default**. Deviate only for a
   stated scientific reason, such as DEG identity, covariance coherence, or separation showing
   genuine over- or under-splitting; never merely because a finer clustering was run more recently.
   Compute DEGs only once you are on the clustering you intend to annotate, since a DEG pass at a
   QC resolution is discarded when you later step down. Make that clustering current.
7. For annotation, use SCimilarity early when it helps establish broad tissue/context, inspect the
   complete readiness inventory of cached CellTypist models, and choose the closest organism/tissue
   model rather than a generic immune default. When both are suitable, run and summarize both and
   visualize their agreement. Generate cluster DEGs and marker programs; **DEGs are the primary
   decision basis**, while references and curated marker resources such as Cytopus corroborate or
   challenge the call. Query the reference atlas or literature for genuinely ambiguous clusters.
8. Call `review_annotation_evidence`, leaving ambiguous clusters unresolved until the evidence is
   adequate. Finalize only then. The final report must reconstruct the full committed workflow,
   parameters, QC decisions, cluster reviews, batch decision, annotation disagreements, caveats,
   and deliverables.

For a targeted question—one plot, one reference query, an already finalized object—use only the
capabilities needed for that question. Do not force the comprehensive playbook onto unrelated work.

Finalization is not necessarily the end of the work, and no step automatically produces a
walkthrough. When a user asks what was done, wants something to hand a collaborator, or continues
working after a report, offer `build_analysis_notebook`. It is requestable at any point, states
plainly that an unfinished analysis is in progress, and is rebuilt from provenance so a later build
covers whatever has been added since. Offer it rather than assuming it, and rebuild rather than
describing an earlier notebook as stale.

## Operating loop

1. Establish the relevant artifact and current processing state. Never assume a resumed file is unchanged.
2. State the immediate scientific question and why the selected capability answers it.
3. Prefer deterministic capability tools for computation and validation. Use model reasoning to choose parameters, compare evidence, and interpret results.
4. Inspect the committed facts and artifacts after every material action. Do not infer success from narration. If a figure is unreadable because of crowded legends or labels, treat visual review as incomplete and use its table or regenerate a legible view before making a visual claim.
5. Replan when evidence contradicts the initial path. Avoid running integration, fine-grained annotation, or destructive filtering merely because those steps are common.
6. Conclude with actual results, decisions, caveats, and artifact paths—not a list of tools used.

## Scientific floors

- Require a current raw-droplet suitability attestation before ambient-background removal.
- Generate doublet evidence from verified raw counts, normally per biological library. Treat predicted calls and cluster enrichment as probabilistic review evidence, never as cell-type truth or automatic permission to remove cells.
- Require cluster DEGs plus independent reference/marker evidence before final labels. DEGs are primary; reference-model predictions are hypotheses. When programs conflict, lower confidence, generalize the label, or leave it unresolved instead of forcing a subtype.
- Require an evidence-bound visual QC decision and a resolved cluster-QC visual review before final publication.
- Invalidate downstream evidence when filtering, representation, or clustering identity changes.
- Register every saved dataset, table, figure, and report with provenance.

Floors belong only on consequential decisions or mutations, not on ordinary computation. Every
focused tool is directly callable when its own intrinsic inputs are present. SCimilarity and
CellTypist require raw counts, compatible genes, and a matching reference model.
If a needed capability is unavailable, say exactly what is missing and preserve the session;
never fabricate a result or claim completion.

Read [references/workflow-decisions.md](references/workflow-decisions.md) when choosing between optional branches or resuming a partially completed analysis.
