from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import load_skills, load_subagents, load_workflows
from .config import LGConfig
from .logging_utils import ensure_log_dir, write_json, write_text
from .memory import MemoryContext
from .reference_search import ReferenceContext


OUTLINE_SKILLS = {
    "direction_expander",
    "reference_retriever",
    "reference_abstractor",
    "worldview_designer",
    "character_arc_designer",
    "conflict_engine",
    "outline_planner",
    "continuity_checker",
    "failure_logger",
}

OUTLINE_SUBAGENTS = {
    "DirectorAgent",
    "ReferenceAgent",
    "WorldbuildingAgent",
    "CharacterAgent",
    "PlotAgent",
    "CriticAgent",
    "ContinuityAgent",
    "MemoryAgent",
}


@dataclass(frozen=True)
class PromptBuildResult:
    prompt: str
    prompt_path: Path
    metadata_path: Path
    metadata: dict[str, Any]


def build_outline_prompt(
    *,
    user_request: str,
    mode: dict[str, Any],
    config: LGConfig,
    memory_context: MemoryContext,
    reference_context: ReferenceContext,
    tool_policy: dict[str, Any],
) -> PromptBuildResult:
    skills = [item for item in load_skills() if item.get("name") in OUTLINE_SKILLS]
    subagents = [item for item in load_subagents() if item.get("name") in OUTLINE_SUBAGENTS]
    workflows = load_workflows()
    workflow = workflows.get("workflows", {}).get("outline_workflow", {})
    prompt = _render_outline_prompt(
        user_request=user_request,
        mode=mode,
        workflow=workflow,
        skills=skills,
        subagents=subagents,
        memory_context=memory_context,
        reference_context=reference_context,
        tool_policy=tool_policy,
    )
    log_dir = ensure_log_dir(config.workspace)
    prompt_path = write_text(log_dir / "last_prompt.md", prompt)
    metadata = {
        "command": "outline",
        "mode": mode.get("name", "OutlineMode"),
        "workspace": str(config.workspace),
        "model": config.model_label,
        "provider": config.provider,
        "skills": [item.get("name") for item in skills],
        "subagents": [item.get("name") for item in subagents],
        "memory_files_found": memory_context.found_count,
        "memory_total_chars": memory_context.total_chars,
        "reference_files_scanned": reference_context.files_scanned,
        "reference_results": reference_context.found_count,
        "reference": reference_context.to_metadata(),
    }
    metadata_path = write_json(log_dir / "last_prompt.meta.json", metadata)
    return PromptBuildResult(prompt=prompt, prompt_path=prompt_path, metadata_path=metadata_path, metadata=metadata)


def _render_outline_prompt(
    *,
    user_request: str,
    mode: dict[str, Any],
    workflow: dict[str, Any],
    skills: list[dict[str, Any]],
    subagents: list[dict[str, Any]],
    memory_context: MemoryContext,
    reference_context: ReferenceContext,
    tool_policy: dict[str, Any],
) -> str:
    return "\n\n".join(
        [
            "# LiteraryGiant Agent Task",
            "## Role\n你是 LiteraryGiant 的 DirectorAgent，负责统筹 ReferenceAgent、WorldbuildingAgent、CharacterAgent、PlotAgent、CriticAgent、ContinuityAgent 和 MemoryAgent。",
            f"## User Request\n{user_request}",
            f"## Mode\n{mode.get('name', 'OutlineMode')}",
            (
                "## Non-Negotiable Rules\n"
                "- 不要照搬 Reference Library 原文。\n"
                "- Reference 只能用于抽象结构、节奏、关系模式、冲突模式。\n"
                "- 不要修改 unrelated files。\n"
                "- 输出默认写入 .literarygiant/output/outline.md 或 timestamped outline 文件。\n"
                "- 如果信息不足，先做合理假设并明确标记。\n"
                "- 需要产出可继续写正文的详细大纲。"
            ),
            "## Available Skills\n" + _skills_text(skills),
            "## Subagent Roles\n" + _subagents_text(subagents),
            "## Tool Policy\n" + json.dumps(tool_policy, ensure_ascii=False, indent=2),
            "## Memory Context\n" + memory_context.to_prompt_text(),
            "## Reference Context\n" + reference_context.to_prompt_text(),
            "## Required Workflow\n" + _workflow_text(workflow),
            (
                "## Required Output Format\n"
                "# Outline\n"
                "## 1. Core Direction\n"
                "- Genre:\n- Tags:\n- Core Hook:\n- Reader Promise:\n"
                "## 2. Commercial Selling Points\n"
                "## 3. Worldview\n"
                "## 4. Main Characters\n"
                "## 5. Conflict Engine\n"
                "## 6. Volume Structure\n"
                "## 7. Chapter Outline\n"
                "## 8. Foreshadowing / Payoff\n"
                "## 9. Emotional Rhythm\n"
                "## 10. Continuity Risks\n"
                "## 11. Next Writing Plan\n"
            ),
            (
                "## Failure Handling\n"
                "如果无法完成真实生成，请输出清晰原因、已使用的上下文、下一步需要补的 adapter 或配置。"
            ),
        ]
    )


def _skills_text(skills: list[dict[str, Any]]) -> str:
    if not skills:
        return "No outline skills were loaded."
    lines: list[str] = []
    for item in skills:
        checklist = ", ".join(item.get("quality_checklist", [])[:4])
        lines.append(f"- {item.get('name')}: {item.get('purpose')} Quality: {checklist}")
    return "\n".join(lines)


def _subagents_text(subagents: list[dict[str, Any]]) -> str:
    if not subagents:
        return "No outline subagents were loaded."
    return "\n".join(f"- {item.get('name')}: {item.get('role')}" for item in subagents)


def _workflow_text(workflow: dict[str, Any]) -> str:
    stages = workflow.get("stages", [])
    if not stages:
        return "1. Direction expansion\n2. Final structured outline"
    lines: list[str] = []
    for index, stage in enumerate(stages, start=1):
        lines.append(f"{index}. {stage.get('agent')} uses {stage.get('skill')}: {stage.get('task')}")
    lines.extend(
        [
            f"{len(lines) + 1}. Conflict engine",
            f"{len(lines) + 2}. Volume outline",
            f"{len(lines) + 3}. Chapter outline",
            f"{len(lines) + 4}. Foreshadowing and payoff",
            f"{len(lines) + 5}. Risk analysis",
            f"{len(lines) + 6}. Final structured outline",
        ]
    )
    return "\n".join(lines)
