from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.execution import (
    EnvironmentBroker,
    EnvironmentProfile,
    EnvironmentRegistry,
    ExecutionRuntime,
)
from scagent_sdk.session import AnalysisSession


def test_environment_broker_executes_handler_in_declared_python(tmp_path: Path) -> None:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        item
        for item in CapabilityRegistry(skills_root).discover()
        if item.manifest.skill_id == "inspect-dataset"
    )
    tool = replace(package.manifest.tools[0], environment="test-science")
    runtime = ExecutionRuntime(name="test-runtime", python=Path(sys.executable))
    profile = EnvironmentProfile(name="test-science", runtime=runtime)
    broker = EnvironmentBroker(EnvironmentRegistry({"test-science": profile}))
    session = AnalysisSession.create(tmp_path / "sessions", title="broker")
    dataset = tmp_path / "matrix.mtx"
    dataset.write_text("%%MatrixMarket matrix coordinate integer general\n", encoding="utf-8")

    response = asyncio.run(
        CapabilityExecutor(session, environment_broker=broker).execute(
            package, tool, {"path": str(dataset)}
        )
    )

    assert response.get("is_error") is not True
    execution_id = response["structuredContent"]["scagent_execution_id"]
    assert CapabilityExecutor(session).commit(execution_id) is True
    artifact = session.store.state.artifacts[execution_id]
    assert artifact["environment"]["name"] == "test-science"
    assert artifact["environment"]["fingerprint"].startswith("sha256:")
    assert artifact["environment"]["runtime"] == "test-runtime"
    assert artifact["environment"]["environment_isolated"] is True


def test_runtime_environment_is_scrubbed_and_prefix_is_explicit(tmp_path: Path) -> None:
    prefix = tmp_path / "runtime"
    (prefix / "bin").mkdir(parents=True)
    python = prefix / "bin" / "python"
    python.write_bytes(Path(sys.executable).read_bytes())
    python.chmod(0o755)
    runtime = ExecutionRuntime(
        name="clean",
        python=python,
        prefix=prefix,
        environment={"LD_PRELOAD": "{prefix}/lib/example.so"},
        inherit_variables=("EXPLICIT_TOKEN",),
    )

    environment = runtime.build_environment(
        {
            "HOME": "/safe/home",
            "PATH": "/wrong/venv/bin:/usr/bin",
            "VIRTUAL_ENV": "/wrong/venv",
            "CONDA_PREFIX": "/wrong/base",
            "PYTHONPATH": "/wrong/modules",
            "SECRET_TOKEN": "must-not-leak",
            "EXPLICIT_TOKEN": "allowed",
        }
    )

    assert environment["PATH"].split(":")[0] == str(prefix / "bin")
    assert environment["CONDA_PREFIX"] == str(prefix)
    assert environment["LD_PRELOAD"] == f"{prefix}/lib/example.so"
    assert environment["EXPLICIT_TOKEN"] == "allowed"
    assert "SECRET_TOKEN" not in environment
    assert "VIRTUAL_ENV" not in environment
    assert "PYTHONPATH" not in environment


def test_registry_v2_shares_physical_runtime_and_hashes_provenance(tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.write_text("environment revision one\n", encoding="utf-8")
    config = tmp_path / "environments.toml"
    config.write_text(
        f'''schema_version = 2
[runtimes.shared]
python = "{sys.executable}"
prefix = "{sys.prefix}"
probe_modules = ["json"]
provenance_files = ["{history}"]
[runtimes.shared.environment]
MARKER = "{{prefix}}"

[capabilities.first]
runtime = "shared"
required_modules = ["json"]

[capabilities.second]
runtime = "shared"
required_modules = ["pathlib"]
''',
        encoding="utf-8",
    )

    registry = EnvironmentRegistry.from_path(config)
    first = registry.resolve("first")
    second = registry.resolve("second")
    before = first.runtime.fingerprint

    assert first.runtime is second.runtime
    assert first.build_environment()["MARKER"] == str(Path(sys.prefix))
    history.write_text("environment revision two\n", encoding="utf-8")
    assert first.runtime.fingerprint != before


def test_registry_reads_legacy_v1_profiles(tmp_path: Path) -> None:
    config = tmp_path / "legacy.toml"
    config.write_text(
        f'''schema_version = 1
[environments.legacy]
python = "{sys.executable}"
gpu_required = false
required_modules = ["json"]
[environments.legacy.environment]
MARKER = "legacy"
''',
        encoding="utf-8",
    )

    profile = EnvironmentRegistry.from_path(config).resolve("legacy")

    assert profile.runtime.name == "legacy-legacy"
    assert profile.build_environment()["MARKER"] == "legacy"


# --- GPU pinning -------------------------------------------------------------


def test_forward_progress_keeps_bar_lines_and_drops_chatter() -> None:
    seen: list[str] = []
    # An ANSI-wrapped tqdm/Lightning epoch bar is surfaced, stripped of escape codes.
    EnvironmentBroker._forward_progress(
        b"\x1b[2KEpoch 5/200:  2%|\xe2\x96\x8a| 5/200 [00:01<00:40]", seen.append
    )
    # An ordinary log line is not progress and must not flood the spinner.
    EnvironmentBroker._forward_progress(b"loading reference model", seen.append)
    assert seen == ["Epoch 5/200:  2%|▊| 5/200 [00:01<00:40]"]


def test_run_worker_streams_progress_and_captures_output(tmp_path: Path) -> None:
    broker = EnvironmentBroker(EnvironmentRegistry({}))
    updates: list[str] = []
    # A worker that emits a carriage-return progress bar to stderr (as tqdm does) and a normal
    # stdout line; both must be captured, and only the bar forwarded as live progress.
    program = (
        "import sys\n"
        "sys.stdout.write('starting worker\\n')\n"
        "sys.stderr.write('Epoch 7/10:  70%|##| 7/10 [00:00<00:00]\\r')\n"
        "sys.stderr.flush()\n"
    )
    completed = broker._run_worker(
        [sys.executable, "-c", program],
        {"PATH": "/usr/bin:/bin"},
        execution_id="exec-1",
        label="train_scvi_latent",
        timeout_seconds=30,
        progress=updates.append,
    )
    assert completed.returncode == 0
    assert "starting worker" in completed.stdout
    assert "Epoch 7/10" in completed.stderr
    assert any("Epoch 7/10" in line for line in updates)


def test_select_gpu_devices_prefers_most_free_memory() -> None:
    from scagent_sdk.execution.broker import EnvironmentBroker

    memory = [
        {"device": 0, "free_mb": 1024},
        {"device": 1, "free_mb": 60000},
        {"device": 2, "free_mb": 30000},
    ]
    # Device 0 is busiest (e.g. colocated with the model gateway) and must not be chosen.
    assert EnvironmentBroker.select_gpu_devices(memory, 1) == [1]
    assert EnvironmentBroker.select_gpu_devices(memory, 2) == [1, 2]


def test_select_gpu_devices_is_deterministic_on_ties() -> None:
    from scagent_sdk.execution.broker import EnvironmentBroker

    memory = [{"device": 3, "free_mb": 500}, {"device": 1, "free_mb": 500}]
    assert EnvironmentBroker.select_gpu_devices(memory, 1) == [1]


def test_select_gpu_devices_returns_empty_without_measurement() -> None:
    from scagent_sdk.execution.broker import EnvironmentBroker

    assert EnvironmentBroker.select_gpu_devices([], 1) == []
    assert EnvironmentBroker.select_gpu_devices([{"device": 0, "free_mb": 10}], 0) == []
