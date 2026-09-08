---
name: scimilarity-annotation
description: Run SCimilarity on raw single-cell counts with model-compatible genes and a local reference model, producing per-cell embeddings, nearest-neighbor labels, reference-distance novelty evidence, and reference atlas cell queries that characterize a population against millions of annotated reference cells.
---

# SCimilarity

SCimilarity is a metric-learning model for single-cell RNA-seq. An encoder maps each cell into a
learned latent space trained from a very large reference collection. `CellAnnotation` then finds
nearby reference cells and votes over their known labels. The output is reference-transfer
evidence: a cell embedding, a per-cell predicted label, and neighbor distances that indicate how
close the query is to the model's reference geometry.

## Minimal inputs

`run_scimilarity_annotation` requires:

1. an H5AD with finite nonnegative integer counts in `layers["counts"]` or `X`;
2. gene names that can be aligned to the selected model's `gene_order.tsv`;
3. a local SCimilarity model directory; and
4. an explicitly declared `organism`.

It performs model-specific gene ordering, zero-filling, total-count normalization, log1p
transformation, embedding, and kNN prediction. Supply raw counts so this model-specific
preprocessing is applied exactly once.

## Organism is declared, then verified

`organism` has no default and is never inferred from letter case, because uppercase symbols are
a pipeline convention rather than a species. Declare it from dataset metadata or from what the
user told you; if you genuinely do not know, ask instead of guessing.

Every call then *verifies* the declaration against the organism-specific parts of both
configured model vocabularies and refuses a contradiction before loading anything. Gene overlap
alone cannot catch this: most mouse symbols are the human symbol in different case, so mouse
data will clear the overlap threshold against the human reference and return confident nonsense.
The verdict is reported as `consistent`, `contradicted`, or `unverified` — the last when only one
organism model exists here, or when the identifiers are not symbols at all. To run a
cross-species reference deliberately, set `allow_species_mismatch=true`; the contradiction is
then recorded with the run and the output is exploratory, not an annotation.

## Gene structures

The tool tests `var_names` and common symbol columns such as `feature_name`, `gene_symbol`,
`hgnc_symbol`, and `mgi_symbol`, then uses the source with greatest model overlap, aligning
symbol case to the exact model vocabulary.

- Human symbol examples: `A1BG`, `CD3D`, `MS4A1`, `TP53`. Human Ensembl IDs such as
  `ENSG00000141510` need an accompanying symbol column or prior conversion.
- Mouse symbol examples: `A1bg`, `Cd3d`, `Ms4a1`, `Trp53` when the model vocabulary uses
  conventional mouse casing. Mouse Ensembl IDs such as `ENSMUSG00000059552` similarly need a
  symbol mapping.

The configured human model on Iris is selected by `SCIMILARITY_MODEL_PATH`; mouse uses
`SCIMILARITY_MODEL_PATH_MOUSE`. An explicit `model_path` overrides either setting. Which organism
models are actually present, their reference vocabulary sizes, and whether each one carries a
cell-search index are stated in the local-prerequisite inventory in your instructions; do not
search the filesystem for them.

## Per-cell outputs and interpretation

The per-cell tool writes:

- `obsm["X_scimilarity"]`, the learned embedding;
- `obs["scimilarity_prediction"]`, the kNN reference label;
- `obs["scimilarity_prediction_min_distance"]`, the nearest-reference distance;
- `obs["scimilarity_prediction_vs_second"]` and `..._vs_all`, the winning label's share of the
  top-two votes and of all `knn_k` votes;
- an annotated H5AD, a cell-level table, and a run report with model and gene-alignment evidence.

Larger distance means less similarity to the indexed reference and should raise out-of-domain or
novelty concern. Distances are model-specific and are not calibrated probabilities. The two vote
margins are how contested a call was: `vs_all` near 1.0 means the neighbors agreed, and a large
fraction of cells below 0.5 means the labels are being split between competing types — read them
before trusting a per-cell label.

`knn_k` (default 50) sets how many reference neighbors vote, and `weighting=true` weights each
vote by inverse distance instead of counting equally.

## Constrained annotation

