what i

## Final baseline

| Check | Before | After |
|---|---|---|
| `python -m pytest` | 70 passed | **124 passed** |
| `ruff check src tests scripts .claude/skills/*/scripts` | clean | clean |
| `mypy src/scagent_sdk` | clean (40 files) | clean (40 files) |
| `scagent-sdk capability validate` | 15/14/21 pass | 15/14/21 pass |
| `scagent-sdk doctor environment` | 6/6 healthy | 6/6 healthy |

## How the work was done

Implementation order **A→E** (finalize tests → batch floor binding → raw-count source → batch
plotting → pDC/plasma+Scrublet), with a bounded live PBMC validation wired in after C.

A repo-wide constraint shaped the test design: the agent `.venv` has **numpy but no
scipy/scanpy/anndata/matplotlib** (those run in the locked Pixi compute env through the broker).
Existing skills already handle this by splitting a pure, unit-testable core from a scanpy-executing
`_execute_*` function that tests monkeypatch. I followed that same pattern for every new test.

---

## P0 #1 — H5AD raw-count source selection + double-normalization protection

**Where I found it.** `.claude/skills/prepare-single-cell/scripts/prepare.py:103` (pre-change) did
`adata.layers["counts"] = adata.X.copy()` unconditionally after load — no source inspection, no
integer/nonnegative check. An H5AD with a normalized/scaled `X` therefore received a **false**
raw-count identity and count-matrix hash, which then propagated into doublet detection and DEGs.
Confirmed the defect was real (not silently fixed) and matched `docs/current-state.md` item 1 and
`docs/scientific-parity.md`.

**What I changed.**
- `.claude/skills/prepare-single-cell/scripts/prepare.py`: added pure helpers `_stored_values`,
  `_inspect_matrix` (finite/nonnegative/all-integer check, sparse + dense), `_choose_count_source`
  (auto/X/raw/layer with ambiguity + double-normalization refusal), and `_candidate_sources` (X,
  each layer, `.raw` aligned to current genes). `run()` now resolves + validates the source
  **before** QC/normalization, materializes the validated counts into `X` and `layers["counts"]`,
  drops stale layers/`.raw`, and emits a `count-source-selection.json` report. The selected source
  is recorded in provenance, the `count_representation` fact, and folded into the count identity.
- `.claude/skills/prepare-single-cell/capability.yaml`: added `counts_source`
  (enum auto/X/raw/layer, default auto) and `counts_layer`.
- `tests/unit/prepare_capability_test.py` (new, 17 tests): matrix inspection (integer/normalized/
  negative/sparse/empty) and every selection branch (auto-X, auto single-layer, auto raw,
  refuse-none, refuse-ambiguous, explicit X/layer/raw errors, unknown source).

## P0 #2 — Bind batch decisions to current identities

**Where I found it.** `src/scagent_sdk/floors/evaluator.py` (pre-change): `batch_decision` only
checked `batch.get("decision")` was truthy; `integration_authorized` only checked
`decision == "integrate"`. Neither compared against the active cell-set/count/clustering identity.
`investigate_batch` already validated input identity at execution time and stored `cell_set_id`/
`clustering_id` **nested inside `evidence`**, but the floors never read them, and `prepare.py`
nulled `cluster_qc`/`annotation`/`finalization` on re-prep but **not `batch`** — so a stale decision
survived a cell-set change. I also found scVI issues a new clustering identity without nulling the
`batch` fact, meaning a stale pre-integration decision could satisfy finalization (the P0 danger, in
the integration path).

**Design decision.** Bind both floors to `cell_set_id` + `count_representation_id` + `clustering_id`
(literal to the P0 text), exempting only `not_applicable`. Because scVI issues a new clustering
identity, this **intentionally** forces batch re-investigation on the integrated clustering before
finalization. Verified this is consistent: scVI preserves `cell_set_id`/`count_representation_id` in
`.uns` and stamps the new `clustering_id`, so re-investigation on integrated data satisfies all three
checks.

