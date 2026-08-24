# LiteraryGiant Agent

LiteraryGiant (`LG`) is an LG-owned literary agent runtime that uses Codex as an updateable inference engine. Product behavior lives outside `core/codex`; the vendored core remains byte-for-byte verifiable against `CODEX_CORE_COMMIT`.

## Repository Boundary

- `core/codex/` - pristine vendored Codex source
- `lg-cli/` - installable `literary` CLI, project store, adapter, runners, events, and knowledge gateway
- `lg-agent/` - task modes and declarative workflows
- `lg-skills/` - `SKILL.md` skills with manifests
- `lg-subagents/` - independent prompts, tools, model profiles, and output schemas
- `lg-config/`, `lg-context/`, `lg-tools/`, `lg-memory/`, `lg-output/` - product policy and conventions

## Install And Start

```bash
python -m pip install ./lg-cli
literary init
literary doctor
literary
```

`lg` and `literarygiant` remain compatibility aliases. New documentation and workflows use `literary`.

Run from the checkout without installing:

```bash
PYTHONPATH=lg-cli python -m lg_cli init
PYTHONPATH=lg-cli python -m lg_cli outline --dry-run "写一个都市重生大纲"
```

LG never opens a Codex login flow. Configure one of these environment variables for real model runs:

```bash
export LITERARYGIANT_API_KEY="..."
# LG_API_KEY, CODEX_API_KEY, and OPENAI_API_KEY are also accepted.
```

Prompt text is sent to `codex exec` over stdin. The child receives the key as `CODEX_API_KEY` inside an isolated `.literarygiant/codex-home`.

## Story Project Workflow

Initialize the current directory, or create a separately registered novel project:

```bash
literary init
literary project create "北城" --path ./north-city
literary -C ./north-city status
```

The local `.literarygiant/story.sqlite3` stores documents, accepted text, candidate versions, scene cards, Story Bible facts, proposals, timeline entries, foreshadowing, and review doubts. Generated text never replaces the active manuscript automatically.

```bash
literary chapter create chapter-1 "第一章"
literary scene create north-gate "北门受阻" --chapter chapter-1 --goal "进城" --conflict "守卫拒绝放行"
literary scene plan north-gate "强化身份暴露风险"
literary scene approve north-gate
literary scene draft north-gate
literary version list north-gate
literary version diff north-gate 1 2
literary version accept north-gate 2
```

Potential facts from model output enter the proposal queue. Only the author promotes them:

```bash
literary bible proposal list
literary bible proposal accept <proposal-id>
literary review consistency
literary export manuscript --format docx
```

## Agent Workflows

The real staged workflows are:

```text
literary outline     literary world       literary character
literary plot        literary write       literary check       literary ref
```

Each stage resolves an independent subagent, skill, model profile, tool declaration, prompt, and strict output schema. `WorkflowRunner` emits `RunEvent` records while persisting prompts, stage outputs, metadata, and events under `.literarygiant/runs/<run-id>/`.

Useful commands:

```bash
literary "帮我设计一个世界观"       # natural-language routing
literary outline --dry-run "方向"    # inspect without a model call
literary outline --raw "方向"        # explicitly enable capped raw retrieval
literary run list
literary run show <run-id> --events
literary run resume <run-id>
literary doctor
```

Failures return nonzero exit codes and remain resumable. LG does not write a fake successful artifact when the key, runtime, model call, or structured output fails.

## Knowledge Order

`KnowledgeGateway` retrieves in this order:

1. `Library/AbstractLibrary`
2. project `ReferenceLibrary`
3. `Library/BridgeIndex` as the safe index for Bridges
4. `Library/TaciturnRaw` only after explicit config or `--raw`, capped per run

References are passed to subagents as untrusted evidence with provenance. The workflow instructs agents to abstract mechanics and avoid copying source expression.

## Codex Core Updates

The parent `LiteraryGiant` repository tracks `LiteraryAgent` as a Git submodule. Inside this repository, `core/codex` is a vendored subtree-style prefix, not another submodule.

```bash
scripts/update_codex_core.sh --verify  # local pin/tree verification
scripts/update_codex_core.sh --check   # fetch and compare upstream/main
scripts/update_codex_core.sh --apply   # stage an update from a clean worktree
```

See [docs/cli-workflows.md](docs/cli-workflows.md), [docs/architecture.md](docs/architecture.md), and [docs/codex-core-updates.md](docs/codex-core-updates.md).
