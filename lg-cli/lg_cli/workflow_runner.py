from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .catalog import load_workflows
from .config import LGConfig
from .core_adapter import CodexExecAdapter, CoreExecutionResult, EngineEventCallback
from .definitions import DefinitionRegistry
from .events import EventSink, EventType, RunEvent
from .knowledge import KnowledgeContext, KnowledgeGateway
from .logging_utils import append_agent_log, ensure_log_dir, utc_now, write_text
from .memory import read_memory_context
from .output_writer import OutputWriteResult, SPECIALIST_OUTPUTS, write_workflow_output
from .project_store import ProjectStore
from .prompt_builder import build_stage_prompt
from .run_store import RunHandle, RunStore
from .story_context import render_story_context

COMMAND_TO_WORKFLOW = {
    "chat": "chat_workflow",
    "code": "code_workflow",
    "write": "chapter_writing_workflow",
    "outline": "outline_workflow",
    "world": "worldbuilding_workflow",
    "character": "character_workflow",
    "plot": "plot_workflow",
    "ref": "reference_workflow",
    "check": "consistency_check_workflow",
}

DEFAULT_REQUESTS = {
    "chat": "请根据当前项目状态给出下一步最有价值的创作行动。",
    "outline": "请基于当前 memory 生成一份可以继续写正文的小说大纲。",
    "world": "请基于当前项目设计一套能够持续制造冲突的世界观。",
    "character": "请基于当前项目设计主要人物、人物弧光和关系张力。",
    "plot": "请基于当前项目设计因果连贯的主线、升级、反转、伏笔和回收。",
    "write": "请基于当前大纲和 memory 写下一章。",
    "check": "请检查当前故事材料的一致性、因果、时间线和设定边界。",
    "ref": "请为当前创作方向检索并抽象可迁移的叙事机制。",
    "code": "请检查 LiteraryGiant 当前状态并提出一个明确的代码任务。",
}

EXIT_OK = 0
EXIT_DEFINITION = 2
EXIT_CONFIGURATION = 3
EXIT_MODEL = 4
EXIT_NOT_FOUND = 5


class ModelAdapter(Protocol):
    def run(
        self,
        *,
        prompt: str,
        config: LGConfig,
        mode: str,
        model_profile: str | None = None,
        output_schema: Path | None = None,
        on_event: EngineEventCallback | None = None,
    ) -> CoreExecutionResult: ...


@dataclass(frozen=True)
class WorkflowExecutionResult:
    workflow_name: str
    run_id: str
    status: str
    text: str
    artifact_path: Path | None
    latest_path: Path | None
    exit_code: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


