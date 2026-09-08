"""Execute capabilities in explicit, scrubbed physical Python runtimes."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.manifest import CapabilityTool
from scagent_sdk.capabilities.registry import SkillPackage
from scagent_sdk.capabilities.results import CapabilityContext
from scagent_sdk.errors import CapabilityExecutionError, CapabilityInterrupted
from scagent_sdk.execution.profile import (
    EnvironmentProfile,
    EnvironmentRegistry,
    ExecutionRuntime,
)


@dataclass(frozen=True)
class EnvironmentExecution:
    value: Any
    provenance: dict[str, Any]


class EnvironmentBroker:
    def __init__(self, registry: EnvironmentRegistry):
        self.registry = registry
        self.worker_path = Path(__file__).with_name("worker.py").resolve()
        self._healthy: set[str] = set()
        self._probe_results: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._running: dict[str, subprocess.Popen[bytes]] = {}
        self._cancelled: set[str] = set()

    @staticmethod
    def _probe_program(modules: tuple[str, ...], gpu_required: bool, gpu_probe: str) -> str:
        return f"""
import importlib
import importlib.metadata
import json

modules = {modules!r}
module_errors = {{}}
module_versions = {{}}
for module in modules:
    try:
        loaded = importlib.import_module(module)
        version = getattr(loaded, "__version__", None)
        if version is None:
            try:
                version = importlib.metadata.version(module.replace("_", "-"))
            except importlib.metadata.PackageNotFoundError:
                version = None
        module_versions[module] = str(version) if version is not None else None
    except Exception as exc:
        module_errors[module] = f"{{type(exc).__name__}}: {{exc}}"

