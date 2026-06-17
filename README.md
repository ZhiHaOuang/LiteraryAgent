<p align="center"><strong>LiteraryAgent</strong> is a modified agent workspace based on the open-source OpenAI Codex CLI.</p>

<p align="center">
  <img src="https://github.com/openai/codex/blob/main/.github/codex-cli-splash.png" alt="OpenAI Codex CLI splash" width="80%" />
</p>

LiteraryAgent is not an OpenAI product. It keeps OpenAI Codex as its upstream engine while adding a separate product identity for literary-agent workflows, monitors, skills, tools, and future internal agent changes.

---

## Fork Status

- Product name: LiteraryAgent
- Upstream source: <https://github.com/openai/codex>
- Active development branch: `literary-agent`
- License: Apache-2.0. Original OpenAI notices are preserved in `LICENSE` and `NOTICE`.

Internal Rust crate names and some implementation paths still use `codex-*` names. Those are treated as engine internals for now so this fork can keep merging upstream changes with manageable conflicts.

## Quickstart

### Build from source

```bash
cd codex-rs
cargo build --bin literary-agent
```

Run the CLI locally:

```bash
cargo run --bin literary-agent -- --help
```

The upstream Codex install scripts and release packages are intentionally not documented here because this fork should be installed and distributed under the LiteraryAgent name.

### Authentication

The underlying engine still uses OpenAI Codex authentication flows. During the early fork phase, expect some prompts, configuration keys, and internal paths to retain upstream naming.

## Development Direction

- Keep upstream Codex as the mergeable engine baseline.
- Add LiteraryAgent-specific orchestration, monitoring, skills, tools, and policies around the engine.
- Modify Codex internals only when the public extension surfaces are not enough.
- Keep rebranding patches explicit so upstream sync conflicts are easy to review.

## Docs

- [Fork notes](./FORK.md)
- [Installing & building](./docs/install.md)
- [Upstream Codex documentation](https://developers.openai.com/codex)
- [Upstream contributing guide](./docs/contributing.md)

This repository is licensed under the [Apache-2.0 License](LICENSE). LiteraryAgent is a modified fork and is not affiliated with or endorsed by OpenAI.
