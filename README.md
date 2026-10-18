# LiteraryGiant Agent

LiteraryGiant (`LG`) is a literary agent system with product-owned task scope, orchestration, story state, and author control. Its shared Literary Backend supports Codex and Native execution adapters; their capability coverage is still being aligned. Product behavior lives outside `core/codex`, which remains an updateable vendored runtime.

The next development priority is the Literary Agent Execution Model: narrative context, semantic tool contracts, creative decision authorization, and task-dependent execution strategies. Skills and subagents support these mechanisms. See the [current architecture](docs/architecture.md) and [verified progress and roadmap](docs/roadmap.md).

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

LG does not automatically open a Codex login flow during writing. Product authentication is API-only; legacy subscription login returns migration guidance; see [authentication profiles](docs/key-profiles.md). API-based runs can use a configured provider profile or these environment variables:

```bash
export LITERARYGIANT_API_KEY="..."
# LG_API_KEY, CODEX_API_KEY, and OPENAI_API_KEY are also accepted.
```

The selected Runtime adapter handles model transport. Codex stages use `codex exec`, while interactive sessions use the session adapter; Native has its own HTTP and MCP loop. Real provider credentials stay out of Codex child environments, which use the product's process-local bridge token. Legacy subscription caches are preserved in place but are not used by product entry points.

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

Potential facts from model output enter the proposal queue. Promotion follows the author's creative authorization policy and source-version checks:

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

The staged path resolves subagents, skills, model profiles, tool declarations, prompts, and output schemas. `WorkflowRunner` persists stage results and `RunEvent` records under `.literarygiant/runs/<run-id>/`. Interactive tool execution and scene operations are separate existing paths. Task-dependent selection across these strategies is an architecture milestone, not a completed guarantee.

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
scripts/update_codex_core.sh --check   # verify the frozen local baseline without fetching
LG_CODEX_REF='REPLACE_WITH_EXPLICIT_REF' scripts/update_codex_core.sh --apply  # clean worktree required
```

See [docs/cli-workflows.md](docs/cli-workflows.md), [docs/architecture.md](docs/architecture.md), and [docs/codex-core-updates.md](docs/codex-core-updates.md).

## Development handoff (2026-10-18)

This repository is pinned as the LiteraryGiant submodule. Clone the parent recursively for both Python projects, requirements, data placeholders and the complete handoff. In that checkout, read `../docs/handoff.md` and `../docs/local-development.md`; online, see the [parent handoff](https://github.com/ZhiHaOuang/LiteraryGiant/blob/main/docs/handoff.md) and [Mac/server workflow](https://github.com/ZhiHaOuang/LiteraryGiant/blob/main/docs/local-development.md).

Use Python 3.12 and the existing Extension lockfile. The pinned runtime installer currently supports Linux x86_64 only; Apple Silicon development does not imply ARM64 Codex runtime integration has been verified. Story workspaces, databases, credentials and conversation exports remain outside public Git.

Historical `/opt/conda/envs/LitIsLand/audits/` evidence was migrated to `/root/private_data/image-layer-migration-20261016/LitIsLand-audits.tar`; it is not a product dependency. The roadmap separates completed work from remaining runtime, context and literary-quality milestones.
