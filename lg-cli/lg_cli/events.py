from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable


class EventType(str, Enum):
    RUN_STARTED = "run.started"
    RUN_RESUMED = "run.resumed"
    CONTEXT_STARTED = "context.started"
    CONTEXT_COMPLETED = "context.completed"
    STAGE_STARTED = "stage.started"
    STAGE_COMPLETED = "stage.completed"
    STAGE_FAILED = "stage.failed"
    MODEL_EVENT = "model.event"
    ARTIFACT_WRITTEN = "artifact.written"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_PLANNED = "run.planned"


@dataclass(frozen=True)
class RunEvent:
    sequence: int
    run_id: str
    type: EventType
    workflow: str
    timestamp: str
    stage_id: str | None = None
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        run_id: str,
        type: EventType,
        workflow: str,
        stage_id: str | None = None,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> "RunEvent":
        return cls(
            sequence=sequence,
            run_id=run_id,
            type=type,
            workflow=workflow,
            timestamp=datetime.now(timezone.utc).isoformat(),
            stage_id=stage_id,
            message=message,
            data=data or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "run_id": self.run_id,
            "type": self.type.value,
            "workflow": self.workflow,
            "timestamp": self.timestamp,
            "stage_id": self.stage_id,
            "message": self.message,
            "data": self.data,
        }


EventSink = Callable[[RunEvent], None]
