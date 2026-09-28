"""Physical execution runtimes mapped to logical capability environments."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli

from scagent_sdk.errors import EnvironmentProfileError

ENVIRONMENT_SCHEMA_VERSION = 2
_SAFE_INHERIT = (
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LANGUAGE",
    "TERM",
    "TZ",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CUDA_VISIBLE_DEVICES",
    "SLURM_JOB_ID",
    "SLURM_STEP_ID",
    "SLURM_LOCALID",
    "SLURM_PROCID",
    "SLURM_NTASKS",
)
_SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"


def _resolve_path(raw: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw))
    return Path(expanded).resolve()


def _resolve_executable(raw: str) -> Path:
    candidate = _resolve_path(raw)
    if Path(raw).expanduser().is_absolute():
        return candidate
    found = shutil.which(os.path.expandvars(os.path.expanduser(raw)))
    return Path(found).resolve() if found else candidate


def _mapping(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EnvironmentProfileError(f"{name} must be a table")
    return {str(key): item for key, item in value.items()}


def _string_mapping(value: Any, *, name: str) -> dict[str, str]:
    data = _mapping(value, name=name)
    if not all(isinstance(item, str) for item in data.values()):
        raise EnvironmentProfileError(f"{name} must contain string values")
    return {key: str(item) for key, item in data.items()}


def _string_tuple(value: Any, *, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise EnvironmentProfileError(f"{name} must be a string list")
    return tuple(value)


def _expand_runtime_value(value: str, prefix: Path | None) -> str:
    expanded = value.replace("{prefix}", str(prefix) if prefix is not None else "")
    return os.path.expandvars(os.path.expanduser(expanded))


@dataclass(frozen=True)
class ExecutionRuntime:
    """One physical Python installation and its activation contract."""

    name: str
    python: Path
    prefix: Path | None = None
    environment: dict[str, str] = field(default_factory=dict)
    inherit_variables: tuple[str, ...] = ()
    path_entries: tuple[str, ...] = ()
    probe_modules: tuple[str, ...] = ()
    gpu_probe: str = "cupy"
    probe_timeout_seconds: int = 120
    provenance_files: tuple[Path, ...] = ()

    def validate(self) -> None:
        if not self.name.strip():
            raise EnvironmentProfileError("runtime name must not be empty")
        if not self.python.is_file() or not os.access(self.python, os.X_OK):
            raise EnvironmentProfileError(
                f"runtime {self.name!r} Python is not executable: {self.python}"
            )
        if self.prefix is not None and not self.prefix.is_dir():
            raise EnvironmentProfileError(
                f"runtime {self.name!r} prefix is not a directory: {self.prefix}"
            )
        missing = [str(path) for path in self.provenance_files if not path.is_file()]
        if missing:
            raise EnvironmentProfileError(
                f"runtime {self.name!r} provenance files are missing: {', '.join(missing)}"
            )
        if self.gpu_probe not in {"cupy", "torch"}:
            raise EnvironmentProfileError("runtime gpu_probe must be cupy or torch")
        if self.probe_timeout_seconds <= 0:
            raise EnvironmentProfileError("runtime probe timeout must be positive")

    @property
    def fingerprint(self) -> str:
        stat = self.python.stat()
        provenance: list[dict[str, str]] = []
        for path in self.provenance_files:
            provenance.append(
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        value = {
            "name": self.name,
            "python": str(self.python),
            "python_size": stat.st_size,
            "python_mtime_ns": stat.st_mtime_ns,
            "prefix": str(self.prefix) if self.prefix is not None else None,
            "environment": self.environment,
            "inherit_variables": self.inherit_variables,
            "path_entries": self.path_entries,
            "probe_modules": self.probe_modules,
            "gpu_probe": self.gpu_probe,
            "probe_timeout_seconds": self.probe_timeout_seconds,
            "provenance": provenance,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def build_environment(self, parent: dict[str, str] | None = None) -> dict[str, str]:
        source = parent if parent is not None else dict(os.environ)
        allowed = set(_SAFE_INHERIT) | set(self.inherit_variables)
        environment = {key: source[key] for key in allowed if key in source}
        environment.update(
            {
                key: _expand_runtime_value(value, self.prefix)
                for key, value in self.environment.items()
            }
        )
        path_parts: list[str] = []
        if self.prefix is not None:
            path_parts.append(str(self.prefix / "bin"))
            environment.setdefault("CONDA_PREFIX", str(self.prefix))
            environment.setdefault("CONDA_DEFAULT_ENV", self.name)
            environment.setdefault("CONDA_SHLVL", "1")
        path_parts.extend(_expand_runtime_value(item, self.prefix) for item in self.path_entries)
        path_parts.extend(_SYSTEM_PATH.split(":"))
        environment["PATH"] = ":".join(dict.fromkeys(item for item in path_parts if item))
        environment.setdefault("PYTHONNOUSERSITE", "1")
        environment.pop("VIRTUAL_ENV", None)
        environment.pop("PYTHONHOME", None)
        environment.pop("PYTHONPATH", None)
        return environment


@dataclass(frozen=True)
class EnvironmentProfile:
    """A logical capability requirement routed to one physical runtime."""

    name: str
    runtime: ExecutionRuntime
    timeout_seconds: int = 3600
    gpu_required: bool = False
    gpu_count: int = 0
    min_gpu_memory_mb: int = 0
    environment: dict[str, str] = field(default_factory=dict)
    required_modules: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.name.strip():
            raise EnvironmentProfileError("environment name must not be empty")
        self.runtime.validate()
        if self.timeout_seconds <= 0:
            raise EnvironmentProfileError("environment timeout must be positive")
        if self.gpu_count < 0 or self.min_gpu_memory_mb < 0:
            raise EnvironmentProfileError("GPU resource values must not be negative")
        if self.gpu_required and self.gpu_count < 1:
            raise EnvironmentProfileError("GPU-required environments need gpu_count >= 1")

    @property
    def python(self) -> Path:
        return self.runtime.python

    @property
    def fingerprint(self) -> str:
        value = {
            "name": self.name,
            "runtime": self.runtime.name,
            "runtime_fingerprint": self.runtime.fingerprint,
            "timeout_seconds": self.timeout_seconds,
            "gpu_required": self.gpu_required,
            "gpu_count": self.gpu_count,
            "min_gpu_memory_mb": self.min_gpu_memory_mb,
            "environment": self.environment,
            "required_modules": self.required_modules,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def build_environment(self, parent: dict[str, str] | None = None) -> dict[str, str]:
        environment = self.runtime.build_environment(parent)
        environment.update(
            {
                key: _expand_runtime_value(value, self.runtime.prefix)
                for key, value in self.environment.items()
            }
        )
        return environment


class EnvironmentRegistry:
    def __init__(
        self,
        profiles: dict[str, EnvironmentProfile],
        *,
        runtimes: dict[str, ExecutionRuntime] | None = None,
        source: Path | None = None,
    ):
        self._profiles = profiles
        self._runtimes = runtimes or {
            profile.runtime.name: profile.runtime for profile in profiles.values()
        }
        self.source = source

    @classmethod
    def empty(cls) -> EnvironmentRegistry:
        return cls({})

    @classmethod
    def from_path(cls, path: str | Path) -> EnvironmentRegistry:
        source = Path(path).expanduser().resolve()
        try:
            raw = tomli.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise EnvironmentProfileError(f"environment profile file not found: {source}") from exc
        except tomli.TOMLDecodeError as exc:
            raise EnvironmentProfileError(f"invalid environment TOML {source}: {exc}") from exc
        # Repository profiles name paths relative to ${SCAGENT_SDK_PROJECT_ROOT}.
        if source.parent.name == "environments" and source.parent.parent.name == "configs":
            os.environ.setdefault("SCAGENT_SDK_PROJECT_ROOT", str(source.parents[2]))
        schema_version = int(raw.get("schema_version", 1))
        if schema_version == 1:
            return cls._from_v1(raw, source)
        if schema_version != ENVIRONMENT_SCHEMA_VERSION:
            raise EnvironmentProfileError(
                f"unsupported environment schema version: {schema_version}"
            )
        runtimes_raw = _mapping(raw.get("runtimes"), name="runtimes")
        capabilities_raw = _mapping(raw.get("capabilities"), name="capabilities")
        runtimes: dict[str, ExecutionRuntime] = {}
        for name, value in runtimes_raw.items():
            data = _mapping(value, name=f"runtime {name!r}")
            python = data.get("python")
            if not isinstance(python, str) or not python.strip():
                raise EnvironmentProfileError(f"runtime {name!r} needs python")
            prefix_raw = data.get("prefix")
            runtime = ExecutionRuntime(
                name=name,
                python=_resolve_executable(python),
                prefix=_resolve_path(prefix_raw) if isinstance(prefix_raw, str) else None,
                environment=_string_mapping(
                    data.get("environment", {}), name=f"runtime {name!r}.environment"
                ),
                inherit_variables=_string_tuple(
                    data.get("inherit_variables"), name=f"runtime {name!r}.inherit_variables"
                ),
                path_entries=_string_tuple(
                    data.get("path_entries"), name=f"runtime {name!r}.path_entries"
                ),
                probe_modules=_string_tuple(
                    data.get("probe_modules"), name=f"runtime {name!r}.probe_modules"
                ),
                gpu_probe=str(data.get("gpu_probe", "cupy")),
                probe_timeout_seconds=int(data.get("probe_timeout_seconds", 120)),
                provenance_files=tuple(
                    _resolve_path(item)
                    for item in _string_tuple(
                        data.get("provenance_files"),
                        name=f"runtime {name!r}.provenance_files",
                    )
                ),
            )
            runtime.validate()
            runtimes[name] = runtime
        profiles: dict[str, EnvironmentProfile] = {}
        for name, value in capabilities_raw.items():
            data = _mapping(value, name=f"capability {name!r}")
            runtime_name = data.get("runtime")
            if not isinstance(runtime_name, str) or runtime_name not in runtimes:
                raise EnvironmentProfileError(
                    f"capability {name!r} references unknown runtime {runtime_name!r}"
                )
            gpu_required = bool(data.get("gpu_required", False))
            profile = EnvironmentProfile(
                name=name,
                runtime=runtimes[runtime_name],
                timeout_seconds=int(data.get("timeout_seconds", 3600)),
                gpu_required=gpu_required,
                gpu_count=int(data.get("gpu_count", 1 if gpu_required else 0)),
                min_gpu_memory_mb=int(data.get("min_gpu_memory_mb", 0)),
                environment=_string_mapping(
                    data.get("environment", {}), name=f"capability {name!r}.environment"
                ),
                required_modules=_string_tuple(
                    data.get("required_modules"), name=f"capability {name!r}.required_modules"
                ),
            )
            profile.validate()
            profiles[name] = profile
        return cls(profiles, runtimes=runtimes, source=source)

    @classmethod
    def _from_v1(cls, raw: dict[str, Any], source: Path) -> EnvironmentRegistry:
        environments = _mapping(raw.get("environments"), name="environments")
        profiles: dict[str, EnvironmentProfile] = {}
        runtimes: dict[str, ExecutionRuntime] = {}
        for name, value in environments.items():
            data = _mapping(value, name=f"environment {name!r}")
            python = data.get("python")
            if not isinstance(python, str) or not python.strip():
                raise EnvironmentProfileError(f"environment {name!r} needs python")
            runtime = ExecutionRuntime(name=f"legacy-{name}", python=_resolve_executable(python))
            gpu_required = bool(data.get("gpu_required", False))
            profile = EnvironmentProfile(
                name=name,
                runtime=runtime,
                timeout_seconds=int(data.get("timeout_seconds", 3600)),
                gpu_required=gpu_required,
                gpu_count=1 if gpu_required else 0,
                environment=_string_mapping(
                    data.get("environment", {}), name=f"environment {name!r}.environment"
                ),
                required_modules=_string_tuple(
                    data.get("required_modules"), name=f"environment {name!r}.required_modules"
                ),
            )
            profile.validate()
            profiles[name] = profile
            runtimes[runtime.name] = runtime
        return cls(profiles, runtimes=runtimes, source=source)

    def resolve(self, name: str) -> EnvironmentProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            source = f" in {self.source}" if self.source else ""
            raise EnvironmentProfileError(
                f"logical environment {name!r} is not configured{source}"
            ) from exc

    def profiles(self) -> tuple[EnvironmentProfile, ...]:
        return tuple(self._profiles[name] for name in sorted(self._profiles))

    def runtimes(self) -> tuple[ExecutionRuntime, ...]:
        return tuple(self._runtimes[name] for name in sorted(self._runtimes))
