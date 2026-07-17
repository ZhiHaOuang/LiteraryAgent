from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..catalog import catalog_file, find_mode, load_subagents
from ..config import LGConfig
from ..core_adapter import CoreExecutionResult, run_model_turn
from ..logging_utils import append_agent_log, ensure_log_dir, utc_now, write_json
from ..memory import read_memory_context
from ..output_writer import OutputWriteResult, write_outline_output
from ..prompt_builder import PromptBuildResult, build_outline_prompt
from ..reference_search import ReferenceContext, search_references


@dataclass(frozen=True)
class OutlineWorkflowResult:
    workflow_name: str
    text: str
    prompt_path: Path
    output: OutputWriteResult
    last_run_path: Path
    used_stub: bool
    adapter_error: str | None


def run_outline_workflow(user_prompt: str, config: LGConfig, *, debug: bool = False) -> OutlineWorkflowResult:
    request = user_prompt.strip() or "请基于当前 memory 生成一个可继续写正文的小说大纲。"
    mode = find_mode("outline") or {"name": "OutlineMode", "command": "outline"}
    tool_policy = _load_tool_policy()
    memory_context = read_memory_context(config.workspace)
    reference_context = _load_references(config, request)
    prompt_result = build_outline_prompt(
        user_request=request,
        mode=mode,
        config=config,
        memory_context=memory_context,
        reference_context=reference_context,
        tool_policy=tool_policy,
    )
    core_result = run_model_turn(prompt=prompt_result.prompt, config=config, mode="outline")
    if core_result.ok and not core_result.used_stub:
        content = _normalize_model_output(core_result.output_text)
    else:
        content = _fallback_outline(
            request=request,
            prompt_result=prompt_result,
            reference_context=reference_context,
            core_result=core_result,
        )
    output = write_outline_output(config.workspace, content, output_root=config.output_path)
    last_run_path = _write_last_run(
        config=config,
        request=request,
        prompt_result=prompt_result,
        reference_context=reference_context,
        core_result=core_result,
        output=output,
        debug=debug,
    )
    append_agent_log(
        config.workspace,
        (
            "outline workflow "
            f"{'degraded' if core_result.used_stub else 'completed'}; "
            f"prompt={prompt_result.prompt_path}; output={output.output_path}"
        ),
    )
    text = _cli_text(
        content=content,
        output=output,
        prompt_result=prompt_result,
        last_run_path=last_run_path,
        core_result=core_result,
        reference_context=reference_context,
    )
    return OutlineWorkflowResult(
        workflow_name="outline_workflow",
        text=text,
        prompt_path=prompt_result.prompt_path,
        output=output,
        last_run_path=last_run_path,
        used_stub=core_result.used_stub,
        adapter_error=core_result.error,
    )


