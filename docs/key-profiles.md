# Persistent DeepSeek Keys

LG profiles are independent from Codex login and shell startup files. Commands
run in the installed LitIsLand environment. No account login is required.

The sandbox environment configuration is stored at
`~/.literarygiant/environments/sandbox/config.toml`, outside Git. The portable,
secret-free template is `lg-config/sandbox.config.example.toml`. On another machine,
configure `[runtime].manifest` with that machine's verified runtime manifest path;
do not reuse a path from a different installation. The environment configuration,
credentials, and workspace database are intentionally not uploaded to GitHub.

```bash
literary auth add personal
literary auth add backup
literary auth list
literary auth use personal
literary auth add personal --replace
```

`add` prompts for a hidden key and activates the saved profile. Never put keys
in command arguments. Automation may use `--key-env VARIABLE_NAME` to read an
already supplied environment variable. The default profile space is `sandbox`.
New processes automatically use its active profile; running sessions keep their
existing key until restarted. Profiles currently target DeepSeek's Anthropic
endpoint and `deepseek-flash`. They override inherited model keys and endpoints.

Use `--environment NAME` before the command for separate spaces:

```bash
literary --environment experiment auth add personal
literary --environment experiment auth list
literary --environment experiment -C /path/to/test-book doctor
```

An explicitly selected space never falls back to inherited model credentials or
inline project keys. Without an explicit space or active sandbox profile, legacy
environment-variable configuration remains available.

Credentials live outside the repository at
`~/.literarygiant/environments/NAME/credentials.json`, with directory permissions
700 and file permissions 600. Keys are plaintext, not encrypted; root and programs
running as the same user can read them. Do not commit or share these files or
include them in unencrypted backups. Listing profiles never displays key fragments.

Configuration isolation is not a security sandbox. Project data still follows
`-C`; use a separate workspace for experiments. The prepared sandbox workspace is
`~/.literarygiant/environments/sandbox/workspace`:

```bash
literary --environment sandbox -C ~/.literarygiant/environments/sandbox/workspace doctor --probe
literary --environment sandbox -C ~/.literarygiant/environments/sandbox/workspace
```

`--probe` explicitly makes a small billable model request. Adding, switching and
listing credentials do not make network requests. This does not update the native
TUI build status or change the default interface.