**What I changed.**
- `src/scagent_sdk/floors/evaluator.py`: rewrote `batch_decision` and `integration_authorized`,
  added `_batch_identities_current`.
- `.claude/skills/batch-investigation/scripts/investigate.py`: added pure
  `_resolve_input_identities` (validates + returns the three ids), recorded `count_representation_id`,
  and promoted all three identities to the `batch` fact top level.
- `.claude/skills/prepare-single-cell/scripts/prepare.py`: added `"batch": None` to the re-prep
  facts patch (RFC-7396 merge → deletes the stale fact).
- `tests/unit/floor_evaluator_test.py`: replaced the obsolete weak
  `test_integration_requires_explicit_integrate_decision` with identity-aware allow/stale/re-fire
  tests (reclustering, cell-set change, not_applicable exemption, integrate staleness).
- `tests/unit/batch_capability_test.py` (new): identity-resolution allow/stale cases.

## P0 #3 — High-cardinality batch figures

**Where I found it.** `.claude/skills/batch-investigation/scripts/investigate.py` (pre-change): the
stacked-bar figure sized its width by *cluster* count (`len(table)`) while the legend cardinality is
the *batch* count — so for 26 samples the width never grew and the default in-axes legend overplots.
The batch UMAP (`color=[batch_key, cluster_key]`) had the same unreadable-legend problem. No plotting
tests existed.

**What I changed.**
- `.claude/skills/batch-investigation/scripts/investigate.py`: added pure `_figure_layout(n_batches,
  n_clusters)` — external-legend stacked bar at low cardinality, colorbar **proportion heatmap** above
  12 batches, clamped figure dimensions, rotated tick labels. The batch UMAP legend is suppressed and
  the panel labeled when cardinality is high, so no visual claim relies on an illegible legend. The
  chosen `figure_mode` is recorded in details.
- `tests/unit/batch_capability_test.py`: layout-decision tests (bar vs heatmap threshold, bounded
  dimensions at extreme cardinality, legend-column growth).

## P0 #4 — Conservative annotation (pDC vs plasma, Scrublet)

**Where I found it.** Root cause was structural, not just prose:
`.claude/skills/marker-annotation/scripts/markers.py` `HUMAN_MARKERS` had a **"Plasma cell"** program
but **no pDC program**, and `GZMB` (a pDC marker) sat only in the "Cytotoxic lymphocyte" program — so
the model had no pDC program to score against, and a pDC cluster could only land on plasma/cytotoxic.
The only pDC/GZMB caution lived in `orchestrate-single-cell/references/workflow-decisions.md`, not in
the focused marker/finalize references, and **neither `marker-annotation` nor `finalize-analysis` had
an `evals/` directory**.

**What I changed.**
- `.claude/skills/marker-annotation/scripts/markers.py`: added a **Plasmacytoid dendritic cell**
  program (`LILRA4`, `IL3RA`, `CLEC4C`, `GZMB`, `IRF7`, `TCF4`, `SERPINF1`), made `GZMB` shared with
  the cytotoxic program (so the existing specificity weighting down-weights it), renamed
  "Dendritic cell" → "Conventional dendritic cell". Extracted pure `_marker_frequency` and
  `_score_programs` for testability.
- References + SKILL guidance: `marker-annotation/references/evidence-standard.md` and `SKILL.md`,
  `finalize-analysis/references/adjudication.md` and `SKILL.md` — pDC-vs-plasma discriminators,
  confidence that tracks conflict, generalize-upward, Scrublet-as-probability.
- Evals: new `marker-annotation/evals/evals.json` and `finalize-analysis/evals/evals.json`; added a
  Scrublet-as-probability case to `doublet-evidence/evals/evals.json`.
- `tests/unit/marker_capability_test.py` (new): pDC signature outranks plasma and vice-versa,
  isolated GZMB supports neither, GZMB is shared, pDC/plasma programs are disjoint.

## P0 #5 — Finalization tests

