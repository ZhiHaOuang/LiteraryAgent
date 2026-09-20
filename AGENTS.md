# LiteraryGiant Agent Guidance

This repository root is the LiteraryGiant product layer.

`core/codex` is vendored Codex core. Keep it updateable and avoid product logic inside it. If a Codex core patch is unavoidable, make it minimal and document the reason in `core/patches`.

For upstream Codex updates, use `scripts/update_codex_core.sh --check` first and `scripts/update_codex_core.sh --apply` only from a clean `LiteraryAgent` worktree. The update path is documented in `docs/codex-core-updates.md`.

LG product logic belongs in:

- `lg-cli/`
- `lg-agent/`
- `lg-prompts/`
- `lg-context/`
- `lg-skills/`
- `lg-subagents/`
- `lg-tools/`
- `lg-config/`
- `lg-memory/`
- `lg-output/`
- `docs/`

LG CLI must not trigger Codex login. Validate provider-scoped credentials before delegation: `DEEPSEEK_API_KEY` for DeepSeek Anthropic, `ANTHROPIC_API_KEY` for Anthropic, or explicit `LITERARYGIANT_API_KEY`/`LG_API_KEY` overrides. Never forward OpenAI credentials to an Anthropic endpoint. Keep real provider keys out of Codex child environments; use the process-local bridge token instead.

Use `KnowledgeGateway` for reference retrieval. Search `AbstractLibrary` first, then the project reference library and `BridgeIndex`. Never scan `Bridges` directly, and require explicit opt-in before reading `TaciturnRaw`.

New literary behavior belongs in declarative workflows, `SKILL.md` definitions, independent subagent prompts/schemas, or the outer Python runtime. Do not add LG behavior to `core/codex`.

Runtime generated story artifacts should default to `.literarygiant/output/`. Durable story memory should default to `.literarygiant/memory/`.

Do not move, delete, or rewrite user data libraries while working on LG agent code.
