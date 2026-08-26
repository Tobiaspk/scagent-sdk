from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

from scagent_sdk.cli import _start_profile_name, main
from scagent_sdk.session import AnalysisSession


def test_cli_create_list_show_and_resume(tmp_path: Path, capsys) -> None:
    assert main(["session", "new", "--title", "CLI session", "--sessions-root", str(tmp_path)]) == 0
    created = json.loads(capsys.readouterr().out)
    session_id = created["session"]["session_id"]

    assert main(["session", "list", "--sessions-root", str(tmp_path)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing[0]["session_id"] == session_id

    assert main(["session", "show", session_id, "--sessions-root", str(tmp_path)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["events"][0]["kind"] == "session.created"

    assert (
        main(
            [
                "session",
                "resume",
                session_id,
                "--model-profile",
                "local-default",
                "--sessions-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    resume = json.loads(capsys.readouterr().out)
    assert resume["mode"] == "fresh"
    assert resume["context"] == ""


def test_cli_lists_and_validates_capabilities(capsys) -> None:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"

    assert main(["capability", "list", "--skills-root", str(skills_root)]) == 0
    listing = json.loads(capsys.readouterr().out)
    by_id = {item["skill"]["id"]: item for item in listing}
    assert by_id["inspect-dataset"]["tools"][0]["name"] == "inspect_dataset"
    assert {tool["name"] for tool in by_id["inspect-media"]["tools"]} == {
        "inspect_image",
        "inspect_pdf",
    }

    assert main(["capability", "validate", "--skills-root", str(skills_root)]) == 0
    validation = json.loads(capsys.readouterr().out)
    readiness = validation.pop("readiness")
    assert validation == {
        "executable_skills": 22,
        "skills": 23,
        "skills_root": str(skills_root.resolve()),
        "status": "pass",
        "tools": 52,
    }
    # Reference-model availability is host state, so assert the inventory's shape, not its verdict.
    assert {report["skill_id"] for report in readiness} == {
        "celltypist-annotation",
        "research-web",
        "scimilarity-annotation",
    }
    assert all(report["status"] in {"ready", "partial", "unavailable"} for report in readiness)


def test_start_resume_defaults_to_the_profile_recorded_in_the_session(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    prompts = profiles / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "base.md").write_text("system", encoding="utf-8")
    (profiles / "recorded.toml").write_text(
        """schema_version = 1
[profile]
name = "recorded"
runtime = "claude-agent-sdk"
transport = "litellm"
model = "primary"
base_url = "http://127.0.0.1:4000"
allow_noauth = true
system_prompt = "prompts/base.md"
""",
        encoding="utf-8",
    )
    session = AnalysisSession.create(tmp_path / "sessions", title="Resume")
    session.bind_runtime(
        runtime="claude-agent-sdk",
        runtime_session_id="sdk-old",
        model_profile="recorded",
        transport="litellm",
        model="primary",
    )
    args = Namespace(profile=None, profiles_root=str(profiles))

    assert _start_profile_name(args, session, resumed=True) == "recorded"
    args.profile = "explicit"
    assert _start_profile_name(args, session, resumed=True) == "explicit"
