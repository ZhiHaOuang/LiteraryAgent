# LiteraryAgent Fork Notes

LiteraryAgent is a modified fork of the OpenAI Codex CLI repository.

## Upstream

- Upstream repository: https://github.com/openai/codex
- Fork repository: https://github.com/ZhiHaOuang/LiteraryAgent
- Upstream remote name in this checkout: `upstream`
- Fork remote name in this checkout: `origin`
- Local development branch: `literary-agent`

## Naming Policy

Use `LiteraryAgent` for the product and `literary-agent` for command-line, package, and repository names in user-facing surfaces.

Do not present this fork as OpenAI Codex or as an official OpenAI distribution. Keep original upstream license and notice files intact.

For now, internal Rust crates, modules, and workspace paths may still use `codex-*` names. Those names are engine internals and are intentionally preserved until a dedicated deep namespace migration is worth the maintenance cost.

## Upstream Sync

Fetch upstream changes with:

```bash
git fetch upstream
```

Merge upstream into the fork branch with:

```bash
git checkout literary-agent
git merge upstream/main
```

Use a merge when you want a visible sync history. Use a rebase only if the fork has not been shared or if the team explicitly agrees to rewrite branch history.
