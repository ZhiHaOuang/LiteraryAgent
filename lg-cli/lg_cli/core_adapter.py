from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import LGConfig
from .paths import core_codex_root


@dataclass(frozen=True)
class CoreCommandCandidate:
    name: str
    command: list[str]
    available: bool
    reason: str


@dataclass(frozen=True)
class CoreStatus:
    root: Path
    available: bool
    cli_entry: Path
    exec_entry: Path
    notes: list[str]
    candidates: list[CoreCommandCandidate]


@dataclass(frozen=True)
class CoreExecutionResult:
    ok: bool
    used_stub: bool
    output_text: str
    error: str | None
    command_name: str | None
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float | None


def inspect_core() -> CoreStatus:
    root = core_codex_root()
    cli_entry = root / "codex-rs" / "cli" / "src" / "main.rs"
    exec_entry = root / "codex-rs" / "exec" / "src" / "cli.rs"
    candidates = discover_core_commands(root)
    notes: list[str] = []
    if not root.exists():
        notes.append("core/codex is missing")
    if not cli_entry.exists():
        notes.append("Codex CLI source not found")
    if not exec_entry.exists():
        notes.append("Codex exec source not found")
    if not notes:
        notes.append("Codex core present; LG adapter can delegate through a process command when credentials exist")
    if not any(candidate.available for candidate in candidates):
        notes.append("No executable Codex core command is currently available")
    return CoreStatus(
        root=root,
        available=root.exists() and cli_entry.exists(),
        cli_entry=cli_entry,
        exec_entry=exec_entry,
        notes=notes,
        candidates=candidates,
    )


def run_model_turn_stub(*, mode: str, prompt: str, has_api_key: bool) -> str:
    if not has_api_key:
        return (
            "Model adapter not executed: no API key configured. "
            "Set LITERARYGIANT_API_KEY, LG_API_KEY, OPENAI_API_KEY, or .literarygiant/config.toml."
        )
    return (
        "Model adapter stub: LG has selected the task mode and workflow, "
        "but the real Codex model loop is not wired yet."
    )


