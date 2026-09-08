# Workflow decisions

## Entry and resume

Inspect durable facts first. Compare the current input identity with the recorded fingerprint. Reuse an artifact only when its provenance is tied to the active dataset/cell-set identity.

## QC and preprocessing

Separate cell-level filtering evidence from cluster-level coherence. Record thresholds and before/after counts. Normalize and select features according to the intended method; preserve raw counts for methods that require them.

For a comprehensive analysis, `calculate_single_cell_qc` is followed by inspection of every
standard QC figure and `review_single_cell_qc`. Use `counts_layer="auto"` unless the user has
identified an exact representation: it selects `layers["counts"]` when present and otherwise
validates `X`. Never retry a missing `counts` layer blindly. A keep-all decision is legitimate,
including for biologically high-mitochondrial tissue, but must name the visual/distributional
evidence. A filter decision is not complete until the retained artifact has been reassessed.

Do not call a bundled preparation pipeline. Resolve counts, measure QC, filter cells or genes,
normalize, select HVGs, compute PCA, build neighbors, compute UMAP, cluster, and rank genes only
when each operation is actually needed. Each consumes an explicit artifact and produces another.

Ambient-background removal is optional and precedes ordinary QC. Consider CellBender only when the
user requests it, the input is a raw unfiltered droplet matrix, or there is concrete ambient-RNA
evidence. First establish exact dataset identity, then use the CellBender input validator to confirm
empty-droplet evidence. Refuse filtered, normalized, or post-CellBender input. Usually let
CellBender estimate expected cells and total droplets on the first run; inspect its UMI curve,
metrics, logs, convergence/report artifacts, and corrected-count comparison before accepting the
new lineage. Evaluate mitochondrial/library QC, doublet evidence, and cluster QC on the selected
corrected counts as appropriate to the analysis.

Run doublet evidence only from a validated raw-count representation, normally separately for each
biological capture/library. Scrublet's expected rate is a prior, not a target, and its
`predicted_doublet` call is evidence rather than ground truth. Review score distributions,
per-library rates, cluster/sample enrichment, library size, detected genes, mitochondrial signal,
and marker coherence together. Keep flagged cells through that review. Removal is a separate,
explicitly confirmed cell-set mutation that requires complete downstream reprocessing.

## Clustering

Treat resolution as a scientific parameter. Compare stability, marker coherence, and interpretability rather than choosing the largest cluster count. Reclustering creates a new identity and makes prior cluster QC and annotation evidence stale.

For an ordinary end-to-end run, use **2.0 once for exploratory cleanup** and **1.0 once for final
annotation**. Resolution 1.5 is an evidence-triggered refinement when a real merge/split ambiguity
remains, not a mandatory intermediate phase. Use a distinct observation key per round.

One round is: cluster → `evaluate_cluster_qc` (report-only) → inspect the attached metric boxplots,
cluster/QC UMAP, and highlight grid, then use the evidence table and selectively open a covariance
heatmap for any ambiguous cluster → `review_cluster_qc` → apply any
confirmed removal with `auto_remove_convergent=true` → re-prepare the retained cells. Re-preparation
must recompute HVGs, not reuse them: subsetting cells changes the variance landscape, and the
embedding the next round clusters on has to reflect the cleaned population. A cleanup also clears
cell-QC and doublet evidence, so both are re-established before the next round is interpreted.

End the cleanup loop as soon as a round flags nothing requiring removal. Investigate and decide
batch handling on that clean exploratory representation before paying for another clustering/QC
round. Then create the annotation clustering at 1.0 (from the integrated representation if the
recorded batch decision selected integration) unless the evidence shows genuine over- or
under-splitting and you say so explicitly.

Each clustering step continues from the artifact the analysis is currently on, so omit the dataset
path and let the runtime supply it. A transforming tool refuses a superseded artifact, because
continuing from one silently discards whatever the intervening steps added.

