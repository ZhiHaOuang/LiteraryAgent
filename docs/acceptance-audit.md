# Requested-version acceptance audit

Final acceptance: 2026-09-23. The three feature groups and a second independent
book are complete. This verifies the workflow, not perfect literary quality.
The author explicitly delegated adjudication of the reviewed final-chapter issue.

## 1. Categorized resources and persistent specialists

- The real `Library/AbstractLibrary/library_index.json` identifies EventsLibrary,
  PayoffAngst, CharacterArc, EmotionRhythm and Worldview. Their category indexes
  contain 14, 12, 10, 16 and 2 records respectively.
- Live KnowledgeGateway searches with each category filter returned three hits
  in the requested category, without raw-text retrieval. SHA-256 snapshots of
  the shared library, pattern and instance indexes were identical before/after.
- `tests/test_knowledge.py` separately verifies AbstractLibrary-before-Bridge
  ordering, raw-reference opt-in, source-locked field exclusion and distinct
  instance identities. These tests do not imply semantic correctness of analysis.
- The new book has 60 current chapter reports and five current volume reports, stored under
  its own `.literarygiant/analysis/`. `book_analysis.py` binds citations to source
  versions and checks freshness; `supervision.py` checks the configured categories
  before acceptance and publishes only accepted-version reports.
- **Residual risk:** source-valid model reports can still be semantically wrong.
  Explicit adjudication now records reviewer, reason, original report and exact
  project/source/report identity. Only the specified finding is dismissed;
  changed sources invalidate the decision. Original reports remain unchanged.
  This is an operator decision record, not an authenticated reviewer identity.

## 2. Per-chapter assets and bidirectional author editing

- Book assets are outside the agent repository, in `Projects/BeyondTheTide/`:
  `manuscript/chapters/`, `ReferenceLibrary/` and `.literarygiant/` have distinct
  responsibilities. Shared source libraries remain external and read-only.
- Actual `literary chapter status` reports all twelve mirrors clean. All twelve
  scenes are accepted and nonempty, including final-chapter version 67.
- `tests/test_chapter_sync.py` covers version-preserving author import,
  database-to-file refresh, simultaneous-edit conflicts, unchanged markers,
  cross-project baseline rejection, atomic multi-part import and EOF normalization.
- The production database is authoritative; importing an author's edited chapter
  is explicit, versioned and conflict checked. It is not an automatic file watcher.
- The book's `exports/` contains the full MD, TXT and DOCX manuscript, plus Bible,
  timeline and foreshadowing exports. All twelve scenes occur in order in each
  manuscript format and match database content after whitespace normalization.
  DOCX ZIP integrity and document XML were also checked.

## 3. Read-only commands and return navigation

- All seven real CLI views ran successfully with `browse <topic> --json`:
  characters (71 entries), events (83), storylines (6), timeline (24), world (76),
  chapters (12), analyses (65). Counts include labeled chapter-level analysis
  observations, not that many unique people/world rules. Timeline combines twelve
  actual checkpoints and twelve explicitly labeled scene schedules.
- Conversation-file hashes, run-directory IDs, active document versions and
  Canonical facts were identical before and after all seven commands.
- `project_browser.py` has no model call in its read path. `tests/test_project_browser.py`
  checks project-local results, no conversation append, detail-to-list-to-exit
  navigation, and a real prompt_toolkit application with injected scrolling/Escape
  input that restores the previous transcript. This last check is automated TUI
  behavior evidence, not a new visual screenshot review.
- A real CLI PTY session at 100 columns by 35 rows additionally passed analysis
  list -> detail -> Escape -> list -> Escape -> chat -> quit. It exited normally
  without adding runs or changing conversation files.
- `/character` and `/plot` remain generation commands; `/characters`, `/events`,
  `/storylines`, `/timeline` and `/browse` are separate read-only views.

## Final verification and delivery

All 217 native-enabled tests passed in LitIsLand (59.578 seconds). A freshly built,
isolated installed wheel passed 37 targeted tests (9.110 seconds); import paths
were checked to ensure tests used the installed package. Its actual CLI also
reported the bidirectional-edit fixture clean. Doctor and `git diff --check`
passed. Package hash and installation evidence are recorded in `book-assets.md`.

The accepted book contains twelve chapters and 28,322 Chinese characters,
excluding punctuation, digits and titles. Final chapter v4 (ID 67) was accepted
through the normal CLI after one explicit, user-authorized adjudication:
PayoffAngst run `20260923T103536Z-b2681783`, finding 1, incorrectly treated a
three-week-later suspension with explicit non-conviction as same-day conviction.
The original finding is retained alongside its decision receipt. No Canonical
rule was changed and no other review was waived.

All five final volume reviews are current, with source evidence across the book
and no unresolved conflicts. The thirteen Canonical rules match the original
production plan. Twelve timeline checkpoints and three paid threads are linked
to accepted sources. The earlier book, TheStartPoint, retains its content and
active-version hash unchanged.

GLM 4.5-air completed subsequent production and volume review. Other configured
GLM models passed smaller probes but are not equally reliable for structured
workflows. Production included documented editorial corrections, not unattended
generation. Some review summaries still use an old working title or imperfect
interpretations; these are reference opinions, not Canonical facts.

Reading links, run IDs, hashes and limitations are recorded in
`Projects/BeyondTheTide/DELIVERY.md`. Detailed history and editorial provenance
remain in that book's `PRODUCTION.md` and `ReferenceLibrary/`.