def run_model_turn(
    *,
    prompt: str,
    config: LGConfig,
    mode: str = "outline",
    timeout_seconds: int = 180,
) -> CoreExecutionResult:
    if not config.api_key:
        return _stub_execution(
            error=(
                "no API key configured; set LITERARYGIANT_API_KEY, LG_API_KEY, "
                "OPENAI_API_KEY, or model.api_key in .literarygiant/config.toml"
            ),
            mode=mode,
        )

    candidates = discover_core_commands(core_codex_root(), prompt=prompt, config=config)
    selected = next((candidate for candidate in candidates if candidate.available), None)
    if selected is None:
        unavailable = "; ".join(f"{item.name}: {item.reason}" for item in candidates) or "no candidates"
        return _stub_execution(error=f"no executable Codex command available ({unavailable})", mode=mode)

    env = _adapter_env(config)
    start = _monotonic()
    try:
        completed = subprocess.run(
            selected.command,
            cwd=config.workspace,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = _monotonic() - start
        return CoreExecutionResult(
            ok=False,
            used_stub=True,
            output_text="",
            error=f"Codex process timed out after {timeout_seconds}s",
            command_name=selected.name,
            command=_redact_command(selected.command),
            returncode=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            duration_seconds=duration,
        )
    except OSError as exc:
        duration = _monotonic() - start
        return CoreExecutionResult(
            ok=False,
            used_stub=True,
            output_text="",
            error=f"failed to launch Codex process: {exc}",
            command_name=selected.name,
            command=_redact_command(selected.command),
            returncode=None,
            stdout="",
            stderr="",
            duration_seconds=duration,
        )

    duration = _monotonic() - start
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    output = _extract_final_text(stdout) or stdout.strip()
    if completed.returncode != 0:
        return CoreExecutionResult(
            ok=False,
            used_stub=True,
            output_text=output,
            error=f"Codex process exited with code {completed.returncode}",
            command_name=selected.name,
            command=_redact_command(selected.command),
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )

    if not output:
        return CoreExecutionResult(
            ok=False,
            used_stub=True,
            output_text="",
            error="Codex process returned no stdout",
            command_name=selected.name,
            command=_redact_command(selected.command),
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )

    return CoreExecutionResult(
        ok=True,
        used_stub=False,
        output_text=output,
        error=None,
        command_name=selected.name,
        command=_redact_command(selected.command),
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
    )


def discover_core_commands(
    root: Path | None = None,
    *,
    prompt: str = "",
    config: LGConfig | None = None,
) -> list[CoreCommandCandidate]:
    root = root or core_codex_root()
    commands: list[CoreCommandCandidate] = []

    env_command = os.environ.get("LG_CODEX_COMMAND", "").strip()
    if env_command:
        parsed = shlex.split(env_command)
        commands.append(
            CoreCommandCandidate(
                name="env:LG_CODEX_COMMAND",
                command=parsed + ([prompt] if prompt else []),
                available=bool(parsed) and _command_exists(parsed[0]),
                reason="configured by LG_CODEX_COMMAND" if parsed else "LG_CODEX_COMMAND is empty",
            )
        )

    cargo = shutil.which("cargo")
    cargo_manifest = root / "codex-rs" / "Cargo.toml"
    cargo_command = [
        cargo or "cargo",
        "run",
        "--quiet",
        "--manifest-path",
        str(cargo_manifest),
        "-p",
        "codex-cli",
        "--bin",
        "literary-agent",
        "--",
        *(_exec_args(prompt=prompt, config=config) if prompt and config else []),
    ]
    commands.append(
        CoreCommandCandidate(
            name="core/codex cargo literary-agent",
            command=cargo_command,
            available=bool(cargo) and cargo_manifest.exists(),
            reason="cargo and core manifest found" if cargo and cargo_manifest.exists() else _missing_reason(cargo, cargo_manifest),
        )
    )

    node = shutil.which("node")
    node_entry = root / "codex-cli" / "bin" / "codex.js"
    node_command = [
        node or "node",
        str(node_entry),
        *(_exec_args(prompt=prompt, config=config) if prompt and config else []),
    ]
    commands.append(
        CoreCommandCandidate(
            name="core/codex node wrapper",
            command=node_command,
            available=bool(node) and node_entry.exists(),
            reason="node and core wrapper found" if node and node_entry.exists() else _missing_reason(node, node_entry),
        )
    )

    installed = shutil.which("codex")
    if installed:
        commands.append(
            CoreCommandCandidate(
                name="installed codex fallback",
                command=[installed, *(_exec_args(prompt=prompt, config=config) if prompt and config else [])],
                available=True,
                reason="system codex command found; used only after core candidates",
            )
        )

    return commands


def _exec_args(*, prompt: str, config: LGConfig) -> list[str]:
    model = config.writer_model or config.default_model
    return [
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "-c",
        "approval_policy=\"never\"",
        "-m",
        model,
        "-C",
        str(config.workspace),
        prompt,
    ]


def _adapter_env(config: LGConfig) -> dict[str, str]:
    env = dict(os.environ)
    env["OPENAI_API_KEY"] = config.api_key or ""
    env["CODEX_HOME"] = str(config.workspace / ".literarygiant" / "codex-home")
    env["LITERARYGIANT_CORE_ADAPTER"] = "1"
    Path(env["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
    return env


def _missing_reason(executable: str | None, path: Path) -> str:
    pieces: list[str] = []
    if not executable:
        pieces.append("executable missing")
    if not path.exists():
        pieces.append(f"path missing: {path}")
    return "; ".join(pieces) or "available"


def _command_exists(command: str) -> bool:
    if Path(command).exists():
        return True
    return shutil.which(command) is not None


def _stub_execution(*, error: str, mode: str) -> CoreExecutionResult:
    return CoreExecutionResult(
        ok=False,
        used_stub=True,
        output_text=run_model_turn_stub(mode=mode, prompt="", has_api_key=False),
        error=error,
        command_name=None,
        command=[],
        returncode=None,
        stdout="",
        stderr="",
        duration_seconds=None,
    )


def _extract_final_text(stdout: str) -> str:
    # Plain exec output is already suitable. If a future command emits JSONL,
    # keep this adapter extensible without making the workflow parse events.
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return ""
    return stdout.strip()


def _redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for item in command:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(item)
        if item in {"--api-key", "api_key", "OPENAI_API_KEY"}:
            skip_next = True
    return redacted


def _monotonic() -> float:
    import time

    return time.monotonic()
