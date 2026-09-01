# Decision guide

## The one evidence axis

The verdict rests on a single gene-level axis. The tool takes **no design or condition inputs** —
whether a sample-linked split is technical or biological is an experimental-design question the user
answers, never a lever the caller can pull to steer the recommendation. `design_interpretation` is
therefore always `unknown`, and the design caveat is stated as narration beside the recommendation.

`gene_evidence` (gene-level):

- `none`: no supported cross-sample population match carries a direct gene difference.
- `localized`: matched populations differ across samples, but no program recurs.
- `recurring_sample_associated`: a consistently directed program recurs across ≥2 distinct
  matched populations — the same cell type separating by sample across the dataset. This is the
  actionable signal: the within-sample identity signatures of the matched clusters agree (same
  population, sample held constant), yet they cluster apart, so a sample-linked axis is splitting a
  shared population.

## Recommendation (non-binding, gene-evidence only)

| gene_evidence | recommendation |
|---|---|
| none / localized | do_not_integrate_based_on_current_evidence |
| recurring_sample_associated | integration_recommended |

`integration_recommended` presents **integrate (scVI)** as the recommended default; it does not
authorize anything. The user still chooses, because the genes cannot settle whether the split is a
technical batch effect or the per-sample biology the user means to study.

## Deciding

- `keep_uncorrected`: effects are modest, biologically entangled, or not harmful to the analysis.
- `integrate`: the user chooses to build a shared corrected representation after weighing the
  evidence and study goal. Record that choice concisely; do not turn it into a model-written proof.
- `separate`: batches are incompatible assays, tissues, species, or irreducibly confounded designs.
- If guidance is needed, ask the user and leave the decision unresolved. If no meaningful batch
  unit is present, `investigate_batch(batch_key=null)` records not-applicable evidence directly.
- Present the options with a recommended default, mirroring the legacy post-investigation
  checkpoint: with recurring gene evidence and confirmed cross-sample identity pairs, the default
  offered is **integrate (scVI)**; with `none`/`localized` evidence it is **keep_uncorrected**.
  The default is a presentation, not an authorization — the user's choice records the decision.

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
