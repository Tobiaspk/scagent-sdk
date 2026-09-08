"""Small JSON process boundary used by environment-routed capability scripts."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _load_handler(source: Path, function_name: str) -> Any:
    spec = importlib.util.spec_from_file_location("scagent_sdk_external_skill", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load capability entrypoint: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = getattr(module, function_name, None)
    if not callable(handler):
        raise RuntimeError(f"entrypoint function is not callable: {function_name}")
    return handler


async def _run(args: argparse.Namespace) -> None:
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    context = SimpleNamespace(
        scientific_session_id=payload["context"]["scientific_session_id"],
        session_dir=Path(payload["context"]["session_dir"]),
        staging_dir=Path(payload["context"]["staging_dir"]),
        skill_id=payload["context"]["skill_id"],
        tool_name=payload["context"]["tool_name"],
        execution_id=payload["context"]["execution_id"],
        state_revision=payload["context"]["state_revision"],
        state_facts=payload["context"]["state_facts"],
        state_lineage=payload["context"].get("state_lineage", {}),
        artifact_relative_path=payload["context"].get("artifact_relative_path", ""),
        state_decisions=payload["context"].get("state_decisions", {}),
        skill_version=payload["context"].get("skill_version", ""),
    )
    handler = _load_handler(args.entrypoint, args.function)
    value = handler(payload["arguments"], context)
    if inspect.isawaitable(value):
        value = await value
    args.output.write_text(
        json.dumps(value, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entrypoint", type=Path, required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
