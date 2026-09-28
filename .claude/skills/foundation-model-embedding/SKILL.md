---
name: foundation-model-embedding
description: Embed single cells with a foundation model (Geneformer, scGPT, UCE, TranscriptFormer, STATE SE) through biorun's tested transform layer, always beside a PCA-50 baseline scored with identical metrics, and check beforehand - without weights or a GPU - whether a dataset is acceptable to each model at all.
---

# Foundation model embedding

Five single-cell foundation models, one typed call, and a rule: **the model's numbers are
never reported without the baseline's.** `biorun` reproduces each model's own published
preprocessing (verified token-for-token against the authors' code), refuses input the model
would silently mishandle, computes PCA-50 on the same cells with the same metric code, and
returns provenance that identifies the weights, the transform version and the seed.

## When a foundation model embedding is warranted

Reach for one of these models when you can say which of the following you need. Otherwise
PCA is the correct answer and this skill's own measurements are the reason.

**Warranted**

- **Transfer to another dataset.** A shared embedding space across experiments is the thing
  PCA structurally cannot give you: PCA components are fitted per dataset and are not
  comparable across them.
- **A pretrained prior for few cells.** With a few hundred cells of a rare population,
  variance-based components are dominated by noise while a pretrained encoder is not.
- **A downstream task that needs the model, not the embedding** — perturbation prediction,
  in-silico gene knockouts, model-specific fine-tuning.
- **A stated requirement to compare against a published model.**

**Not warranted**

- **Clustering and cell typing on one ordinary dataset.** Measured on PBMCs: all four cell
  models beat PCA-50 by only 1.5-2.4 accuracy points on cross-validated kNN label transfer,
  and PCA's macro-F1 was the *best* of the five on one of the two datasets. A 20-minute GPU
  job for one to two points, when marker-based or reference-based annotation is available,
  is usually not the right trade.
- **A dataset whose labels came from a PCA pipeline.** Leiden or louvain clusters computed
  from a PCA embedding of the same cells make the baseline score against labels derived from
  the baseline. That comparison is circular and must not be reported as a model comparison;
  say so explicitly if it is all that exists.
- **Anything where you cannot name the label column both methods will be scored on.**

## Always check the input first

`check_dataset_for_fm` runs each model's validator only: no weights, no GPU, seconds rather
than a job. It exists because these five models disagree about what an input *is*, and the
disagreements are silent:

| model | wants | refuses on |
|---|---|---|
| Geneformer V2 | raw integer counts, **Ensembl ids** | gene symbols; non-integer values |
| scGPT human | raw integer counts, **HGNC symbols** | Ensembl ids (zero genes map) |
| UCE 4-layer | raw integer counts, symbols with an ESM-2 embedding | wrong identifier space |
| TranscriptFormer | raw counts, Ensembl ids, **an `obs['assay']` label** | missing or unrecognized assay |
| STATE SE-600M | raw counts, HGNC symbols | a matrix whose largest count is <= 35 |

Two of those refusals are the point of running the check. Handing scGPT Ensembl ids maps
*zero* genes. TranscriptFormer maps an unrecognized `obs['assay']` string to `unknown`
without complaining, and the difference between `10x 3' v2` and `10x 3' v3` - two equally
correct descriptions of one experiment - moves its embeddings by 0.93 mean cosine. STATE SE
guesses whether its input is counts or log-data from whether any value exceeds 35, and
exponentiates your counts if it guesses wrong.

A refusal is an answer, not a failure. Report which models accept the dataset and what the
others need; do not work around a refusal by transforming the data until it passes.

## Reading the result

`embed_cells_with_fm` returns, for the model and for PCA-50 computed on the same cells:

- `knn_accuracy` and `knn_macro_f1` - cross-validated (5-fold, k=15) label transfer on the
  chosen `label_key`, so the number is out-of-fold rather than train-on-all;
- `silhouette` - cluster compactness, which rewards separation rather than recoverability;
  a model can win here while losing on kNN, and scGPT did;
- `batch_silhouette` when a batch column exists.

**Report both columns.** "Model X reached 0.94 accuracy" is not a result; "0.94 against the
PCA-50 baseline's 0.94 on the same labels" is. When the model does not beat the baseline,
say that plainly - it is the most useful thing you can tell a user about whether to keep
paying for the GPU.

`pooling` defaults to each model's own published convention (`cls` for scGPT, UCE and STATE
SE; `mean` for the others) because that is what their papers report; overriding it is
legitimate but changes the numbers and should be stated.

`max_cells` subsamples - seeded, and stratified on `label_key` when there is one - for a
first look at a large dataset. Say that a reported number came from a subsample.

## Provenance

Every run records the model id, the sha256 of the weight files, the transform id and
version, a hash mixing the input data with the transform's parameters and pinned asset
versions, the seed, the device and the package versions. Two of these models
(scGPT, UCE) have stochastic preprocessing upstream, so a seed is part of the result rather
than a detail: the same cells embedded twice with different seeds are not the same
embedding. Quote the `transform_input_hash` when comparing two runs.

Licences differ and are on the model card: Geneformer is Apache-2.0, scGPT and
TranscriptFormer MIT, UCE MIT, and **STATE (SE and ST) is non-commercial for both weights
and code**. If a user's work is commercial or commercially sponsored, STATE is not available
to them; say so rather than returning its numbers.

## What this skill does not do

It does not annotate cells - use `celltypist-annotation`, `scimilarity-annotation` or
`marker-annotation` for that, and prefer them for ordinary cell typing. It does not
fine-tune. It does not download weights: a model reported as unavailable in readiness names
the command that would fetch it, and that is a decision for the user.
