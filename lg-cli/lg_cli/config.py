from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


DEFAULT_CONFIG_TEXT = """[model]
provider = "openai"
# Leave model names empty to use the current Codex default.
default = ""
writer = ""
coder = ""
critic = ""

[paths]
memory = ".literarygiant/memory"
output = ".literarygiant/output"
reference = "ReferenceLibrary"

[knowledge]
# Set library to an explicit Library directory when automatic discovery is not suitable.
library = ""
top_k = 6
allow_raw_reference = false

[agent]
default_mode = "chat"
execution_strategy = "staged"
enable_shell = false
enable_reference = true
timeout_seconds = 300
max_stage_context_chars = 24000
"""


class ConfigError(ValueError):
    """Raised when LiteraryGiant configuration is invalid."""


@dataclass(frozen=True)
class LGConfig:
    workspace: Path
    provider: str
    default_model: str
    writer_model: str
    coder_model: str
    critic_model: str
    memory_path: Path
    output_path: Path
    reference_path: Path
    library_path: Path | None
    knowledge_top_k: int
    allow_raw_reference: bool
    default_mode: str
    execution_strategy: str
    enable_shell: bool
    enable_reference: bool
    timeout_seconds: int
    max_stage_context_chars: int
    api_key: str | None
    api_key_source: str
    loaded_files: tuple[Path, ...]
    warnings: tuple[str, ...]

    @property
    def model_label(self) -> str:
        return self.default_model or "auto (core default)"

    def model_for_mode(self, mode: str) -> str:
        if mode == "code":
            return self.coder_model or self.default_model
        if mode in {"check", "critic", "ref"}:
            return self.critic_model or self.writer_model or self.default_model
        return self.writer_model or self.default_model

    def model_for_profile(self, profile: str, *, mode: str = "") -> str:
        normalized = profile.strip().lower()
        if normalized in {"coder", "code"}:
            return self.coder_model or self.default_model
        if normalized in {"critic", "reviewer", "reference"}:
            return self.critic_model or self.writer_model or self.default_model
        if normalized in {"writer", "creative", "director"}:
            return self.writer_model or self.default_model
        return self.model_for_mode(mode)


def project_config_path(workspace: Path) -> Path:
    return workspace / ".literarygiant" / "config.toml"


def user_config_path() -> Path:
    return Path.home() / ".literarygiant" / "config.toml"


def load_config(workspace: Path | None = None) -> LGConfig:
    root = (workspace or Path.cwd()).resolve()
    data: dict[str, Any] = {}
    loaded: list[Path] = []
    warnings: list[str] = []

    # The requested order is environment > user config > project config.
    for path in (project_config_path(root), user_config_path()):
        if path.exists():
            _merge(data, _read_toml(path))
            loaded.append(path)

    model = _section(data, "model")
    paths = _section(data, "paths")
    knowledge = _section(data, "knowledge")
    agent = _section(data, "agent")

    api_key, api_source = _api_key_from_env()
    if not api_key:
        configured_key = _string(model.get("api_key"))
        if configured_key:
            api_key = configured_key
            api_source = "config:model.api_key"
            warnings.append(
                "model.api_key is supported for compatibility; environment variables are safer for secrets."
            )

    default_model = _env_string("LITERARYGIANT_MODEL", "LG_MODEL") or _string(model.get("default"))
    writer_model = _string(model.get("writer"))
    coder_model = _string(model.get("coder"))
    critic_model = _string(model.get("critic"))
    library_raw = _string(knowledge.get("library"))

    return LGConfig(
        workspace=root,
        provider=_env_string("LITERARYGIANT_PROVIDER", "LG_PROVIDER")
        or _string(model.get("provider"))
        or "openai",
        default_model=default_model,
        writer_model=writer_model,
        coder_model=coder_model,
        critic_model=critic_model,
        memory_path=_workspace_path(root, _string(paths.get("memory")) or ".literarygiant/memory"),
        output_path=_workspace_path(root, _string(paths.get("output")) or ".literarygiant/output"),
        reference_path=_workspace_path(root, _string(paths.get("reference")) or "ReferenceLibrary"),
        library_path=_workspace_path(root, library_raw) if library_raw else None,
        knowledge_top_k=_bounded_int(knowledge.get("top_k"), default=6, minimum=1, maximum=30),
        allow_raw_reference=_boolean(knowledge.get("allow_raw_reference"), False),
        default_mode=_string(agent.get("default_mode")) or "chat",
        execution_strategy=_choice(
            agent.get("execution_strategy"), {"single-pass", "staged"}, "staged"
        ),
        enable_shell=_boolean(agent.get("enable_shell"), False),
        enable_reference=_boolean(agent.get("enable_reference"), True),
        timeout_seconds=_bounded_int(agent.get("timeout_seconds"), 300, 10, 3600),
        max_stage_context_chars=_bounded_int(
            agent.get("max_stage_context_chars"), 24000, 4000, 200000
        ),
        api_key=api_key,
        api_key_source=api_source,
        loaded_files=tuple(loaded),
        warnings=tuple(warnings),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"config root must be a table: {path}")
    return value


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        elif isinstance(value, dict):
            target[key] = dict(value)
        else:
            target[key] = value


def _workspace_path(root: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _api_key_from_env() -> tuple[str | None, str]:
    for name in ("LITERARYGIANT_API_KEY", "LG_API_KEY", "CODEX_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value, f"env:{name}"
    return None, "not configured"


def _env_string(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _boolean(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"expected boolean, got {value!r}")
    return value


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"expected integer, got {value!r}")
    if not minimum <= value <= maximum:
        raise ConfigError(f"integer {value} must be between {minimum} and {maximum}")
    return value


def _choice(value: Any, choices: set[str], default: str) -> str:
    text = _string(value) or default
    if text not in choices:
        raise ConfigError(f"expected one of {sorted(choices)}, got {text!r}")
    return text
