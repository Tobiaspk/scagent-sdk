---
name: cluster-qc
description: Adjudicate a supplied clustering with three independent axes — metric QC, DEG identity, and covariance/coherence (plus technical Moran's I) — and optionally remove clusters on which all three converge as junk within a bounded fraction.
---

# Cluster QC

Call `evaluate_cluster_qc` with a compatible clustered AnnData, a grouping column, and `X_pca` for
separation evidence. It evaluates three independent axes per cluster and synthesizes one decision:

- **Metric QC** — robust per-cluster severity from mitochondrial/ribosomal fraction, library size,
  detected genes, doublet enrichment, tiny size, and negative silhouette. No single signal
  decides removal.
- **DEG identity** — whether the cluster carries a discriminating cell-identity program, or only
  nuisance/broad markers (`junk_markers`).
- **Covariance/coherence** — within-cluster gene-gene correlation structure with a saved heatmap
  per eligible cluster. Technical Moran's I for mitochondrial fraction and library size localizes
  technical pockets and is never cell-type evidence.

A cluster can be auto-removed **only** when metric QC is adverse, its DEGs are junk, and its covariance
is unstructured/weak — and the total removal stays below `auto_remove_max_fraction`. A missing or
inconclusive axis never counts as agreement; conflicts (`junk_markers_but_structured`,
`conflicting`, `inconclusive`) are flagged for review and kept. An identity-supported cluster with
weak covariance is kept: weak structure alone is common and is not a defect. Set
`auto_remove_convergent=false` is the default inspect/report-only behavior. Set it true explicitly
only when mutation is intended.

Review every warning, especially tiny clusters, negative silhouette, weak markers, high-MT
clusters, and Scrublet enrichment. Doublet enrichment nominates review; it never deletes a cluster
or proves a coherent state is artifactual. `attested` means the checks ran for the current
identities — not that every cluster is biologically valid.

The capability saves a per-cluster metric boxplot (library size and genes per cell on log axes,
metric-flagged clusters in red), a cluster/QC UMAP, a per-cluster highlight grid, and one
covariance heatmap for every eligible cluster. Inspect all of them, including heatmaps not
attached inline, then call `review_cluster_qc`. The highlight grid is where a per-cluster
judgement is actually made: the overlaid UMAP cannot separate thirty-plus colors, so use the grid
to see whether a flagged cluster is one coherent region or debris scattered across the embedding.
Review the overview and drill into the specific clusters whose evidence is genuinely conflicting;
per-cluster heatmaps are available on demand rather than a mandatory checklist. A review decision
records the judgment but does not gate final publication.

For a comprehensive end-to-end run, work down resolutions 2.0, 1.5, and 1.0 **iteratively, not side
by side**, and give **every clustering you create its own QC round** — evaluate, inspect the
figures, review, and ask at each rung whether a population now needs to be removed. Each resolution
is the next phase on the cells the previous round left behind, not a
competing candidate on the same cells: evaluate and review at 2.0, apply any confirmed removal,
re-prepare the retained cells (normalize, re-select HVGs, PCA, neighbors, UMAP), then cluster and
evaluate again at the next resolution with a distinct cluster key. The high resolution exists to
expose small low-quality populations while they are still separable; 1.5 is the round after a
confirmed removal and the first re-clustering on an integrated embedding (integration reshuffles
neighborhoods, so its clusters are re-adjudicated, not inherited); 1.0 is the default annotation
granularity and gets a full QC round before DEGs or labels bind to it. A clean round ends the
*removal loop* — stop hunting for more cells to cut — but it never waives QC on the later rungs:
what was invisible inside a healthy neighbour at one granularity, or before correction, can be
separable at the next. This is guidance, not hardcoding: users and data can change the ladder.

When a removal is applied, the capability issues fresh dataset/cell-set/count identities from
preserved raw counts and invalidates downstream evidence: re-prepare, recluster, and re-run this
capability on the cleaned cells. A held-for-review removal (at or above the fraction bound) removes
nothing until you decide. Old attestations cannot satisfy the current-clustering floor.

Read [references/interpretation.md](references/interpretation.md) before merging, keeping, or
removing a suspicious cluster.
