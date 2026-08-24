# LG Core Adapter

`lg-cli/lg_cli/core_adapter.py` is the only model-engine boundary used by LG workflows. It delegates to the stable non-interactive `codex exec` surface while keeping every LG prompt, skill, workflow, subagent, and persistence rule outside `core/codex`.

## Contract

- no login command or inherited Codex session
- API key supplied by environment and mapped to child `CODEX_API_KEY`
- prompt supplied on stdin, never argv
- isolated `.literarygiant/codex-home`
- candidate `--version` health probes
- JSONL event forwarding
- authoritative final response through `--output-last-message`
- optional strict `--output-schema`
- read-only sandbox for literary modes; workspace-write only for explicit code mode
- timeout and process-group termination
- nonzero child status preserved as workflow failure

Candidate order is explicit `LG_CODEX_COMMAND`, built vendored binaries, vendored Node wrapper, installed `codex`, then an opt-in cargo build fallback. Source presence alone is not runtime health.

Future app-server or SDK adapters can implement the same `ModelAdapter` protocol. They do not require changes to declarative workflows or subagent definitions.