class WorkflowRunner:
    def __init__(
        self,
        config: LGConfig,
        *,
        adapter: ModelAdapter | None = None,
        registry: DefinitionRegistry | None = None,
        knowledge: KnowledgeGateway | None = None,
        store: RunStore | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter or CodexExecAdapter()
        self.registry = registry or DefinitionRegistry(config.workspace)
        self.knowledge = knowledge or KnowledgeGateway(config)
        self.store = store or RunStore(config.workspace)

    def run(
        self,
        command: str,
        request: str,
        *,
        dry_run: bool = False,
        allow_raw: bool | None = None,
        on_event: EventSink | None = None,
        resumed_from: str | None = None,
    ) -> WorkflowExecutionResult:
        workflow_name, workflow = self._workflow(command)
        normalized_request = request.strip() or DEFAULT_REQUESTS.get(command, DEFAULT_REQUESTS["chat"])
        handle = self.store.create(
            workflow=workflow_name,
            command=command,
            request=normalized_request,
            provider=self.config.provider,
            model=self.config.model_for_mode(command),
            resumed_from=resumed_from,
        )
        return self._execute(
            handle=handle,
            workflow_name=workflow_name,
            workflow=workflow,
            command=command,
            request=normalized_request,
            dry_run=dry_run,
            allow_raw=allow_raw,
            on_event=on_event,
            resumed_from=resumed_from,
        )

    def resume(
        self,
        run_id: str,
        *,
        dry_run: bool = False,
        allow_raw: bool | None = None,
        on_event: EventSink | None = None,
    ) -> WorkflowExecutionResult:
        try:
            previous = self.store.load(run_id)
        except FileNotFoundError as exc:
            return WorkflowExecutionResult(
                workflow_name="unknown",
                run_id=run_id,
                status="failed",
                text="",
                artifact_path=None,
                latest_path=None,
                exit_code=EXIT_NOT_FOUND,
                error=str(exc),
            )
        if previous.get("status") == "completed":
            return WorkflowExecutionResult(
                workflow_name=str(previous.get("workflow") or "unknown"),
                run_id=run_id,
                status="completed",
                text="Run is already complete; nothing to resume.",
                artifact_path=_optional_path(previous.get("artifact_path")),
                latest_path=_optional_path(previous.get("latest_path")),
                exit_code=EXIT_OK,
            )
        return self.run(
            str(previous.get("command") or "chat"),
            str(previous.get("request") or ""),
            dry_run=dry_run,
            allow_raw=allow_raw,
            on_event=on_event,
            resumed_from=run_id,
        )

    def _execute(
        self,
        *,
        handle: RunHandle,
        workflow_name: str,
        workflow: dict[str, Any],
        command: str,
        request: str,
        dry_run: bool,
        allow_raw: bool | None,
        on_event: EventSink | None,
        resumed_from: str | None,
    ) -> WorkflowExecutionResult:
        sequence = 0

        def emit(
            event_type: EventType,
            message: str,
            *,
            stage_id: str | None = None,
            data: dict[str, Any] | None = None,
        ) -> None:
            nonlocal sequence
            sequence += 1
            event = RunEvent.create(
                sequence=sequence,
                run_id=handle.run_id,
                type=event_type,
                workflow=workflow_name,
                stage_id=stage_id,
                message=message,
                data=data,
            )
            self.store.append_event(handle, event)
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:
                    pass

        emit(
            EventType.RUN_RESUMED if resumed_from else EventType.RUN_STARTED,
            f"Resuming {workflow_name}" if resumed_from else f"Starting {workflow_name}",
            data={"command": command, "resumed_from": resumed_from},
        )
        emit(EventType.CONTEXT_STARTED, "Loading memory and knowledge context")
        memory = read_memory_context(
            self.config.workspace,
            memory_root=self.config.memory_path,
            max_total_chars=min(16000, self.config.max_stage_context_chars // 2),
        )
        knowledge = self._knowledge_context(workflow, request, allow_raw)
        project = ProjectStore(self.config.workspace)
        project_context = (
            render_story_context(project.context_snapshot(query=request),
                                 max_chars=min(36000, self.config.max_stage_context_chars // 2))
            if project.initialized else ""
        )
        emit(
            EventType.CONTEXT_COMPLETED,
            f"Loaded {memory.found_count} memory files and {knowledge.found_count} knowledge hits",
            data={
                "memory_files": memory.found_count,
                "memory_chars": memory.total_chars,
                "knowledge": knowledge.to_metadata(),
            },
        )

        stages = self._selected_stages(workflow)
        if dry_run:
            plan = _plan_markdown(
                workflow_name=workflow_name,
                workflow=workflow,
                command=command,
                request=request,
                stages=stages,
                memory_files=memory.found_count,
                knowledge=knowledge,
            )
            output = write_workflow_output(
                self.config.workspace,
                "dry-run",
                plan,
                output_root=self.config.output_path,
                run_id=handle.run_id,
            )
            emit(
                EventType.RUN_PLANNED,
                "Dry-run plan created without invoking a model",
                data={"artifact_path": str(output.output_path)},
            )
            self.store.finalize(
                handle,
                status="planned",
                artifact_path=output.output_path,
                latest_path=output.latest_path,
            )
            return WorkflowExecutionResult(
                workflow_name,
                handle.run_id,
                "planned",
                plan,
                output.output_path,
                output.latest_path,
                EXIT_OK,
            )

        if not self.config.credentials_configured:
            return self._fail(
                handle,
                workflow_name,
                emit,
                error=(
                    "No API key or subscription login is configured. Use literary auth add "
                    "for API access or literary auth login for ChatGPT; use --dry-run to inspect the workflow."
                ),
                exit_code=EXIT_CONFIGURATION,
            )

        prior_outputs: list[tuple[str, str]] = []
        completed_stage_ids = self._restore_prior_stages(
            resumed_from=resumed_from,
            handle=handle,
            stages=stages,
            prior_outputs=prior_outputs,
            emit=emit,
        )

        for index, stage in enumerate(stages, start=1):
            stage_id = str(stage.get("id") or f"stage-{index}")
            if stage_id in completed_stage_ids:
                continue
            try:
                agent = self.registry.agent(str(stage.get("agent") or "director"))
                skill = self.registry.skill(str(stage.get("skill") or "direction-expander"))
            except KeyError as exc:
                return self._fail(
                    handle,
                    workflow_name,
                    emit,
                    error=str(exc),
                    exit_code=EXIT_DEFINITION,
                    stage_id=stage_id,
                )

            prompt = build_stage_prompt(
                command=command,
                workflow_name=workflow_name,
                workflow_description=str(workflow.get("description") or ""),
                stage_id=stage_id,
                stage_index=index,
                stage_count=len(stages),
                stage_task=str(stage.get("task") or "Complete the assigned stage."),
                expected_outputs=[str(item) for item in workflow.get("outputs", [])],
                user_request=(
                    "Project writing instructions:\n" + self.config.project_instructions
                    + "\n\nAuthor request:\n" + request
                    if self.config.project_instructions else request
                ),
                agent=agent,
                skill=skill,
                memory_context=memory,
                knowledge_context=knowledge,
                prior_outputs=prior_outputs,
                max_context_chars=self.config.max_stage_context_chars,
                project_context=project_context,
            )
            prompt_path = self.store.write_stage_prompt(handle, stage_id=stage_id, prompt=prompt.text)
            write_text(ensure_log_dir(self.config.workspace) / "last_prompt.md", prompt.text)
            emit(
                EventType.STAGE_STARTED,
                f"{agent.name} using {skill.id}",
                stage_id=stage_id,
                data={
                    "index": index,
                    "total": len(stages),
                    "agent": agent.id,
                    "skill": skill.id,
                    "model_profile": agent.model_profile,
                    "context_chars": prompt.context_chars,
                    "truncated_sections": list(prompt.truncated_sections),
                },
            )

            def engine_event(payload: dict[str, Any], current_stage: str = stage_id) -> None:
                emit(
                    EventType.MODEL_EVENT,
                    _engine_event_message(payload),
                    stage_id=current_stage,
                    data={"engine_event": payload},
                )

            core_result = self.adapter.run(
                prompt=prompt.text,
                config=self.config,
                mode=command,
                model_profile=agent.model_profile,
                output_schema=agent.output_schema,
                on_event=engine_event,
            )
            if not core_result.ok:
                emit(
                    EventType.STAGE_FAILED,
                    core_result.error or "Model stage failed",
                    stage_id=stage_id,
                    data=_adapter_metadata(core_result),
                )
                return self._fail(
                    handle,
                    workflow_name,
                    emit,
                    error=core_result.error or "Model stage failed",
                    exit_code=EXIT_CONFIGURATION if core_result.used_stub else EXIT_MODEL,
                    stage_id=stage_id,
                )

            try:
                parsed = _parse_stage_output(core_result.output_text)
            except ValueError as exc:
                emit(EventType.STAGE_FAILED, str(exc), stage_id=stage_id)
                return self._fail(
                    handle,
                    workflow_name,
                    emit,
                    error=str(exc),
                    exit_code=EXIT_MODEL,
                    stage_id=stage_id,
                )
            artifact = str(parsed["artifact_markdown"]).strip()
            stage_artifact = None
            if command not in {"chat", "code"} and index < len(stages):
                stage_artifact = write_workflow_output(
                    self.config.workspace, SPECIALIST_OUTPUTS.get(agent.id, command), artifact,
                    output_root=self.config.output_path, run_id=handle.run_id, stage_id=stage_id,
                ).output_path
            self.store.write_stage(
                handle,
                stage_id=stage_id,
                content=artifact.rstrip() + "\n",
                metadata={
                    "stage_id": stage_id,
                    "agent": agent.id,
                    "skill": skill.id,
                    "model_profile": agent.model_profile,
                    "prompt_path": str(prompt_path),
                    "summary": parsed["summary"],
                    "handoff": parsed["handoff"],
                    "risks": parsed["risks"],
                    "adapter": _adapter_metadata(core_result),
                    "artifact_path": str(stage_artifact) if stage_artifact else None,
                },
            )
            prior_outputs.append((stage_id, artifact))
            emit(
                EventType.STAGE_COMPLETED,
                str(parsed["summary"]),
                stage_id=stage_id,
                data={"index": index, "total": len(stages), "risks": parsed["risks"]},
            )

        if not prior_outputs:
            return self._fail(
                handle,
                workflow_name,
                emit,
                error="Workflow completed no stages and produced no artifact.",
                exit_code=EXIT_MODEL,
            )
        final_content = prior_outputs[-1][1]
        output = write_workflow_output(
            self.config.workspace,
            command,
            final_content,
            output_root=self.config.output_path,
            run_id=handle.run_id,
        )
        emit(
            EventType.ARTIFACT_WRITTEN,
            f"Artifact written to {output.output_path}",
            data={
                "artifact_path": str(output.output_path),
                "latest_path": str(output.latest_path),
            },
        )
        emit(EventType.RUN_COMPLETED, f"Completed {workflow_name}")
        self.store.finalize(
            handle,
            status="completed",
            artifact_path=output.output_path,
            latest_path=output.latest_path,
        )
        append_agent_log(
            self.config.workspace,
            f"workflow completed run={handle.run_id} workflow={workflow_name} output={output.output_path}",
        )
        return WorkflowExecutionResult(
            workflow_name,
            handle.run_id,
            "completed",
            final_content,
            output.output_path,
            output.latest_path,
            EXIT_OK,
        )

    def _workflow(self, command: str) -> tuple[str, dict[str, Any]]:
        workflow_name = COMMAND_TO_WORKFLOW.get(command)
        if workflow_name is None:
            raise ValueError(f"unknown LiteraryGiant command: {command}")
        workflows = load_workflows().get("workflows", {})
        workflow = workflows.get(workflow_name)
        if not isinstance(workflow, dict):
            raise ValueError(f"workflow definition is missing: {workflow_name}")
        return workflow_name, workflow

    def _selected_stages(self, workflow: dict[str, Any]) -> list[dict[str, Any]]:
        raw = [dict(stage) for stage in workflow.get("stages", []) if isinstance(stage, dict)]
        if self.config.execution_strategy != "single-pass" or len(raw) <= 1:
            return raw
        final = next((stage for stage in reversed(raw) if stage.get("final")), raw[-1])
        tasks = "\n".join(
            f"{index}. {stage.get('task', '')}" for index, stage in enumerate(raw, start=1)
        )
        return [
            {
                "id": "single-pass",
                "agent": final.get("agent", "director"),
                "skill": final.get("skill", "direction-expander"),
                "task": f"Complete the full workflow in one pass:\n{tasks}",
                "final": True,
            }
        ]

    def _knowledge_context(
        self,
        workflow: dict[str, Any],
        request: str,
        allow_raw: bool | None,
    ) -> KnowledgeContext:
        policy = str(workflow.get("knowledge") or "optional")
        if policy == "none" or not self.config.enable_reference:
            warning = (
                "Knowledge retrieval is disabled for this workflow."
                if policy == "none"
                else "Knowledge retrieval is disabled by configuration."
            )
            return KnowledgeContext((), self.knowledge.library_root, 0, (), (warning,))
        return self.knowledge.search(request, allow_raw=allow_raw)

    def _restore_prior_stages(
        self,
        *,
        resumed_from: str | None,
        handle: RunHandle,
        stages: list[dict[str, Any]],
        prior_outputs: list[tuple[str, str]],
        emit: Any,
    ) -> set[str]:
        if not resumed_from:
            return set()
        manifest = self.store.load(resumed_from)
        previous_completed = set(str(item) for item in manifest.get("completed_stages", []))
        restored: set[str] = set()
        for stage in stages:
            stage_id = str(stage.get("id") or "")
            if stage_id not in previous_completed:
                break
            content = self.store.read_stage(resumed_from, stage_id)
            if content is None:
                break
            self.store.write_stage(
                handle,
                stage_id=stage_id,
                content=content,
                metadata={**self.store.read_stage_metadata(resumed_from, stage_id),
                          "stage_id": stage_id, "restored_from": resumed_from},
            )
            prior_outputs.append((stage_id, content))
            restored.add(stage_id)
            emit(
                EventType.STAGE_COMPLETED,
                f"Restored completed stage from {resumed_from}",
                stage_id=stage_id,
                data={"restored": True},
            )
        return restored

    def _fail(
        self,
        handle: RunHandle,
        workflow_name: str,
        emit: Any,
        *,
        error: str,
        exit_code: int,
        stage_id: str | None = None,
    ) -> WorkflowExecutionResult:
        emit(EventType.RUN_FAILED, error, stage_id=stage_id, data={"exit_code": exit_code})
        self.store.finalize(handle, status="failed", error=error)
        _append_failure(self.config.workspace, handle.run_id, workflow_name, stage_id, error)
        append_agent_log(
            self.config.workspace,
            f"workflow failed run={handle.run_id} workflow={workflow_name} error={error}",
        )
        return WorkflowExecutionResult(
            workflow_name,
            handle.run_id,
            "failed",
            "",
            None,
            None,
            exit_code,
            error,
        )


def _parse_stage_output(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```json") and raw.endswith("```"):
        raw = raw[7:-3].strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model stage returned invalid structured output: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Model stage output must be a JSON object.")
    required = {"summary", "artifact_markdown", "handoff", "risks"}
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"Model stage output is missing: {', '.join(missing)}")
    if not isinstance(value["artifact_markdown"], str) or not value["artifact_markdown"].strip():
        raise ValueError("Model stage artifact_markdown must be a non-empty string.")
    if not isinstance(value["summary"], str):
        raise ValueError("Model stage summary must be a string.")
    if not isinstance(value["handoff"], dict):
        raise ValueError("Model stage handoff must be an object.")
    if not isinstance(value["risks"], list) or not all(
        isinstance(item, str) for item in value["risks"]
    ):
        raise ValueError("Model stage risks must be an array of strings.")
    return value


def _adapter_metadata(result: CoreExecutionResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "used_stub": result.used_stub,
        "error": result.error,
        "command_name": result.command_name,
        "returncode": result.returncode,
        "duration_seconds": result.duration_seconds,
        "attempted_candidates": list(result.attempted_candidates),
    }


def _engine_event_message(payload: dict[str, Any]) -> str:
    event_type = str(payload.get("type") or "model.event")
    item = payload.get("item")
    if isinstance(item, dict):
        item_type = item.get("type")
        if item_type:
            return f"{event_type}: {item_type}"
    return event_type


def _plan_markdown(
    *,
    workflow_name: str,
    workflow: dict[str, Any],
    command: str,
    request: str,
    stages: list[dict[str, Any]],
    memory_files: int,
    knowledge: KnowledgeContext,
) -> str:
    stage_lines = []
    for index, stage in enumerate(stages, start=1):
        stage_lines.append(
            f"{index}. `{stage.get('id')}`: `{stage.get('agent')}` + `{stage.get('skill')}`\n"
            f"   {stage.get('task')}"
        )
    outputs = "\n".join(f"- {item}" for item in workflow.get("outputs", []))
    warnings = "\n".join(f"- {item}" for item in knowledge.warnings) or "- none"
    return (
        f"# LG Workflow Plan: {command}\n\n"
        f"- Workflow: `{workflow_name}`\n"
        f"- Strategy: dry-run\n"
        f"- Memory files loaded: {memory_files}\n"
        f"- Knowledge hits: {knowledge.found_count}\n"
        f"- Knowledge tiers: {', '.join(knowledge.tiers_searched) or 'none'}\n\n"
        f"## Request\n\n{request}\n\n"
        f"## Stages\n\n{chr(10).join(stage_lines)}\n\n"
        f"## Output Contract\n\n{outputs}\n\n"
        f"## Knowledge Warnings\n\n{warnings}\n"
    )


def _append_failure(
    workspace: Path,
    run_id: str,
    workflow_name: str,
    stage_id: str | None,
    error: str,
) -> None:
    path = ensure_log_dir(workspace) / "failures.md"
    if not path.exists():
        path.write_text("# Failures\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            f"## {utc_now()} · {run_id}\n\n"
            f"- workflow: `{workflow_name}`\n"
            f"- stage: `{stage_id or 'n/a'}`\n"
            f"- error: {error}\n\n"
        )


def _optional_path(value: Any) -> Path | None:
    return Path(value) if isinstance(value, str) and value else None
