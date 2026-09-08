---
name: analysis-workspace
description: Inspect files, read text, run general shell commands, and execute auditable custom Python in the configured scientific environment. On Iris this is trusted in-process execution with provenance, not a security sandbox; use focused scientific skills when they fit and custom work when they do not.
---

# Analysis Workspace

The workspace is an escape hatch, not a workflow gate.

- `list_workspace` and `read_text_file` provide bounded inspection.
- `run_shell_command` runs ordinary Bash on Iris, including pipes, redirects, environment
  expansion, and installed commands. It captures the command, stdout, stderr, exit code, working
  directory, and duration. Only catastrophic broad-host destructive patterns are refused. Each
  successful run also becomes a browsable bundle in the session's `shell/` directory, named for the
  command and listed in `outputs.md`, so point the user there when asked what commands ran.
- `run_analysis_code` executes saved Python in the scientific environment. Normal imports,
  filesystem access, subprocesses, introspection, and package APIs are available. Code, stdout,
  stderr, purpose, environment provenance, and registered outputs remain inspectable.

Iris execution runs under the user's OS identity and is not a security boundary. The source of
trust is the authenticated user/session plus audit artifacts. A future DGX Spark/Linux backend may
route the same tools through OpenShell for actual isolation; do not imitate that boundary with a
fragile Python denylist.

Reach for a focused skill before writing code that reproduces one. Figures in particular belong to
`visualize-single-cell`: hand-written plotting has no layout rules, no artifact contract, and no
guarantee the figure returns for inspection, and debugging a plotting script through repeated tool
calls is the slowest way to answer a question. Write custom code for genuine gaps, not for work a
tool already does.

Use `export_anndata` for format-only H5AD delivery instead of hand-writing an AnnData conversion;
it preserves the complete object, verifies the round trip, and registers the result.

Do not use the shell to discover your own capabilities. Which packages are importable and which
reference models are cached is stated in your instructions; `find` over shared or networked trees
(`/`, `/data1`, an environment prefix) is slow, times out, and answers nothing you were not
already told. Search the filesystem only for the user's own data, with a specific parent directory
and a bounded `-maxdepth`. A timed-out search is a signal to stop searching, not to retry it
differently.

Use explicit paths, deterministic seeds, non-overwriting outputs, and
`register_artifact(path, name, media_type)` for durable results. Custom artifacts do not
automatically become validated annotation or publication evidence.

Your code runs with `np`, `pd`, `sc`, `ad`, `plt`, `Path`, `register_artifact`, `session_dir`, and
`workspace` already bound. `workspace` is a `Path` to this execution's staging directory: write
anything you intend to keep underneath it and register it from there. `register_artifact` refuses a
path outside `workspace`, so writing elsewhere first and registering afterwards always fails -- do
not go hunting for the staging path, it is already in scope.

Read [references/custom-code.md](references/custom-code.md).
