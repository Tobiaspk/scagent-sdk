from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scagent_sdk.capabilities.executor import _concise_capability_error
from scagent_sdk.capabilities.registry import CapabilityRegistry


def _package() -> Any:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        item
        for item in CapabilityRegistry(skills_root).discover()
        if item.manifest.skill_id == "analysis-workspace"
    )


def _tool(name: str) -> Any:
    package = _package()
    tool = next(tool for tool in package.manifest.tools if tool.name == name)
    return package.load_handler(tool)


def test_custom_python_has_no_ast_denylist() -> None:
    handler = _tool("run_analysis_code")
    assert "_validate" not in handler.__globals__
    source = (_package().root / "scripts" / "run_code.py").read_text(encoding="utf-8")
    assert "BLOCKED_IMPORTS" not in source
    assert 'compile(code, "analysis.py", "exec")' in source


def test_text_reader_returns_explicit_nonrecursive_pages(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("abcdefghij", encoding="utf-8")
    handler = _tool("read_text_file")

    first = handler({"path": str(path), "max_chars": 4}, SimpleNamespace())
    second = handler(
        {"path": str(path), "offset_chars": first["details"]["next_offset_chars"], "max_chars": 4},
        SimpleNamespace(),
    )

    assert first["details"] == {
        "path": str(path),
        "text": "abcd",
        "offset_chars": 0,
        "next_offset_chars": 4,
        "truncated": True,
    }
    assert second["details"]["text"] == "efgh"
    assert second["details"]["next_offset_chars"] == 8


def test_general_shell_supports_pipes_expansion_and_provenance(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    context = SimpleNamespace(session_dir=tmp_path, staging_dir=staging)
    result = _tool("run_shell_command")(
        {
            "command": "printf 'alpha\\nbeta\\n' | tail -n 1",
            "timeout_seconds": 5,
        },
        context,
    )
    assert result["details"]["stdout"] == "beta\n"
    assert (staging / "shell-command.sh").is_file()
    assert (staging / "shell-run.json").is_file()
    # The summary has to identify which command ran; a bare duration is useless on resume.
    assert result["summary"].endswith(": printf 'alpha\\nbeta\\n' | tail -n 1")


def test_general_shell_summary_bounds_a_long_multiline_command(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    context = SimpleNamespace(session_dir=tmp_path, staging_dir=staging)
    command = 'python3 -c "\nimport json\n' + "# padding comment\n" * 40 + '"'
    result = _tool("run_shell_command")({"command": command, "timeout_seconds": 15}, context)

    summary = result["summary"]
    assert "\n" not in summary
    assert summary.startswith("Shell command completed in ")
    assert summary.endswith("…")
    assert len(summary.split(": ", 1)[1]) == 120


def test_general_shell_failure_names_the_command_without_hiding_the_error(
    tmp_path: Path,
) -> None:
    """The terminal reduces a failure to its last line, so the command must not sit there."""

    staging = tmp_path / "stage"
    staging.mkdir()
    context = SimpleNamespace(session_dir=tmp_path, staging_dir=staging)
    with pytest.raises(RuntimeError) as failure:
        _tool("run_shell_command")(
            {"command": "echo boom-detail >&2; exit 3", "timeout_seconds": 5}, context
        )

    message = str(failure.value)
    assert "exited 3: echo boom-detail >&2; exit 3" in message
    assert message.strip().splitlines()[-1].strip() == "boom-detail"


def test_general_shell_refuses_catastrophic_root_removal(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    context = SimpleNamespace(session_dir=tmp_path, staging_dir=staging)
    with pytest.raises(ValueError, match="catastrophic destructive pattern"):
        _tool("run_shell_command")({"command": "rm -rf /"}, context)


def test_concise_error_extracts_final_exception_line() -> None:
    message = (
        "run_analysis_code failed in gpu-singlecell (exit 1): Traceback (most recent call last):\n"
        '  File "x", line 43, in run\n'
        "    raise ValueError(...)\n"
        "ValueError: input count matrix is invalid"
    )
    assert _concise_capability_error(message) == "input count matrix is invalid"


def test_concise_error_passes_through_single_line_messages() -> None:
    message = "scientific floor denied execution: [dataset_identity] no identity. Run inspect."
    assert _concise_capability_error(message) == message


def test_concise_error_is_bounded() -> None:
    assert len(_concise_capability_error("KeyError: " + "z" * 5000)) <= 300
