from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .credentials import environment_dir, read_profiles

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


DEFAULT_CONFIG_TEXT = """[model]
provider = "deepseek-anthropic"
base_url = "https://api.deepseek.com/anthropic"
max_output_tokens = 8192
default = "deepseek-flash"
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
    api_key: str | None = field(repr=False)
    api_key_source: str
    loaded_files: tuple[Path, ...]
    warnings: tuple[str, ...]
    base_url: str = ""
    max_output_tokens: int = 8192
    runtime_manifest: Path | None = None

    @property
    def uses_anthropic(self) -> bool:
        return self.provider in {"anthropic", "deepseek-anthropic"}

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


def load_config(workspace: Path | None = None, *, environment: str | None = None) -> LGConfig:
    root = (workspace or Path.cwd()).resolve()
    data: dict[str, Any] = {}
    loaded: list[Path] = []
    warnings: list[str] = []

    # The requested order is environment > user config > project config.
    user_path = environment_dir(environment) / "config.toml" if environment else user_config_path()
    for path in (project_config_path(root), user_path):
        if path.exists():
            _merge(data, _read_toml(path))
            loaded.append(path)

    model = _section(data, "model")
    paths = _section(data, "paths")
    knowledge = _section(data, "knowledge")
    agent = _section(data, "agent")
    runtime = _section(data, "runtime")

    # An explicitly selected space must not inherit production model credentials.
    env_string = (lambda *names: "") if environment else _env_string
    saved = read_profiles(environment or "sandbox")
    active = saved.get("active")
    profile = saved["profiles"].get(active) if active else None
    if profile:
        model = {"provider": profile["provider"], "default": "deepseek-flash",
                 "base_url": "https://api.deepseek.com/anthropic"}
        env_string = lambda *names: ""

    provider = (env_string("LITERARYGIANT_PROVIDER", "LG_PROVIDER")
                or _string(model.get("provider")) or "deepseek-anthropic")
    if provider in {"anthropic", "deepseek-anthropic"} and model.get("provider") and provider != model["provider"]:
        # Models, endpoint, and compatibility credentials belong to a provider.
        model = {"provider": provider}
        warnings.append("provider override: previous provider's models, endpoint, and config key ignored")
    api_key, api_source = (None, "not configured") if environment or profile else _api_key_from_env(provider)
    if profile:
        api_key, api_source = profile["key"], f"profile:{environment or 'sandbox'}/{active}"
    if not api_key and not environment:
        configured_key = _string(model.get("api_key"))
        if configured_key:
            api_key = configured_key
            api_source = "config:model.api_key"
            warnings.append(
                "model.api_key is supported for compatibility; environment variables are safer for secrets."
            )

    default_model = env_string("LITERARYGIANT_MODEL", "LG_MODEL") or _string(model.get("default"))
    base_url = env_string("LG_BASE_URL") or _string(model.get("base_url"))
    if provider in {"anthropic", "deepseek-anthropic"}:
        base_url = base_url or env_string("ANTHROPIC_BASE_URL") or (
            "https://api.deepseek.com/anthropic" if provider == "deepseek-anthropic"
            else "https://api.anthropic.com"
        )
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ConfigError("Anthropic base_url must be an HTTPS URL without credentials, query, or fragment")
        if provider == "deepseek-anthropic" and not default_model:
            default_model = "deepseek-flash"
        if not default_model:
            raise ConfigError("Anthropic requires an explicit model.default or LG_MODEL")
    writer_model = _string(model.get("writer"))
    coder_model = _string(model.get("coder"))
    critic_model = _string(model.get("critic"))
    library_raw = _string(knowledge.get("library"))

    return LGConfig(
        workspace=root,
        provider=provider,
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
        base_url=base_url.rstrip("/"),
        max_output_tokens=_bounded_int(model.get("max_output_tokens"), 8192, 1, 65536),
        runtime_manifest=_workspace_path(root, runtime["manifest"]) if _string(runtime.get("manifest")) else None,
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


def _api_key_from_env(provider: str = "openai") -> tuple[str | None, str]:
    names = ["LITERARYGIANT_API_KEY", "LG_API_KEY"]
    if provider == "deepseek-anthropic":
        names.extend(["DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"])
    elif provider == "anthropic":
        names.append("ANTHROPIC_API_KEY")
    else:
        names.extend(["CODEX_API_KEY", "OPENAI_API_KEY"])
    for name in names:
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
