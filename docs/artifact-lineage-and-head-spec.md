# Artifact lineage and the identity head

Status: v2.2 proposal, revised after three review rounds; not implemented
Date: 2026-07-27
Scope: move H5AD continuity from model discretion into the capability executor, without
restoring an in-memory AnnData, without introducing tool-order enforcement, and without changing
floor semantics.

## Revision history

**v1 → v2.** The layer and owner were accepted; the topology was wrong and is replaced.

| v1 | v2 | why |
| --- | --- | --- |
| nodes keyed by `identity_key` | nodes keyed by `execution_id` | identity cannot represent two deliberate branches carrying the same identities — the bug class this spec exists to fix |
| `parent_key = active` at commit | `parent = producer of the resolved input` | `a32a26bb`, `7f05a905`, `a5236aba` all read one UMAP; the v1 rule recorded them as a chain, fabricating ancestry |
| reject sibling inputs | reject **any** explicit tracked non-head input | an ancestor path defeated v1's own regression test (see [Acceptance case 3](#acceptance-cases)) |
| "bump `SESSION_SCHEMA_VERSION`, default `lineage` to empty" | split the constant, real v1→v2 reducer | one shared constant is validated by both metadata and state; bumping it makes every existing session refuse to open |
| identity = all present `analysis.*.id` | versioned allowlist, **fail closed** on unknown axes | auto-absorption silently changes semantics when an axis is added later |
| `convert_gene_ids` noted as evidence only | decided: `feature_schema` axis + fix a pre-existing stale hash | leaving semantics undecided leaves a live correctness gap |

**v2 → v2.1.** Topology accepted; four contract defects corrected.

| v2 | v2.1 | why |
| --- | --- | --- |
| branch commit leaves `active_execution_id` unchanged | branch commit is **state-isolated**: its `facts_patch` lands on the node, not in global facts | a non-activating clustering branch would still overwrite `facts.analysis.clustering`, so the active H5AD and the active facts would describe different clusterings |
| allowlist of six axes, fail closed | allowlist of the **eight** axes actually present | `expression`, `hvg`, and `umap` all carry ids in the CRC state; fail-closed on a six-axis list would raise on the reference session immediately |
| D8a `feature_schema` axis, "numeric evidence survives" | D8 **safe option**: re-mint count representation, accept cascade | `dataset_revision.id` is derived from `count_id` (`counts.py:171-176`), so the cascade is unavoidable; and mito/ribo are computed by gene-name prefix (`qc.py:105-110`), so a relabel changes QC *values*, not just labels |
| "omitted `path` → active head" | D5 must also remove `path` from schema `required` and inject before dispatch | `required: [path]` with `additionalProperties: false` appears in 20 manifests; an omitted path is rejected before `execute` is ever called |

**v2.1 → v2.2.** Three state-contract gaps closed.

| v2.1 | v2.2 | why |
| --- | --- | --- |
| node scope inferred from shape ("contains an identity id field") | central **versioned root-scope registry**, fail closed on unknown roots | the rule is disproven by the reference session: `reference_runs.<method>.<exec>.cell_set_id` exists, so shape classifies it node-scoped while it is semantically session-scoped. Scope is semantic and cannot be inferred from JSON |
| read-only tools "leave `lineage` byte-identical" | read-only evidence **attaches to a node**; the invariant becomes "topology, paths, and active head unchanged" | `facts_snapshot` lives inside `lineage`, so the v2.1 invariant and the snapshot model were mutually contradictory. Cluster-QC and annotation evidence in the CRC session came from read-only executions |
| D8 "invalidates all count-bound evidence" | D8 names an **explicit invalidation patch** including `annotation` and `finalization` | `current_annotation_evidence` checks only `clustering_id` (`floors/evaluator.py:210-239`), which a relabel does not change — so annotation evidence would stay "current" and permit finalization against the old gene vocabulary |

## Contents