**Where I found it.** The strengthened contract already existed in
`.claude/skills/finalize-analysis/scripts/finalize.py` (`_validate_label_contract`: exact coverage,
override justification for every final≠DEG mismatch, unknown-cluster rejection, empty-value
rejection, confidence enum, staleness, non-overwrite) — but there was **no finalize test file**, so
the contract was entirely unverified. I also noticed there were no `prepare_`/`batch_`/`marker_`
capability test files either.

**What I changed.**
- `.claude/skills/finalize-analysis/scripts/finalize.py`: behavior-preserving refactor into pure
  validators (`_resolve_arguments`, `_validate_inputs`, existing `_validate_label_contract`), an
  `_execute_finalization` (scanpy I/O), and `_result_envelope`/`run` (envelope assembly).
- `tests/unit/finalize_capability_test.py` (new, 17 tests): manifest floors + strictness; the full
  label contract; non-overwrite + staleness; and the emitted state/decisions/artifacts.

---

## Live validation (deterministic-science items)

Added `scripts/validate_p0_live.py` (follows the existing `validate_doublets_live.py` pattern — real
brokered GPU compute, **no** model loop, **no** CellBender/scVI training). Datasets discovered on
Iris (I inspected h5ad structure with the compute env's `h5py`, since importing anndata/scanpy
outside the broker fails on cupy/CUDA_PATH):

- **Refusal:** `/data1/peerd/ibrahih3/SEACells/data/pbmc3k_processed.h5ad` — scaled `X`,
  log-normalized `.raw`, no integer counts → `auto` **refused** with the double-normalization message
  rather than preparing.
- **Layer auto-selection:**
  `/data1/peerd/ibrahih3/cs_agent/run_2026_07_14_154445/combined_truly_raw_annotated.h5ad` —
  log-normalized `X`, integer `raw_counts` **layer**, a misleadingly-named log-normalized `.raw`, and
  a 7-level `sample` column. `auto` correctly selected `layer:raw_counts` over the lognorm `.raw` and
  prepared 36,247 × 14,836 → 21 clusters. The `count-source-selection.json` report showed
  `X` count_like=false, `raw` count_like=false, `layer:raw_counts` count_like=true.
- **Batch identity binding:** the recorded batch decision matched the current cell-set,
  count-representation, and clustering identities; `batch_decision` passed, and a forced new
  clustering identity made it go stale.
- **Figure:** I inspected the real `batch-composition.png` (7 samples → bar path) — legend sits
  outside the plot area, cluster labels rotated and readable.

## Validation levels (kept distinct)

- **Deterministic contract:** all five items — 124 tests, ruff/mypy/capability-validate clean.
- **Live compute:** P0 #1 (refusal + layer auto-selection), P0 #2 (identity binding + staleness),
  P0 #3 bar-path render — one brokered run.
- **Still open (not claimed done):**
  - P0 #4 **model-behavior evals** are written but not *run* (separate from the deterministic scoring
    test).
  - Batch **heatmap-path live render** — no integer-count dataset with >12 batches exists locally
    (the 15-sample GSE155249 is lognorm-only, so prepare refuses it); the heatmap *decision* is
    unit-tested, only its live rendering is deferred.

## Files touched

Code / skills:
- `.claude/skills/prepare-single-cell/scripts/prepare.py`, `capability.yaml`
- `.claude/skills/batch-investigation/scripts/investigate.py`
- `src/scagent_sdk/floors/evaluator.py`
- `.claude/skills/marker-annotation/scripts/markers.py`, `references/evidence-standard.md`, `SKILL.md`, `evals/evals.json`
- `.claude/skills/finalize-analysis/scripts/finalize.py`, `references/adjudication.md`, `SKILL.md`, `evals/evals.json`
- `.claude/skills/doublet-evidence/evals/evals.json`
- `scripts/validate_p0_live.py` (new)