def _load_tool_policy() -> dict[str, Any]:
    path = catalog_file("lg-tools/tool_policy.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_references(config: LGConfig, request: str) -> ReferenceContext:
    if not config.enable_reference:
        return ReferenceContext(
            results=[],
            warnings=["Reference search disabled by config agent.enable_reference=false."],
            roots_checked=[],
            files_scanned=0,
        )
    return search_references(config.workspace, request)


def _normalize_model_output(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return "# Outline\n\n[empty model output]"
    if cleaned.startswith("#"):
        return cleaned + "\n"
    return "# Outline\n\n" + cleaned + "\n"


def _fallback_outline(
    *,
    request: str,
    prompt_result: PromptBuildResult,
    reference_context: ReferenceContext,
    core_result: CoreExecutionResult,
) -> str:
    profile = _fallback_profile(request)
    subagents = [item.get("name", "") for item in load_subagents()]
    flow = [
        "DirectorAgent: 扩展题材、读者承诺和商业标签。",
        "ReferenceAgent: 检索参考库并只抽象结构，不复制原文。",
        "WorldbuildingAgent: 建立可制造冲突的世界规则和限制。",
        "CharacterAgent: 建立主角欲望、反派镜像和关系张力。",
        "PlotAgent: 生成分卷、章节节点、钩子、伏笔和回收。",
        "CriticAgent: 标出弱动机、重复爽点和连续性风险。",
        "MemoryAgent: 记录后续应该写入 memory 的稳定事实。",
    ]
    reference_lines = [
        f"- results: {reference_context.found_count}",
        f"- files_scanned: {reference_context.files_scanned}",
    ]
    for item in reference_context.results[:4]:
        reference_lines.append(f"- {item.path} (score={item.score})")

    return "\n".join(
        [
            "# Outline Workflow Stub Result",
            "",
            "## User Request",
            request,
            "",
            "## Status",
            "LG 已完成外层 agent workflow 编排，但本次没有调用真实 Codex 模型输出。",
            "",
            "## Reason",
            core_result.error or "adapter returned degraded result",
            "",
            "## Prompt Saved",
            str(prompt_result.prompt_path),
            "",
            "## Reference Context",
            "\n".join(reference_lines),
            "",
            "## Planned Agent Flow",
            "\n".join(f"{index}. {item}" for index, item in enumerate(flow, start=1)),
            "",
            "## Available Subagents",
            ", ".join(name for name in subagents if name) or "No subagents loaded.",
            "",
            "## Deterministic Outline Skeleton",
            "### 1. Core Direction",
            f"- Genre: {profile['genre']}",
            f"- Core Hook: {profile['hook']}",
            f"- Reader Promise: {profile['promise']}",
            "### 2. Commercial Selling Points",
            "\n".join(f"- {item}" for item in profile["selling_points"]),
            "### 3. Conflict Engine",
            f"- 外部压力: {profile['external_pressure']}",
            f"- 内部压力: {profile['internal_pressure']}",
            f"- 升级方式: {profile['escalation']}",
            "### 4. Volume Structure",
            "\n".join(f"- {item}" for item in profile["volumes"]),
            "### 5. Chapter Outline Seed",
            "\n".join(f"- {item}" for item in profile["chapters"]),
            "### 6. Continuity Risks",
            "\n".join(f"- {item}" for item in profile["risks"]),
            "- 重要事实应在真实模型输出后写入 STORY_BIBLE/TIMELINE/CHARACTERS。",
            "",
            "## Next Required Adapter Work",
            "- 配置 API key 后，LG 会尝试调用 core/codex 的非交互 exec 入口。",
            "- 如果本地没有 cargo，可使用 core/codex 的 node wrapper 或设置 LG_CODEX_COMMAND。",
            "- Codex login 不在 LG CLI 暴露；LG 通过环境变量传入 OPENAI_API_KEY。",
            "",
        ]
    )


def _fallback_profile(request: str) -> dict[str, Any]:
    tags: list[str] = []
    if "都市" in request:
        tags.append("都市")
    if "重生" in request:
        tags.append("重生")
    if "网恋" in request:
        tags.append("网恋")
    if "修罗场" in request:
        tags.append("修罗场")
    if "爽文" in request:
        tags.append("爽文")
    if not tags:
        tags = ["长篇商业小说"]

    if "网恋" in request and "盗图" in request:
        hook = "男主因为妹妹盗图被卷入多线网恋误会，必须在掉马、修罗场和现实追责之间反向控场。"
        promise = "误会密集、身份反转、关系拉扯和连续掉马危机，每一阶段都有更高压的公开翻盘。"
        selling = [
            "盗图引发的身份错位天然制造强钩子。",
            "多个女生的期待、误会和现实交叉，适合连续修罗场。",
            "男主从被动背锅到主动控局，爽点来自澄清、反击和关系重排。",
        ]
        external = "被盗图女生、妹妹的隐瞒、网络舆论、现实熟人圈和可能的反向人肉。"
        internal = "男主既要自证清白，又要处理被冒名期间形成的真实情感债。"
        escalation = "先处理单个误会，再出现多线撞车，最后转成公开身份危机与现实利益冲突。"
        volumes = [
            "第一卷: 发现盗图与网恋账号，第一次线下撞车并临时补救。",
            "第二卷: 多个女生陆续察觉异常，男主被迫建立统一说法并追查妹妹动机。",
            "第三卷: 舆论扩散，旧聊天记录被断章取义，男主完成公开反转。",
            "第四卷: 情感线与家庭线收束，妹妹承担后果，男主完成真正选择。",
        ]
        chapters = [
            "Ch01: 男主收到陌生女生的亲密质问，发现自己的照片被妹妹拿去网恋。",
            "Ch02: 第一个女生线下出现，男主边稳局边套出账号信息。",
            "Ch03: 第二个女生发来威胁截图，修罗场从线上烧到现实。",
            "Ch04: 妹妹拒不承认，男主找到聊天风格里的破绽。",
            "Ch05: 多方约在同一地点，男主用一次公开操作反杀谎言。",
        ]
        risks = [
            "妹妹的动机需要可理解但不可轻飘，避免单纯工具人。",
            "多女主关系不能只靠误会堆叠，每条线要有不同利益和情绪诉求。",
            "爽点要包含澄清、控场、反击、补偿和选择，而不是只靠尴尬场面。",
        ]
    elif "重生" in request:
        hook = "主角带着上一世失败经验回到关键节点，用信息差和行动力连续反击。"
        promise = "开局压迫明确、反击迅速、每个阶段都有身份、资源或关系的可见提升。"
        selling = [
            "重生信息差带来的先手优势。",
            "都市关系网中的误会、背叛、补偿和翻盘。",
            "低谷开局到资源整合的连续升级。",
        ]
        external = "旧敌、家庭/职场/资本关系、舆论误判。"
        internal = "主角必须避免重走上一世的冲动决策。"
        escalation = "每次胜利暴露更高层阻力，同时解锁新的资源与关系。"
        volumes = [
            "第一卷: 重回关键日，止损并完成第一次公开反击。",
            "第二卷: 建立资源基本盘，修复或重塑核心关系。",
            "第三卷: 旧敌升级，主角用未来认知完成跨层打击。",
            "第四卷: 真正幕后压力出现，阶段性主线收束。",
        ]
        chapters = [
            "Ch01: 主角醒来，发现回到命运转折前一小时。",
            "Ch02: 主角拒绝上一世错误选择，引发身边人错愕。",
            "Ch03: 第一次小反击成功，但也惊动旧敌。",
            "Ch04: 主角找到上一世被忽略的关键盟友。",
            "Ch05: 阶段危机爆发，以公开场合反转收尾。",
        ]
        risks = [
            "重生信息差不能万能，需要成本、误差和对手学习能力。",
            "爽点不要只靠打脸，要交替使用关系修复、资源升级、危机预判和情绪补偿。",
        ]
    else:
        hook = "围绕用户输入建立一个强误会/强压迫开局，让主角通过连续决策夺回主动权。"
        promise = "每个阶段都有明确危机、反转、关系变化和可见收益。"
        selling = [
            "开局问题足够具体，便于快速进入冲突。",
            "主角的选择会改变关系、资源和风险。",
            "每卷用更高成本换取更强 payoff。",
        ]
        external = "对手、误会、资源短缺、舆论或制度压力。"
        internal = "主角需要在欲望、道德成本和现实收益之间做选择。"
        escalation = "从小范围危机升级到公开冲突，再升级到不可逆选择。"
        volumes = [
            "第一卷: 明确开局危机并完成第一次反击。",
            "第二卷: 扩大关系网和资源盘，制造新代价。",
            "第三卷: 对手升级，旧问题反噬。",
            "第四卷: 主线收束，核心承诺兑现。",
        ]
        chapters = [
            "Ch01: 主角遭遇具体危机。",
            "Ch02: 主角做出反常选择并引发连锁反应。",
            "Ch03: 第一次局部胜利带来更大麻烦。",
            "Ch04: 新盟友或新对手出现。",
            "Ch05: 阶段性反转收尾。",
        ]
        risks = [
            "主角目标要尽早具体化。",
            "每章需要实质变化，避免只解释设定。",
        ]

    return {
        "genre": "/".join(tags),
        "hook": hook,
        "promise": promise,
        "selling_points": selling,
        "external_pressure": external,
        "internal_pressure": internal,
        "escalation": escalation,
        "volumes": volumes,
        "chapters": chapters,
        "risks": risks,
    }


def _write_last_run(
    *,
    config: LGConfig,
    request: str,
    prompt_result: PromptBuildResult,
    reference_context: ReferenceContext,
    core_result: CoreExecutionResult,
    output: OutputWriteResult,
    debug: bool,
) -> Path:
    payload: dict[str, Any] = {
        "schema_version": "lg.run.v1",
        "timestamp": utc_now(),
        "workflow": "outline_workflow",
        "request": request,
        "workspace": str(config.workspace),
        "provider": config.provider,
        "model": config.writer_model or config.default_model,
        "api_key_source": config.api_key_source,
        "prompt_path": str(prompt_result.prompt_path),
        "prompt_metadata_path": str(prompt_result.metadata_path),
        "output_path": str(output.output_path),
        "latest_path": str(output.latest_path),
        "reference": reference_context.to_metadata(),
        "adapter": {
            "ok": core_result.ok,
            "used_stub": core_result.used_stub,
            "error": core_result.error,
            "command_name": core_result.command_name,
            "command": core_result.command,
            "returncode": core_result.returncode,
            "duration_seconds": core_result.duration_seconds,
            "stdout_chars": len(core_result.stdout),
            "stderr_chars": len(core_result.stderr),
        },
    }
    if debug:
        payload["adapter"]["stdout_tail"] = core_result.stdout[-4000:]
        payload["adapter"]["stderr_tail"] = core_result.stderr[-4000:]
    log_dir = ensure_log_dir(config.workspace)
    return write_json(log_dir / "last_run.json", payload)


def _cli_text(
    *,
    content: str,
    output: OutputWriteResult,
    prompt_result: PromptBuildResult,
    last_run_path: Path,
    core_result: CoreExecutionResult,
    reference_context: ReferenceContext,
) -> str:
    status = "degraded fallback" if core_result.used_stub else "completed"
    lines = [
        "LG workflow: outline_workflow",
        f"Status: {status}",
        f"Prompt: {prompt_result.prompt_path}",
        f"Output: {output.output_path}",
        f"Latest: {output.latest_path}",
        f"Run log: {last_run_path}",
        f"References: {reference_context.found_count} result(s), {reference_context.files_scanned} file(s) scanned",
        f"Adapter: {core_result.command_name or 'stub'}",
    ]
    if core_result.error:
        lines.append(f"Adapter note: {core_result.error}")
    lines.extend(["", "---", "", content.strip()])
    return "\n".join(lines)
