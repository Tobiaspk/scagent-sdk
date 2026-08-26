"""Assemble validated skill capabilities into isolated Claude SDK extensions."""

from __future__ import annotations

import importlib
from typing import Any

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.instructions import instruction_sources, render_skill_instructions
from scagent_sdk.capabilities.readiness import (
    probe_environments,
    probe_packages,
    render_readiness_block,
)
from scagent_sdk.capabilities.registry import CapabilityRegistry, SkillPackage
from scagent_sdk.capabilities.skill_plugin import materialize_skill_plugin
from scagent_sdk.execution.broker import EnvironmentBroker
from scagent_sdk.floors import FloorEvaluator
from scagent_sdk.runtime.claude import ClaudeRuntimeExtensions
from scagent_sdk.runtime.observer import NullRuntimeObserver, RuntimeObserver, ToolActivity
from scagent_sdk.session import AnalysisSession


class CapabilityAssembler:
    def __init__(
        self,
        registry: CapabilityRegistry,
        session: AnalysisSession,
        *,
        sdk_module: Any | None = None,
        observer: RuntimeObserver | None = None,
        environment_broker: EnvironmentBroker | None = None,
    ):
        self.registry = registry
        self.session = session
        self.environment_broker = environment_broker
        self.executor = CapabilityExecutor(session, environment_broker=environment_broker)
        self._sdk_module = sdk_module
        self.observer = observer or NullRuntimeObserver()
        self.floors = FloorEvaluator()

    def _sdk(self) -> Any:
        return self._sdk_module or importlib.import_module("claude_agent_sdk")

    def assemble(self) -> ClaudeRuntimeExtensions:
        sdk = self._sdk()
        packages = self.registry.discover()
        discovered_skills = self.registry.skills()
        self.executor.recover_pending()
        mcp_servers: dict[str, Any] = {}
        allowed_tools: list[str] = []
        post_hooks: list[Any] = []
        pre_hooks: list[Any] = []
        for package in packages:
            server_name = package.manifest.skill_id.replace("-", "_")
            sdk_tools: list[Any] = []
            for tool in package.manifest.tools:
                handler = self._handler(package, tool)
                sdk_tool = sdk.tool(tool.name, tool.description, tool.input_schema)(handler)
                sdk_tools.append(sdk_tool)
                full_name = f"mcp__{server_name}__{tool.name}"
                allowed_tools.append(full_name)
                post_hooks.append(sdk.HookMatcher(matcher=full_name, hooks=[self._post_tool_hook]))
                if tool.floors:
                    pre_hooks.append(
                        sdk.HookMatcher(
                            matcher=full_name,
                            hooks=[self._pre_tool_hook(tool.floors)],
                        )
                    )
            mcp_servers[server_name] = sdk.create_sdk_mcp_server(
                server_name, version=package.manifest.version, tools=sdk_tools
            )
        hooks: dict[str, list[Any]] = {}
        if pre_hooks:
            hooks["PreToolUse"] = pre_hooks
        if post_hooks:
            hooks["PostToolUse"] = post_hooks
        readiness = probe_packages(packages, broker=self.environment_broker)
        environments = probe_environments(packages, broker=self.environment_broker)
        if readiness or environments:
            self.session.store.record(
                "capability.readiness_probed",
                payload={
                    "reports": [report.to_dict() for report in readiness],
                    "environments": [report.to_dict() for report in environments],
                },
            )
        self.session.store.record(
            "capability.registry_assembled",
            payload={
                "skills_root": str(self.registry.skills_root),
                "skills": [
                    {
                        "id": package.manifest.skill_id,
                        "version": package.manifest.version,
                        "fingerprint": package.fingerprint,
                        "tools": [tool.name for tool in package.manifest.tools],
                    }
                    for package in packages
                ],
                "allowed_tools": allowed_tools,
                "enabled_skills": [
                    {
                        "name": skill.name,
                        "fingerprint": skill.fingerprint,
                        "executable": skill.executable,
                    }
                    for skill in discovered_skills
                ],
            },
        )
        plugin_root = materialize_skill_plugin(
            self.registry.skills_root, self.session.directory / "runtime" / "skills-plugin"
        )
        instructions = render_skill_instructions(discovered_skills)
        self.session.store.record(
            "capability.instructions_injected",
            payload={
                "bytes": len(instructions),
                "skills": instruction_sources(discovered_skills),
            },
        )
        suffix = "\n\n".join(
            block
            for block in (render_readiness_block(readiness, environments), instructions)
            if block
        )
        return ClaudeRuntimeExtensions(
            mcp_servers=mcp_servers,
            allowed_tools=tuple(allowed_tools),
            hooks=hooks,
            skills=tuple(skill.name for skill in discovered_skills),
            # `Skill` is the one built-in tool this runtime grants: without it the skill
            # instructions beside every capability never reach the model, leaving it to work from
            # tool schemas alone. Deny-by-default still holds for every other built-in tool.
            tools=("Skill",),
            plugins=({"type": "local", "path": str(plugin_root)},),
            include_hook_events=True,
            system_prompt_suffix=suffix,
        )

    def _handler(self, package: SkillPackage, tool: Any) -> Any:
        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            activity = ToolActivity(
                tool_name=tool.name,
                label=tool.activity_label or tool.name.replace("_", " ").title(),
                skill_id=package.manifest.skill_id,
            )
            self.observer.on_tool_started(activity)

            def _progress(text: str) -> None:
                # Called from the compute worker thread; the observer only stores the string.
                self.observer.on_tool_progress(activity, text)

            response = await self.executor.execute(
                package, tool, arguments, progress=_progress
            )
            if response.get("is_error"):
                # The full detail stays in the tool result the model receives; the terminal only
                # needs the concise one-line summary.
                summary = response.get("error_summary")
                if not summary:
                    content = response.get("content") or []
                    summary = (
                        str(content[0].get("text", "capability failed"))
                        if content
                        else "capability failed"
                    )
                self.observer.on_tool_failed(activity, str(summary))
            else:
                structured = response.get("structuredContent") or {}
                self.observer.on_tool_finished(activity, structured.get("summary"))
            return response

        return handler

    def _pre_tool_hook(self, floors: tuple[str, ...]) -> Any:
        async def hook(
            _input_data: dict[str, Any],
            _tool_use_id: str | None,
            _context: dict[str, Any],
        ) -> dict[str, Any]:
            failures = self.floors.failures(self.session.store.state, floors)
            if not failures:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                    }
                }
            reason = " ".join(
                f"[{failure.floor}] {failure.reason} {failure.remediation}" for failure in failures
            )
            self.session.store.record(
                "floor.denied",
                payload={
                    "floors": [failure.floor for failure in failures],
                    "reason": reason,
                },
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        return hook

    async def _post_tool_hook(
        self,
        input_data: dict[str, Any],
        _tool_use_id: str | None,
        _context: dict[str, Any],
    ) -> dict[str, Any]:
        self.executor.commit_from_hook(input_data)
        return {}
