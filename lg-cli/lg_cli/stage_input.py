"""Product-owned stage input, shared by persistence, recovery and execution."""
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class StageInput:
    prompt: str
    instructions: str


def prepare_stage_input(prompt: str, *, output_schema: Path | None = None,
                        guidance: str | None = None, instructions: str | None = None) -> StageInput:
    if instructions is None:
        instructions = resources.files('lg_cli.resources').joinpath('workflow-stage.txt').read_text()
    if output_schema and output_schema.name in {
            'stage-output.schema.json', 'scene-draft.schema.json', 'revision.schema.json'}:
        from .story_index import GRAPH_INSTRUCTIONS
        prompt += '\n' + GRAPH_INSTRUCTIONS
    guidance = (os.environ.get('LG_WORKFLOW_GUIDANCE', '') if guidance is None else guidance).strip()
    if guidance:
        prompt += "\n\nAuthor's latest instructions for this workflow attempt:\n" + guidance
    return StageInput(prompt, instructions)
