"""Versioned capability.yaml contract for executable skill packages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scagent_sdk.errors import CapabilityManifestError

CAPABILITY_SCHEMA_VERSION = 1
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENTRYPOINT = re.compile(r"^(?P<path>[^:]+\.py):(?P<function>[A-Za-z_][A-Za-z0-9_]*)$")
_LINEAGE_OPERATIONS = frozenset({"checkout"})
_ADOPT_UNTRACKED_ARGUMENT = "adopt_untracked"
_ADOPT_UNTRACKED_SCHEMA = {
    "type": "boolean",
    "default": False,
    "description": (
        "Explicitly replace the active analysis with an untracked matrix. Use only when starting "
        "a new line of work from a file this analysis did not produce."
    ),
}


def _mapping(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapabilityManifestError(f"{name} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _nonempty(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityManifestError(f"{name} must be a non-empty string")
    return value.strip()


def _with_executor_lineage_controls(
    schema: dict[str, Any], *, primary_matrix_output: str | None, advances_lineage: bool
) -> dict[str, Any]:
    """Expose executor-owned controls without duplicating them across every manifest."""

    if primary_matrix_output is None or not advances_lineage:
        return schema
    updated = dict(schema)
    properties = dict(updated.get("properties") or {})
    properties.setdefault(_ADOPT_UNTRACKED_ARGUMENT, dict(_ADOPT_UNTRACKED_SCHEMA))
    updated["properties"] = properties
    return updated


@dataclass(frozen=True)
class CapabilityTool:
    name: str
    description: str
    entrypoint: str
    input_schema: dict[str, Any]
    environment: str = "current"
    activity_label: str | None = None
    floors: tuple[str, ...] = ()
    # Which argument carries the analysis matrix this tool reads. Declared, not inferred: the
    # executor resolves an omitted value to the active lineage head, and validates a supplied one
    # against it. Absent means the tool takes no lineage input -- an arbitrary file it merely
    # inspects is not the analysis matrix.
    primary_matrix_input: str | None = None
    # Which declared artifact is the tool's matrix output. Named rather than detected by suffix,
    # because CellBender emits three ``.h5`` matrices of which only the filtered one continues the
    # analysis, and some tools publish a format-only matrix without moving the head.
    primary_matrix_output: str | None = None
    # Most matrix outputs become the next analysis version. A format-only publication still has to
    # declare which artifact is the matrix, but setting this false records it as a derivative of
    # the consumed version without moving the active head.
    advances_lineage: bool = True
    # A lineage mutation the executor performs on this tool's behalf. Skills must never write
    # session state, so a tool that switches the active version declares the intent and validates
    # its target; the executor applies it. Currently only ``checkout``.
    lineage_operation: str | None = None

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.name):
            raise CapabilityManifestError(f"tool name {self.name!r} must use lowercase snake_case")
        _nonempty(self.description, name=f"tool {self.name}.description")
        match = _ENTRYPOINT.fullmatch(self.entrypoint)
        if match is None or Path(match.group("path")).is_absolute():
            raise CapabilityManifestError(
                f"tool {self.name}.entrypoint must be relative/path.py:function"
            )
        schema_type = self.input_schema.get("type")
        if schema_type != "object":
            raise CapabilityManifestError(
                f"tool {self.name}.input_schema must be a JSON object schema"
            )
        _nonempty(self.environment, name=f"tool {self.name}.environment")
        if self.activity_label is not None:
            _nonempty(self.activity_label, name=f"tool {self.name}.activity_label")
        for floor in self.floors:
            _nonempty(floor, name=f"tool {self.name}.floors")
        if self.primary_matrix_input is not None:
            _nonempty(self.primary_matrix_input, name=f"tool {self.name}.primary_matrix_input")
            properties = self.input_schema.get("properties")
            if isinstance(properties, dict) and self.primary_matrix_input not in properties:
                raise CapabilityManifestError(
                    f"tool {self.name}.primary_matrix_input names "
                    f"{self.primary_matrix_input!r}, which is not an input_schema property"
                )
            required = self.input_schema.get("required")
            if isinstance(required, list) and self.primary_matrix_input in required:
                raise CapabilityManifestError(
                    f"tool {self.name}.primary_matrix_input {self.primary_matrix_input!r} must not "
                    "be in input_schema.required: the executor resolves an omitted value to the "
                    "active lineage head, and a required argument can never be omitted"
                )
        if self.primary_matrix_output is not None:
            _nonempty(self.primary_matrix_output, name=f"tool {self.name}.primary_matrix_output")
        if not self.advances_lineage and self.primary_matrix_output is None:
            raise CapabilityManifestError(
                f"tool {self.name}.advances_lineage=false requires primary_matrix_output"
            )
        if self.lineage_operation is not None and self.lineage_operation not in _LINEAGE_OPERATIONS:
            raise CapabilityManifestError(
                f"tool {self.name}.lineage_operation must be one of "
                f"{sorted(_LINEAGE_OPERATIONS)}, not {self.lineage_operation!r}"
            )

    @property
    def entrypoint_parts(self) -> tuple[Path, str]:
        match = _ENTRYPOINT.fullmatch(self.entrypoint)
        assert match is not None
        return Path(match.group("path")), match.group("function")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "entrypoint": self.entrypoint,
            "environment": self.environment,
            "activity_label": self.activity_label,
            "floors": list(self.floors),
            "input_schema": self.input_schema,
            "primary_matrix_input": self.primary_matrix_input,
            "primary_matrix_output": self.primary_matrix_output,
            "advances_lineage": self.advances_lineage,
            "lineage_operation": self.lineage_operation,
        }

    @classmethod
    def from_dict(cls, value: Any) -> CapabilityTool:
        data = _mapping(value, name="tool")
        try:
            primary_matrix_output = (
                _nonempty(data["primary_matrix_output"], name="tool.primary_matrix_output")
                if data.get("primary_matrix_output") is not None
                else None
            )
            advances_lineage = data.get("advances_lineage", True)
            if not isinstance(advances_lineage, bool):
                raise CapabilityManifestError("tool.advances_lineage must be true or false")
            input_schema = _with_executor_lineage_controls(
                _mapping(data["input_schema"], name="tool.input_schema"),
                primary_matrix_output=primary_matrix_output,
                advances_lineage=advances_lineage,
            )
            return cls(
                name=_nonempty(data["name"], name="tool.name"),
                description=_nonempty(data["description"], name="tool.description"),
                entrypoint=_nonempty(data["entrypoint"], name="tool.entrypoint"),
                input_schema=input_schema,
                environment=_nonempty(data.get("environment", "current"), name="tool.environment"),
                activity_label=(
                    _nonempty(data["activity_label"], name="tool.activity_label")
                    if data.get("activity_label") is not None
                    else None
                ),
                floors=tuple(str(item) for item in data.get("floors", [])),
                primary_matrix_input=(
                    _nonempty(data["primary_matrix_input"], name="tool.primary_matrix_input")
                    if data.get("primary_matrix_input") is not None
                    else None
                ),
                primary_matrix_output=primary_matrix_output,
                advances_lineage=advances_lineage,
                lineage_operation=(
                    _nonempty(data["lineage_operation"], name="tool.lineage_operation")
                    if data.get("lineage_operation") is not None
                    else None
                ),
            )
        except KeyError as exc:
            raise CapabilityManifestError(f"tool is missing required field: {exc.args[0]}") from exc


@dataclass(frozen=True)
class CapabilityReadiness:
    """A fast, side-effect-free probe of local prerequisites this skill cannot download.

    Reference models and other host assets decide whether a skill can run at all, so the
    answer belongs in the model's context before it is asked, not behind a filesystem
    search. The probe runs in the control plane with only the declared capability
    environment's variables, so it must use the standard library and never import
    scientific packages or touch a GPU.
    """

    entrypoint: str
    environment: str | None = None

    def __post_init__(self) -> None:
        match = _ENTRYPOINT.fullmatch(self.entrypoint)
        if match is None or Path(match.group("path")).is_absolute():
            raise CapabilityManifestError("readiness.entrypoint must be relative/path.py:function")
        if self.environment is not None:
            _nonempty(self.environment, name="readiness.environment")

    @property
    def entrypoint_parts(self) -> tuple[Path, str]:
        match = _ENTRYPOINT.fullmatch(self.entrypoint)
        assert match is not None
        return Path(match.group("path")), match.group("function")

    def to_dict(self) -> dict[str, Any]:
        return {"entrypoint": self.entrypoint, "environment": self.environment}

    @classmethod
    def from_dict(cls, value: Any) -> CapabilityReadiness:
        data = _mapping(value, name="readiness")
        try:
            environment = data.get("environment")
            return cls(
                entrypoint=_nonempty(data["entrypoint"], name="readiness.entrypoint"),
                environment=(
                    _nonempty(environment, name="readiness.environment")
                    if environment is not None
                    else None
                ),
            )
        except KeyError as exc:
            raise CapabilityManifestError(
                f"readiness is missing required field: {exc.args[0]}"
            ) from exc


@dataclass(frozen=True)
class CapabilityManifest:
    skill_id: str
    version: str
    description: str
    tools: tuple[CapabilityTool, ...]
    readiness: CapabilityReadiness | None = None
    schema_version: int = CAPABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_SCHEMA_VERSION:
            raise CapabilityManifestError(
                f"unsupported capability schema version: {self.schema_version}"
            )
        if not _SKILL_NAME.fullmatch(self.skill_id):
            raise CapabilityManifestError(
                f"skill id {self.skill_id!r} must use lowercase kebab-case"
            )
        _nonempty(self.version, name="skill.version")
        _nonempty(self.description, name="skill.description")
        if not self.tools:
            raise CapabilityManifestError("capability manifest must declare at least one tool")
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise CapabilityManifestError("tool names must be unique within a skill")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "skill": {
                "id": self.skill_id,
                "version": self.version,
                "description": self.description,
            },
            "tools": [tool.to_dict() for tool in self.tools],
            "readiness": self.readiness.to_dict() if self.readiness else None,
        }

    @classmethod
    def from_path(cls, path: str | Path) -> CapabilityManifest:
        source = Path(path).expanduser().resolve()
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CapabilityManifestError(f"capability manifest not found: {source}") from exc
        except yaml.YAMLError as exc:
            raise CapabilityManifestError(f"invalid YAML in {source}: {exc}") from exc
        data = _mapping(raw, name="capability manifest")
        skill = _mapping(data.get("skill"), name="skill")
        tools = data.get("tools")
        if not isinstance(tools, list):
            raise CapabilityManifestError("tools must be a list")
        readiness = data.get("readiness")
        try:
            return cls(
                schema_version=data["schema_version"],
                skill_id=_nonempty(skill["id"], name="skill.id"),
                version=_nonempty(skill["version"], name="skill.version"),
                description=_nonempty(skill["description"], name="skill.description"),
                tools=tuple(CapabilityTool.from_dict(item) for item in tools),
                readiness=(
                    CapabilityReadiness.from_dict(readiness) if readiness is not None else None
                ),
            )
        except KeyError as exc:
            raise CapabilityManifestError(
                f"manifest is missing required field: {exc.args[0]}"
            ) from exc
