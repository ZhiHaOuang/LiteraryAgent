# LiteraryGiant Agent

LiteraryGiant, abbreviated LG, is a custom agent framework layered around a vendored Codex core.

`core/codex` contains the original Codex fork and should remain updateable. LG product logic lives outside core:

- `lg-cli/` - user-facing `lg` CLI
- `lg-agent/` - task modes, workflow definitions, and core adapter notes
- `lg-prompts/` - prompt templates
- `lg-context/` - reference and memory context design
- `lg-skills/` - LG skill catalog
- `lg-subagents/` - LG subagent catalog
- `lg-tools/` - tool policy and allowlist
- `lg-config/` - default LG config
- `lg-memory/` - memory templates
- `lg-output/` - output conventions

Run without installing:

```bash
PYTHONPATH=lg-cli python -m lg_cli --help
PYTHONPATH=lg-cli python -m lg_cli status
PYTHONPATH=lg-cli python -m lg_cli init
PYTHONPATH=lg-cli python -m lg_cli outline "写个都市重生爽文"
```

Install the CLI:

```bash
python -m pip install -e lg-cli
lg status
```

LG does not trigger Codex login. Configure a model key with `LITERARYGIANT_API_KEY`, `LG_API_KEY`, `OPENAI_API_KEY`, or `.literarygiant/config.toml`.

## Current Workflow Loop

`lg outline "<direction>"` now runs the first LG-owned workflow loop:

1. load LG config
2. read `.literarygiant/memory`
3. search `ReferenceLibrary`, `AbstractLibrary`, `Bridges`, `TaciturnRaw`, parent `Library/*`, and `.learnings`
4. build an LG prompt from modes, skills, subagents, memory, reference snippets, and tool policy
5. save `.literarygiant/logs/last_prompt.md`
6. attempt a non-interactive Codex core adapter when an API key is configured
7. otherwise write a structured degraded fallback
8. save `.literarygiant/output/outlines/outline-*.md`, `.literarygiant/output/outline.latest.md`, and `.literarygiant/logs/last_run.json`

The Codex source remains isolated in `core/codex`; LG workflow, prompt, memory, reference, skill, subagent, and CLI code stays outside core so future Codex updates can be merged with less friction.

## Codex Core Updates

The parent `LiteraryGiant` repo tracks `LiteraryAgent` as a submodule. Inside `LiteraryAgent`, `core/codex` is a vendored Codex prefix, not a separate submodule.

Check upstream Codex status:

```bash
cd LiteraryAgent
scripts/update_codex_core.sh --check
```

After the current LG migration is committed and the worktree is clean, apply a core update with:

```bash
scripts/update_codex_core.sh --apply
```

Details live in `docs/codex-core-updates.md`.
