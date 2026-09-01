# ADR 0011: Artifacts move to Zarr stores with hard-link dedup; Zarr v3 is deferred

Status: proposed. Feasibility and compatibility spiked against the real `rapids` env on 2026-08-04
(round-trip, format-degradation, and dedup-payoff measurements below are all reproduced, not
estimated). No code has changed yet; this records the decision and its gating facts before the
executor and retention work is scheduled.

## Context

Every scientific step today reads a full AnnData, mutates it, and writes a full new gzipped
`.h5ad`:

```python
adata = sc.read_h5ad(path)                       # whole object
# ... add one obsm, one obs column, an HVG mask ...
adata.write_h5ad(out, compression="gzip")        # whole object, again
```

The executor hardcodes this shape: `_MATRIX_SUFFIX = ".h5ad"`, with the standing note that *0 of 61
executions ever produced more than one `.h5ad`* (`capabilities/executor.py`). So a UMAP step that
adds a single `obsm['X_umap']` rewrites X, layers, raw, obs, and var — the entire matrix — as a
brand-new file. Across N steps this is ≈ N full copies of the matrix, which is the footprint the
retention work (ADR context D11, `state/retention.py`) was built to account for.

The retention accounting deliberately separates `apparent` / `unique` / `shared` / `reclaimable`
bytes precisely because a byte-sharing storage format would make them diverge. Today they do not
diverge: a monolithic HDF5 file is one inode, so **two versions can never share bytes** — no
hard-link, no reflink, no copy-on-write of the unchanged arrays. `shared` and `reclaimable` are
therefore structurally dormant. The measured symptom recorded in the retention commit — "6.5 MB
apparent, 0.0 MB freed" for a branch that only added an embedding — is not a fixture artifact; it is
the steady state of an H5AD-per-step store.

The container format is the lever, not the format label. A **Zarr store is a directory of
independently-addressable chunk files**, and that is the single property that unlocks everything the
retention layer already anticipates.

## What was measured (rapids env, 2026-08-04)

Runtime: `.pixi/envs/rapids` — `anndata 0.12.17`, `scanpy 1.12.1`, `zarr 2.18.7`, `numcodecs
0.15.1`, Python 3.14.

1. **Zarr v2 round-trips today, no lock change.** A 2000×1500 sparse AnnData with obs/var/obsm/
   layers writes and reads back cleanly through `write_zarr`/`read_zarr`. Sizes on that fixture:
   h5ad+gzip 2.25 MB (1 file); zarr v2 1.89 MB (66 files). Zarr was already *smaller* here, before
   any dedup, and its default codec is faster than gzip.

2. **Zarr v3 is not producible in this env, and the failure is silent.** anndata 0.12.17 exposes
   `settings.zarr_write_format`, `settings.auto_shard_zarr_v3`, and `settings.copy_on_write_X`, so
   the library is v3-aware. But setting `zarr_write_format = 3` under `zarr 2.18.7` still writes a
   **v2 store** — the on-disk metadata is 26 `.zarray`/`.zgroup` files and zero `zarr.json` (a v3
   store is the reverse). The round-trip "succeeds" only because it silently produced v2. Genuine v3
   requires zarr-python ≥ 3.1 (anndata pins `zarr!=3.0.*,>=2.18.7`, excluding the 3.0.x series).

3. **The dedup payoff is real and mechanically sound.** Writing a parent version, then a child that
   is identical except for an added `obsm['X_umap']`, produced a child store that is **98.9%
   byte-identical to the parent — 66 of 70 chunk files, 2.97 MB of 3.00 MB**. Only the new
   `X_umap` array files and the tiny consolidated `.zmetadata` differed. anndata's chunk output is
   **deterministic** for unchanged arrays, so content-identity hard-linking will actually match;
   this is not aspirational.

4. **Codec pipeline is ready for a future v3.** `numcodecs 0.15.1` already ships the `zarr3` codec
   entrypoints (`numcodecs.blosc`, `crc32c`, etc.).

## Decision

Adopt **Zarr v2 stores as the artifact unit now**, paired with **executor-owned hard-link dedup at
promotion time**, and **defer the Zarr v3 format** to a later, separately-gated migration. The
footprint win comes from the directory-of-chunks model plus dedup; it does not require v3, and v3 is
currently blocked (below).

