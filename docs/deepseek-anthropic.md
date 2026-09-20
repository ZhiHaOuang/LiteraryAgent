# DeepSeek Anthropic Integration

## Current Status

Implemented and tested locally:

- Provider-scoped configuration and credentials, with DeepSeek Anthropic as the
  default for newly initialized projects.
- Codex Responses to Anthropic Messages conversion outside the pristine core.
- Streaming text, function tools, custom text tools, tool results, namespace
  preservation, structured final answers, usage accounting, and failure handling.
- A local authenticated bridge that keeps the real API key out of Codex children.
- A model catalog containing only the configured LG models. Thinking is disabled;
  the 64,000-token context budget is an LG working limit, not a provider capacity claim.
- Real Codex 0.154.0 integration tests against simulated Anthropic streams,
  including a tool roundtrip and calls to the real LG writing MCP service.
- Direct prose editing with atomic stale-version checks, version history, exact
  diffs and restoration. Setting changes remain proposals, not silent Canonical edits.

Not yet verified: live DeepSeek generation (no API key was available), a completed
LG native TUI build, interactive approval/recovery behavior in that build, and
end-to-end specialist orchestration. The default `literary` UI is still the
existing Python interface. `literary native` is a gated launcher, not a fallback
to the unmodified Codex interface.

## Configure and Check

The project model settings are:

```toml
[model]
provider = "deepseek-anthropic"
base_url = "https://api.deepseek.com/anthropic"
default = "deepseek-flash"
writer = ""
critic = ""
coder = ""
max_output_tokens = 8192
```

Set `DEEPSEEK_API_KEY` in the environment. Do not put the key in command arguments,
source control, story databases, or chat messages. An interactive Bash prompt can
read it without echoing it or recording the value in shell history:

```bash
read -rsp 'DeepSeek API key: ' DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
printf '\n'
literary -C /root/private_data/LiteraryGiant/LiteraryAgent doctor
literary -C /root/private_data/LiteraryGiant/LiteraryAgent doctor --probe
```

Normal doctor checks do not call the model. `--probe` explicitly invokes one
small request through Codex and the bridge, with a 64-token output limit.
It exits unsuccessfully if credentials are missing or the roundtrip fails.

`LG_PROVIDER`, `LG_MODEL`, and `LG_BASE_URL` can override provider settings.
Changing providers via the environment discards the previous provider's model
profiles, endpoint, and compatibility config key. DeepSeek accepts
`DEEPSEEK_API_KEY`, or `ANTHROPIC_API_KEY` for compatibility; explicit
`LITERARYGIANT_API_KEY` and `LG_API_KEY` take precedence. OpenAI keys are not used.

For a version-locked runtime, add the installer-generated manifest path:

```toml
[runtime]
manifest = "/opt/conda/envs/LitIsLand/lg-runtime/rust-v0.154.0/runtime.json"
```

`LG_CODEX_RUNTIME_MANIFEST` overrides this path. A configured invalid manifest
fails closed rather than falling back to a different installed Codex version.

## Native UI Overlay

The native UI uses the upstream standalone `codex-tui` entry point, without the
top-level Codex login subcommand. `native-ui/patches/` changes the main brand
labels, disables logout, and rejects providers that require OpenAI account login.
Other upstream UI wording still requires a broader writing-focused review.

Use Python 3.12 and Rust 1.95.0 in the isolated `LitIsLand` environment. Prepare
a fresh directory without touching core:

```bash
python scripts/build_native_ui.py --destination /path/to/lg-native-build --prepare-only
python scripts/build_native_ui.py --destination /path/to/lg-native-build --resume --jobs 1
```

The builder verifies source and patch checksums on resume. For this upstream
release it repairs only local workspace package versions in Cargo.lock, rejects
any third-party dependency change, then builds with `--locked`. It marks the
runtime `built` only after successful compilation and a version probe.

After a successful build:

```bash
literary -C /path/to/novel native --manifest /path/to/lg-native-build/native-runtime.json
```

The launcher checks core identity, binary checksum, and available patch sources.
It connects the writing MCP service, disables shell tools and plugin discovery,
and uses the project's separate Codex home for native conversations. Its explicit
tool allowlist authorizes project reads, author-directed prose writes and undo,
reference search, and setting proposals. It does not authorize raw source access
or Canonical overwrites. Native conversation recovery still needs TUI acceptance
testing after compilation; workflow run recovery is a separate mechanism.

## Limitations

- This bridge intentionally rejects images, opaque reasoning history,
  provider-side web search, unsupported content types, and response-ID-only
  continuation. It sends full conversation input on each request.
- Thinking is explicitly disabled. Text/tool support does not imply complete
  Anthropic feature compatibility.
- Structured answers use a schema tool because DeepSeek's Anthropic interface
  does not support `output_config.format`; LG validates the resulting JSON.
- Upstream errors and truncated streams fail the turn; they are not accepted as
  successful chapter output. Raw upstream exception bodies are not logged.
- An interrupted native build is not a usable native application. The available
  container was limited to 0.5 CPU and 2 GiB memory; compilation was interrupted
  before a binary was produced. Prepared builds remain explicitly unactivated.

## References

- [DeepSeek Anthropic compatibility](https://api-docs.deepseek.com/guides/anthropic_api/)
- [Anthropic streaming protocol](https://platform.claude.com/docs/en/build-with-claude/streaming)
- [MCP Python SDK v1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
