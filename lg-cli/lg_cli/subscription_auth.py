"""Explicit, isolated ChatGPT login delegated to the pinned Codex executable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from .config import ConfigError, LGConfig, load_config
from .credentials import checked_name, environment_dir, read_profiles, save_subscription
from .providers import validate_model


def subscription_args() -> list[str]:
    settings = {
        "model_provider": "openai",
        "forced_login_method": "chatgpt",
        "cli_auth_credentials_store": "file",
        "analytics.enabled": False,
        "feedback.enabled": False,
        "check_for_update_on_startup": False,
        "features.plugins": False,
        "features.apps": False,
        "features.recommended_plugins": False,
    }
    return [
        arg
        for key, value in settings.items()
        for arg in ("-c", f"{key}={json.dumps(value)}")
    ]


def pinned_command(config: LGConfig) -> tuple[str, ...]:
    from .core_adapter import discover_core_commands

    if not config.runtime_manifest and not os.environ.get("LG_CODEX_RUNTIME_MANIFEST"):
        raise ConfigError(
            "Subscription login requires a pinned runtime.manifest in the LG environment config"
        )
    candidates = discover_core_commands(runtime_manifest=config.runtime_manifest)
    if not candidates or not candidates[0].available:
        raise ConfigError(
            "Pinned Codex runtime verification failed; run literary doctor"
        )
    return candidates[0].command


def subscription_status(
    config: LGConfig, *, command: tuple[str, ...] | None = None
) -> tuple[bool, str]:
    from .core_adapter import _adapter_env

    if not config.credentials_configured:
        return False, "No LG subscription login; run literary auth login gpt"
    try:
        result = subprocess.run(
            [
                *(command or pinned_command(config)),
                "login",
                "status",
                *subscription_args(),
            ],
            env=_adapter_env(config),
            cwd=config.codex_auth_home,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ConfigError):
        return (
            False,
            "Cannot verify the isolated Codex subscription login; run literary doctor",
        )
    ok = result.returncode == 0 and "Logged in using ChatGPT" in result.stderr
    return (
        ok,
        "ChatGPT login cached in LG (account access not probed)"
        if ok
        else "LG subscription login is unavailable; run literary auth login gpt",
    )


def login_subscription(
    environment: str,
    name: str,
    workspace: Path,
    *,
    model: str = "",
    browser: bool = False,
) -> int:
    from .core_adapter import _adapter_env

    checked_name(name)
    previous = read_profiles(environment)["profiles"].get(name)
    if previous and previous.get("auth_mode") != "chatgpt":
        raise ConfigError(
            "Name already belongs to an API profile; choose another subscription name"
        )
    model = model or (previous.get("model", "") if previous else "")
    if model:
        validate_model(model)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ConfigError("Subscription login requires your interactive terminal")
    config = load_config(workspace, environment=environment)
    command = pinned_command(config)
    directory = environment_dir(environment) / "codex-auth" / name
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ConfigError("Subscription auth directory must not be a symlink")
    directory.chmod(0o700)
    config = replace(
        config,
        auth_mode="chatgpt",
        provider="codex",
        protocol="codex",
        api_key=None,
        codex_auth_home=directory,
    )
    args = [*command, "login", *subscription_args()]
    if not browser:
        args.append("--device-auth")
    print(
        "Opening official Codex ChatGPT login. This uses subscription access, not an API key."
    )
    process = subprocess.Popen(args, env=_adapter_env(config), cwd=directory)
    try:
        code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return 130
    if code != 0:
        return code if code > 0 else 128 - code
    cache = directory / "auth.json"
    if cache.is_file() and not cache.is_symlink():
        cache.chmod(0o600)
    ok, message = subscription_status(config, command=command)
    if not ok:
        raise ConfigError(message)
    save_subscription(environment, name, model)
    print(f"Subscription profile activated: {environment}/{name}")
    return 0
