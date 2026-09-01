---
name: single-cell-clustering
description: Cluster cells from an existing neighbor graph or rank genes for an existing grouping as separate operations. Use when the scientific question requires discrete groups or group-wise differential expression.
---

# Single-Cell Clustering

`cluster_single_cells` runs Leiden on an explicitly named neighbor graph and writes labels to an
explicit, non-conflicting observation key.

`rank_single_cell_groups` ranks genes for any existing categorical grouping, including clusters,
conditions, or another supplied group key.

Leiden requires a neighbor graph. Gene ranking requires a grouping and an appropriate expression
matrix.

## Choosing the resolution

`resolution` has **no default** and must be stated on every call. Granularity is a scientific
choice that differs by phase, so a single inherited convention value (Seurat's 0.8, Scanpy's 1.0)
would silently decide it for you and make every clustering in a run look identical in intent.
Say which phase you are in and pick for that phase:

- **Exploratory / cluster-QC clustering — start high, typically 2.0.** Cluster QC can only judge a
  population that is actually separated. A small dying, ambient-dominated, or doublet population is
  a few hundred cells; at coarse resolution it is absorbed into a healthy neighbour and its
  mitochondrial, library-size, and doublet signal is diluted below any threshold — the cluster looks
  merely mediocre and survives. A high first pass over-splits on purpose so those populations exist
  as their own clusters while they are still separable, and it also exposes the sample-private
  structure a batch diagnostic looks for. Over-splitting is cheap here: neighbouring fragments of
  one cell type are merged by interpretation, whereas a hidden bad population is not recoverable.
- **Then come down, never back up.** Each later round runs on the cells the previous round left
  behind, so it re-prepares (normalize, HVGs, PCA, neighbors, UMAP) and clusters at a *coarser*
  value — 1.5 for the round after a confirmed removal and for the first re-clustering on an
  integrated embedding, then 1.0 — and each new clustering gets its own cluster-QC round before
  anything downstream binds to it. Climbing back up after
  descending re-splits the groups a coarser round just adjudicated and invalidates the QC
  reasoning that was done on them; a finer clustering run late is also the usual way a run ends up
  annotating something other than the clustering it reviewed.
- **Annotation clustering — 1.0 by default.** It is the granularity at which cluster DEGs read as
  cell identities rather than as within-type gradients. Deviate for stated evidence (DEG identity,
  covariance coherence, separation showing genuine over- or under-splitting), not because a finer
  clustering was the most recent one.

This is reasoning to apply, not a rule to obey: nothing in the code enforces the sequence. A user
instruction, a source protocol being reproduced, or data whose structure genuinely argues otherwise
outranks it — say what you chose and why. What is not acceptable is an unstated resolution chosen
by habit. Use a distinct `cluster_key` per round (e.g. `leiden_res_2_0`) so earlier rounds stay
readable and no labels are overwritten.

Read [references/clustering-contract.md](references/clustering-contract.md).
