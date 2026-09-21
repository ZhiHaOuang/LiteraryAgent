# Model Providers and Authentication

In an interactive terminal, `literary auth add gpt` asks you to choose subscription,
paid OpenAI API, or an existing proxy. Cancelling does not start login or change the
active profile. Non-interactive calls must use an explicit provider/login command.

## Company APIs

```bash
literary auth add deepseek
literary auth add glm --model glm-5.3
literary auth add stepfun --model step-3.5-flash
literary auth use glm
literary auth model YOUR_MODEL_ID
literary auth list
literary auth status
```

`add` prints the destination and model before prompting for a hidden API key.
GLM and StepFun prompt for a model ID when omitted in an interactive terminal;
automated calls must supply `--model`. Example IDs are from official documentation,
not a guarantee of account access or a dynamically discovered model catalog.

Multiple keys or models for one provider use distinct profile names:

```bash
literary auth add glm-reviewer --provider glm --model YOUR_MODEL_ID
literary auth add glm-reviewer --replace
literary auth use glm-reviewer --model YOUR_OTHER_MODEL_ID
```

The replacement command rotates only the key unless new provider/model/endpoint
options are explicitly given. There is no automatic failover to another key,
provider or billing channel. Running turns finish with their starting profile.

| Provider | Protocol | Default Base URL |
| --- | --- | --- |
| deepseek | Anthropic Messages | https://api.deepseek.com/anthropic |
| glm | Anthropic Messages | https://open.bigmodel.cn/api/anthropic |
| stepfun | Anthropic Messages | https://api.stepfun.com |
| anthropic | Anthropic Messages | https://api.anthropic.com |
| openai | OpenAI Responses | https://api.openai.com/v1 |

StepFun's subscription channel is a different endpoint. Only when you intend to
use that channel, configure `--base-url https://api.stepfun.com/step_plan`.
Do not confuse ordinary API billing with a company's coding/subscription plan.

Provider-scoped environment keys are also supported when no saved profile is
selected: `DEEPSEEK_API_KEY`, `GLM_API_KEY` / `ZHIPU_API_KEY`,
`STEPFUN_API_KEY` / `STEP_API_KEY`, `ANTHROPIC_API_KEY`, and `OPENAI_API_KEY`.
Explicit `--environment NAME` does not inherit these production keys.

## ChatGPT Subscription

```bash
literary --environment sandbox auth login gpt
literary --environment sandbox auth status
literary --environment sandbox auth use gpt
```

This runs the already installed, verified Codex binary's `login --device-auth`.
Complete the displayed official login in your own browser. Device-code login may
need to be enabled in account/workspace security settings. `--browser` selects
the alternative browser callback flow. LG does not install a proxy or launch
login merely because a writing request lacks credentials.

The environment must contain `[runtime].manifest` pointing to the pinned runtime.
Login uses an LG-owned `CODEX_HOME`, with file-based credentials and the ChatGPT
login method explicitly enforced. It does not reuse `~/.codex`, browser cookies,
or production API keys. Codex handles subscription token refresh. Successful
cached-login verification is not a guarantee of live entitlement or quota.

Without `--model`, the subscription profile uses Codex's account default. Specify
only a model that the account supports; API-only models are not automatically
available on a subscription. Each account can have a separate profile name.
Changing accounts may separate native Codex session histories because each profile
has its own auth home; the novel database remains shared by project workspace.

For paid OpenAI API access instead, make that choice explicit:

```bash
literary auth add gpt-api --provider openai --model YOUR_API_MODEL_ID
```

## Existing CPA or Other Proxies

```bash
literary auth add gpt-proxy --provider responses-compatible --base-url http://127.0.0.1:8317/v1 --model YOUR_PROXY_MODEL_ID
```

The URL above is an example, not an installed service. Enter the proxy's own client
key, not a ChatGPT password, cookie, or refresh token. The proxy must serve streaming
`POST /responses`, tool calls, full conversation input and terminal completion
events. A Chat Completions-only proxy is not supported by this transport. Use
`anthropic-compatible` only for a proxy exposing Messages instead. CPA-specific
deployment, account import, refresh logic and endpoint variants require inspecting
the actual project's documentation; LG does not implement or install these here.

HTTPS is required except on loopback (`localhost`, `127.0.0.1`, `::1`). Endpoints
containing credentials, query parameters or fragments are rejected. Redirects are
not followed, inherited HTTP proxy settings are not used by LG's API bridges, and
upstream error bodies are suppressed. Model requests contain private writing data;
only configure endpoints you trust.

## Terminal Interface

```bash
literary auth
literary --environment experiment auth
```

The terminal manager uses plain ASCII numbered menus in the terminal's default
foreground/background, with no color codes, borders, full-screen display or arrow
key controls. Enter an item number to select it; zero or an empty menu selection
returns. Text prompts display defaults; Ctrl-C cancels. Key prompts disable echo.
It supports profile selection, adding providers, changing the model, replacing API
keys, and explicit ChatGPT login.
The existing Python writing terminal exposes `/auth` and `/model`; `/auth use NAME`
or `/model MODEL_ID` applies to the next turn while preserving `--environment`.

The separately built native LG TUI reuses Codex's model picker for its active
provider. API model catalogs contain configured models, not a fetched inventory.
Switching providers requires restarting that native session. This patch changes
the native runtime fingerprint, so an older prepared/build directory must not be
treated as a current verified build. Native UI compilation and live subscription
testing remain separate acceptance steps.

## Verification and Limits

```bash
literary --environment sandbox -C /path/to/test-book doctor
literary --environment sandbox -C /path/to/test-book doctor --probe
```

Only `--probe` sends a model request. Automated tests use simulated upstreams and a
real pinned Codex process for protocol/MCP roundtrips. They do not prove that your
key, subscription, quota, or private proxy is usable; test each desired profile
explicitly. Messages currently supports text, tools and validated JSON output;
images and opaque reasoning blocks are not supported. StepFun requests omit the
undocumented `thinking` and `tool_choice` fields and reject forced tool selection.
Truncated streams and invalid structured final output fail rather than succeeding.

Official references checked for this implementation:
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [GLM Anthropic compatibility](https://docs.bigmodel.cn/cn/guide/develop/claude/introduction)
- [StepFun Messages API](https://platform.stepfun.com/docs/zh/api-reference/chat/messages-create)
