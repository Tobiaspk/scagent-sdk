---
name: marker-annotation
description: Generate group-wise differential-expression evidence and score transparent curated marker programs for a supplied grouping and expression matrix.
---

# Marker Annotation Evidence

Call `evaluate_marker_evidence` for a supplied grouping column and appropriate expression matrix.
This produces candidates and competing evidence for later label adjudication.

Inspect statistically supported positive DEGs, expression fractions, log-fold changes, marker
coverage, shared-marker ambiguity, and specificity-weighted overlap. Reject candidates driven by
ambient RNA, generic stress/cell-cycle programs, or a single gene. An `insufficient` overlap is not
a negative label; it means the supplied marker programs do not support that candidate. Generalize
labels when subtype evidence is weak. Add tissue-specific marker sets through `marker_sets` when
the built-in broad programs are insufficient, and cite their source in the analysis rationale.

Separate look-alike programs on their full positive and negative evidence. A `GZMB`-high cluster is
not a plasma cell without immunoglobulin/secretory markers (`MZB1`, `JCHAIN`, `SDC1`, `XBP1`,
`IGHG1`); consider plasmacytoid dendritic cells when their defining program (`LILRA4`, `IL3RA`,
`CLEC4C`, `IRF7`/`TCF4`) is present. Predicted-doublet enrichment is probabilistic barcode evidence
for review, never a cell-type label.

Use an independent reference when it is appropriate and likely to add information. Strong marker
evidence does not need a ceremonial second model, and reference inference does not replace weak or
conflicting biology.

For a comprehensive analysis, CellTypist and SCimilarity can be useful when appropriate models are
ready, but neither is mandatory. `review_annotation_evidence` is an optional concise checkpoint; it
infers current evidence keys when they are omitted and can record ambiguous clusters without
blocking finalization. DEGs are usually the primary label hypothesis; reference labels,
curated programs, Cytopus-covered marker knowledge, and atlas queries corroborate or challenge it.
For human data, keep `use_cytopus=true` unless the package is unavailable or the user explicitly
opts out. The capability records whether the knowledge base loaded and which programs contributed;
Cytopus augments marker hypotheses but does not replace cluster DEGs as the primary evidence.
Preserve ambiguity as uncertainty or an unknown/general label instead of forcing resolution.

Read [references/evidence-standard.md](references/evidence-standard.md) for label-strength expectations and the pDC-versus-plasma discriminator.
