# Persistent Provider Profiles

LG profiles are independent from production Codex login and shell startup files.
Commands run in the installed LitIsLand environment. Company API profiles need no
account login. ChatGPT subscription profiles use an explicit official Codex login
in a separate LG auth directory. See [model providers](model-providers.md).

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
New processes automatically use its active profile. In the Python writing terminal,
`/auth use NAME` reloads configuration for the next turn; an already running turn
keeps its existing provider. The native TUI requires restarting to switch providers.
Old profiles and arbitrary new names without `--provider` retain DeepSeek defaults.
Recognized provider names such as `glm` and `stepfun` select their respective
presets. Profiles bind key, endpoint, protocol, and model together and override
inherited model keys and endpoints. Key rotation preserves the existing settings.

Use `--environment NAME` before the command for separate spaces:

```bash
literary --environment experiment auth add personal
literary --environment experiment auth list
literary --environment experiment -C /path/to/test-book doctor
```

An explicitly selected space never falls back to inherited model credentials or
inline project keys. Without an explicit space or active sandbox profile, legacy
environment-variable configuration remains available.

API credentials live outside the repository at
`~/.literarygiant/environments/NAME/credentials.json`, with directory permissions
700 and file permissions 600. Keys are plaintext, not encrypted; root and programs
running as the same user can read them. Do not commit or share these files or
include them in unencrypted backups. Listing profiles never displays key fragments.
The old DeepSeek-only format is read without rewriting; the next explicit change
saves the versioned format and preserves the existing profiles.

ChatGPT subscription credentials stay in
`~/.literarygiant/environments/NAME/codex-auth/PROFILE/auth.json` and are managed by
the pinned Codex executable. LG stores only the profile metadata, not a duplicate
OAuth token or browser cookie. API keys and subscription access never fall back
to one another. Re-login may update the selected profile's official auth cache.

Configuration isolation is not a security sandbox. Project data still follows
`-C`; use a separate workspace for experiments. The prepared sandbox workspace is
`~/.literarygiant/environments/sandbox/workspace`:

```bash
literary --environment sandbox -C ~/.literarygiant/environments/sandbox/workspace doctor --probe
literary --environment sandbox -C ~/.literarygiant/environments/sandbox/workspace
```

`--probe` explicitly makes a small model request, consuming API or subscription
usage as applicable. Adding API keys, switching and listing profiles do not make
network requests. `auth login` explicitly starts the official login network flow.
This does not complete the native TUI build or change the default writing interface.
