# LG Core Adapter

`core/codex` is the vendored Codex engine. LiteraryGiant product logic must stay outside it.

The first LG scaffold exposes a stub adapter in `lg-cli/lg_cli/core_adapter.py`. It verifies that Codex core exists and reports that the real model loop is not wired yet.

Planned adapter entrypoints:

- `lg_core_adapter.run_model_turn(mode, prompt, context, config)`
- `lg_core_adapter.run_code_task(prompt, workspace, policy)`
- `lg_core_adapter.apply_patch(patch, workspace, policy)`
- `lg_core_adapter.run_shell(command, workspace, policy)`
- `lg_core_adapter.read_file(path, budget)`
- `lg_core_adapter.search_files(query, roots)`

Integration order:

1. Keep `core/codex` buildable as its own upstream tree.
2. Use Codex exec/app-server public surfaces where possible.
3. Add a thin process adapter before adding Rust crate dependencies.
4. Only patch Codex core when a stable capability cannot be reached otherwise.
5. Record every core patch in `core/patches` with reason and replay instructions.