Pass `target_celltypes` to restrict predictions to labels you expect in this tissue. The
unconstrained call still runs from the same embeddings, so you get both, plus their agreement and
the count of reassigned cells. Use it when unconstrained output contains types that cannot be
present in the sample; treat a low agreement as a signal about the safelist or the sample rather
than as a correction.

The labels must be exact labels of this model, which are long ontology strings — they are
validated first and unknown labels are refused with near-miss suggestions, because safelisting
works by deleting every other label from the index and a typo would otherwise narrow the reference
to nothing. Safelisted types that end up with no cells are reported: that is evidence too.

If cluster-level consensus is useful, call `summarize_scimilarity_by_cluster` afterwards with any
grouping column. That aggregation is optional and deliberately separate from inference.

## Querying the reference atlas

`query_reference_cells` answers a different question from label transfer: *which* reference cells
is this population like. It builds a centroid from selected cells, searches the reference
cell-search index, and returns, per query, the reference cell-type / tissue / disease / study
composition of the nearest cells, the neighbor-distance distribution, and a coherence statistic.
Use it to characterize a novel or ambiguous population, to see the tissue and disease context a
population resembles, or to test whether one cluster is really two things.

This is also the first point in an analysis where **tissue** can be read off the data rather than
assumed. If the dataset carried no tissue metadata, a broad query across the main clusters is
worth running for that alone: the returned `tissue` / `tissue_general` composition, weighted by
how large each cluster is and read against the reference background, is evidence for what this
sample is. Report it as what the reference says with its numbers, not as a settled fact — a
population can resemble a tissue it did not come from. Until something like this has run, tissue
is simply unknown, and "unknown" is the correct thing to say in a rationale or a report.

Select cells one of two ways, never both:

- `group_key` (plus optional `group_values`) selects groups of any obs column — a clustering, an
  annotation, a sample. Omitting `group_values` selects every group.
- `cell_ids` selects an explicit set of `obs_names`.

Then choose what a query *is* with `query_mode`:

- `centroid` (default) averages each selected group into one query vector. This is the right mode
  for "what is cluster 7 like".
- `cells` makes every selected cell its own query, for "what does this specific cell resemble".
  It costs one atlas search plus one metadata join per cell — about 17 seconds each on this host —
  so `max_query_cells` defaults to **10** and a larger selection is refused with its size rather
  than silently subsampled or left running for a long time. Coherence does not apply to a single
  cell.

Two options make the results interpretable rather than merely descriptive:

- `exclude_studies` drops hits from named reference studies. If your data is itself in the
  reference, self-matches otherwise dominate and tell you nothing; the removed count is reported,
  and a query left with nothing says so explicitly instead of looking like a weak match.
- `compare_to_reference_background` (default on) reports the reference-wide disease and tissue
  composition of the leading matched cell type, so "62% of my neighbors are from Crohn's tissue"
  can be read as enrichment against how common that is in the reference for that cell type.
  `summarize_samples` (default on) adds which studies and samples the hits came from as a fraction
  of each sample, separating a population scattered thinly across many samples from one
  concentrated in a specific sample.

Four more properties matter when you call it:

- **One call, many groups.** Opening the index costs about 90 seconds and tens of gigabytes of
  memory on this host; the searches then share it. Query every group you care about in a single
  call rather than one call per group.
- **Coherence is the expensive part.** Each query measured 55 seconds with coherence and 6
  seconds without, because coherence needs ten extra atlas searches. Set
  `measure_coherence=false` when you only need composition across many groups.
- **It refuses rather than truncates.** More groups than `max_queries` is an error naming the fix,
  not a silent subset. Groups below `min_query_cells` are reported as skipped.
- **Coherence is relative to `k`.** It counts how many of each sub-centroid's 100 nearest
  reference cells fall within the whole query's top-`k`, so compare coherence only across equal
  `k`. It is not measured for queries under 20 cells, and says why.

Reference labels and reference composition are hypotheses, not final truth. Reconcile them with
gene-level evidence and biological context when the task is final annotation.

Read [references/interpretation.md](references/interpretation.md) for model files, preprocessing,
distance and coherence semantics, query cost, and failure modes.
