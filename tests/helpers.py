from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lg_cli.config import LGConfig
from lg_cli.core_adapter import CoreExecutionResult


def make_config(
    root: Path,
    *,
    api_key: str | None = "test-key",
    library_path: Path | None = None,
) -> LGConfig:
    return LGConfig(
        workspace=root,
        provider="openai",
        default_model="",
        writer_model="",
        coder_model="",
        critic_model="",
        memory_path=root / ".literarygiant" / "memory",
        output_path=root / ".literarygiant" / "output",
        reference_path=root / "ReferenceLibrary",
        library_path=library_path,
        knowledge_top_k=3,
        allow_raw_reference=False,
        default_mode="chat",
        execution_strategy="staged",
        enable_shell=False,
        enable_reference=True,
        timeout_seconds=30,
        max_stage_context_chars=12000,
        api_key=api_key,
        api_key_source="test" if api_key else "not configured",
        loaded_files=(),
        warnings=(),
    )


class FakeAdapter:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        *,
        prompt,
        config,
        mode,
        model_profile=None,
        output_schema=None,
        on_event=None,
    ) -> CoreExecutionResult:
        number = len(self.calls) + 1
        self.calls.append(
            {
                "prompt": prompt,
                "mode": mode,
                "model_profile": model_profile,
                "output_schema": output_schema,
            }
        )
        if on_event is not None:
            on_event({"type": "turn.started", "item": {"type": "agent_message"}})
        if self.fail_at == number:
            return CoreExecutionResult(
                ok=False,
                used_stub=False,
                output_text="",
                error=f"synthetic failure at stage {number}",
                command_name="fake",
                command=["fake"],
                returncode=9,
                stdout="",
                stderr="synthetic",
                duration_seconds=0.01,
            )
        payload = {
            "summary": f"completed stage {number}",
            "book_overview": "林舟在旅途中寻找失踪者，揭开故乡的秘密。",
            "artifact_markdown": f"# Stage {number}\n\nGenerated for {mode}.",
            "handoff": {
                "established_facts": [f"fact-{number}"],
                "assumptions": [],
                "constraints": [],
                "open_questions": [],
                "next_actions": [],
            },
            "risks": [],
        }
        return CoreExecutionResult(
            ok=True,
            used_stub=False,
            output_text=json.dumps(payload),
            error=None,
            command_name="fake",
            command=["fake"],
            returncode=0,
            stdout="",
            stderr="",
            duration_seconds=0.01,
        )


def reference_card(category, *, stored=False):
    from lg_cli.reference_contracts import card_schema, finalize_cards
    def example(schema):
        if 'enum' in schema:
            return schema['enum'][-1]
        if schema['type'] == 'object':
            return {key:example(value) for key,value in schema['properties'].items()}
        if schema['type'] == 'array':
            return []
        if schema['type'] == 'integer':
            return 1
        return ''
    card = example(card_schema(category))
    if stored:
        card = finalize_cards({'entries':[{'instance_card':card, 'evidence':[]}]}, category, {})['entries'][0]['instance_card']
    return card
