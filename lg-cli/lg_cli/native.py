"""Validated launcher for the separately built LG terminal application."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

from .config import ConfigError, LGConfig
from .core_adapter import _adapter_env, _bundled_core_pin
from .paths import product_root
from .project_store import ProjectStore
from .provider_transport import provider_bridge


def native_binary(manifest_path: Path) -> Path:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        data.get("schema_version") != 1
        or data.get("kind") != "lg-native-tui"
        or data.get("status") != "built"
    ):
        raise ConfigError("LG native runtime has not been successfully built")
    if data.get("commit") != _bundled_core_pin():
        raise ConfigError("LG native runtime does not match the pinned core")
    patches = product_root() / "native-ui" / "patches"
    if patches.is_dir():
        expected = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in patches.glob("*.patch")
        }
        if data.get("patches") != expected:
            raise ConfigError("LG native UI patches changed; rebuild before launching")
    binary = Path(data["binary"])
    if not binary.is_absolute():
        raise ConfigError("LG native binary path must be absolute")
    checksum = hashlib.sha256()
    with binary.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            checksum.update(chunk)
    if checksum.hexdigest() != data.get("binary_sha256"):
        raise ConfigError("LG native binary checksum mismatch")
    return binary


def writing_settings(config: LGConfig, *, control_socket: str | None = None,
                     require_elicitation: bool = False) -> dict:
    instructions = resources.files("lg_cli.resources").joinpath("native-writing.txt")
    settings = {
        "project_doc_max_bytes": 0,
        "skills.bundled.enabled": False,
        "include_apps_instructions": False,
        "include_collaboration_mode_instructions": False,
        "model_instructions_file": str(instructions),
        "mcp_servers.lg_writing.command": sys.executable,
        "mcp_servers.lg_writing.env.PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "mcp_servers.lg_writing.args": [
            "-m",
            "lg_cli.writing_mcp",
            "--workspace",
            str(config.workspace),
        ],
        "mcp_servers.lg_writing.startup_timeout_sec": 30,
        "mcp_servers.lg_writing.cwd": str(config.workspace),
        "mcp_servers.lg_writing.enabled": True,
        "mcp_servers.lg_writing.env_vars": [],
        "mcp_servers.lg_writing.required": True,
        "mcp_servers.lg_writing.enabled_tools": [
            "project_memory",
            "read_document",
            "create_chapter",
            "edit_manuscript",
            "update_book_overview",
            "document_history",
            "reference_search",
            "propose_setting",
            "document_diff",
            "restore_manuscript",
            "book_dashboard", "studio_status", "studio_initialize", "studio_save",
            "studio_draft", "studio_review_context", "studio_review", "studio_accept",
            "book_library", "book_entry", "book_action_context", "studio_reference",
            "studio_adopt_reference", "studio_accept_proposal",
            "studio_decision", "studio_decision_options", "studio_select_decision", "studio_withdraw_decision", "studio_source_impact",
            "studio_knowledge", "studio_knowledge_view", "studio_knowledge_propose",
            "studio_knowledge_confirm", "studio_knowledge_withdraw",
        ],
        "mcp_servers.lg_writing.default_tools_approval_mode": "approve",
        "features.shell_tool": False,
        "features.unified_exec": False,
        "features.apply_patch_freeform": False,
        "analytics.enabled": False,
        "feedback.enabled": False,
        "check_for_update_on_startup": False,
        "features.plugins": False,
        "features.apps": False,
        "features.recommended_plugins": False,
        "features.view_image": False,
        "features.image_generation": False,
        "features.browser_use": False,
        "features.computer_use": False,
        "features.enable_mcp_apps": False,
        "features.skill_mcp_dependency_install": False,
        "features.code_mode": False,
        "features.code_mode_only": False,
        "features.code_mode_host": False,
        "features.code_mode_prewarm": False,
        "features.executor_capability_discovery": False,
        "features.hooks": False,
        "notify": [],
        "web_search": "disabled",
    }
    if config.environment:
        settings["mcp_servers.lg_writing.args"].extend(["--environment", config.environment])
    if require_elicitation:
        settings["mcp_servers.lg_writing.args"].append("--require-elicitation")
    if control_socket:
        settings["mcp_servers.lg_writing.args"].extend(["--control-socket", control_socket])
        settings["mcp_servers.lg_writing.enabled_tools"].extend([
            "workflow_status", "stop_workflow", "retry_workflow",
        ])
    return settings


def writing_args(config: LGConfig, *, control_socket: str | None = None, require_elicitation: bool = False) -> list[str]:
    settings = writing_settings(config, control_socket=control_socket, require_elicitation=require_elicitation)
    return [
        arg
        for key, value in settings.items()
        for arg in ("-c", f"{key}={json.dumps(value)}")
    ]


def launch_native(
    config: LGConfig, *, prompt: str = "", manifest: str | None = None
) -> int:
    config.require_api_auth()
    path = manifest or os.environ.get("LG_NATIVE_RUNTIME_MANIFEST")
    if not path:
        raise ConfigError(
            "Set LG_NATIVE_RUNTIME_MANIFEST to a successfully built native-runtime.json"
        )
    binary = native_binary(Path(path).expanduser())
    if not (config.uses_anthropic or config.uses_responses):
        raise ConfigError(
            "This native launcher requires a configured Messages or Responses provider"
        )
    if not config.credentials_configured:
        raise ConfigError("Configure credentials with literary auth add before starting the writing session")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ConfigError("Native mode requires an interactive terminal")
    if not ProjectStore(config.workspace).initialized:
        raise ConfigError("Initialize the novel project with literary init first")
    with provider_bridge(config) as bridge:
        env = _adapter_env(config)
        env.pop("CODEX_API_KEY", None)
        if bridge:
            env["LG_BRIDGE_TOKEN"] = bridge.token
        env["NO_PROXY"] = env["no_proxy"] = ",".join(
            filter(
                None,
                [
                    env.get("NO_PROXY", ""),
                    env.get("no_proxy", ""),
                    "127.0.0.1,localhost,::1",
                ],
            )
        )
        command = [
            str(binary),
            *(["--model", config.default_model] if config.default_model else []),
            "--cd",
            str(config.workspace),
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "on-request",
            *(bridge.codex_args() if bridge else []),
            *writing_args(config, require_elicitation=True),
        ]
        if prompt:
            command.extend(["--", prompt])
        process = subprocess.Popen(command, cwd=config.workspace, env=env)
        try:
            returncode = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return 130
        return returncode if returncode >= 0 else 128 - returncode