### Artifact unit becomes a store directory

The immutable-artifact contract holds, but the unit changes from a file to a directory. A capability
writes `<name>.zarr/` beneath its staging directory; the executor promotes the whole directory as
one artifact, freezes it, and records it in the lineage/head index exactly as it records an `.h5ad`
today. The 48 KiB inline-result limit, non-overwrite, environment-provenance fingerprinting, and
exact/reconstructed resume are all preserved — a store directory is still content that gets frozen
and hashed; only the walk is over a tree rather than a single path.

This requires generalizing the executor's single-`.h5ad` assumption (`_MATRIX_SUFFIX`,
`primary_matrix_output`) — the D5 item the executor comment already flags as pending. A second
matrix output must become a declared field, not a guess, before a store can be the primary output.

### Hard-link dedup at promotion

When the executor freezes a child store whose parent version is known (the common case — most steps
have a lineage parent), it walks the child's chunk files and, for any file byte-identical to the
same path in the parent store, replaces the child's copy with a hard-link to the parent's frozen,
read-only file. Because frozen artifacts are immutable, sharing an inode is safe: nothing can mutate
a shared chunk in place. This is what turns retention's `reclaimable` from a dormant column into the
number a prune may actually promise — and it is exactly the case ADR-fix `ec091c4` hardened `st_nlink`
reclaimability for (a chunk with a live link in a retained sibling frees nothing when the candidate
is deleted).

Dedup is content-identity based (compare size then hash), so it is independent of anndata internals
and degrades safely: if a future anndata makes output nondeterministic, unmatched files are simply
copied, never corrupted.

### Compression

Switch the codec from HDF5-gzip to Zarr **blosc/zstd**. It is smaller and markedly faster on this
data shape, and it composes with dedup (deterministic per-chunk output). This is an independent win
available the moment artifacts are Zarr, before dedup lands.

### Why v3 is deferred, not chosen

The v3-specific benefit is **sharding** (`auto_shard_zarr_v3`): bundling many chunks into one
storage object to control file/inode count. That matters here — the 3000×2000 fixture already
produced 70 files, and a real 500k×30k matrix chunked naively becomes thousands of small files,
which is genuine inode/small-file pressure on the Iris GPFS-class filesystem. So v3 is worth wanting
eventually. But:

