# LG CLI Adaptation

The LG CLI is intentionally outside `core/codex`.

It starts with a Codex-like command surface:

- default interactive dashboard
- `chat`
- `code`
- `status`
- `skills`
- `agents`
- task commands with positional prompts

The first implementation is Python to keep the scaffold runnable and easy to iterate. The command model mirrors Codex CLI concepts, while the real Codex engine remains in `core/codex`. A later Rust CLI can reuse these command contracts and delegate to Codex exec/app-server through the same adapter boundary.