Tests (new): `tests/unit/{prepare,batch,marker,finalize}_capability_test.py`;
edited `tests/unit/floor_evaluator_test.py`.

Docs: `docs/current-state.md`, `docs/skill-catalog.md`, `docs/scientific-parity.md`.

---

# Follow-up: sandbox, code visibility, and error UX

Prompted by a real session (`sessions/1178245a-...`) where `run_analysis_code` failed twice on
"dunder attribute access is blocked", the `code/` directory looked empty, and the terminal dumped a
full traceback.

## What was actually happening (diagnosis)

- **Dunder failures were the sandbox, not the data.** `.claude/skills/analysis-workspace/scripts/run_code.py`
  AST-validated the submitted code and rejected **any** attribute starting with `__`
  (`run_code.py:42`, pre-change). The model's inspection snippets used `type(x).__name__` /
  `.__class__` and were rejected until it removed all dunders. The `runpy ... run_name='__main__'`
  frames in the traceback are the compute worker's own bootstrap, not the user's code.
- **`code/` looked empty because saved code goes elsewhere.** The session `code/` dir is created in
  `state/store.py:130` but **nothing ever wrote to it** (verified by grep). `run_code.py` writes
  `analysis.py` into staging, which the executor commits to
  `artifacts/capabilities/<execution-id>/analysis.py`. The two failed runs saved nothing at all
  because `_validate` raises before the file is written — only the successful third run produced a
  file. Confirmed on disk: one `analysis.py` under `artifacts/capabilities/`, `code/` empty, two
  `capability.execution_failed` events.
- **Both the model and the user got the full traceback.** `executor.execute` returned
  `Capability failed: <full exc>` as the tool result; `capabilities/assembly.py` passed that same
  string to `on_tool_failed`, which the terminal printed verbatim.

## What I changed

1. **Relaxed the dunder rule to a targeted denylist** (`run_code.py`): `BLOCKED_DUNDERS` now blocks
   only the object-graph-escape / dynamic-rebind dunders (`__globals__`, `__builtins__`, `__bases__`,
   `__base__`, `__mro__`, `__subclasses__`, `__subclasshook__`, `__init_subclass__`,
   `__class_getitem__`, `__code__`, `__closure__`, `__getattribute__`/`__getattr__`/`__setattr__`/
   `__delattr__`, `__reduce__`/`__reduce_ex__`). Routine `.__name__`/`.__class__`/`.__dict__`/
   `.__version__` introspection now runs. Blocked imports and `open`/`eval`/`exec` are unchanged.
2. **Populate `code/`** (`capabilities/executor.py`): `commit()` now mirrors any committed
   `text/x-python` artifact into the session `code/` directory as `<execid8>-<name>.py` (best-effort,
   idempotent). The authoritative provenance copy still lives with its result under
   `artifacts/capabilities/`.
3. **Friendlier failure UX** (`capabilities/executor.py` + `assembly.py`): a new
   `_concise_capability_error` reduces a wrapped subprocess traceback to its final exception line.
   The error return now carries `error_summary` (one clean line) for the terminal, while the
   model-facing `content` keeps the full detail. `assembly._handler` shows the concise line to the
   user.

## Verified (live, through the broker)

- Introspection that used to fail now runs: `arr.__class__.__name__` → `ndarray`,
  `np.__version__` → `2.4.6`.
- A genuine escape (`().__class__.__bases__[0].__subclasses__()`) is still blocked, now with the
  concise message `run_analysis_code failed: access to dunder attribute '__subclasses__' is blocked
  in custom code` — no traceback dump.
- `code/` was populated with `<execid8>-analysis.py`.

Deterministic tests: `tests/unit/sandbox_and_error_test.py` (dunder allow/deny, concise extraction)
and additions to `tests/unit/capability_executor_test.py` (code mirror, concise `error_summary`).
Baseline: **135 tests pass**, ruff + mypy clean, capability validate 15/14/21.

