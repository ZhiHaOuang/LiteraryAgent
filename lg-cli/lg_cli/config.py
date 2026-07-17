from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_TEXT = """[model]
provider = "openai"
default = "gpt-5.5-thinking"
writer = "gpt-5.5-thinking"
coder = "gpt-5.5-codex"
critic = "gpt-5-mini"

[paths]
memory = ".literarygiant/memory"
output = ".literarygiant/output"
reference = "ReferenceLibrary"

[agent]
default_mode = "chat"
enable_shell = false
enable_reference = true
"""


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
    default_mode: str
    enable_shell: bool
    enable_reference: bool
    api_key: str | None
    api_key_source: str
    loaded_files: list[Path]

    @property
    def model_label(self) -> str:
        return self.default_model or "not configured"


def project_config_path(workspace: Path) -> Path:
    return workspace / ".literarygiant" / "config.toml"


def user_config_path() -> Path:
    return Path.home() / ".literarygiant" / "config.toml"


def load_config(workspace: Path | None = None) -> LGConfig:
    root = (workspace or Path.cwd()).resolve()
    data: dict[str, dict[str, Any]] = {}
    loaded: list[Path] = []

    # Lower priority first, then higher priority. The requested effective
    # priority is env > user config > project config.
    for path in [project_config_path(root), user_config_path()]:
        if path.exists():
            _merge(data, _parse_toml_like(path))
            loaded.append(path)

    api_key, api_source = _api_key_from_env()
    if not api_key:
        api_key = str(data.get("model", {}).get("api_key") or "").strip() or None
        api_source = "config:model.api_key" if api_key else "not configured"

    provider = str(data.get("model", {}).get("provider") or "openai")
    default_model = str(data.get("model", {}).get("default") or "gpt-5.5-thinking")
    writer_model = str(data.get("model", {}).get("writer") or default_model)
    coder_model = str(data.get("model", {}).get("coder") or "gpt-5.5-codex")
    critic_model = str(data.get("model", {}).get("critic") or "gpt-5-mini")
    paths = data.get("paths", {})
    agent = data.get("agent", {})
    return LGConfig(
        workspace=root,
        provider=provider,
        default_model=default_model,
        writer_model=writer_model,
        coder_model=coder_model,
        critic_model=critic_model,
        memory_path=_workspace_path(root, str(paths.get("memory") or ".literarygiant/memory")),
        output_path=_workspace_path(root, str(paths.get("output") or ".literarygiant/output")),
        reference_path=_workspace_path(root, str(paths.get("reference") or "ReferenceLibrary")),
        default_mode=str(agent.get("default_mode") or "chat"),
        enable_shell=bool(agent.get("enable_shell", False)),
        enable_reference=bool(agent.get("enable_reference", True)),
        api_key=api_key,
        api_key_source=api_source,
        loaded_files=loaded,
    )


def _workspace_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _api_key_from_env() -> tuple[str | None, str]:
    for name in ("LITERARYGIANT_API_KEY", "LG_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value, f"env:{name}"
    return None, "not configured"


def _merge(target: dict[str, dict[str, Any]], source: dict[str, dict[str, Any]]) -> None:
    for section, values in source.items():
        target.setdefault(section, {}).update(values)


def _parse_toml_like(path: Path) -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = {}
    section = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            data.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        data.setdefault(section, {})[key] = _parse_value(value)
    return data


def _parse_value(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value
