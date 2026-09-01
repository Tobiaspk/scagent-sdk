# Interpretation

## The three axes

- **Metric QC** flags a cluster; it does not decide removal. Severity is `clean` (no adverse
  signal), `ambiguous` (one moderate signal), or `obvious` (two independent moderate signals, or
  one extreme degradation signal). Assess mitochondrial %, ribosomal %, library size, detected
  genes, doublet enrichment, and silhouette jointly — never a single metric.
- **DEG identity** asks whether a coherent cluster-specific program exists, not what to call it.
  `junk_markers` means zero discriminating genes (only mitochondrial/ribosomal/hemoglobin/MALAT1
  nuisance or broad activation/stress/housekeeping/cell-cycle genes). `inconclusive` means some
  but too few discriminating genes.
- **Covariance/coherence** distinguishes a structured transcriptional program from unstructured
  noise. `mean_abs_corr` and the high-correlation-pair fraction summarize the gene-gene structure;
  the saved `cluster_<id>_correlation.png` heatmap is the visual evidence — flat/speckled supports
  junk, clear blocks support real biology. Cite the heatmap path and evidence id when you use it.

## Synthesis and removal

- Only `confirmed_junk` (metric-adverse **and** junk DEGs **and** unstructured/weak covariance)
  enters the automatic removal set, and only below `auto_remove_max_fraction`.
- A metric-clean cluster with junk markers and no structure is `unstructured_junk_markers` — kept
  for review, because "metric-clean" is not "coherent" (a homotypic-doublet or noise mixture can
  have normal library size, genes, and MT). The coherence axis surfaces it; it does not
  auto-remove it.
- Preserve real conflicts. `junk_markers_but_structured` and `conflicting` are kept and
  investigated. `identity_without_structure` is kept without mandatory review because weak
  covariance alone is not evidence of a defect. A missing or `inconclusive` axis is never treated
  as agreement.

## Other signals

- Tiny clusters may be rare biology, doublets, ambient artifacts, or unstable partitions. Size
  alone is not a deletion rule.
- Silhouette depends on the representation and penalizes continuous trajectories.
- **Technical Moran's I** (`moran_local_mt`, `moran_local_lib`, plus global) localizes technical
  pockets — high local MT Moran with an elevated MT z-score suggests a coherent high-MT/death
  pocket; it is not cell-type evidence and never substitutes for missing DEG or covariance evidence.
- Compare a cluster's predicted-doublet rate with the global rate; review marker identity and
  library composition before filtering, and use per-cell `predicted_doublet` calls rather than
  deleting a whole cluster by association. Missing doublet evidence is reported explicitly.
- Structure thresholds (0.08 / 0.12 / 0.18) and z-gates (2.0 / 3.0) are legacy-compatibility
  starting points recorded in the evidence artifact, not validated universal constants; sweep them
  across datasets before treating them as mature.
- Re-run QC whenever the cell set, representation, neighbors graph, or clustering changes; after an
  applied cleanup, re-prepare and recluster the remaining cells first.
