# Decision guide

## The two evidence axes

`gene_evidence` (primary, gene-level):

- `none`: no supported cross-sample population match carries a direct gene difference.
- `localized`: matched populations differ across samples, but no program recurs.
- `recurring_sample_associated`: a consistently directed program recurs across ≥2 distinct
  matched populations — the pattern suspicious for ambient RNA, sample-specific background, or a
  procedure/source effect.

`design_interpretation` (experimental design):

- `confounded_with_biology`: the batch is perfectly confounded with a supplied condition column.
- `orthogonal_but_not_known_technical`: a supplied condition is present and not confounded, but the
  batch is not documented technical.
- `unknown`: no design information resolves the cause.

## Recommendation (non-binding)

| gene_evidence | design | recommendation |
|---|---|---|
| none / localized | any | do_not_integrate_based_on_current_evidence |
| recurring | unknown / confounded_with_biology | cannot_determine_technical_vs_biological |
| recurring | orthogonal_but_not_known_technical | integration_optional_for_confirmed_replicates |

## Deciding

- `keep_uncorrected`: effects are modest, biologically entangled, or not harmful to the analysis.
- `integrate`: the user chooses to build a shared corrected representation after weighing the
  evidence and study goal. Record that choice concisely; do not turn it into a model-written proof.
- `separate`: batches are incompatible assays, tissues, species, or irreducibly confounded designs.
- If guidance is needed, ask the user and leave the decision unresolved. If no meaningful batch
  unit is present, `investigate_batch(batch_key=null)` records not-applicable evidence directly.

## Recurrence is advisory, not replication

Recurrence is computed from **cell-level Wilcoxon** tests between matched regions — a
legacy-compatible advisory signal. Cells are not independent biological replicates, so a recurring
program is **not** evidence of sample-level replication, and the list may contain **low-expression
or compositional false positives**: with hundreds of cells, small random differences reach
`q <= 0.05`. Before treating a recurring program as real, read `recurring-programs.csv` together
with `direct-matched-region-degs.csv` and weigh the detection fractions (`pct_a`/`pct_b`), effect
size, and gene class. Sample-aware pseudobulk contrasts remain the appropriate tool for replicated
inference and are not implemented here.

## Durable decision

The decision persists only the choice, its short rationale, and the current `evidence_id`. The
evidence holds the cell-set and count identities used for currency. Integration and reclustering
are consequences of the decision and do not stale it; changing cells or counts does. The full
scientific reasoning remains in the evidence artifact rather than being duplicated into state.

Correction cannot recover a biological contrast perfectly confounded with batch. A non-confounded
condition column alone does not make sample-wide differences technical — donor and other biological
effects can remain. Sample-segregated clusters can be donor/patient-private biology (donor-specific
states in normal tissue, or malignant clones/CNVs in tumors); do not assume the tissue is a tumor
without dataset context. The advisory mixing and association metrics are representation- and
composition-dependent and are never optimization targets.
