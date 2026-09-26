from __future__ import annotations

import hashlib
import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .config import LGConfig
from .paths import core_codex_root

if TYPE_CHECKING:
    from .anthropic_bridge import AnthropicBridge


EngineEventCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class CoreCommandCandidate:
    name: str
    command: tuple[str, ...]
    available: bool
    reason: str
    source: str
    version: str | None = None


@dataclass(frozen=True)
class CoreStatus:
    root: Path
    source_available: bool
    runtime_available: bool
    pinned_commit: str | None
    notes: tuple[str, ...]
    candidates: tuple[CoreCommandCandidate, ...]

    @property
    def available(self) -> bool:
        # An installed Codex runtime is sufficient for wheel installations.
        # Vendored source remains independently visible for update verification.
        return self.runtime_available


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
    events: tuple[dict[str, Any], ...] = ()
    attempted_candidates: tuple[str, ...] = ()


class CodexExecAdapter:
    """Stable process adapter for `codex exec`.

    Prompt text is sent through stdin and never appears in argv. JSONL events
    are forwarded as they arrive, while `--output-last-message` supplies the
    authoritative final response.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or core_codex_root()

    def inspect(self) -> CoreStatus:
        return inspect_core(self.root)

    def run(
        self,
        *,
        prompt: str,
        config: LGConfig,
        mode: str,
        model_profile: str | None = None,
        output_schema: Path | None = None,
        on_event: EngineEventCallback | None = None,
    ) -> CoreExecutionResult:
        if not config.credentials_configured:
            return _failure(
                "no API key configured; use literary auth add PROVIDER or a provider-scoped environment variable",
                used_stub=True,
            )

        if config.auth_mode == "chatgpt":
            from .subscription_auth import pinned_command, subscription_status
            try:
                command = pinned_command(config)
            except ValueError as exc:
                return _failure(str(exc), used_stub=True)
            ok, message = subscription_status(config, command=command)
            if not ok:
                return _failure(message, used_stub=True)
        candidates = discover_core_commands(self.root, probe=True, runtime_manifest=config.runtime_manifest)
        available = [item for item in candidates if item.available]
        if not available:
            detail = "; ".join(f"{item.name}: {item.reason}" for item in candidates)
            return _failure(f"no healthy Codex runtime is available ({detail})", used_stub=True)

        from .provider_transport import provider_bridge

        with provider_bridge(config) as bridge:
            return self._run_available(available, prompt, config, mode, model_profile, output_schema, on_event, bridge)

    def _run_available(
        self,
        available: list[CoreCommandCandidate],
        prompt: str,
        config: LGConfig,
        mode: str,
        model_profile: str | None,
        output_schema: Path | None,
        on_event: EngineEventCallback | None,
        bridge: AnthropicBridge | None,
    ) -> CoreExecutionResult:
        attempts: list[str] = []
        for candidate in available:
            attempts.append(candidate.name)
            self._active_process = None
            try:
                result = self._run_candidate(
                    candidate=candidate,
                    prompt=prompt,
                    config=config,
                    mode=mode,
                    model_profile=model_profile,
                    output_schema=output_schema,
                    on_event=on_event,
                    attempts=attempts,
                    bridge=bridge,
                )
            except KeyboardInterrupt:
                if self._active_process is not None:
                    _terminate_process(self._active_process)
                    _wait_quietly(self._active_process)
                raise
            finally:
                self._active_process = None
            if result.returncode is None and result.error and "failed to launch" in result.error:
                continue
            return result

        return _failure(
            f"failed to launch all healthy Codex candidates: {', '.join(attempts)}",
            attempted_candidates=tuple(attempts),
        )

    def _run_candidate(
        self,
        *,
        candidate: CoreCommandCandidate,
        prompt: str,
        config: LGConfig,
        mode: str,
        model_profile: str | None,
        output_schema: Path | None,
        on_event: EngineEventCallback | None,
        attempts: list[str],
        bridge: AnthropicBridge | None = None,
    ) -> CoreExecutionResult:
        temp_dir = config.workspace / ".literarygiant" / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        final_path = temp_dir / f"core-final-{uuid.uuid4().hex}.txt"
        command = [
            *candidate.command,
            *_exec_args(
                config=config,
                mode=mode,
                model_profile=model_profile,
                final_path=final_path,
                output_schema=output_schema,
                provider_args=bridge.codex_args() if bridge else None,
            ),
        ]
        env = _adapter_env(config)
        if bridge:
            env.pop("CODEX_API_KEY", None)
            env["LG_BRIDGE_TOKEN"] = bridge.token
            no_proxy = ",".join(filter(None, [env.get("NO_PROXY", ""), env.get("no_proxy", ""), "127.0.0.1,localhost,::1"]))
            env["NO_PROXY"] = no_proxy
            env["no_proxy"] = no_proxy
        started = time.monotonic()
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        events: list[dict[str, Any]] = []

        try:
            process = subprocess.Popen(
                command,
                cwd=config.workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=os.name != "nt",
            )
            self._active_process = process
        except OSError as exc:
            return _failure(
                f"failed to launch {candidate.name}: {exc}",
                command_name=candidate.name,
                command=command,
                duration=time.monotonic() - started,
                attempted_candidates=tuple(attempts),
            )

        stdout_queue: queue.Queue[str | None] = queue.Queue()

        def read_stdout() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    stdout_queue.put(line)
            finally:
                stdout_queue.put(None)

        def read_stderr() -> None:
            assert process.stderr is not None
            stderr_lines.extend(process.stderr.readlines())

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        assert process.stdin is not None
        try:
            process.stdin.write(prompt)
            if not prompt.endswith("\n"):
                process.stdin.write("\n")
            process.stdin.close()
        except BrokenPipeError:
            pass

        deadline = started + config.timeout_seconds
        stdout_done = False
        timed_out = False
        while not stdout_done:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process(process)
                break
            try:
                line = stdout_queue.get(timeout=min(0.1, remaining))
            except queue.Empty:
                if process.poll() is not None and not stdout_thread.is_alive():
                    break
                continue
            if line is None:
                stdout_done = True
                continue
            stdout_lines.append(line)
            event = _parse_json_event(line)
            if event is not None:
                events.append(event)
                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception:
                        # Rendering must never be able to abort an engine turn.
                        pass

        if timed_out:
            _wait_quietly(process)
        else:
            try:
                process.wait(timeout=max(1.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process(process)
                _wait_quietly(process)

        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        duration = time.monotonic() - started
        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        final_text = _read_final_message(final_path) or _extract_final_text(events, stdout)
        try:
            final_path.unlink(missing_ok=True)
        except OSError:
            pass

        if timed_out:
            return _failure(
                f"Codex process timed out after {config.timeout_seconds}s",
                command_name=candidate.name,
                command=command,
                returncode=None,
                stdout=stdout,
                stderr=stderr,
                duration=duration,
                events=tuple(events),
                attempted_candidates=tuple(attempts),
            )

        if process.returncode != 0:
            detail = _engine_error_summary(events) or _stderr_summary(stderr)
            message = f"Codex process exited with code {process.returncode}"
            if detail:
                message += f": {detail}"
            return _failure(
                message,
                command_name=candidate.name,
                command=command,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration=duration,
                events=tuple(events),
                attempted_candidates=tuple(attempts),
            )

        if not final_text.strip():
            return _failure(
                "Codex process completed without a final response",
                command_name=candidate.name,
                command=command,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration=duration,
                events=tuple(events),
                attempted_candidates=tuple(attempts),
            )

        return CoreExecutionResult(
            ok=True,
            used_stub=False,
            output_text=final_text.strip(),
            error=None,
            command_name=candidate.name,
            command=_redact_command(command),
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            events=tuple(events),
            attempted_candidates=tuple(attempts),
        )


def inspect_core(root: Path | None = None, *, runtime_manifest: Path | None = None) -> CoreStatus:
    root = root or core_codex_root()
    candidates = tuple(discover_core_commands(root, probe=True, runtime_manifest=runtime_manifest))
    source_available = (root / "codex-rs" / "Cargo.toml").exists()
    runtime_available = any(item.available for item in candidates)
    notes: list[str] = []
    if not source_available:
        notes.append("vendored Codex source is missing")
    if not runtime_available:
        notes.append("no healthy Codex executable passed its version probe")
    if source_available and runtime_available:
        notes.append("vendored source and at least one healthy runtime are available")
    return CoreStatus(
        root=root,
        source_available=source_available,
        runtime_available=runtime_available,
        pinned_commit=(
            _read_core_pin(root.parent.parent / "CODEX_CORE_COMMIT") or _bundled_core_pin()
        ),
        notes=tuple(notes),
        candidates=candidates,
    )


def discover_core_commands(root: Path | None = None, *, probe: bool = True, runtime_manifest: Path | None = None) -> list[CoreCommandCandidate]:
    root = root or core_codex_root()
    manifest = os.environ.get("LG_CODEX_RUNTIME_MANIFEST", "").strip() or runtime_manifest
    if manifest:
        return [_locked_runtime(Path(manifest).expanduser(), root, probe=probe)]
    raw: list[tuple[str, list[str], str, bool]] = []

    env_command = os.environ.get("LG_CODEX_COMMAND", "").strip()
    if env_command:
        raw.append(("env:LG_CODEX_COMMAND", shlex.split(env_command), "environment", True))

    for name, path in (
        ("vendored release binary", root / "codex-rs" / "target" / "release" / "codex"),
        ("vendored debug binary", root / "codex-rs" / "target" / "debug" / "codex"),
    ):
        raw.append((name, [str(path)], "vendored-binary", True))

    node = shutil.which("node")
    node_entry = root / "codex-cli" / "bin" / "codex.js"
    raw.append(
        (
            "vendored node wrapper",
            [node or "node", str(node_entry)],
            "vendored-wrapper",
            True,
        )
    )

    installed = shutil.which("codex")
    if installed:
        raw.append(("installed codex", [installed], "system", True))

    cargo = shutil.which("cargo")
    manifest = root / "codex-rs" / "Cargo.toml"
    raw.append(
        (
            "vendored cargo build-on-demand",
            [
                cargo or "cargo",
                "run",
                "--quiet",
                "--manifest-path",
                str(manifest),
                "-p",
                "codex-cli",
                "--bin",
                "codex",
                "--",
            ],
            "vendored-source",
            False,
        )
    )

    candidates: list[CoreCommandCandidate] = []
    seen: set[tuple[str, ...]] = set()
    for name, command, source, should_probe in raw:
        key = tuple(command)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            _evaluate_candidate(
                name=name,
                command=command,
                source=source,
                root=root,
                probe=probe and should_probe,
            )
        )
    return candidates


def _locked_runtime(path: Path, root: Path, *, probe: bool) -> CoreCommandCandidate:
    name = "pinned Codex runtime"
    command: tuple[str, ...] = ()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("unsupported runtime manifest")
        pin = _read_core_pin(root.parent.parent / "CODEX_CORE_COMMIT") or _bundled_core_pin()
        if not pin or manifest.get("commit") != pin:
            raise ValueError("runtime commit does not match CODEX_CORE_COMMIT")
        binary = Path(manifest["binary"])
        if not binary.is_absolute():
            raise ValueError("runtime binary path must be absolute")
        command = (str(binary),)
        checksum = hashlib.sha256()
        with binary.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                checksum.update(chunk)
        if checksum.hexdigest() != manifest.get("binary_sha256"):
            raise ValueError("runtime binary checksum mismatch")
        expected = manifest.get("version")
        tag = manifest.get("tag")
        if not isinstance(tag, str) or not tag.startswith("rust-v") or expected != f"codex-cli {tag[6:]}":
            raise ValueError("runtime tag and version mismatch")
        candidate = _evaluate_candidate(
            name=name, command=list(command), source="pinned-runtime", root=root, probe=probe
        )
        if candidate.available and probe and candidate.version != expected:
            raise ValueError("runtime version probe does not match manifest")
        return candidate
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return CoreCommandCandidate(name, command, False, f"runtime verification failed: {exc}", "pinned-runtime")


def run_model_turn(
    *,
    prompt: str,
    config: LGConfig,
    mode: str = "outline",
    model_profile: str | None = None,
    timeout_seconds: int | None = None,
    output_schema: Path | None = None,
    on_event: EngineEventCallback | None = None,
) -> CoreExecutionResult:
    if timeout_seconds is not None and timeout_seconds != config.timeout_seconds:
        # Preserve the old function parameter without mutating frozen config.
        from dataclasses import replace

        config = replace(config, timeout_seconds=timeout_seconds)
    return CodexExecAdapter().run(
        prompt=prompt,
        config=config,
        mode=mode,
        model_profile=model_profile,
        output_schema=output_schema,
        on_event=on_event,
    )


def run_model_turn_stub(*, mode: str, prompt: str, has_api_key: bool) -> str:
    del prompt
    if not has_api_key:
        return (
            f"{mode} model turn not executed: no API key configured. "
            "Use --dry-run for a planned workflow or configure an LG API key."
        )
    return f"{mode} model turn was not executed by this compatibility stub."


def _evaluate_candidate(
    *,
    name: str,
    command: list[str],
    source: str,
    root: Path,
    probe: bool,
) -> CoreCommandCandidate:
    if not command or not _command_exists(command[0]):
        return CoreCommandCandidate(name, tuple(command), False, "executable missing", source)
    if source == "vendored-wrapper" and not (root / "codex-cli" / "bin" / "codex.js").exists():
        return CoreCommandCandidate(name, tuple(command), False, "wrapper source missing", source)
    if source == "vendored-source":
        manifest = root / "codex-rs" / "Cargo.toml"
        opted_in = os.environ.get("LG_CODEX_BUILD_ON_DEMAND", "").strip() == "1"
        available = opted_in and manifest.exists() and _command_exists(command[0])
        if available:
            reason = "explicit build-on-demand fallback enabled"
        elif not opted_in:
            reason = "disabled; set LG_CODEX_BUILD_ON_DEMAND=1 to opt in"
        else:
            reason = "cargo or manifest missing"
        return CoreCommandCandidate(name, tuple(command), available, reason, source)
    if not probe:
        return CoreCommandCandidate(name, tuple(command), True, "probe skipped", source)

    try:
        completed = subprocess.run(
            [*command, "--version"],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CoreCommandCandidate(name, tuple(command), False, f"probe failed: {exc}", source)
    version = (completed.stdout or completed.stderr).strip().splitlines()
    version_text = version[-1] if version else None
    if completed.returncode != 0:
        return CoreCommandCandidate(
            name,
            tuple(command),
            False,
            f"version probe exited {completed.returncode}: {_stderr_summary(completed.stderr)}",
            source,
            version_text,
        )
    return CoreCommandCandidate(name, tuple(command), True, "version probe passed", source, version_text)


def _exec_args(
    *,
    config: LGConfig,
    mode: str,
    model_profile: str | None,
    final_path: Path,
    output_schema: Path | None,
    provider_args: list[str] | None = None,
) -> list[str]:
    args = [
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox",
        "read-only" if mode != "code" else "workspace-write",
        "--color",
        "never",
        "--json",
        "--output-last-message",
        str(final_path),
        "-c",
        'approval_policy="never"',
    ]
    model = config.model_for_profile(model_profile or "", mode=mode)
    if model:
        args.extend(["--model", model])
    if provider_args:
        args.extend(provider_args)
    elif config.auth_mode == "chatgpt":
        from .subscription_auth import subscription_args
        args.extend(subscription_args())
    elif config.provider and config.provider != "openai":
        args.extend(["-c", f'model_provider={json.dumps(config.provider)}'])
    # LG already schedules these stages; an unattended exec must not start a
    # second collaboration workflow with unconfigured models or interactive input.
    stage_settings = {
        "project_doc_max_bytes": 0,
        "skills.bundled.enabled": False,
        "include_apps_instructions": False,
        "include_collaboration_mode_instructions": False,
        "model_instructions_file": str(resources.files("lg_cli.resources").joinpath("workflow-stage.txt")),
        "features.multi_agent": False,
        "features.multi_agent_v2": False,
        "features.collaboration_modes": False,
        "features.default_mode_request_user_input": False,
    }
    for key, value in stage_settings.items():
        args.extend(["-c", f"{key}={json.dumps(value)}"])
    if output_schema is not None:
        args.extend(["--output-schema", str(output_schema)])
    args.extend(["--cd", str(config.workspace), "-"])
    return args


def _adapter_env(config: LGConfig) -> dict[str, str]:
    env = dict(os.environ)
    from .providers import PROVIDERS
    secrets = {"LITERARYGIANT_API_KEY", "LG_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "LG_BRIDGE_TOKEN", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID"}
    secrets.update(name for preset in PROVIDERS.values() for name in preset.key_envs)
    secrets.update(name for name, value in env.items() if config.api_key and value == config.api_key)
    for name in secrets:
        env.pop(name, None)
    if not config.uses_anthropic and not config.uses_responses and config.auth_mode != "chatgpt":
        env["CODEX_API_KEY"] = config.api_key or ""
    codex_home = config.codex_auth_home if config.auth_mode == "chatgpt" else config.workspace / ".literarygiant" / "codex-home"
    if codex_home is None or codex_home.is_symlink():
        raise ValueError("A private LG Codex home is required")
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        codex_home.chmod(0o700)
    except OSError:
        pass
    env["CODEX_HOME"] = str(codex_home)
    env["LITERARYGIANT_CORE_ADAPTER"] = "1"
    return env


def _parse_json_event(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _extract_final_text(events: list[dict[str, Any]], stdout: str) -> str:
    messages: list[str] = []
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            messages.append(text.strip())
    if messages:
        return messages[-1]
    plain = [line for line in stdout.splitlines() if _parse_json_event(line) is None]
    return "\n".join(plain).strip()


def _read_final_message(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _terminate_process(process: subprocess.Popen[str]) -> None:
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass


def _wait_quietly(process: subprocess.Popen[str]) -> None:
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        process.wait()


def _failure(
    error: str,
    *,
    used_stub: bool = False,
    command_name: str | None = None,
    command: list[str] | None = None,
    returncode: int | None = None,
    stdout: str = "",
    stderr: str = "",
    duration: float | None = None,
    events: tuple[dict[str, Any], ...] = (),
    attempted_candidates: tuple[str, ...] = (),
) -> CoreExecutionResult:
    return CoreExecutionResult(
        ok=False,
        used_stub=used_stub,
        output_text="",
        error=error,
        command_name=command_name,
        command=_redact_command(command or []),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
        events=events,
        attempted_candidates=attempted_candidates,
    )


def _engine_error_summary(events: list[dict]) -> str:
    for event in reversed(events):
        error = event.get("error") if event.get("type") == "turn.failed" else event
        if event.get("type") in {"error", "turn.failed"} and isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return " ".join(message.split())[:280]
    return ""


def _stderr_summary(stderr: str, limit: int = 280) -> str:
    text = " ".join(line.strip() for line in stderr.splitlines() if line.strip())
    return text[:limit] + ("..." if len(text) > limit else "")


def _command_exists(command: str) -> bool:
    path = Path(command)
    return (path.exists() and path.is_file()) or shutil.which(command) is not None


def _redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for item in command:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(item)
        if item in {"--api-key", "api_key", "OPENAI_API_KEY", "CODEX_API_KEY"}:
            skip_next = True
    return redacted


def _read_core_pin(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def _bundled_core_pin() -> str | None:
    try:
        text = resources.files("lg_cli.resources").joinpath("CODEX_CORE_COMMIT.txt").read_text(
            encoding="ascii"
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None
    return text.strip() or None
