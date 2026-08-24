from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from .paths import data_file


RESOURCE_NAMES = {
    "lg-agent/task_modes.json": "task_modes.json",
    "lg-agent/workflows.json": "workflows.json",
    "lg-skills/skills.json": "skills.json",
    "lg-subagents/subagents.json": "subagents.json",
    "lg-tools/tool_policy.json": "tool_policy.json",
    "lg-context/context_spec.json": "context_spec.json",
}


def load_json_catalog(relative_path: str) -> Any:
    checkout_path = data_file(*relative_path.split("/"))
    if checkout_path.exists():
        return json.loads(checkout_path.read_text(encoding="utf-8"))

    resource_name = RESOURCE_NAMES.get(relative_path)
    if resource_name is None:
        raise FileNotFoundError(f"unknown LiteraryGiant catalog: {relative_path}")
    bundled = resources.files("lg_cli.resources").joinpath(resource_name)
    return json.loads(bundled.read_text(encoding="utf-8"))


def load_skills() -> list[dict[str, Any]]:
    payload = load_json_catalog("lg-skills/skills.json")
    return list(payload.get("skills", []))


def load_subagents() -> list[dict[str, Any]]:
    payload = load_json_catalog("lg-subagents/subagents.json")
    return list(payload.get("subagents", []))


def load_task_modes() -> list[dict[str, Any]]:
    payload = load_json_catalog("lg-agent/task_modes.json")
    return list(payload.get("modes", []))


def load_workflows() -> dict[str, Any]:
    return dict(load_json_catalog("lg-agent/workflows.json"))


def find_mode(name: str) -> dict[str, Any] | None:
    lower = name.lower()
    for mode in load_task_modes():
        if str(mode.get("name", "")).lower() == lower or str(mode.get("command", "")).lower() == lower:
            return mode
    return None


def catalog_file(relative_path: str) -> Path:
    path = data_file(*relative_path.split("/"))
    if not path.exists():
        raise FileNotFoundError(
            f"{relative_path} is not a filesystem resource in this installation; use load_json_catalog"
        )
    return path