Files: `.claude/skills/analysis-workspace/scripts/run_code.py`,
`src/scagent_sdk/capabilities/executor.py`, `src/scagent_sdk/capabilities/assembly.py`,
`tests/unit/sandbox_and_error_test.py`, `tests/unit/capability_executor_test.py`;
docs `docs/current-state.md`, `docs/skill-catalog.md`.

---

# Follow-up: `describe_dataset` — deterministic H5AD content inspection

## Why (root cause of the repeated inspection failures)

Asking "what's in this dataset" failed 4× because there was **no deterministic tool for dataset
contents** — `inspect_dataset` only reports byte identity (size/signature/hash). The model was
forced to hand-write anndata code against a 684 MB backed file and tripped on newer-anndata API
(`n_vars`, no `.close()`) and backed-mode sparse (`_CSRDataset` has no `.data`). Band-aiding each
error fixes nothing; the fix is a robust content tool.

## What I learned from legacy `scagent/core/inspector.py`

Read-only reference (never imported). The transferable core is `dataset_facts()` + its matrix
helpers, not the 40-flag `DataState`:

- `_sample_matrix_values` samples stored values **randomly** and is hardened against cupy/GPU
  arrays, scipy sparse, and **backed `_CSRDataset`** (guarded coercion → empty, never a crash) —
  the exact object that broke the model.
- count detection is from **values, not dtype** (`fraction_integer_valued`) — float32-but-integer
  is counts; log-normalized `.raw` (decimals) is not.
- **facts vs judgment** is explicit: `dataset_facts` is judgment-free; roles/species/"is counts"
  belong to the model. That is our architecture.
- deliberately **not** ported: `recommend_next_steps` (a goal→step DAG, conflicts with
  floors-not-pipelines) and the verdict-style `DataState`; and the *exact* (not fuzzy) role-name
  matching is the version to keep if we add role ranking later.

## What I built

`describe_dataset` in the `inspect-dataset` skill (env `gpu-singlecell`), reimplemented from those
lessons:

- opens the H5AD **backed**, materializes only a bounded row block (`adata[:sample_rows].to_memory()`)
  for matrix value facts so a large matrix is never loaded whole;
- reports judgment-free facts: shape; `X`/layer/`raw` value facts (dtype, sampled min/max,
  `fraction_integer_valued`, `all_integer_sample`, `has_negative_sample`); per-column `obs`/`var`
  facts (dtype, cardinality, missingness, value-counts or numeric stats); `obsm`/`varm`/`obsp`/`uns`
  keys; gene-identifier and 10x-barcode signals; and normalization signals (`log1p_in_uns`, value
  evidence);
- writes `dataset-contents.json` + a readable `dataset-contents.md` and records `facts.dataset_contents`;
- pure helpers are numpy-only and unit-tested (including a duck-typed backed `_CSRDataset`, scipy
  sparse, and a GPU array); pandas/anndata stay inside `run`.

## Verified (live, through the broker, on the file that broke the model)

`Reyfman_all_raw.h5ad` (43,632 × 33,694, backed) described in **one call, no crash**:
`X` all-integer, min 1, max 1361, float32 sparse → correctly reads as counts despite the float
dtype; `cell_barcode` surfaced as an identifier (`unique_fraction=0.97`); `donor`/`sample` = 8 each
with value counts; gene names symbol-like. The backed `_CSRDataset` was handled by the bounded
row-block materialization.

Deterministic tests: `tests/unit/describe_capability_test.py` (10) — value facts, the backed-CSR
empty-sample guard, GPU pull-to-host, duck sparse, gene/obs-name signals, manifest.
Baseline: **145 tests pass**, ruff + mypy clean, capability validate **15/14/22**.

Files: `.claude/skills/inspect-dataset/scripts/describe.py`, `capability.yaml` (v0.2.0), `SKILL.md`;
`tests/unit/describe_capability_test.py`; count fixes in `tests/unit/cli_test.py`; docs
`docs/current-state.md`, `docs/skill-catalog.md`, `docs/scientific-parity.md`.
