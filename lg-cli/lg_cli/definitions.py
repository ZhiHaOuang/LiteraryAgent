from __future__ import annotations

import json
from importlib import resources
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from .catalog import load_skills as load_skill_catalog
from .catalog import load_subagents as load_subagent_catalog
from .paths import product_root


@dataclass(frozen=True)
class SkillDefinition:
    id: str
    description: str
    instructions: str
    tools: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    path: Path | None
    source: str


@dataclass(frozen=True)
class SubagentDefinition:
    id: str
    name: str
    role: str
    prompt: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    model_profile: str
    output_schema: Path | None
    path: Path | None
    source: str


class DefinitionRegistry:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._skills = self._load_skills()
        self._agents = self._load_agents()

    @property
    def skills(self) -> tuple[SkillDefinition, ...]:
        return tuple(self._skills.values())

    @property
    def agents(self) -> tuple[SubagentDefinition, ...]:
        return tuple(self._agents.values())

    def skill(self, skill_id: str) -> SkillDefinition:
        normalized = skill_id.replace("_", "-")
        try:
            return self._skills[normalized]
        except KeyError as exc:
            raise KeyError(f"unknown LG skill: {skill_id}") from exc

    def agent(self, agent_id: str) -> SubagentDefinition:
        normalized = _agent_id(agent_id)
        try:
            return self._agents[normalized]
        except KeyError as exc:
            raise KeyError(f"unknown LG subagent: {agent_id}") from exc

    def _load_skills(self) -> dict[str, SkillDefinition]:
        definitions: dict[str, SkillDefinition] = {}
        for item in load_skill_catalog():
            skill_id = str(item.get("name") or "").replace("_", "-")
            definitions[skill_id] = SkillDefinition(
                id=skill_id,
                description=str(item.get("purpose") or item.get("description") or ""),
                instructions=str(item.get("instructions") or ""),
                tools=tuple(str(value) for value in item.get("tools_allowed", [])),
                inputs=tuple(str(value) for value in item.get("inputs", [])),
                outputs=tuple(str(value) for value in item.get("outputs", [])),
                path=None,
                source="bundled-catalog",
            )

        roots = [product_root() / "lg-skills", self.workspace / ".literarygiant" / "skills"]
        for root in roots:
            if not root.exists():
                continue
            for skill_file in sorted(root.glob("*/SKILL.md")):
                definition = _read_skill_directory(skill_file.parent, root == roots[-1])
                definitions[definition.id] = definition
        return definitions

    def _load_agents(self) -> dict[str, SubagentDefinition]:
        definitions: dict[str, SubagentDefinition] = {}
        for item in load_subagent_catalog():
            agent_id = str(item.get("id") or _agent_id(str(item.get("name") or "agent")))
            definitions[agent_id] = SubagentDefinition(
                id=agent_id,
                name=str(item.get("name") or agent_id),
                role=str(item.get("role") or ""),
                prompt=_catalog_agent_prompt(item),
                tools=tuple(str(value) for value in item.get("allowed_tools", [])),
                skills=tuple(str(value).replace("_", "-") for value in item.get("skills", [])),
                model_profile=str(item.get("model_profile") or "writer"),
                output_schema=_bundled_output_schema(),
                path=None,
                source="bundled-catalog",
            )

        roots = [product_root() / "lg-subagents", self.workspace / ".literarygiant" / "subagents"]
        for root in roots:
            if not root.exists():
                continue
            for manifest_path in sorted(root.glob("*/agent.toml")):
                definition = _read_agent_directory(manifest_path.parent, root == roots[-1])
                definitions[definition.id] = definition
        return definitions


def _read_skill_directory(directory: Path, project_local: bool) -> SkillDefinition:
    skill_text = (directory / "SKILL.md").read_text(encoding="utf-8")
    metadata, body = _frontmatter(skill_text)
    manifest_path = directory / "manifest.toml"
    manifest = _read_toml(manifest_path) if manifest_path.exists() else {}
    skill_id = str(manifest.get("id") or metadata.get("name") or directory.name).replace("_", "-")
    return SkillDefinition(
        id=skill_id,
        description=str(metadata.get("description") or ""),
        instructions=body.strip(),
        tools=_tuple(manifest.get("tools")),
        inputs=_tuple(manifest.get("inputs")),
        outputs=_tuple(manifest.get("outputs")),
        path=directory,
        source="project" if project_local else "builtin",
    )


def _read_agent_directory(directory: Path, project_local: bool) -> SubagentDefinition:
    manifest = _read_toml(directory / "agent.toml")
    schema_path = directory / str(manifest.get("output_schema") or "output.schema.json")
    if schema_path.exists():
        json.loads(schema_path.read_text(encoding="utf-8"))
    return SubagentDefinition(
        id=str(manifest.get("id") or directory.name),
        name=str(manifest.get("name") or directory.name),
        role=str(manifest.get("role") or ""),
        prompt=(directory / str(manifest.get("prompt") or "prompt.md")).read_text(encoding="utf-8"),
        tools=_tuple(manifest.get("tools")),
        skills=tuple(value.replace("_", "-") for value in _tuple(manifest.get("skills"))),
        model_profile=str(manifest.get("model_profile") or "writer"),
        output_schema=schema_path if schema_path.exists() else None,
        path=directory,
        source="project" if project_local else "builtin",
    )


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return {}, text
    metadata: dict[str, str] = {}
    for line in text[4:marker].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        raw = value.strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        metadata[key.strip()] = str(parsed)
    return metadata, text[marker + 5 :]


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        value = tomllib.load(handle)
    return value if isinstance(value, dict) else {}


def _tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def _agent_id(name: str) -> str:
    lowered = name.removesuffix("Agent")
    output: list[str] = []
    for index, char in enumerate(lowered):
        if index and char.isupper():
            output.append("-")
        output.append(char.lower())
    return "".join(output).replace("_", "-")


def _catalog_agent_prompt(item: dict[str, Any]) -> str:
    sections = [
        f"# {item.get('name') or 'LG Subagent'}",
        str(item.get("role") or ""),
    ]
    for title, key in (
        ("Responsibilities", "responsibilities"),
        ("Handoff Rules", "handoff_rules"),
        ("Quality Gate", "quality_criteria"),
        ("Known Failure Modes", "failure_modes"),
    ):
        values = item.get(key, [])
        if isinstance(values, list) and values:
            sections.append(f"## {title}\n" + "\n".join(f"- {value}" for value in values))
    return "\n\n".join(section for section in sections if section).strip()


def _bundled_output_schema() -> Path | None:
    try:
        candidate = resources.files("lg_cli.resources").joinpath("stage-output.schema.json")
        path = Path(str(candidate))
    except (ModuleNotFoundError, TypeError):
        return None
    return path if path.exists() else None