- **scimilarity 0.4.1 hard-pins `zarr<3.0.0`** and runs in the *same* `rapids-main` runtime as every
  other capability (`configs/environments/iris.toml`). It is the **only** hard `zarr<3` resolver pin
  in the env (fsspec/tifffile/xarray constrain zarr only inside uninstalled test/io extras; anndata
  allows `>=3.1`; rapids-singlecell/cupy/cudf don't pin zarr at all). So scimilarity alone blocks the
  main env from zarr 3, and bumping zarr is a `pixi.lock` change (large blast radius per AGENTS.md).
  The resolution is to **split scimilarity into its own zarr-2 sidecar runtime** — the recommended
  path, and cleaner than it first appeared (see below) — leaving the main env free to move to v3.
- **Sharding is in tension with dedup.** A shard is one file bundling many chunks; if a shard mixes
  changed and unchanged chunks it can no longer be hard-linked. A v3 migration must shard along array
  boundaries so unchanged arrays (obs, var, X) stay in dedup-able shards, or it trades the byte-dedup
  win for the file-count win. That is a design decision to make deliberately, with dedup already in
  place to measure against — not a default to flip.

So: v2 + dedup now (no lock change, blocked by nothing, proven); v3 later, gated on scimilarity and
on shard-vs-dedup alignment.

### The scimilarity sidecar (verified 2026-08-04)

Splitting scimilarity out is the v3 path, and inspection of the installed package makes it low-risk:

- **scimilarity's zarr use is a training-corpus concern, not an inference one.** All substantive zarr
  usage lives in `zarr_dataset.py` / `zarr_data_models.py` / `training_models.py` (the training data
  loaders), which our annotation-only usage never invokes. The only zarr reference on the inference
  path is one type-check in `cell_embedding.get_embeddings`: `isinstance(X.data, zarr.core.Array)`,
  guarding an eager-materialize branch for zarr-backed sparse arrays. We pass in-memory scipy
  matrices, so it is functionally dead — but the `import zarr` still runs, and `zarr.core.Array` is a
  **v2-only attribute**, so the line would `AttributeError` under zarr 3. That rules out running
  unmodified scimilarity against zarr 3 in-place (would need a vendored/upstream one-line guard, which
  ADR 0010's additive-only overlay cannot supply); isolation, not removal, is the safe lever.
- **scimilarity does not need the RAPIDS stack.** Its hard deps are `torch, pytorch-lightning,
  scanpy, tiledb, tiledb-vector-search, hnswlib, captum, obonet, pyarrow, zarr<3` — no `cupy`, no
  `rapids_singlecell`. So the sidecar does not share the RAPIDS RMM/cupy pool that made splitting
  segger intractable; it is a torch-on-GPU sidecar directly analogous to `cellbender`. Our skill adds
  only `scanpy` (zarr-3-agnostic) on top.

Cost: a second env carrying the torch+scanpy surface, cross-process handoff for annotation (the
pattern already exists), and the ~47 GiB reference atlas activating against the sidecar. Acceptable,
and it converts P3 from "blocked, indefinite" to "scheduled work behind a known env split."

### Upstream has already fixed this (verified 2026-08-04) — the sidecar is now the fallback

Checking upstream changed the recommendation. scimilarity `main` (HEAD `3ce3ec8`, 2026-03-14) is the
commit *"Add code to work with both zarr 2 and 3, add tests (#43)"* + *"Run tests for zarr v2 and v3
(#44)"*. It both **removes the upper bound** (`setup.cfg` now reads `zarr>=2.6.1`, no `<3.0.0`) and
**fixes the code** (`cell_embedding` now uses `zarr.Array`, which is a valid top-level alias in *both*
zarr 2.18.7 and zarr 3 — confirmed by import in our env). So the incompatibility that motivated the
sidecar is already solved; it is simply **unreleased** — PyPI's latest is still 0.4.1 (predating the
fix, still `zarr.core.Array` + `zarr<3.0.0`), and no tag exists past 0.4.1.

That yields a cheaper v3 path than the sidecar:

- **Preferred: wait for the next scimilarity release** that includes `3ce3ec8`, then just relax our
  pin and bump zarr. Zero code on our side; timeline is external but the work is done upstream.
- **If we want it sooner: pin scimilarity to commit `3ce3ec8`** via `git+https` (reproducible through
  `pixi.lock`), which gets dual-zarr support now, and bump the main env's zarr to ≥3.1.
- **Sidecar is now the fallback**, only if neither lands and we still need v3.

One caveat that gates both fast paths: commit #43's own message says *"ignore 3.14 for now"* — the
dual-zarr test matrix explicitly skips Python 3.14, which is exactly our runtime. So the upstream
fix is not upstream-*tested* on our interpreter. Before adopting either fast path we must run
scimilarity's suite (or at minimum the annotation skill end-to-end) under py3.14 + zarr 3 ourselves.

## Consequences

- Retention's `reclaimable`/`shared` accounting becomes live instead of dormant; a prune can promise
  real bytes, which is the prerequisite for any actual deletion path (still unbuilt — this ADR does
  not add deletion; it makes deletion meaningful).
- The steady-state footprint of an N-step analysis drops from ≈ N matrix copies toward one matrix
  plus per-step deltas. On the measured add-an-embedding case that is a ~33× reduction in new bytes
  for that step (30 KB written vs a full ~3 MB copy).
- The artifact unit is a directory. Anything that assumed one file per version — the lineage/head
  index walk, output-view size reporting, retention's `du`-style accounting — must treat a store
  as a tree. Retention and the executor walk trees; the output view now projects a declared `.zarr`
  directory as one intermediate data artifact rather than silently dropping it.
- Reproducibility is unchanged: stores are frozen, hashed, and fingerprinted like files. Hard-linking
  is invisible to readers and safe under immutability.
- We take on more inodes per artifact under v2 (no sharding). Acceptable at current dataset sizes;
  the file-count ceiling is the concrete trigger to revisit the v3 migration.

## Build order

- **P0 (landed on `feat/zarr-artifact-storage`)** — Executor accepts a `.zarr` store directory as a
  first-class artifact, sized by its tree (the atomic staging move already carries a nested tree).
  Every pipeline reader tolerates a `.zarr` input via a byte-identical `_read_matrix` recipe
  (drift-guarded), and every intermediate writer emits a `.zarr` store via `_write_matrix` (blosc by
  default); input guards relaxed `is_file()` → `exists()`; lineage reconstruction fallback recognizes
  `.zarr`. `finalize` (portable deliverable), `convert` (`.h5ad`-only), and CellBender (`.h5`) keep
  their native formats by decision. Proven: preprocess writes a real zarr v2 store live and it round
  -trips; full deterministic suite green. No dedup yet — pre-dedup this is roughly size-neutral vs
  gzip h5ad; the win is P1. Full multi-skill live validation belongs to a GPU session.
- **P1 (landed on `feat/zarr-artifact-storage`)** — After a result commits, the executor hard-links
  every file in the new artifact that is byte-identical (same relative path) to its lineage parent
  (`resolved_input_execution_id`) onto the parent's frozen inode. Runs post-commit, not under the
  session lock; best-effort and per-file guarded (cross-device/race keeps its own copy, never fails a
  commit). Retention needed no change — it already measures by `(dev, inode)`/`st_nlink`, so
  `reclaimable` now diverges from `apparent`. Measured on a real read-modify-write cycle (read a
  `.zarr`, add a clustering + UMAP, write a new `.zarr`): 99.7% of the child's bytes are byte
  -identical to the parent (174/187 files) — that step stores ~0.05 MB of new bytes instead of
  duplicating a 15 MB matrix. This is the footprint payoff. **Correction (later commit `ff96ef0`):**
  the first implementation (`d433ec4`) matched files by artifact-relative *path*, but each step
  names its store differently (`pca.zarr` vs `neighbors.zarr`), so nothing matched on a real
  pipeline (~0% dedup) — the unit tests reused one store name and missed it. Dedup now matches by
  **content** (sha256, size-bucketed, filecmp collision guard). Verified on the real session's
  `pca.zarr → neighbors.zarr`: 1272 files linked, 374 MB reclaimed (68% of the child) vs 0 before.
- **Portable-delivery follow-up (2026-08-26)** — `export_anndata` is the ungated rung between an
  internal Zarr working version and floor-gated scientific finalization. It writes one compressed,
  round-trip-verified H5AD, registers it as a user-facing data artifact, preserves the AnnData
  payload, records the consumed lineage input, and does not advance the scientific head. This
  keeps Zarr-per-step dedup while making partial analyses deliverable without an ad-hoc script.
- **P2** — Guarded prune that frees only `reclaimable` bytes, plus the disposition vocabulary
  (`retained`/`pinned`/`rejected`) retention already flagged as missing. First point at which bytes
  are actually deleted.
- **P3 (v3 migration; upstream fix exists, unreleased)** — Preferred: adopt a scimilarity release
  containing `3ce3ec8` (or pin that commit via `git+https`), relax our `zarr<3`, bump the main env's
  zarr to ≥3.1. Sidecar is the fallback. Either way: first validate scimilarity under **py3.14 +
  zarr 3** (upstream skips 3.14), then run the full scanpy / rapids-singlecell / scvi / cellbender
  round-trips, with a shard grid aligned to array boundaries so sharding and dedup do not fight.

## Open questions

- When does the scimilarity release carrying `3ce3ec8` land, and does its dual-zarr path actually
  pass on py3.14 (upstream's own matrix skips 3.14)? These decide whether v3 is a pin bump or needs
  the sidecar. A `git+https` pin to `3ce3ec8` is the bridge if we want v3 before the release.
- Should raw-count inputs adopted from outside the analysis be converted to Zarr on ingest, or kept
  as-is and only *derived* artifacts stored as Zarr? Converting on ingest maximizes dedup (the
  parent of the first step is already a store) but touches user-supplied files.
- Reflink (`--reflink=auto` / `FICLONE`) instead of hard-link would give copy-on-write semantics and
  survive a future in-place-mutation mistake, but depends on the underlying filesystem supporting it
  (GPFS generally does not). Hard-link is the portable floor; worth probing the real mount.
