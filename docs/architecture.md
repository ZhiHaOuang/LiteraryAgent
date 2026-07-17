# LiteraryGiant Agent Architecture

LiteraryGiant, abbreviated LG, is the custom agent framework for this project.

## Layers

1. LG UI Layer: `lg-cli`
2. LG Agent Layer: `lg-agent`, `lg-prompts`, `lg-skills`, `lg-subagents`
3. LG Context Layer: `lg-context`, `lg-memory`, `.literarygiant/memory`
4. LG Tool Layer: `lg-tools`
5. LG Adapter Layer: `lg-agent/core_adapter.md` and `lg-cli/lg_cli/core_adapter.py`
6. Codex Core Layer: `core/codex`

## Boundary Rule

`core/codex` is an updateable vendor engine. Do not put LiteraryGiant workflows, prompts, subagents, or reference-library logic inside it.

## Current Integration Status

The scaffold is runnable. `outline` now has the first LG-owned workflow loop:

- display the LG dashboard
- initialize `.literarygiant/`
- list skills, subagents, and modes
- show status
- read memory and parent reference libraries
- build and save `.literarygiant/logs/last_prompt.md`
- attempt a non-interactive Codex process adapter when an API key is configured
- write degraded fallback output when no key or core executable is available
- save `.literarygiant/logs/last_run.json` and timestamped outline outputs
- enter workflow stubs for world, character, plot, write, ref, check, chat, and code

The adapter deliberately avoids login. LG validates configuration first and passes credentials through environment variables.

## Next Integration Step

Promote the remaining modes into dedicated workflow modules:

1. `world` - memory/reference/prompt/core/output loop for world bibles
2. `character` - cast and relationship design loop
3. `write` - chapter drafting loop with memory update proposals
4. `check` - continuity audit loop
5. `ref` - reference retrieval and abstraction loop

Keep `core/codex` as an updateable vendor engine. New LG behavior should live in `lg-cli`, `lg-agent`, `lg-skills`, `lg-subagents`, `lg-tools`, and project `.literarygiant` state.
