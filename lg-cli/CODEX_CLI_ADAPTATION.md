# LiteraryGiant CLI Adaptation

The Python `literary` CLI borrows the useful interaction shape of Codex CLI while remaining an LG-owned product surface. It does not fork the interactive Codex UI, does not expose Codex login, and does not patch `core/codex` branding or product behavior. `lg` remains a compatibility alias.

Implemented surfaces:

- responsive startup dashboard and persistent prompt history
- natural-language routing plus explicit literary commands
- streaming `RunEvent` progress and JSONL mode
- `--dry-run`, model profile override, staged/single-pass selection, and explicit `--raw`
- `literary doctor`
- local story projects, Story Bible state, scene approval, and candidate version history
- `literary run list`, `literary run show`, and `literary run resume`
- durable artifacts, stage prompts, handoffs, errors, and exit codes
- installable wheel with bundled catalogs, schemas, and core pin

The CLI depends only on LG contracts (`ProjectStore`, `StoryWorkflowService`, `WorkflowRunner`, `KnowledgeGateway`, `DefinitionRegistry`, `RunStore`, and `ModelAdapter`). A later Rust or richer TUI implementation can preserve those contracts, the SQLite project schema, and the on-disk run schema.
