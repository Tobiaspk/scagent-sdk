# Capability skill authoring

Create a standard skill folder beneath `.claude/skills`. Keep `SKILL.md` concise and put repeatable
computation in `scripts/`, detailed scientific material in `references/`, and output templates in
`assets/`. Do not add a README inside an individual skill.

`SKILL.md` is model-facing and **always in the model's context**: the runtime injects every skill's
instructions into the system prompt at assembly, and the `Skill` tool plus each section's absolute
directory cover the deeper `references/` material. Write it as the scientific contract for the
capability — when it applies, what its inputs mean, how to read its output, where it misleads — not
as a summary of the tool schema. Because it is always loaded, keep it dense and free of filler; a
128 KiB total budget across all skills is enforced, and long-form method detail belongs in
`references/`.

Add `capability.yaml` only when the skill exposes executable tools:

```yaml
schema_version: 1
skill:
  id: example-skill
  version: "0.1.0"
  description: One-line capability purpose.
tools:
  - name: inspect_example
    description: Precise description shown to the model.
    entrypoint: scripts/inspect.py:run
    environment: gpu-singlecell
    activity_label: Inspecting example
    floors: [dataset_identity]
    input_schema:
      type: object
      additionalProperties: false
      properties:
        path: {type: string}
      required: [path]
```

The entrypoint may be synchronous or asynchronous and must accept `(arguments, context)`. Write
artifacts only beneath `context.staging_dir`, then return finite JSON values:

```python
def run(arguments, context):
    output = context.staging_dir / "evidence.json"
    output.write_text("{}\n", encoding="utf-8")
    return {
        "schema_version": 1,
        "summary": "Inspected the input.",
        "details": {"rows": 10},
        "facts_patch": {"inspection": {"rows": 10}},
        "decisions_patch": {},
        "artifacts": [{
            "name": "inspection-evidence",
            "relative_path": "evidence.json",
            "media_type": "application/json",
        }],
    }
```

The harness validates paths and JSON, spills details above 48 KiB, stages the result, and commits
facts/artifacts from PostToolUse. Capability code must never edit `state.json` or `events.jsonl`.
`environment: current` imports the handler in the agent runtime and is suitable for lightweight
standard-library capabilities. Other names must be present in the selected host environment TOML;
the broker performs real imports and any required GPU check, then runs the handler across a JSON
subprocess boundary. Missing, unhealthy, or unknown environments fail closed. Each committed result
records the logical name, Python executable, and environment fingerprint.

To let a multimodal model inspect a generated figure, declare it in both `artifacts` and
`model_media`:

```python
figure = {
    "name": "cluster-umap",
    "relative_path": "cluster-umap.png",
    "media_type": "image/png",
}
return {
    "summary": "Generated the cluster UMAP.",
    "artifacts": [figure],
    "model_media": [figure],
}
```

The harness accepts PNG, JPEG, WebP, and GIF, enforces at most eight images, 2 MiB per image and
8 MiB per result, and attaches transient base64 MCP image blocks after validating staged paths.
Those ceilings are the binding ones: the runtime's own message frame is sized well above them, so
figures never fail a turn at the transport layer. Keep figures near the model's ~1568 px
resampling edge rather than at the ceiling. The durable result
stores only artifact metadata and paths. Normalize arbitrary input through `inspect-media` rather
than returning an unchecked file directly.

`floors` names independent state predicates evaluated by PreToolUse. Add a floor only when the
tool accepts a consequential decision, performs a high-impact operation that needs a separate
suitability attestation, or certifies/publishes a result. Do not floor ordinary measurement,
transformation, model training/inference, or comparison: those tools validate their intrinsic
inputs locally, and the model decides how to satisfy them from the user's request and current
artifact. A floor must not encode a tool-order workflow or use remediation to prescribe a pipeline.
Add deny/allow/staleness tests for every floor. `activity_label` supplies the Rich terminal text
used in `▶ …` and `✓ … done` lines.

If the skill cannot run without a host asset it will never download — a cached reference model —
declare a readiness probe so the model is told what exists instead of searching for it:

```yaml
readiness:
  entrypoint: scripts/readiness.py:probe
  environment: celltypist
```

```python
def probe(environment):
    directory = Path(environment["HOME"]) / ".celltypist" / "data" / "models"
    models = sorted(item.name for item in directory.glob("*.pkl"))
    if not models:
        return {"status": "unavailable", "summary": f"no cached models at {directory}"}
    return {
        "status": "ready",
        "summary": f"{len(models)} cached classifiers available",
        "details": [f"cached models: {', '.join(models)}"],
    }
```

The probe receives the declared capability environment's resolved variables (nothing is inherited
beyond that environment's own configuration) and runs **in the control plane** at session assembly,
so it must use only the standard library — no scientific imports, no GPU, no subprocesses, and no
long or unbounded filesystem walks. `status` must be `ready`, `partial`, `unavailable`, or
`unknown`; anything else, an exception, or exceeding the probe timeout is reported as `unknown`.
Verdicts are appended to the model's system prompt as local prerequisites and recorded as a
`capability.readiness_probed` event, so name the concrete choices the model needs (model filenames,
paths, vocabulary sizes) rather than a bare yes or no.

Validate discovery before model testing:

```bash
scagent-sdk capability validate
scagent-sdk capability list
python -m pytest
```

Use the skill's deterministic tests to validate scripts. Use live agent evaluations separately to
test whether a supported model selects and interprets the capability appropriately.
