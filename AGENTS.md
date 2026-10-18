# LiteraryGiant Agent Guidance

This repository root is the LiteraryGiant product layer.

`core/codex` is vendored Codex core. Keep it updateable and avoid product logic inside it. If a Codex core patch is unavoidable, make it minimal and document the reason in `core/patches`.

Codex is a frozen upstream baseline. `scripts/update_codex_core.sh --check` without `LG_CODEX_REF` verifies locally without fetching. An upstream comparison or update requires an explicitly selected `LG_CODEX_REF`; apply only from a clean `LiteraryAgent` worktree. Preserve the pristine prefix and register derivatives separately. The update path is documented in `docs/codex-core-updates.md`.

LG product logic belongs in:

- `lg-cli/`
- `lg-agent/`
- `lg-prompts/`
- `lg-context/`
- `lg-skills/`
- `lg-subagents/`
- `lg-tools/`
- `lg-config/`
- `lg-memory/`
- `lg-output/`
- `docs/`

LG is API-only. Do not start ChatGPT subscription OAuth, browser/device login, or reactivate legacy subscription profiles. Retain migration messages and preserve existing auth caches without reading or copying their tokens. API keys, provider configuration, and request authentication remain supported; never silently switch providers or authentication methods. Runtime selection requires a verified manifest or an explicit development command; do not restore automatic PATH, Node-wrapper, or Cargo-build fallback.

Validate provider-scoped credentials before delegation. API profiles bind the provider, protocol, endpoint, and model to their key; never fall back across providers. Keep real API keys out of Codex child environments; use the process-local bridge token instead. Stored credentials and subscription auth caches must stay outside Git.

Use `KnowledgeGateway` for reference retrieval. Search `AbstractLibrary` first, then the project reference library and `BridgeIndex`. Never scan `Bridges` directly, and require explicit opt-in before reading `TaciturnRaw`.

Advance Runtime trimming and the Literary Agent Execution Model in parallel. Trim retired product integrations, duplicate entry points and their exclusive dependencies to reduce maintenance coupling; origin in a coding agent is not a deletion criterion. Preserve the general model/tool loop, tool registration and scheduling, sessions/history, concurrency, search, file and diff editing, constrained scripts, isolation, approvals, cancellation and recovery. Adapt domain defaults in the product strategy layer instead of removing their underlying execution mechanisms.

Distinguish capability implementation, release inclusion and per-task authorization. Keeping a tool does not grant every task access to it. Formal story changes must pass the Kernel's version, scope and author-authorization checks; generic file or shell tools must not bypass those checks. Keep unsafe execution paths unavailable to writing tasks until isolation is verified. Each trim batch must record exact removals, disconnected dependencies, build/runtime verification, the artifact used by CLI/Extension, and rollback. Verify an unchanged pinned-source Rust build before changing Rust dependencies; independent product-layer removals need their own relevant build, not completion of all R2 work. Do not assume the Rust TUI is approved for removal. Obtain approval for concrete file deletions before applying them.

Prioritize the product-owned Literary Agent Execution Model: task interpretation and strategy, bounded orchestration, semantic tool contracts, narrative context, story-state transitions, author control, and observable outcomes. Place new behavior in the existing product orchestration and domain services first. Skills and subagent prompts are supporting strategies, not substitutes for host-enforced permissions, information visibility, or state transitions. Do not add LG behavior to `core/codex`.

Use `docs/architecture.md` for current responsibilities and `docs/roadmap.md` for verified progress and the next architecture milestones. Distinguish implemented behavior, tested guarantees, and proposed contracts. Measure narrative consistency, author control, context/tool cost, recovery, and literary quality rather than counting skills or subagents. Select execution depth by task scope; a local wording edit must not require a full-book workflow. Do not depend on access to private model reasoning; persist goals, constraints, candidates, evidence, concise decision summaries, and outcomes instead.

Treat world facts, character beliefs, reader knowledge, and author plans as distinct semantics when designing context and state changes. Story time and reading order are separate. Generating or comparing plot options does not authorize selecting an official direction or changing long-term plans. Preserve existing explicit author authorization policies, and bind consequential decisions to concrete candidates and source versions. Enforce deterministic state and authorization constraints while keeping aesthetic judgments reviewable by the author.

Evolve LG through verified, minimal changes. Preserve the existing React UI while gradually routing CLI and Extension through the same Literary Backend contract. Literary state and authorization belong to the product; runtimes are replaceable execution adapters. External projects supply selected capabilities, never another authoritative story store. Refactor only for a concrete requirement or a reproduced technical limitation, preserve compatible interfaces where possible, and validate changes in LitIsLand before applying them to the project.

Agent-generated manuscript changes are candidates until creative approval and adoption. Author edits made directly in an editor retain their separate versioned save path. Permission to generate or save a candidate does not authorize adopting it or promoting its facts to Canonical.

File reusable artifacts at creation, not in a later cleanup pass. Workflows route outlines/plots/scene plans to `ReferenceLibrary/plans/`, world/character notes and memory proposals to `ReferenceLibrary/bible/`, critiques to `ReferenceLibrary/reviews/`, reference briefs to `ReferenceLibrary/sources/research/`, and prose candidates to `ReferenceLibrary/drafts/`. Version-validated analyses belong in `ReferenceLibrary/analyses/`. Useful intermediate specialist results follow the same routing and remain explicitly provisional. Filing does not promote Canonical facts or accepted manuscript. Conversations, dry-run previews, database, runs and recovery state remain in `.literarygiant/`. Legacy files require explicit `project organize --apply` to relocate, but newly generated artifacts must already use the classified paths. See `docs/book-layout.md`. Do not recreate empty placeholder documents or eagerly populate output directories.

Do not move, delete, or rewrite user data libraries while working on LG agent code.
