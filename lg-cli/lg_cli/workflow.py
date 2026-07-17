from __future__ import annotations

from dataclasses import dataclass

from .catalog import load_workflows
from .config import LGConfig
from .core_adapter import run_model_turn_stub
from .workflows.outline import run_outline_workflow


COMMAND_TO_WORKFLOW = {
    "outline": "outline_workflow",
    "world": "worldbuilding_workflow",
    "character": "character_workflow",
    "plot": "outline_workflow",
    "write": "chapter_writing_workflow",
    "check": "consistency_check_workflow",
    "ref": "reference_workflow",
    "code": "code_workflow",
    "chat": "chat_workflow",
}


@dataclass(frozen=True)
class WorkflowResult:
    workflow_name: str
    text: str


def run_workflow(command: str, user_prompt: str, config: LGConfig, *, debug: bool = False) -> WorkflowResult:
    if command == "outline":
        result = run_outline_workflow(user_prompt, config, debug=debug)
        return WorkflowResult(workflow_name=result.workflow_name, text=result.text)

    workflow_name = COMMAND_TO_WORKFLOW.get(command, "chat_workflow")
    workflows = load_workflows()
    workflow = workflows.get("workflows", {}).get(workflow_name, {})
    stages = workflow.get("stages", [])
    outputs = workflow.get("outputs", [])
    adapter_note = run_model_turn_stub(
        mode=command,
        prompt=user_prompt,
        has_api_key=bool(config.api_key),
    )
    lines = [
        f"LG workflow: {workflow_name}",
        f"Mode command: {command}",
        f"Input: {user_prompt or '(empty)'}",
        "",
        "Planned orchestration:",
    ]
    for index, stage in enumerate(stages, start=1):
        agent = stage.get("agent", "DirectorAgent")
        skill = stage.get("skill", "none")
        lines.append(f"  {index}. {agent} · skill={skill} · {stage.get('task', '')}")
    lines.extend(["", "Expected output sections:"])
    for output in outputs:
        lines.append(f"  - {output}")
    lines.extend(
        [
            "",
            "Adapter status:",
            f"  - {adapter_note}",
            "  - Codex core stays in core/codex; LG prompt/skill/subagent logic stays outside core.",
            "",
            "Stub result:",
            _stub_result(command, user_prompt),
        ]
    )
    return WorkflowResult(workflow_name=workflow_name, text="\n".join(lines))


def _stub_result(command: str, user_prompt: str) -> str:
    if command == "outline":
        return (
            "题材定位：待模型接入后扩展。\n"
            "核心卖点：围绕输入方向建立主角欲望、压迫、反击与连续钩子。\n"
            "分卷结构：开局压迫 -> 能力兑现 -> 关系扩张 -> 反派升级 -> 阶段高潮。\n"
            "风险点：设定漂移、爽点重复、主角动机不足。"
        )
    if command == "world":
        return "世界观草案：规则、资源、势力、等级、代价与禁忌将由 WorldbuildingAgent 展开。"
    if command == "character":
        return "人物草案：主角欲望、创伤、关系张力、反派镜像和成长节点将由 CharacterAgent 展开。"
    if command == "write":
        return "章节草稿：需要 outline/world/character memory 后进入正文生成。"
    if command == "check":
        return "一致性检查：将输出矛盾列表、严重程度、涉及文件和修复建议。"
    if command == "code":
        return "代码任务：后续通过 Codex exec adapter 接入，当前仅生成执行计划。"
    return f"已进入 {command} workflow stub：{user_prompt}"
