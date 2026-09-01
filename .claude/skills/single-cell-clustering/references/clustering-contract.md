# Clustering and group-ranking contract

Leiden partitions a neighbor graph. Resolution controls granularity but does not have a universal
correct value. A clustering identity binds the graph, labels, key, resolution, and seed.

Because there is no universal value, `resolution` carries no default and must be supplied. The
common library defaults (0.8 in Seurat, 1.0 in Scanpy) are conventions from a different context,
not a recommendation for the phase you are in, and accepting one silently records a scientific
choice nobody made. The reasoned starting points — high (≈2.0) for exploratory cluster QC,
descending to ≈1.0 for the clustering you intend to annotate — are explained in `SKILL.md`. They
are defaults for judgement, not constraints: an explicit user or source choice, or evidence from
stability/marker coherence, overrides them, and the chosen value belongs in the narrative and the
report either way.

Resolution changes only the labels. It does not move the embedding or rebuild the neighbor graph,
so a resolution change is not a substitute for re-preparing after cells are removed: removing cells
changes the variance landscape, and the HVG mask, PCA, neighbors, and UMAP must be recomputed
before the next clustering round means anything.

A graph derived from PCA, scVI, SCimilarity, or another explicit representation can be used for
Leiden when scientifically justified.

Ranked genes compare each group with a reference population using the requested Scanpy method.
The input matrix should match the inferential goal; log-normalized expression is typical for
Wilcoxon ranking. A ranked list is evidence, not a finalized cell-type label.

The two tools are intentionally independent. Ranking can consume any existing group key, including
labels created outside this SDK.
