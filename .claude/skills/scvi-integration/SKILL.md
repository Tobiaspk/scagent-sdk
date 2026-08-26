---
name: scvi-integration
description: Train a scVI latent model from raw counts and an explicit batch covariate (saving the model, training history, and X_scVI representation), and score how well an integration mixed batches. Use for representation learning or integration experiments; scientific adoption is a separate decision.
---

# scVI Latent Model

scVI is a variational generative model for single-cell count data. It models observed counts while
learning a lower-dimensional latent representation and can condition on an experimental batch
covariate.

`train_scvi_latent` requires raw counts in `layers["counts"]` and an observation column named
by `batch_key`. It validates those intrinsic inputs, trains the model, and saves:

- the model archive;
- training history;
- an H5AD with `obsm["X_scVI"]`.

`X_scVI` can be supplied explicitly to later representation and clustering operations.

`score_integration` verifies a correction after training: it computes per-cell neighborhood batch
entropy (in [0, 1], 1 = perfectly mixed) on the corrected latent `X_scVI` and, for comparison, on
the uncorrected `X_pca` baseline — never on `X_umap`, a distorting 2-D projection. It is the cheap,
read-only complement to `train_scvi_latent`: it answers *how well* an integration mixed samples,
not *whether* to integrate (that is decided once, on the uncorrected pass, by `investigate_batch`).
Use it once after integrating instead of re-running the gene-first batch investigation. A recurring
sample-linked gene program surviving after scVI is expected per-donor biology, not evidence the
integration failed — read success from the mixing improvement.

Evaluate batch evidence before adopting a corrected representation. Do not encode the biological
condition of interest as a nuisance batch merely to increase mixing.

Read [references/assumptions.md](references/assumptions.md).