gpu_required = {gpu_required!r}
gpu_devices = None
gpu_error = None
gpu_memory = []
cuda_runtime_version = None
gpu_probe = {gpu_probe!r}
if gpu_required and gpu_probe == "cupy" and "cupy" not in module_errors:
    try:
        import cupy
        gpu_devices = cupy.cuda.runtime.getDeviceCount()
        cuda_runtime_version = cupy.cuda.runtime.runtimeGetVersion()
        for index in range(gpu_devices):
            with cupy.cuda.Device(index):
                free_bytes, total_bytes = cupy.cuda.runtime.memGetInfo()
            gpu_memory.append({{
                "device": index,
                "free_mb": int(free_bytes // (1024 * 1024)),
                "total_mb": int(total_bytes // (1024 * 1024)),
            }})
    except Exception as exc:
        gpu_error = f"{{type(exc).__name__}}: {{exc}}"
elif gpu_required and gpu_probe == "torch" and "torch" not in module_errors:
    try:
        import torch
        gpu_devices = torch.cuda.device_count()
        cuda_runtime_version = torch.version.cuda
        for index in range(gpu_devices):
            free_bytes, total_bytes = torch.cuda.mem_get_info(index)
            gpu_memory.append({{
                "device": index,
                "free_mb": int(free_bytes // (1024 * 1024)),
                "total_mb": int(total_bytes // (1024 * 1024)),
            }})
    except Exception as exc:
        gpu_error = f"{{type(exc).__name__}}: {{exc}}"

print(json.dumps({{
    "module_errors": module_errors,
    "module_versions": module_versions,
    "gpu_required": gpu_required,
    "gpu_devices": gpu_devices,
    "gpu_memory": gpu_memory,
    "gpu_error": gpu_error,
    "cuda_runtime_version": cuda_runtime_version,
}}))
"""

    def _run_probe(
        self,
        runtime: ExecutionRuntime,
        *,
        modules: tuple[str, ...],
        gpu_required: bool,
        environment: dict[str, str],
    ) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                [
                    str(runtime.python),
                    "-c",
                    self._probe_program(modules, gpu_required, runtime.gpu_probe),
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=runtime.probe_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CapabilityExecutionError(
                f"runtime {runtime.name!r} health probe exceeded {runtime.probe_timeout_seconds}s"
            ) from exc
        if completed.returncode != 0:
            tail = completed.stderr.strip()[-4000:] or completed.stdout.strip()[-4000:]
            raise CapabilityExecutionError(f"runtime {runtime.name!r} health probe failed: {tail}")
        try:
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise CapabilityExecutionError(
                f"runtime {runtime.name!r} health probe returned invalid output"
            ) from exc
        if not isinstance(result, dict):
            raise CapabilityExecutionError(
                f"runtime {runtime.name!r} health probe returned a non-object"
            )
        return {str(key): value for key, value in result.items()}

    @staticmethod
    def _validate_profile(profile: EnvironmentProfile, result: dict[str, Any]) -> None:
        all_errors = result.get("module_errors", {})
        module_errors = {
            module: all_errors[module]
            for module in profile.required_modules
            if module in all_errors
        }
        if module_errors:
            raise CapabilityExecutionError(
                f"environment {profile.name!r} has unusable required modules: "
                + "; ".join(f"{module}: {error}" for module, error in module_errors.items())
            )
        devices = int(result.get("gpu_devices") or 0)
        if profile.gpu_required and devices < profile.gpu_count:
            raise CapabilityExecutionError(
                f"environment {profile.name!r} requires {profile.gpu_count} GPU(s), "
                f"but {devices} are usable: {result.get('gpu_error')}"
            )
        if profile.min_gpu_memory_mb:
            suitable = [
                item
                for item in result.get("gpu_memory", [])
                if int(item.get("free_mb", 0)) >= profile.min_gpu_memory_mb
            ]
            if len(suitable) < profile.gpu_count:
                raise CapabilityExecutionError(
                    f"environment {profile.name!r} requires {profile.gpu_count} GPU(s) with "
                    f"at least {profile.min_gpu_memory_mb} MiB free"
                )

    @staticmethod
    def select_gpu_devices(gpu_memory: list[dict[str, Any]], gpu_count: int) -> list[int]:
        """Pick the ``gpu_count`` devices with the most free memory, most-free first.

        On a shared node the busiest GPU is frequently device 0 (often colocated with the model
        gateway), so leaving device selection to the framework silently lands training there. The
        probe already measures free memory per device; this turns that measurement into an explicit
        pin. Returns an empty list when no usable measurement exists, in which case the caller
        leaves ``CUDA_VISIBLE_DEVICES`` untouched.
        """
        usable = [
            (int(item["device"]), int(item.get("free_mb", 0)))
            for item in gpu_memory
            if isinstance(item, dict) and item.get("device") is not None
        ]
        if not usable or gpu_count <= 0:
            return []
        usable.sort(key=lambda entry: (-entry[1], entry[0]))
        return [device for device, _free in usable[: max(1, gpu_count)]]

    @staticmethod
    def _profile_probe_result(
        profile: EnvironmentProfile, result: dict[str, Any]
    ) -> dict[str, Any]:
        versions = result.get("module_versions", {})
        return {
            "name": profile.name,
            "fingerprint": profile.fingerprint,
            "runtime": profile.runtime.name,
            "runtime_fingerprint": profile.runtime.fingerprint,
            "python": str(profile.python),
            "prefix": str(profile.runtime.prefix) if profile.runtime.prefix else None,
            "required_modules": list(profile.required_modules),
            "module_versions": {
                module: versions.get(module) for module in profile.required_modules
            },
            "gpu_required": profile.gpu_required,
            "gpu_count": profile.gpu_count,
            "min_gpu_memory_mb": profile.min_gpu_memory_mb,
            "gpu_devices": result.get("gpu_devices"),
            "gpu_memory": result.get("gpu_memory", []),
            "gpu_error": result.get("gpu_error"),
            "cuda_runtime_version": result.get("cuda_runtime_version"),
            "environment_isolated": True,
        }

    def probe(self, name: str) -> dict[str, Any]:
        profile = self.registry.resolve(name)
        modules = tuple(dict.fromkeys((*profile.runtime.probe_modules, *profile.required_modules)))
        result = self._run_probe(
            profile.runtime,
            modules=modules,
            gpu_required=profile.gpu_required,
            environment=profile.build_environment(),
        )
        self._validate_profile(profile, result)
        output = self._profile_probe_result(profile, result)
        self._healthy.add(name)
        self._probe_results[name] = output
        return output

    def probe_all(self) -> list[dict[str, Any]]:
        profiles = self.registry.profiles()
        groups: dict[str, list[EnvironmentProfile]] = {}
        for profile in profiles:
            groups.setdefault(profile.runtime.name, []).append(profile)
        output: dict[str, dict[str, Any]] = {}
        for runtime_name in sorted(groups):
            group = groups[runtime_name]
            runtime = group[0].runtime
            modules = tuple(
                dict.fromkeys(
                    (
                        *runtime.probe_modules,
                        *(module for profile in group for module in profile.required_modules),
                    )
                )
            )
            environment = runtime.build_environment()
            for profile in group:
                for key, value in profile.environment.items():
                    rendered = value.replace(
                        "{prefix}", str(runtime.prefix) if runtime.prefix else ""
                    )
                    existing = environment.get(key)
                    if existing is not None and existing != rendered:
                        raise CapabilityExecutionError(
                            f"capabilities sharing runtime {runtime_name!r} disagree on {key}"
                        )
                    environment[key] = rendered
            result = self._run_probe(
                runtime,
                modules=modules,
                gpu_required=any(profile.gpu_required for profile in group),
                environment=environment,
            )
            for profile in group:
                self._validate_profile(profile, result)
                item = self._profile_probe_result(profile, result)
                output[profile.name] = item
                self._healthy.add(profile.name)
                self._probe_results[profile.name] = item
        return [output[profile.name] for profile in profiles]

    def running_executions(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._running))

    def cancel(self, execution_id: str) -> bool:
        """Stop one running worker and everything it spawned. Returns whether one was running."""

        with self._lock:
            process = self._running.get(execution_id)
            self._cancelled.add(execution_id)
        if process is None:
            return False
        self._terminate(process)
        return True

    def cancel_all(self) -> tuple[str, ...]:
        """Stop every running worker. Returns the execution IDs that were signalled."""

        return tuple(
            execution_id
            for execution_id in self.running_executions()
            if self.cancel(execution_id)
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        """Signal the worker's whole process group, escalating only if it ignores SIGTERM.

        Scientific workers spawn their own children (CUDA contexts, data loaders, BLAS pools);
        killing only the direct child would leave those holding GPU memory. ``start_new_session``
        makes each worker a process-group leader so one signal reaches the entire tree.
        """

        if process.poll() is not None:
            return
        for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                return
            try:
                process.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue

    def _run_worker(
        self,
        command: list[str],
        environment: dict[str, str],
        *,
        execution_id: str,
        label: str,
        timeout_seconds: float | None,
        progress: Callable[[str], None] | None = None,
        stdout_sink: Path | None = None,
        stderr_sink: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one capability worker as an interruptible process group, streaming its output.

        ``subprocess.run`` cannot be stopped from another thread, so a long compute would run
        to completion no matter what the user pressed. Registering the ``Popen`` here lets
        :meth:`cancel` signal it while the event loop stays free to service the interrupt.

        Both pipes are drained live by reader threads instead of a single buffered
        ``communicate()``: draining as data arrives is what lets a long training run report its
        own progress (scVI epoch bars, tqdm) through ``progress`` while it is still running, and
        also prevents a full pipe buffer from stalling the worker. The captured text is still
        returned intact for logs and error tails. The result envelope travels by file, not stdout,
        so streaming the pipes never disturbs it.
        """

        with self._lock:
            self._cancelled.discard(execution_id)
        process = subprocess.Popen(  # noqa: S603 - command is built from validated profiles
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        with self._lock:
            self._running[execution_id] = process

        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        # Live sinks: append the worker's output to disk as it streams, so the log is readable while
        # the job runs (not only rewritten at completion). Unbuffered so a watcher sees it promptly.
        out_sink = open(stdout_sink, "wb", buffering=0) if stdout_sink is not None else None
        err_sink = open(stderr_sink, "wb", buffering=0) if stderr_sink is not None else None
        readers = [
            threading.Thread(
                target=self._pump_stream,
                args=(process.stdout, out_chunks, progress, out_sink),
                daemon=True,
            ),
            threading.Thread(
                target=self._pump_stream,
                args=(process.stderr, err_chunks, progress, err_sink),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                self._terminate(process)
                process.wait()
                raise
        finally:
            # Join the readers so the pipes are fully drained before we assemble the captured
            # text; the process has exited (or been killed), so both reads have hit EOF.
            for reader in readers:
                reader.join()
            for sink in (out_sink, err_sink):
                if sink is not None:
                    with suppress(Exception):
                        sink.close()
            with self._lock:
                self._running.pop(execution_id, None)
                cancelled = execution_id in self._cancelled
                self._cancelled.discard(execution_id)
        if cancelled:
            raise CapabilityInterrupted(f"{label} was stopped by the user")
        stdout = b"".join(out_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(err_chunks).decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    # A worker line worth surfacing as live progress: a tqdm/Lightning bar or an epoch counter.
    # Kept deliberately narrow so ordinary log chatter never floods the spinner.
    _PROGRESS_HINT = re.compile(r"Epoch\s+\d|\d+%\|| it/s|\d+/\d+\s*\[")
    _ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

    @classmethod
    def _pump_stream(
        cls,
        stream: Any,
        chunks: list[bytes],
        progress: Callable[[str], None] | None,
        sink: Any = None,
    ) -> None:
        """Drain one worker pipe, capturing every byte and forwarding progress-like fragments.

        Progress bars overwrite in place with carriage returns rather than newlines, so the
        buffer is split on both ``\\r`` and ``\\n`` to surface intermediate bar updates, not only
        completed lines. Only fragments that look like a bar/epoch counter are forwarded, and any
        failure in the callback is swallowed — telemetry must never break the compute.

        ``sink`` (an open binary file) receives every byte as it arrives, so a long run's log is
        visible on disk *while it runs* — not only rewritten at completion. This is what lets an
        out-of-process watcher tail live progress; a sink write must never break the compute either.
        """

        raw = stream.raw if hasattr(stream, "raw") else stream
        buffer = b""
        try:
            while True:
                data = os.read(raw.fileno(), 65536)
                if not data:
                    break
                chunks.append(data)
                if sink is not None:
                    with suppress(Exception):
                        sink.write(data)
                        sink.flush()
                if progress is None:
                    continue
                buffer += data
                fragments = re.split(rb"[\r\n]", buffer)
                buffer = fragments.pop()
                for fragment in fragments:
                    cls._forward_progress(fragment, progress)
        except (OSError, ValueError):
            pass
        finally:
            if progress is not None and buffer:
                cls._forward_progress(buffer, progress)
            with suppress(Exception):
                stream.close()

    @classmethod
    def _forward_progress(cls, fragment: bytes, progress: Callable[[str], None]) -> None:
        text = cls._ANSI.sub("", fragment.decode("utf-8", errors="replace")).strip()
        if text and cls._PROGRESS_HINT.search(text):
            with suppress(Exception):
                progress(text[:160])

    def execute(
        self,
        package: SkillPackage,
        tool: CapabilityTool,
        arguments: dict[str, Any],
        context: CapabilityContext,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> EnvironmentExecution:
        profile = self.registry.resolve(tool.environment)
        if profile.name not in self._healthy:
            self.probe(profile.name)
        relative_path, function_name = tool.entrypoint_parts
        entrypoint = (package.root / relative_path).resolve()
        entrypoint.relative_to(package.root.resolve())
        input_path = context.staging_dir / ".worker-input.json"
        output_path = context.staging_dir / ".worker-output.json"
        stdout_path = context.staging_dir / "execution.stdout.log"
        stderr_path = context.staging_dir / "execution.stderr.log"
        payload = {
            "arguments": arguments,
            "context": {
                "scientific_session_id": context.scientific_session_id,
                "session_dir": str(context.session_dir),
                "staging_dir": str(context.staging_dir),
                "skill_id": context.skill_id,
                "tool_name": context.tool_name,
                "execution_id": context.execution_id,
                "state_revision": context.state_revision,
                "state_facts": context.state_facts,
                "state_lineage": context.state_lineage,
                "artifact_relative_path": context.artifact_relative_path,
                "state_decisions": context.state_decisions,
                "skill_version": context.skill_version,
            },
        }
        input_path.write_text(
            json.dumps(payload, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
        )
        environment = profile.build_environment()
        # Unbuffered worker output so a progress bar reaches the streamed pipes as it is drawn,
        # rather than sitting in a block buffer until the process exits.
        environment.setdefault("PYTHONUNBUFFERED", "1")
        pinned_devices: list[int] = []
        if profile.gpu_required:
            probe_result = self._probe_results.get(profile.name, {})
            pinned_devices = self.select_gpu_devices(
                list(probe_result.get("gpu_memory", [])), profile.gpu_count
            )
            if pinned_devices:
                # Pin the idlest device(s) so frameworks defaulting to device 0 do not land on a
                # busy GPU. Indices are the CUDA-visible ordering the probe measured.
                environment["CUDA_VISIBLE_DEVICES"] = ",".join(map(str, pinned_devices))
        bootstrap = f"import runpy; runpy.run_path({str(self.worker_path)!r}, run_name='__main__')"
        command = [
            str(profile.python),
            "-c",
            bootstrap,
            "--entrypoint",
            str(entrypoint),
            "--function",
            function_name,
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ]
        try:
            completed = self._run_worker(
                command,
                environment,
                execution_id=context.execution_id,
                label=tool.name,
                timeout_seconds=profile.timeout_seconds,
                progress=progress,
                stdout_sink=stdout_path,
                stderr_sink=stderr_path,
            )
        except subprocess.TimeoutExpired as exc:
            input_path.unlink(missing_ok=True)
            raise CapabilityExecutionError(
                f"{tool.name} exceeded {profile.timeout_seconds}s in {profile.name}"
            ) from exc
        except CapabilityInterrupted:
            input_path.unlink(missing_ok=True)
            raise
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        input_path.unlink(missing_ok=True)
        if completed.returncode != 0:
            tail = completed.stderr.strip()[-4000:] or completed.stdout.strip()[-4000:]
            raise CapabilityExecutionError(
                f"{tool.name} failed in {profile.name} (exit {completed.returncode}): {tail}"
            )
        if not output_path.is_file():
            raise CapabilityExecutionError(
                f"{tool.name} produced no result in environment {profile.name}"
            )
        try:
            value = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CapabilityExecutionError(
                f"{tool.name} returned invalid JSON in {profile.name}: {exc}"
            ) from exc
        output_path.unlink(missing_ok=True)
        probe = self._probe_results.get(profile.name, {})
        return EnvironmentExecution(
            value=value,
            provenance={
                "name": profile.name,
                "fingerprint": profile.fingerprint,
                "runtime": profile.runtime.name,
                "runtime_fingerprint": profile.runtime.fingerprint,
                "python": str(profile.python),
                "prefix": str(profile.runtime.prefix) if profile.runtime.prefix else None,
                "gpu_required": profile.gpu_required,
                "gpu_count": profile.gpu_count,
                "min_gpu_memory_mb": profile.min_gpu_memory_mb,
                "pinned_cuda_devices": pinned_devices,
                "module_versions": probe.get("module_versions", {}),
                "cuda_runtime_version": probe.get("cuda_runtime_version"),
                "environment_isolated": True,
            },
        )
