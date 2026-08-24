from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import LGConfig
from .events import EventSink
from .workflow_runner import WorkflowRunner


@dataclass(frozen=True)
class WorkflowResult:
    workflow_name: str
    text: str
    run_id: str
    status: str
    exit_code: int
    artifact_path: Path | None
    error: str | None


def run_workflow(
    command: str,
    user_prompt: str,
    config: LGConfig,
    *,
    debug: bool = False,
    dry_run: bool = False,
    allow_raw: bool | None = None,
    on_event: EventSink | None = None,
) -> WorkflowResult:
    del debug
    result = WorkflowRunner(config).run(
        command,
        user_prompt,
        dry_run=dry_run,
        allow_raw=allow_raw,
        on_event=on_event,
    )
    return WorkflowResult(
        workflow_name=result.workflow_name,
        text=result.text,
        run_id=result.run_id,
        status=result.status,
        exit_code=result.exit_code,
        artifact_path=result.artifact_path,
        error=result.error,
    )
