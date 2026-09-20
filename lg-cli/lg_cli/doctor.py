from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from .catalog import load_workflows
from .config import LGConfig
from .core_adapter import CodexExecAdapter, inspect_core
from .definitions import DefinitionRegistry
from .knowledge import KnowledgeGateway
from .paths import product_root
from .project_store import ProjectStore


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "message": self.message}


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return not any(check.status == "FAIL" for check in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1


def run_doctor(config: LGConfig, *, probe: bool = False) -> DoctorReport:
    checks: list[DoctorCheck] = []
    lg_root = config.workspace / ".literarygiant"
    checks.append(
        DoctorCheck(
            "workspace",
            "PASS" if lg_root.is_dir() else "WARN",
            str(lg_root) if lg_root.is_dir() else f"not initialized; run `literary init` ({lg_root})",
        )
    )
    story_store = ProjectStore(config.workspace)
    if story_store.initialized:
        try:
            healthy, message = story_store.integrity_check()
            summary = story_store.summary() if healthy else None
            if summary:
                message += (
                    f"; project={summary['name']}, chapters={summary['chapters']}, "
                    f"scenes={summary['scenes']}, candidates={summary['candidate_versions']}, "
                    f"proposals={summary['pending_proposals']}"
                )
            checks.append(DoctorCheck("story-db", "PASS" if healthy else "FAIL", message))
        except Exception as exc:
            checks.append(DoctorCheck("story-db", "FAIL", str(exc)))
    else:
        checks.append(
            DoctorCheck(
                "story-db",
                "WARN",
                f"not initialized; run `literary init` ({story_store.database_path})",
            )
        )
    checks.append(
        DoctorCheck(
            "config",
            "PASS",
            f"provider={config.provider}, model={config.model_label}, files={len(config.loaded_files)}",
        )
    )
    checks.append(
        DoctorCheck(
            "api-key",
            "PASS" if config.api_key else "WARN",
            config.api_key_source if config.api_key else "not configured; dry-run remains available",
        )
    )

    if config.uses_anthropic:
        try:
            import anthropic
            checks.append(DoctorCheck("provider-bridge", "PASS", f"Anthropic SDK {anthropic.__version__}; Messages bridge ready (API not probed)"))
        except ImportError:
            checks.append(DoctorCheck("provider-bridge", "FAIL", "Anthropic SDK missing; reinstall literarygiant-cli with dependencies"))
    core = inspect_core(runtime_manifest=config.runtime_manifest)
    checks.append(
        DoctorCheck(
            "core-runtime",
            "PASS" if core.runtime_available else "FAIL",
            _runtime_summary(core),
        )
    )
    pin_valid = bool(core.pinned_commit and re.fullmatch(r"[0-9a-f]{40}", core.pinned_commit))
    checks.append(
        DoctorCheck(
            "core-pin",
            "PASS" if pin_valid else "FAIL",
            core.pinned_commit or "CODEX_CORE_COMMIT is missing",
        )
    )
    checks.append(_verify_core_tree(core.source_available))

    try:
        registry = DefinitionRegistry(config.workspace)
        schema_count = sum(1 for agent in registry.agents if agent.output_schema is not None)
        definition_status = "PASS" if len(registry.skills) >= 15 and len(registry.agents) >= 12 else "FAIL"
        checks.append(
            DoctorCheck(
                "definitions",
                definition_status,
                f"skills={len(registry.skills)}, subagents={len(registry.agents)}, schemas={schema_count}",
            )
        )
        checks.append(_validate_workflow_definitions(registry))
    except Exception as exc:
        checks.append(DoctorCheck("definitions", "FAIL", str(exc)))

    gateway = KnowledgeGateway(config)
    if gateway.library_root is None:
        checks.append(DoctorCheck("knowledge", "WARN", "no Library/AbstractLibrary root discovered"))
    else:
        abstract = gateway.library_root / "AbstractLibrary"
        bridge_index = gateway.library_root / "BridgeIndex"
        status = "PASS" if abstract.is_dir() else "FAIL"
        message = f"AbstractLibrary={abstract.is_dir()}, BridgeIndex={bridge_index.is_dir()} ({gateway.library_root})"
        checks.append(DoctorCheck("knowledge", status, message))

    output_parent = _existing_parent(config.output_path)
    writable = output_parent is not None and os.access(output_parent, os.W_OK)
    checks.append(
        DoctorCheck(
            "output",
            "PASS" if writable else "FAIL",
            f"writable parent: {output_parent}" if writable else f"not writable: {config.output_path}",
        )
    )
    if probe:
        checks.append(probe_provider(config))
    return DoctorReport(tuple(checks))


def probe_provider(config: LGConfig) -> DoctorCheck:
    if not config.api_key:
        return DoctorCheck("provider-probe", "FAIL", "No provider API key configured; no request sent")
    result = CodexExecAdapter().run(
        prompt="This is a connectivity check, not a writing task. Do not call tools. Reply only LG_READY.",
        config=replace(config, max_output_tokens=64, timeout_seconds=60, enable_shell=False),
        mode="chat", model_profile="default",
    )
    ready = result.ok and not result.used_stub and "LG_READY" in result.output_text
    return DoctorCheck("provider-probe", "PASS" if ready else "FAIL",
        "Real model roundtrip passed through the configured Codex runtime" if ready
        else result.error or "Model did not return the expected connectivity marker")


def _runtime_summary(core) -> str:
    healthy = [candidate for candidate in core.candidates if candidate.available]
    if not healthy:
        return "no candidate passed health checks"
    return ", ".join(
        f"{candidate.name} ({candidate.version or candidate.reason})" for candidate in healthy
    )


def _verify_core_tree(source_available: bool) -> DoctorCheck:
    root = product_root()
    script = root / "scripts" / "update_codex_core.sh"
    if not source_available or not script.exists():
        return DoctorCheck(
            "core-tree",
            "WARN",
            "vendored source verification is unavailable in this installation",
        )
    try:
        completed = subprocess.run(
            [str(script), "--verify"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck("core-tree", "FAIL", f"verification failed to run: {exc}")
    lines = [line.strip() for line in (completed.stdout or completed.stderr).splitlines() if line.strip()]
    verified = next((line for line in reversed(lines) if line.startswith("Codex core verified:")), None)
    output = verified or (lines[-1] if lines else "")
    return DoctorCheck(
        "core-tree",
        "PASS" if completed.returncode == 0 else "FAIL",
        output[-360:] or f"verification exited {completed.returncode}",
    )


def _validate_workflow_definitions(registry: DefinitionRegistry) -> DoctorCheck:
    missing: list[str] = []
    workflows = load_workflows().get("workflows", {})
    for workflow_name, workflow in workflows.items():
        if not isinstance(workflow, dict):
            missing.append(f"{workflow_name}: invalid workflow")
            continue
        for stage in workflow.get("stages", []):
            try:
                registry.agent(str(stage.get("agent") or ""))
                registry.skill(str(stage.get("skill") or ""))
            except KeyError as exc:
                missing.append(f"{workflow_name}/{stage.get('id')}: {exc}")
    if missing:
        return DoctorCheck("workflows", "FAIL", "; ".join(missing[:5]))
    return DoctorCheck("workflows", "PASS", f"validated {len(workflows)} workflows")


def _existing_parent(path: Path) -> Path | None:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current if current.exists() and current.is_dir() else None
