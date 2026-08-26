"""Workspace inspection and auditable shell execution."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

_SUMMARY_COMMAND_MAXLEN = 120
_DESTRUCTIVE_PATTERNS = (
    "rm -rf /",
    "rm -rf ~",
    "mkfs",
    "shutdown",
    "reboot",
    ":(){:|:&};:",
)


def _command_excerpt(command: str) -> str:
    """Collapse a command onto one bounded line so it can identify a run in a summary.

    An inline heredoc or ``python3 -c`` block is multi-line and arbitrarily long; a summary that
    carried it verbatim would be unreadable in a transcript and unusable as a name.
    """

    collapsed = " ".join(command.split())
    if len(collapsed) > _SUMMARY_COMMAND_MAXLEN:
        collapsed = collapsed[: _SUMMARY_COMMAND_MAXLEN - 1].rstrip() + "…"
    return collapsed


def list_workspace(arguments: dict[str, Any], _context: Any) -> dict[str, Any]:
    root = Path(str(arguments["path"])).expanduser().resolve()
    max_entries = int(arguments.get("max_entries", 200))
    depth = int(arguments.get("depth", 2))
    if not root.is_dir():
        raise NotADirectoryError(root)
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if len(relative.parts) > depth:
            continue
        stat = path.stat()
        entries.append(
            {
                "path": str(relative),
                "kind": "directory" if path.is_dir() else "file",
                "size_bytes": stat.st_size if path.is_file() else None,
                "modified_time_ns": stat.st_mtime_ns,
            }
        )
        if len(entries) >= max_entries:
            break
    return {
        "summary": f"Listed {len(entries)} entries beneath {root}.",
        "details": {
            "root": str(root),
            "entries": entries,
            "truncated": len(entries) >= max_entries,
        },
    }


def read_text_file(arguments: dict[str, Any], _context: Any) -> dict[str, Any]:
    path = Path(str(arguments["path"])).expanduser().resolve()
    max_chars = int(arguments.get("max_chars", 20000))
    offset = int(arguments.get("offset_chars", 0))
    if offset < 0:
        raise ValueError("offset_chars must be nonnegative")
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        if offset:
            handle.read(offset)
        text = handle.read(max_chars + 1)
    truncated = len(text) > max_chars
    chunk = text[:max_chars]
    next_offset = offset + len(chunk) if truncated else None
    return {
        "summary": (
            f"Read characters {offset}:{offset + len(chunk)} from {path.name}"
            + (f"; continue at offset {next_offset}." if next_offset is not None else ".")
        ),
        "details": {
            "path": str(path),
            "text": chunk,
            "offset_chars": offset,
            "next_offset_chars": next_offset,
            "truncated": truncated,
        },
    }


def run_shell_command(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    command = str(arguments["command"]).strip()
    if not command:
        raise ValueError("command must not be empty")
    lowered = " ".join(command.lower().split())
    if any(pattern in lowered for pattern in _DESTRUCTIVE_PATTERNS):
        raise ValueError(
            "command matches a catastrophic destructive pattern; use a narrower explicit target"
        )
    cwd_arg = arguments.get("cwd")
    cwd = (
        Path(str(cwd_arg)).expanduser().resolve()
        if cwd_arg is not None
        else context.session_dir.resolve()
    )
    if not cwd.is_dir():
        raise NotADirectoryError(cwd)
    timeout = int(arguments.get("timeout_seconds", 600))
    started = time.monotonic()
    # The capability worker already provides the locked scientific environment. Starting a login
    # shell needlessly reruns Iris profile initialization (~5 seconds per command in the live
    # environment) and can overwrite that environment. Use a plain shell with explicit pipefail.
    completed = subprocess.run(
        ["bash", "-c", "set -o pipefail\n" + command],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    duration = time.monotonic() - started
    (context.staging_dir / "shell-command.sh").write_text(
        "#!/usr/bin/env bash\nset -o pipefail\n" + command + "\n", encoding="utf-8"
    )
    (context.staging_dir / "shell.stdout.txt").write_text(
        completed.stdout, encoding="utf-8"
    )
    (context.staging_dir / "shell.stderr.txt").write_text(
        completed.stderr, encoding="utf-8"
    )
    report = {
        "command": command,
        "cwd": str(cwd),
        "timeout_seconds": timeout,
        "exit_code": int(completed.returncode),
        "duration_seconds": duration,
    }
    (context.staging_dir / "shell-run.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    excerpt = _command_excerpt(command)
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout)[-8000:]
        # The command goes on its own line so the terminal's concise one-line extraction still
        # lands on the actual error at the end of the captured output.
        raise RuntimeError(f"shell command exited {completed.returncode}: {excerpt}\n{tail}")
    return {
        "summary": f"Shell command completed in {duration:.2f}s: {excerpt}",
        "details": {
            **report,
            "stdout": completed.stdout[-40000:],
            "stderr": completed.stderr[-8000:],
        },
        "artifacts": [
            {
                "name": "shell-command",
                "relative_path": "shell-command.sh",
                "media_type": "text/x-shellscript",
            },
            {
                "name": "shell-stdout",
                "relative_path": "shell.stdout.txt",
                "media_type": "text/plain",
            },
            {
                "name": "shell-stderr",
                "relative_path": "shell.stderr.txt",
                "media_type": "text/plain",
            },
            {
                "name": "shell-run",
                "relative_path": "shell-run.json",
                "media_type": "application/json",
            },
        ],
    }
