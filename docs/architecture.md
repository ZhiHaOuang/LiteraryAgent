# LiteraryGiant Architecture

## Ownership Boundary

LG owns orchestration, literary policy, persistence, and user experience. Codex owns the model execution engine.

```text
CLI / interactive session
        |
        +-> StoryWorkflowService -> ProjectStore -> story.sqlite3
        |          |                    +-> documents + candidate versions
        |          |                    +-> Story Bible + proposals
        |          |                    +-> scenes + timeline + foreshadowing + review doubts
        |          +-> RunEvent -> RunStore
        |
        +-> WorkflowRunner -> RunEvent -> RunStore
                   +-> DefinitionRegistry -> SKILL.md + subagent prompt/model/tools/schema
                   +-> MemoryContext
                   +-> KnowledgeGateway -> AbstractLibrary -> ReferenceLibrary -> BridgeIndex -> raw opt-in
        |
CodexExecAdapter -> codex exec (prompt over stdin, JSONL events, structured final output)
```

`core/codex` contains no LG branding or product patches. `CODEX_CORE_COMMIT` and `scripts/update_codex_core.sh --verify` prove that its committed tree matches the pinned upstream commit.

## Execution Model

1. CLI resolves explicit commands or routes natural-language intent.
2. `WorkflowRunner` creates `.literarygiant/runs/<run-id>/run.json`.
3. Memory and knowledge context are loaded once with bounded budgets.
4. Each workflow stage resolves one subagent and one skill.
5. A stage prompt includes the original request, bounded context, and prior handoffs.
6. `CodexExecAdapter` runs `codex exec` with the stage model profile and JSON schema.
7. JSONL engine events are wrapped as durable `RunEvent` records.
8. Valid stage output is stored before the next stage starts.
9. The final stage becomes a timestamped artifact and `<command>.latest.md`.
10. Failed runs retain completed stages and can be resumed into a new linked run.

The default strategy is `staged`. `single-pass` is available as an explicit lower-cost configuration and collapses the workflow into its final synthesizer.

Story operations add a human-gated state machine around model execution:

1. A scene card is created from explicit author constraints.
2. `scene plan` creates a candidate plan; it cannot authorize prose generation.
3. `scene approve` records the author's decision.
4. `scene draft` creates an inactive candidate document version.
5. `version diff/accept/reject/restore` controls manuscript activation and history.
6. Durable facts emitted by a model become proposals; only explicit author commands can promote them to Canonical.

## Durable State

```text
.literarygiant/
  project.json
  story.sqlite3
  config.toml
  history
  memory/
  output/
    candidates/
    exports/
  runs/<run-id>/
    run.json
    events.jsonl
    stages/<stage-id>.prompt.md
    stages/<stage-id>.md
    stages/<stage-id>.json
  logs/agent.log
  logs/failures.md
```

`story.sqlite3` is local-first and contains revision history instead of destructive overwrites. Memory updates are proposed by the workflow but are not silently committed to canon. References are untrusted evidence. Original source text remains disabled unless the user explicitly opts in.

## Adapter Contract

The stable adapter surface is `codex exec`, not a patch inside Codex. It:

- probes every executable candidate with `--version`
- sends prompts only through stdin
- uses an isolated mode-`0700` `CODEX_HOME`
- forwards JSONL events
- reads the authoritative `--output-last-message`
- applies per-subagent model profiles and output schemas
- terminates process groups on timeout
- preserves stderr and nonzero process exit codes

Build-on-demand from vendored Rust source is disabled unless `LG_CODEX_BUILD_ON_DEMAND=1`; an unbuilt source tree is not reported as a healthy runtime.

## Extension Points

- Add a skill through `SKILL.md`, `manifest.toml`, and `skills.json`.
- Add a subagent through `agent.toml`, `prompt.md`, and `output.schema.json`.
- Add stages declaratively in `lg-agent/workflows.json`.
- Add project-level story operations through `StoryWorkflowService` while keeping author approval in `ProjectStore` state transitions.
- Override skills or subagents per project under `.literarygiant/skills` and `.literarygiant/subagents`.
- Implement another model adapter against the `ModelAdapter` protocol without changing workflows.
