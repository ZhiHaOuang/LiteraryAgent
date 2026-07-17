from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import data_file


def load_json_catalog(relative_path: str) -> Any:
    path = data_file(*relative_path.split("/"))
    return json.loads(path.read_text(encoding="utf-8"))


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
    return data_file(*relative_path.split("/"))