That makes the cleanup loop's shape: cluster, read the evidence, clean up only when warranted, then
rebuild and repeat. When a comparison genuinely needs two clusterings of the *same* embedding side by
side, pass `branch_from` with the version to fork from; each alternative is recorded without
changing which version is active. The full or short version ID reported by `analysis-versions` is
accepted directly. Use `analysis-versions` to list them and to switch to the one the
evidence supports, and switch before annotating or finalizing, since those run against the active
version. A branch's cluster QC is bound to that branch and does not become the session's current
evidence until it is active — so compare from what each run returned, not from session facts.

Each clustering step continues from the artifact the analysis is currently on, so omit the dataset
path and let the runtime supply it. A transforming tool refuses a superseded artifact, because
continuing from one silently discards whatever the intervening steps added.

That makes the descent above the default shape: cluster, read the evidence, clean up, cluster again
from the result. When a comparison genuinely needs two clusterings of the *same* embedding side by
side, pass `branch_from` with the version to fork from; each alternative is recorded without
changing which version is active. The full or short version ID reported by `analysis-versions` is
accepted directly. Use `analysis-versions` to list them and to switch to the one the
evidence supports, and switch before annotating or finalizing, since those run against the active
version. A branch's cluster QC is bound to that branch and does not become the session's current
evidence until it is active — so compare from what each run returned, not from session facts.

## Batch handling

A sample-like column is not proof that correction is appropriate. Examine composition, per-sample
QC/doublet summaries, neighborhood mixing, sample-associated expression programs, and experimental
design. Strong association or weak mixing does not identify technical causality when sample,
condition, donor, tissue, or cell type is confounded. Record one decision: keep combined
uncorrected, integrate with a justified method, analyze separately, or request user guidance. A
decision is current only for the cell/count/representation/clustering identities it evaluated.

For many batches, prefer readable heatmaps or bounded legends and always retain the underlying
tables. Do not claim visual separation from an artifact whose labels cannot be read.
When an uncorrected UMAP already exists, normally plot it by the meaningful sample/batch key during
the investigation and keep its artifact identity with the explanation. Treat that view as advisory
context only: it localizes separation but cannot determine technical versus biological cause. Do
not build an otherwise-unneeded UMAP merely to complete a fixed sequence.

## Annotation

CellTypist and SCimilarity are per-cell reference methods. Run either directly when raw counts,
compatible genes, and a suitable model exist; neither depends on clustering or cluster QC.
Aggregate predictions by cluster only when that summary answers the question.

For final labels, read cluster DEGs and reconcile CellTypist, SCimilarity, curated markers,
ontology, and competing labels. Explicitly test close alternatives with discriminating positive
and negative programs; for example, do not equate isolated `GZMB` expression with plasma cells
without immunoglobulin/secretory evidence, and consider pDC programs when their defining genes and
context are present. Generalize upward or remain unresolved when subtype evidence is weak. Every
final label that differs from the independently written DEG-derived hypothesis needs an explicit
override justification. Never overwrite source labels without explicit authorization.

In a comprehensive run, inspect the readiness inventory before choosing CellTypist. SCimilarity
can establish broad context, but it does not replace a tissue-appropriate classifier when one is
cached. Use both suitable reference methods by default, plot their agreement, and document a
specific waiver when only one can run. Local curated marker resources (including Cytopus where
covered and available) are corroboration; cluster DEGs remain the primary source of truth.

## Downstream analysis

Choose cell-level DEG only for exploratory cluster markers. Prefer sample-aware pseudobulk for condition comparisons with biological replicates. Interpret pathways from ranked, quality-controlled contrasts and report database/version assumptions.

## Completion

Save a final dataset and a reasoning report. Include counts, cleanup decisions, clustering/batch decisions, annotation evidence, caveats, and the purpose of key artifacts. A resumable session with an explicit missing capability is preferable to an invented result.
