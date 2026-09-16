# Native Writing Agent Refactor

Status: implementation in progress; not an acceptance report.

## Confirmed Product Contract

- Reuse the Codex native terminal UI, shortcuts, streaming, tool progress,
  session recovery, and interactive approvals.
- Replace product branding and login entry points. No LG account service.
- Use Anthropic API credentials. Never store API keys in story databases or logs.
- Focus tools and default instructions on fiction writing.
- Store the authoritative manuscript and Story Bible in the project database.
- Explicit editing requests update active text and preserve previous versions.
- Discuss conflicts with established facts before applying conflicting changes.
- Discuss large assignments such as volume planning before delegation; routine
  checks can execute directly. Show specialist progress through the main agent.
- Preserve each conversation separately and share project knowledge across them.
- Allow external-editor export and explicit import with stale-version detection.
- Track the latest stable Codex release, not main; lock source and executable.

## Implementation Boundaries

Keep core/codex byte-for-byte equivalent to the selected upstream release.
Maintain native UI modifications as reviewable LG patches outside that prefix.
Build the patched application in an isolated build tree. Record upstream commit,
patch identity, runtime version, platform, and artifact checksum.

Expose database operations through a writing tool service with real execution
checks. Prompt instructions alone are not authorization. Use optimistic version
checks for edits, atomic revision activation, and durable delegation decisions.

Anthropic requires a tested protocol adapter: the current Codex WireApi only
supports Responses. Preserve tool-call IDs, streaming lifecycle, cancellation,
errors, and conversation state across the adapter. Unsupported request features
must fail explicitly. Do not advertise compatibility from version probes alone.

## Acceptance Scenarios

1. Launch literary into the LG-branded native terminal without Codex login.
2. Discuss a novel over several turns, restart, resume, and retain prior choices.
3. Open a new discussion in the same project and retrieve established story facts.
4. Read selected reference material and propose a volume plan before delegation.
5. Display specialist progress and retain their results across interruption.
6. Draft the next chapter using prior chapters and relevant project knowledge.
7. Rewrite active prose directly, inspect the diff, and restore the old version.
8. Detect a proposed Canonical conflict and ask before changing the fact.
9. Export to an editor and import deliberately; reject stale imports.
10. Upgrade a stable Codex release in isolation and pass runtime, UI, provider,
    tool-call, persistence, and recovery tests before activating it.

Real Anthropic generation and interactive terminal acceptance remain pending
until exercised against a configured provider and the built native application.