1. [Problem](#problem)
2. [Observed evidence](#observed-evidence)
3. [Why point fixes do not generalize](#why-point-fixes-do-not-generalize)
4. [Terminology](#terminology)
5. [Proposed change](#proposed-change)
6. [Migration](#migration)
7. [Acceptance cases](#acceptance-cases)
8. [Invariants and tests](#invariants-and-tests)
9. [Residual open questions](#residual-open-questions)
10. [Non-goals](#non-goals)
11. [Review checklist](#review-checklist)

## Problem

A session carries scientific state in two substrates with different merge semantics:

| substrate | merges by | owned by | can drift |
| --- | --- | --- | --- |
| `state.facts` | identity key, via `apply_merge_patch` at commit (`store.py:53`, `executor.py:478`) | executor | no |
| the H5AD chain | whichever `path` argument the model passed | the model | yes |

Facts converge regardless of the order tools run in. The matrix converges only if the model threads
output paths forward correctly. Every capability that adds columns to the matrix inherits this
exposure; nothing in the executor, the floors, or the capability contract constrains which file a
tool is handed.

The failure mode: facts, review, and report stay correct while the delivered H5AD silently lacks
the `obs`/`obsm` produced on a sibling branch.

## Observed evidence

Source: `sessions/run_20260727T052254Z_58b435` (CRC, 960 cells, 14 clusters).

**The reported incident chained correctly by model choice, not by construction.**

```
clustered.h5ad                     a5236aba
  └─ scimilarity-annotated.h5ad    48ebcd21   (read a5236aba)
       └─ celltypist-annotated.h5ad 294debc7  (read 48ebcd21)
            └─ final-annotated.h5ad 2cdb90eb  (read 294debc7)
```

Had CellTypist been handed `clustered.h5ad` — equally consistent with both skills' contracts, since
both need only raw counts plus gene symbols — both branches would carry identical `cell_set_id` and
`clustering_id`, so no floor would object, and `final-annotated.h5ad` would ship without
`scimilarity_prediction`.

**In this incident the adjudication remained correct, because its inputs were facts.**

```
annotation.evidence.scimilarity.cell_set_id   == annotation.evidence.celltypist.cell_set_id
annotation.evidence.scimilarity.clustering_id == annotation.evidence.celltypist.clustering_id
annotation.review.evidence_ids = {markers, scimilarity, celltypist}
```

`finalize` adjudicated from the evidence CSVs those facts reference, not from its input file's `obs`.
This is a statement about *this* incident, not a general guarantee: unchanged `analysis.*` ids do
not prove a mutation was column-only. See [D8](#d8--gene-vocabulary-identity-take-the-safe-option).

**A head pointer already exists, and is already stale.**
`facts.analysis.dataset_revision.prepared_path` points at `.../a5236aba/clustered.h5ad` — the
correct chosen clustering, but two commits behind the true head; neither annotation advanced it.

**The convention is duplicated per skill and has drifted.** Twelve skill scripts write an H5AD;
`prepared_path` is set by eight, re-asserted by one read-only skill, omitted by four:

| skill script | writes H5AD | sets `prepared_path` |
| --- | --- | --- |
| `single-cell-counts/scripts/counts.py:225` | yes | yes |
| `single-cell-qc/scripts/qc.py:516,626` | yes | yes |
| `doublet-evidence/scripts/doublets.py:491,738` | yes | yes |
| `expression-preprocessing/scripts/preprocess.py:88,186` | yes | yes |
| `dimensionality-reduction/scripts/reduce.py:168,268,351` | yes | yes |
| `single-cell-clustering/scripts/clustering.py:106` | yes | yes |
| `cluster-qc/scripts/evaluate.py:1257` | yes | yes |
| `scvi-integration/scripts/integrate.py:101` | yes | yes |
| `batch-investigation/scripts/investigate.py:397` | **no** | yes (re-asserts its input) |
| `inspect-dataset/scripts/convert.py` | yes | **no** |
| `scimilarity-annotation/scripts/run_scimilarity.py` | yes | **no** |
| `celltypist-annotation/scripts/run_celltypist.py` | yes | **no** |
| `finalize-analysis/scripts/finalize.py` | yes | **no** |

The four omissions are exactly the tools implicated in the incident. One missing owner, not four
defects.

**Footprint consequence.** 58 executions produced 15 H5ADs and 228 MB of artifacts from a 12 MB
input (≈19×). Three sibling `clustered.h5ad` files exist from a resolution sweep; two are
unreachable from any live result (~28 MB). No retention or pruning policy exists anywhere in
`src/scagent_sdk`.

## Why point fixes do not generalize

- **Rematerialize output from facts.** Works only for column-shaped results. Cannot express
  `X`-shaped results: normalization, HVG subsetting, filtering, CellBender, scVI latents.
- **Set `prepared_path` in the four missing skills.** A ninth and tenth copy of a convention that
  has already drifted four times, and nothing for the eleventh capability added later.
- **Add a floor.** Floors are predicates over identities. Both branches in the failure mode carry
  *identical* identities, so no predicate over `facts.analysis.*` can separate them.

The property that must hold — "the matrix a tool receives is the current one for its lineage" — is a
statement about executor dispatch, so it belongs in the executor.

## Terminology

- **Lineage forest.** Each H5AD-producing execution has exactly one parent. Merges are out of
  scope, so this is a forest, not a DAG. If a future capability combines two matrices, the model
  extends to multiple parents; nothing here forecloses that, and nothing here assumes it.
- **Node.** One H5AD-producing execution. Keyed by `execution_id`.
- **Head.** The most recent node on a given line of descent. `active_execution_id` names the one
  the executor resolves omitted inputs against.
- **Branch.** A node whose parent was not the active head at dispatch. Branching is a topology
  fact, not an identity fact.
- **Identity signature.** An indexed attribute of a node, used for staleness and overlay
  eligibility queries. **Never** node identity.

## Proposed change

### D1 — An executor-owned lineage forest in session state

Add a top-level `lineage` field to `SessionState` (`contracts/state.py:72`), sibling to `artifacts`.

```jsonc
"lineage": {
  "active_execution_id": "294debc7-…",
  "nodes": {
    "294debc7-…": {
      "parent_execution_id":  "48ebcd21-…",
      "head_path":            "artifacts/capabilities/294debc7-…/celltypist-annotated.h5ad",
      "identity_signature":   "identity:v1:sha256:…",
      "requested_input":      "artifacts/capabilities/48ebcd21-…/scimilarity-annotated.h5ad",
      "resolved_input_execution_id": "48ebcd21-…",
      "branch_intent":        false,
      "created_by":           {"skill_id": "celltypist-annotation", "tool_name": "run_celltypist_annotation"},
      "facts_snapshot":       { "analysis": { … }, "annotation": { … } }
    }
  },
  "by_identity_signature": { "identity:v1:sha256:…": ["48ebcd21-…", "294debc7-…"] }
}
```

Top-level, **not** under `facts`, is deliberate: skills return `facts_patch` and must never write
the index (AGENTS.md — "never let a skill edit session state directly"). Placing it beside
`artifacts` keeps the executor the sole writer, matching `executor.py:481-486`.

**`identity_signature` allowlist (v1).** Computed from a versioned allowlist of identity-bearing
axes; an `analysis.*.id` present in facts but absent from the allowlist **raises** rather than
falling through, so adding an axis is a deliberate, reviewed act. The v1 list is every axis that
currently carries an id — verified against the CRC state, all eight present:

`dataset_revision`, `cell_set`, `count_representation`, `representation`, `clustering`,
`expression`, `hvg`, `umap`.

Because signatures no longer define topology, over-inclusion is harmless: an exact signature only
makes staleness and overlay-eligibility queries finer. A shorter list would fail closed on the
reference session on day one.

**`facts_snapshot` and the node/session partition.** Every matrix node carries the facts that
describe *it*. Scope is declared in a **central versioned registry** in the runtime package — one
table, not a per-skill manifest field:

| fact root | scope | rationale |
| --- | --- | --- |
| `analysis` | node | the identity axes themselves |
| `annotation` | node | evidence and review bound to a clustering |
| `batch` | node | evidence and decision bound to cells/counts/representation |
| `cell_qc` | node | metrics and thresholds bound to a cell set |
| `cluster_qc` | node | three-axis evidence bound to a clustering |
| `doublets` | node | scores and review bound to a cell set |
| `finalization` | node | labels bound to a clustering |
| `dataset`, `dataset_contents` | session | properties of the input file |
| `gene_conversion` | session | a record of a conversion, not a derived state |
| `reference_runs` | session | a reuse cache keyed internally by execution and cell set |
| `custom_analysis` | session | free-form provenance |

An unregistered root **fails closed**, so adding a fact root is a deliberate act.

Scope cannot be inferred from JSON shape. A "node-scoped iff it contains an identity id" rule is
disproven by the reference session: `reference_runs.celltypist.<execution_id>.cell_set_id` exists,
which would classify `reference_runs` node-scoped, yet it is a reuse cache that is already
self-describing — snapshotting it per node would duplicate it and defeat reuse across branches.
Scope is semantic.

Global `state.facts` is then defined as *the active node's snapshot plus the session-scoped roots*.
Partitioning only `analysis` is insufficient: evidence carrying a branch's `clustering_id` would
otherwise merge globally while the clustering fact it refers to does not exist there.

### D2 — Node creation at commit; parent is the resolved input's producer

In `CapabilityExecutor.commit` (`executor.py:451`):

1. No declared primary matrix output → **no topology change**: no node is created, no parent is
   recorded, `active_execution_id` and every `head_path` are untouched. But such a tool may still
   write node-scoped facts, so it is not inert. See "Read-only evidence commits" below.
2. Otherwise create a node with `parent_execution_id = resolved_input_execution_id`, recorded at
   dispatch (D3), and set `facts_snapshot = apply_merge_patch(parent.facts_snapshot, node-scoped
   part of facts_patch)` — derived from the **parent node**, never from current global facts.
3. **Non-branch commit:** set `active_execution_id` to the new node and merge the node-scoped patch
   into global facts as today. Session-scoped roots merge globally in both cases.
4. **Branch commit** (`branch_intent: true`): the active head and global `facts.analysis` are left
   **unchanged**. The patch lands only on the node. The branch is reachable via `checkout_head`
   (D4).

Parentage never consults the active head at commit time. This is the v1 defect: three clusterings
read one UMAP, and deriving parentage from a mutating `active` recorded them as a chain.

State isolation for branches is not optional. `commit` currently applies `facts_patch` globally and
unconditionally (`executor.py:478-486`), so without rule 4 a non-activating clustering branch would
rewrite `facts.analysis.clustering` while the active H5AD still holds the previous clustering — the
active matrix and the active facts would describe different objects, and every floor keyed on
`clustering_id` would evaluate against a clustering that is not in the active file.

**Consequence, stated plainly:** evidence produced on a branch is invisible to floors and to
fact-reading tools until that branch is checked out. Comparing branches is therefore done from tool
result envelopes and artifacts, not from facts. This is the correct meaning of "branch," but it is a
real workflow change and `orchestrate-single-cell` guidance must say so (D7).

**Read-only evidence commits.** Most scientific evidence in a session is produced by tools that
write no H5AD: `investigate_batch`, `evaluate_marker_evidence`, `review_annotation_evidence`,
`summarize_celltypist_by_cluster`, `summarize_scimilarity_by_cluster`, and the cluster-QC evidence
operations. In the CRC session, `annotation.evidence.celltypist` came from execution `35cf4cf9` and
`annotation.evidence.scimilarity` from `32b3daf7` — both read-only. If their patches updated only
global facts, a later `checkout_head` would restore an older snapshot and silently discard them.

Rules:

1. No matrix output → no topology, path, or head change, ever.
2. Node-scoped roots in the patch update the **target node's** `facts_snapshot`.
3. They additionally merge into global facts **only when the target node is the active head**.
4. A tool invoked without a matrix input (a pure review over facts) targets the active node.
5. A read-only tool invoked against a tracked non-active input targets **that** node, and is
   permitted only under the same explicit branch intent D3 requires for matrix inputs.
6. Session-scoped roots merge globally in every case, and are never snapshotted.

Rule 2 is why the v2.1 test "read-only tools leave `lineage` byte-identical" was wrong:
`facts_snapshot` lives inside `lineage`, so that invariant contradicted the snapshot model. The
correct invariant is that **topology, `head_path` values, and `active_execution_id` are unchanged**.

### D3 — Path resolution policy

In `CapabilityExecutor.execute`, alongside `_resolve_session_paths` (`executor.py:171`, helper
`:56`). Five cases, exhaustive:

| supplied `path` | action |
| --- | --- |
| omitted | resolve to `lineage.nodes[active].head_path` |
| the active head | accept |
| a tracked non-head node (ancestor **or** sibling) | **reject** unless branch intent (D4) |
| an untracked path, **forest empty** | accept as root, `parent_execution_id: null` |
| an untracked path, **forest non-empty** | **reject** unless explicit `adopt_untracked` intent |

The ancestor case is why v1's regression test was worthless: after SCimilarity advanced the head,
`clustered.h5ad` became an ancestor rather than a sibling, so a sibling-only rule accepted it and
discarded the column exactly as before.

The last row closes a bypass. `analysis-workspace`'s `run_analysis_code` can register a generated
H5AD as an artifact without declaring a `primary_matrix_output`, so it creates no node. If untracked
inputs were accepted unconditionally, that file would later be accepted as a fresh root and silently
detach the analysis from its lineage. Mid-session adoption must be explicit.

### D4 — Explicit branch intent and explicit head switching

- `branch_from` / `branch: true` on the call declares a deliberate fork. A resolution sweep is a
  legitimate workflow; without a vocabulary for it, D3 turns real work into a hard error.
- Switching which branch is active requires an explicit `checkout_head` / `adopt_head` operation.
  Referencing a branch path never implicitly activates it. `checkout_head` **atomically** switches
  `active_execution_id`, the node-scoped part of `state.facts`, and the derived `prepared_path` in
  one recorded event — a partial switch would reproduce the divergence D2 rule 4 prevents.
- **Finalization runs against the active head only, never a branch.** Comparing two annotation
  branches and finalizing the winner therefore requires an explicit `checkout_head` first.

### D5 — Declared matrix roles, and making omission reachable

`capability.yaml` declares `primary_matrix_input` and `primary_matrix_output` alongside the existing
argument schema (`capabilities/manifest.py`). "Any `.h5ad` artifact" is too implicit for a future
tool that writes more than one.

A matrix output normally creates the next analysis node. A format-only delivery instead declares
the same explicit `primary_matrix_output` plus `advances_lineage: false`: the artifact records the
matrix it consumed and the matrix it published, but creates no node, does not move the active head,
and cannot return node-scoped scientific facts. This keeps a portable H5AD from replacing the Zarr
working version or breaking dedup for the next scientific transformation.

Declaration alone is insufficient: an omitted `path` never reaches `execute` today. 20 manifests
declare `required: [path]` with `additionalProperties: false`, so schema validation rejects the call
first. D5 therefore also requires:

1. removing the primary matrix input from each schema's `required` list;
2. executor **injection** of the resolved head into `resolved_arguments` before handler invocation
   (`executor.py:171-182`), so every skill still receives a concrete absolute path and no skill
   needs to know the head exists;
3. a specific, actionable error when the input is omitted and no active head exists — distinct from
   a generic missing-argument failure.

### D6 — Optimistic commit check

Record `base_head_execution_id` (the active head at dispatch) in `CapabilityContext`. At commit,
if the active head has moved and branch intent was not declared, **reject the commit**. Compute runs
for minutes in a subprocess (`execution/broker.py:120`); without this, a long scVI run can silently
rebase onto a head that appeared while it was running.

The check must run **before** `os.replace(pending, final)` (`executor.py:461-463`). Moving the
staging directory first and rejecting afterwards would leave a committed-looking artifact directory
with no lineage node and no state record.

### D7 — Envelope, base prompt, and orchestration guidance

- Envelope (`executor.py:438`) gains `resolved_input` (path plus which of D3's five cases applied)
  and `lineage` (`node_id`, `parent`, `branch_intent`, whether the head advanced). Silent resolution
  must be visible, or a `FileNotFound` is traded for an invisible wrong-input.
- `configs/models/prompts/base.md:104-105` currently tells the model to reuse `files[].path`
  verbatim — guidance added for the CRC path-resolution defect. D3 supersedes it; rewrite, do not
  append.
- `orchestrate-single-cell` guidance gains the branch/checkout workflow from D4.

### D8 — Gene-vocabulary identity: take the safe option

`inspect-dataset/scripts/convert.py:171` patches only `{"gene_conversion": report}` while writing a
new H5AD with rewritten `var_names`. The conversion preserves `n_vars`, makes duplicate symbols
unique rather than dropping them, leaves unmapped genes under their original identifier, and saves
originals to a `var` column. It is a relabel of the var axis; count values are untouched.

**The tempting fine-grained option does not survive the current contracts.** A separate
`feature_schema` axis with asymmetric invalidation — gene-label evidence stale, numeric evidence
preserved — is unimplementable here, for two independent reasons:

1. `_count_matrix_identity` (`counts.py:16-35`) already hashes `var_names` at lines 32-34, so
   re-minting on relabel changes `count_representation.id`; and `revision_id` is derived from
   `count_id` (`counts.py:171-176`), so `dataset_revision.id` cascades too. Cell-QC, doublet, batch,
   and cluster-QC evidence all bind to `count_representation_id` and floors deliberately stale them
   when it changes (`floors/evaluator.py:79-80, 101-102, 135`). Numeric evidence cannot be exempted
   without rewriting every binding.
2. Numeric evidence is not actually label-independent. `qc.py:105-110` derives mitochondrial and
   ribosomal fractions by **gene-name prefix matching** (`MT-`/`mt-`, `RPS`/`RPL`). Against Ensembl
   identifiers those patterns match nothing, so `pct_counts_mt` is ~0 before conversion and a real
   value after. A relabel changes QC *values*, not merely their display names — and QC values drive
   filtering decisions.

**Decision — safe option.** `convert_gene_ids` re-mints `count_representation` (and therefore
`dataset_revision`) whenever a count representation already exists, and accepts conservative
recomputation. No `feature_schema` axis is introduced by this spec.

"Invalidate all count-bound evidence" is not sufficient as a specification, because the most
scientifically affected evidence is **not** count-bound. `current_annotation_evidence`
(`floors/evaluator.py:210-239`) checks only `clustering_id`, `status`, and review coherence — never
`count_representation_id`. A relabel does not change `clustering_id`, so marker, CellTypist, and
SCimilarity evidence computed against the old gene vocabulary would remain "current" and permit
finalization. Since floor changes are out of scope here, the conversion patch must clear it
explicitly. The exact patch:

```jsonc
{
  "analysis": { "count_representation": {…re-minted…}, "dataset_revision": {…re-minted…} },
  "annotation": null,        // marker/CellTypist/SCimilarity evidence + review
  "finalization": null,      // labels adjudicated from that evidence
  "reference_runs": null     // cached model runs keyed to the old vocabulary
}
```

`cell_qc`, `cluster_qc`, `doublets`, and `batch` need no explicit clearing — their floors already
stale on `count_representation_id`. `reference_runs` is cleared rather than snapshotted because a
cached CellTypist/SCimilarity run against Ensembl identifiers is not reusable against symbols; if a
cheaper marking is preferred, the entries must at minimum carry the old
`count_representation_id` and be treated as stale.

**Centralize the identity helper.** `_count_matrix_identity` and the dataset-revision derivation are
currently private to `single-cell-counts/scripts/counts.py:16-35,171-176`. `convert_gene_ids`
must not reimplement them — that would recreate exactly the per-skill duplication this spec exists
to remove. Promote both into a shared platform identity helper, and have the conversion update both
`facts.analysis` **and** `adata.uns["scagent_sdk"]` (`counts.py:177-190`), which carries the same ids
inside the file.

The exposure is order-dependent and narrow. The CRC session ran `convert` (`6f857eef`) →
`single-cell-counts` (`ea99c91b`), so the count id correctly hashed symbols and nothing was stale.
The reverse order — materialize counts, then convert — leaves `count_matrix_id` hashed over Ensembl
identifiers while the file carries symbols, with nothing re-minting it. That is a live defect today,
independent of lineage, and D8 fixes it.

A finer-grained identity model — separating numeric count values, stable feature entities, and
feature labels, then re-pointing every evidence and floor binding — is genuinely worth doing and is
**explicitly out of scope**. It deserves its own spec and its own review.

### D9 — Deterministic recovery ordering

`recover_pending` (`executor.py:496-508`) iterates `sorted(candidates)` — UUID4 directory names, so
arbitrary order. Head advancement is order-dependent; recovery must replay by staged event sequence.

That leaves one gap to define. `_stage_result` writes `result.json` (`executor.py:412`) *before*
recording `capability.result_staged` (`:414`), so a crash between them yields a staging directory
with no event and therefore no sequence to order by. Rule: such a directory is **not** recoverable
as a lineage node — it is quarantined and reported, not silently adopted at an arbitrary position.

### D10 — Split the schema version constant

`SESSION_SCHEMA_VERSION = 1` (`contracts/state.py:16`) is a single constant validated by both
`SessionMetadata` (`:37`) and `SessionState` (`:90`), each raising on mismatch. Adding `lineage`
changes `state.json` only; it must not force an unrelated `session.json` migration. Split into
`SESSION_METADATA_SCHEMA_VERSION` and `SESSION_STATE_SCHEMA_VERSION`, bumping only the latter.

### D11 — Deferred: pruning and column overlays

Both are downstream of the forest and explicitly out of scope here.

- **Pruning** additionally requires a branch **status** vocabulary — `retained` / `pinned` /
  `rejected`. Correct parent pointers alone do not say which heads are live; a rejected sweep branch
  is still reachable from its own head.
- **Column overlays** (write an `obs`/`obsm` delta plus a parent pointer instead of a full matrix
  copy) are where the 19× footprint goes away. Eligibility is "parent and child share an
  `identity_signature` and only column-shaped data was added." Attempting this before the forest
  exists would build a resolver with nothing to resolve toward.

## Migration

Two distinct problems; v1 conflated them.

**Historical (existing sessions) — reconstructible today.** Each committed execution's `result.json`
records `arguments.path` (the actual absolute path used, since the model has always passed explicit
paths), its declared `files`, and `state_patch.facts`. That is sufficient to rebuild the forest for
`run_20260727T052254Z_58b435`; the full chain was traced from exactly this data during review. A
v1→v2 reducer replays **all** committed events, not just post-checkpoint ones — `_apply_event`
(`store.py:281`) only applies stored patches, and historical events contain no lineage patch, so a
schema default cannot substitute for a reducer.

Identity signatures are reconstructible for the same reason: every allowlisted axis id is already
recorded in historical `state_patch.facts`. This is a direct benefit of D8's safe option — a
`feature_schema` hash over ordered `var_names` would **not** have been reconstructible, since no
event records the gene list, and migration would have needed a legacy-unknown sentinel or a re-read
of every historical H5AD.

Node `facts_snapshot` values are likewise reconstructible, but **only if the reducer replays every
committed event, not just H5AD-producing ones**. The CRC session's `cluster_qc` and
`annotation.evidence.*` facts came from read-only executions (`35cf4cf9`, `32b3daf7`, `5a6e338d`);
walking parent chains over node-producing patches alone would reconstruct a forest whose snapshots
are missing most of the session's evidence. The reducer therefore processes the event log in
sequence, routing each committed patch by the D2 read-only rules — for historical events the target
node is the one whose execution produced the tool's input path, or the head at that point in the
replay when there was no matrix input.

Measured against the CRC log, this is unambiguous. Sixteen read-only executions write facts:

| target derivable from | count | examples |
| --- | --- | --- |
| recorded input path (D2 rule 2) | 10 | `evaluate_cluster_qc`, `evaluate_marker_evidence`, `summarize_celltypist_by_cluster`, `investigate_batch` |
| head at that point in replay (D2 rule 4) | 6 | `review_cluster_qc`, `review_annotation_evidence`, `decide_batch_handling`, `run_analysis_code` |

Every one of the six pathless executions is a review or decision over facts, which is exactly the
case rule 4 covers. Note `investigate_batch` (`842d78b9`) patches `analysis` **and** `batch` from a
read-only execution — the `prepared_path` re-assertion at `investigate.py:397`. The reducer must
drop that stale `prepared_path` assertion rather than replay it as a head claim.

**Prospective (once omitted paths default).** `_stage_result` currently persists `arguments`, the
model's originals, not `resolved_arguments` (`executor.py:204` vs `:171`; also `:397`, `:424`,
`:472`). Replay determinism requires persisting `requested_arguments`, `resolved_arguments`,
`resolved_input_execution_id`, and `branch_intent`.

**`prepared_path`.** Keep the key, change its writer: the executor derives it from the active head;
the nine skill call sites above are stripped. `batch-investigation:397` must be removed outright —
under an execution-keyed model a read-only tool has no node, so a read-only tool asserting a head
goes from redundant to wrong.

## Acceptance cases

1. Three resolution runs from one UMAP become **siblings**, not a chain.
2. Sequential omitted-path annotations compose into **one** head carrying both columns.
3. An explicitly stale annotation input is **rejected** (the v1 test that would have passed).
4. Explicit same-identity branching creates **two distinct** execution nodes.
5. Existing CRC lineage reconstructs **exactly** from historical committed events.
6. Pending recovery cannot reorder or silently rebase executions.
7. An unknown `analysis.*.id` axis **fails loudly**; all eight current axes pass.
8. Read-only tools cannot change topology, `head_path`, or `active_execution_id` — but their
   node-scoped evidence attaches to a node and survives a checkout round-trip.
9. A branch commit leaves **both** `active_execution_id` and the active node-scoped facts unchanged;
   a clustering branch does not alter `facts.analysis.clustering`.
10. `checkout_head` switches head, node-scoped facts, and `prepared_path` atomically; an interrupted
    checkout leaves all three at their prior values.
11. An omitted primary matrix input reaches the executor and resolves; with an empty forest it
    raises a specific "no active head" error rather than a schema violation.
12. An untracked path mid-session is rejected without `adopt_untracked`, including an H5AD written
    by `run_analysis_code`.
13. An unregistered fact root **fails closed** rather than defaulting to either scope.
14. After gene conversion on a session with existing annotation evidence, `finalize` is **blocked**
    rather than proceeding against the old gene vocabulary.

## Invariants and tests

Deterministic contract tests; all inputs are JSON, no compute required.

1. Column-only tool at unchanged identity extends the head line (case 2).
2. New `clustering_id` creates a node whose parent is its resolved input, not the active head
   (case 1).
3. Read-only evidence tool leaves topology, `head_path` values, and `active_execution_id` unchanged
   while writing its node-scoped evidence to the target node's snapshot (case 8).
4. Forest **and every node snapshot** rebuilt by event replay equal the live values; fixture is the
   CRC `events.jsonl`, which is only reproduced if read-only executions are replayed too (case 5).
5. Each of D3's five path cases produces its specified outcome (case 3).
6. Branch intent yields two nodes and leaves `active_execution_id` unchanged (case 4).
7. A staged-but-uncommitted execution does not create a node — the two-phase guarantee at
   `executor.py:461-463` holds.
8. An interrupted or failed execution (`executor.py:206-240`) leaves `lineage` unchanged.
9. `recover_pending` over several orphans replays by event sequence and matches sequential commits
   (case 6).
10. An `analysis.*.id` outside the allowlist raises (case 7).
11. D6: a commit whose `base_head_execution_id` is no longer active is rejected absent branch
    intent, and the staging directory is **not** moved (case 6).
12. `convert_gene_ids` after `single-cell-counts` re-mints `count_representation` and cascades to
    `dataset_revision`; all count-bound evidence is marked stale (D8).
13. Branch isolation: a branch commit's node-scoped patch is absent from global facts and present in
    the node snapshot; session-scoped roots merge globally in both cases (case 9).
14. `checkout_head` atomicity, including the interrupted case (case 10).
15. A staging directory with `result.json` but no `capability.result_staged` event is quarantined,
    not adopted (D9).
16. Read-only evidence written while node B is active is still present after `checkout_head(A)` then
    `checkout_head(B)` (case 8), and absent from A's snapshot.
17. An unregistered fact root raises at commit (case 13).
18. D8's conversion patch clears `annotation`, `finalization`, and `reference_runs`, and
    `current_annotation_evidence` then fails (case 14).

Regression fixture: both annotators dispatched with **omitted** paths, asserting the delivered
artifact carries both prediction columns. Separately, an explicitly stale input is rejected.

## Residual open questions

1. **Floor independence.** Floors stay pure predicates over facts and do not consult `lineage`;
   head validation lives in dispatch and commit. Confirmed as a decision, but it means two
   independent staleness notions coexist, and a user-facing explanation must cover both.
2. **Registry drift.** The root-scope registry fails closed on unknown roots, which catches
   additions. It does not catch a root whose *meaning* changes scope over time — `reference_runs`
   would become node-scoped the day a reference run stops being reusable across branches. A
   periodic review, or a test asserting the registry against the roots present in a reference
   session, is the cheap mitigation.
3. **Branch working ergonomics.** With branch facts invisible until checkout, a model comparing
   three clusterings must reason from envelopes alone. Acceptable for a sweep; possibly awkward for
   a long-lived branch. Worth revisiting after the first real use, not before.
4. **D8 cost in practice.** Conservative re-minting means a late `convert_gene_ids` invalidates
   cluster QC and annotation evidence. Correct, but if it proves common in real sessions, that is
   the signal to fund the separate feature-identity spec.

## Non-goals

- Restoring a persistent in-memory AnnData. The subprocess boundary (`execution/broker.py:120`)
  makes it impossible and undesirable.
- Merge nodes. Single-parent only; the forest does not forbid a later extension.
- Enforcing tool order. The forest constrains which **file** a tool reads, never which **tool** runs
  next. Floors remain state predicates, not pipelines.
- Changing floor semantics, the 48 KiB inline limit, artifact immutability, non-overwrite, or
  environment provenance.
- Implementing D11 in this change.

## Review checklist

- [ ] Is single-parent (forest) sufficient for every current capability, or does any skill already
      read two matrices?
- [ ] Does the D3 five-case table cover every reachable input state, including an untracked path
      that later becomes tracked?
- [ ] Is the root-scope registry correct for all twelve roots, particularly `reference_runs` as
      session-scoped and `finalization` as node-scoped?
- [ ] Do the six read-only-commit rules cover every current no-matrix-output tool, including
      `run_analysis_code` writing `custom_analysis`?
- [ ] Does D8's explicit patch leave any evidence binding pointing at a stale
      `count_representation_id` that no floor catches?
- [ ] The reducer resolves all 16 CRC read-only executions (10 by input path, 6 by replay head) —
      does that generalize, or is there a tool shape that would defeat both rules?
- [ ] Does the v1→v2 reducer reconstruct the CRC forest unambiguously, including the three-way sweep
      and the two abandoned branches?
- [ ] Is D6's optimistic check sufficient, or is a dispatch-time lock needed for concurrent
      long-running compute?
- [ ] Does executor-owned `lineage` outside `facts` fully preserve the skills-never-write-state
      boundary, given skills receive `state_facts` read-only at `executor.py:169`?
- [ ] Is there a failure mode where head resolution produces a *scientifically* wrong result rather
      than a merely surprising one?
- [ ] Does anything here conflict with the priorities in `docs/current-state.md`?
