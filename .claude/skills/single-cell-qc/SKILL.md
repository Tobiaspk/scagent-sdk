---
name: single-cell-qc
description: Calculate per-cell and per-gene single-cell QC evidence, or explicitly filter cells or genes as separate operations. Use to inspect library size, detected genes, mitochondrial fraction, and threshold effects.
---

# Single-Cell QC

QC measurement and filtering are intentionally separate.

- `calculate_single_cell_qc` adds count-derived metrics and threshold flags but removes nothing.
- `review_single_cell_qc` records the required visual interpretation and keep/filter decision.
- `filter_single_cells` removes cells only when `confirm_filtering=true`.
- `filter_single_cell_genes` removes genes only when `confirm_filtering=true`.

The tools require an H5AD containing finite nonnegative integer counts in the selected layer or
in `X`. Filtering creates a new cell-set or count identity and makes evidence tied to the old
artifact stale; historical artifacts remain intact.

Use `counts_layer="auto"` by default: it selects `layers["counts"]` when present and otherwise
validates `X`. Use `null` only to explicitly force `X`, and name a layer only when it is known to
exist. The calculation emits the standard QC suite: combined distributions/UMI knee, per-metric
violins with the cells jittered over them, log-scaled count/gene histograms, mitochondrial
histogram, count/gene/mitochondrial scatters, ribosomal-versus-mitochondrial scatter, and — when
the artifact already carries doublet scores — a doublet violin and histogram. Inspect every
returned figure before review. After UMAP, also call `plot_qc_embedding` to localize these
signals, and use `visualize-single-cell` for anything this suite does not cover: per-sample QC
splits are the usual next step, since a pooled distribution hides the one failing library.

Library size and detected genes are drawn on log axes with log-spaced bins. Read thresholds off
those; a linear axis compresses the low tail into a few bars and hides the shape you are judging.

Thresholds are dataset- and assay-dependent. A PBMC default is not automatically appropriate for
nuclei, tumors, low-depth libraries, or large metabolically active cells. As starting points for
*flagging*, mitochondrial fraction runs much lower in single-nucleus data (a few percent) than in
whole cells (tens of percent), and a threshold copied across that boundary is meaningless.

## Early QC is instrumentation, not surgery

The flags this capability writes are measurements. Removal is a separate decision, and for cells it
is normally made **after clustering**, in `cluster-qc`, not here:

- A per-cell threshold cannot tell a dying cell from a real high-mitochondrial cell type.
  Cardiomyocytes, hepatocytes, proximal tubule, and activated or secretory cells legitimately carry
  a high mitochondrial fraction; low-complexity libraries are also normal for small resting cells.
  Only the embedding shows whether the flagged cells form one coherent population — evidence of a
  real failing subset — or are scattered through healthy clusters, where they are individual
  measurement noise the clustering will absorb anyway.
- Cluster context supplies evidence a per-cell cut cannot: whether the group has a discriminating
  identity program, whether its gene-gene covariance is structured, whether doublet scores are
  enriched in it. That is why doublet-flagged cells are kept in the object rather than deleted —
  their *distribution across clusters* is the evidence.
- Deleting the tail early also removes the evidence for the decision. After a pre-clustering cut,
  the recalculated QC shows zero flagged cells, the QC review has nothing left to judge, and cluster
  QC never sees the population that was removed. The cut becomes unauditable and unreviewable.

So the ordinary path is: calculate flags → inspect every figure → `review_single_cell_qc` with a
`keep_all` (or `request_guidance`) decision that says what the tail looks like and where it will be
adjudicated → normalize/HVG/PCA/neighbors/UMAP → `plot_qc_embedding` → cluster at exploratory
resolution → `evaluate_cluster_qc`, where removal is decided with three-axis evidence.

`filter_single_cells` before clustering is a fallback, appropriate when the barcodes are
unambiguously not cells (empty droplets, near-zero complexity debris), when a user or a source
protocol specifies the cut, or when the flagged fraction is so large that the embedding itself would
be dominated by debris. Take that path deliberately: review first, state the threshold and the count
it removes, say why the cut is safe before cluster context exists, and then recalculate and review
QC on the retained artifact. Do not filter first and review afterwards — the review must be able to
change the outcome. The tool carries no threshold defaults precisely so that this stays a stated
choice.

A high flagged fraction is a question, not an automatic deletion. Compare threshold options,
distribution shape, QC-on-embedding localization, doublet evidence, and later cluster coherence.
Record why all cells are kept or which cells should be filtered.

Read [references/qc-contract.md](references/qc-contract.md) for metric definitions and mutation
semantics.
