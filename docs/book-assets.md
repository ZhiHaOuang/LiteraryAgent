# Book assets and author edits

## Confirmed design

The story database remains authoritative. Each chapter has an editable Markdown
mirror, and explicit import creates database revisions without discarding history.
Shared AbstractLibrary data is read-only; book-specific analysis belongs to the
book. Publishing patterns back to the shared library requires author confirmation.

The agreed supervision policy is relevant specialists per chapter and a full
review per volume. Only source-evidenced canon conflicts block acceptance;
stylistic recommendations do not. New books enable all five chapter specialists;
the per-book policy can select a smaller relevant subset. Full-volume review always
uses all five directions. Existing projects explicitly opt in with `analysis enable`.

Acceptance requires another independent complete book, roughly twelve chapters
and 30,000 Chinese characters, generated through LG. Production started with the
configured StepFun profile; the author approved GLM after StepFun returned HTTP 402.
The existing book must be preserved. This acceptance run completed on 2026-09-23:
`Projects/BeyondTheTide/DELIVERY.md` records the twelve-chapter, 28,322-character
book, five whole-book reviews, exports and one explicitly delegated adjudication.

## Chapter files

```text
Book/
  manuscript/chapters/<chapter-slug>.md
  ReferenceLibrary/
  exports/
  .literarygiant/
    story.sqlite3
    chapter-sync/<chapter-id>.json
    analysis/<chapter-slug>/<category>.json
    analysis/candidates/<version-id>/<category>.json
    analysis/volumes/<part-slug-or-__book__>/<category>.json
    supervision.json
    conversations/
    memory/
    runs/
```

The Markdown files are working copies of active chapter and scene text, including
active drafts. They are not publication exports. Publication exports retain the
existing accepted/final filtering. Candidate revisions do not replace active text.

```sh
literary -C /path/to/book chapter sync
literary -C /path/to/book chapter status
literary -C /path/to/book chapter diff chapter-1
literary -C /path/to/book chapter import chapter-1
```

The same commands work with `/chapter ...` inside the TUI. `sync` and `status`
accept an optional chapter slug; `diff` and `import` require one. Edit prose inside
the invisible HTML document markers. Keep the chapter heading, marker IDs and
marker order unchanged, so scenes remain separate database records.

Import validates the saved project identity, active versions and chapter structure.
All changed chapter/scene parts are committed in one transaction; unchanged parts
do not create versions. Previous versions remain available through `version list`,
`version diff`, and `version restore`.

Sync never overwrites externally edited or untracked files. If both sides changed,
import refuses the conflict. Use `chapter diff` and reconcile explicitly; no force
overwrite or automatic merge is provided. Preserve your edited copy before refreshing
from the database. A crash between database commit and baseline save can require
manual reconciliation, but does not silently discard author text.

Creating chapter/scene records, accepting versions and editing active text refresh
the corresponding mirror automatically after the database transaction commits.
An external edit or filesystem failure leaves the database saved and emits a
warning instead of replacing the file. Use `chapter status` to inspect it.

## Local browsing

`/browse` opens the project browser. `/characters`, `/events`, `/storylines` and
`/timeline` open the corresponding lists. `/browse world` and `/browse chapters`
are also available. Select an item to read it; Escape returns to the list, then
Escape or Back returns to the existing conversation. Browsing does not invoke a
model or create a conversation. Character/world facts come from Canonical memory;
documents show their state explicitly. Empty lists mean no matching records exist,
not that the model has inferred there are no characters or events.

For noninteractive use, `literary browse <topic> --json` returns structured entries.
`literary characters`, `literary events`, and `literary storylines` are aliases.
Generation commands `/character` and `/plot` remain separate.

## Source-linked analyses

`/analysis chapter chapter-1` calls the five existing specialists for CharacterArc,
EventsLibrary, PayoffAngst, EmotionRhythm and Worldview. `--category Worldview`
limits the operation to one direction. Each stage uses the specialist's own prompt
and model profile with a strict analysis schema. `/analysis list` and
`/browse analyses` inspect results without model calls.

Each report records the project, source versions, Canonical snapshot fingerprint
and run ID. Evidence quotations must occur exactly in the cited document version.
The model selects version-bound source-span IDs; the host resolves them to exact
original quotations. Unknown spans are rejected, and Canonical fact quotes are
resolved from the selected fact ID rather than rewritten by the model.
A claimed canon conflict must also quote an existing Canonical fact. This validates
the evidence, not the model's semantic interpretation; reports are not promoted
to canon automatically. Changed manuscript or Canonical memory makes old reports
stale. Reports whose source text is unchanged but whose Canonical context changed
are marked `needs-canon-review`. Their source-validated observations remain available
as labeled historical interpretations, but their findings cannot approve a candidate.
Writing never retrieves reports for changed source text.
Prompts and historical reports remain in their run directories.

### Explicit review adjudication

A false-positive Canonical finding can be dismissed by an explicitly authorized
reviewer, without changing the original report or Canonical facts:

```sh
literary -C /path/to/book analysis adjudicate chapter-12-main --version 4 \
  --category PayoffAngst --finding 1 --reviewer "Author" \
  --reason "Explain the source/fact comparison and why this is a false positive" --confirm
literary -C /path/to/book version accept chapter-12-main 4
```

Finding numbers are one-based indexes in `report.findings`. Each immutable receipt
retains the original review, reviewer, reason, date and reviewed-snapshot digest
under `.literarygiant/analysis/adjudications/`. Dismissal applies to exactly one
finding in that report. Other conflicts, missing/stale reports and Canonical
changes still block acceptance. A different report or source snapshot requires
new review; receipts are not blanket exceptions. The confirmation records an
operator decision, not an authenticated external identity or a model judgment.
Adjudication is not exposed as a writing-agent MCP tool.

For a current whole-book review, use `analysis adjudicate __book__ --volume`
with the same category, finding, reviewer, reason and confirmation flags.
`analysis list --json` and `/browse analyses` show adjudications alongside the
unchanged findings. Adjudicating does not itself accept manuscript text.

Manuscript exports default to the book's `exports/` directory, with timestamped
filenames. `--output` can select a stable delivery filename explicitly.

Writing context includes unresolved `open` and `planned` foreshadowing, bounded
to 40 entries with open threads prioritized. State labels are retained; planned
threads are intentions, not established events. Paid/abandoned threads and other
books' threads are excluded. This fixes a discovered gap where planned threads
appeared in browsing but were omitted from writing context. Two regressions cover
state preservation, isolation and the cap. Actual BeyondTheTide context includes
its three labeled planned threads. Full regression suite: 208 passing tests in
52.930 seconds after this change. The real chapter-10 drafting prompt also contains
the labeled planned threads. The wheel was rebuilt and revalidated after the fix.

For supervised books, `version accept`, `scene accept` and `run adopt --accept`
automatically review the candidate text before activation. A database-level check
revalidates source and Canonical fingerprints under a write transaction. Candidate
reports never replace active reports before acceptance. Missing, stale or conflicting
reviews fail closed. A current canon conflict is not silently retried; its report
path is shown for discussion or correction. Stylistic findings do not block.

`analysis volume` reviews the full book across chapters; `analysis volume <part>`
restricts it to an existing part. `analysis list --volume` inspects results.
Changed chapters, membership or Canonical memory make the volume report stale.
This is a model semantic review with validated citations, not a proof of consistency.

Validation checkpoint: 200 tests passed in LitIsLand after the source-span update
and alignment of the core SSE idle timeout with LG's request deadline.
TheStartPoint's twelve chapter
mirrors are clean, and an unchanged chapter import was exercised through the real
CLI. StepFun Worldview run `20260922T161656Z-75c1cd5b` completed with validated
chapter evidence after one bounded repair. Its earlier rejected output remains
in the run directory. BeyondTheTide chapter 1 has now passed all five specialists
and candidate v3 was accepted. The remaining chapters and full-book acceptance
are still pending. Chapter 2 candidate v4 is saved, but its first specialist review
returned provider HTTP 402; it was not accepted. The author subsequently approved
GLM. All five reviews subsequently completed after an SSE idle-timeout fix. One
reviewer flagged an attribution difference between Canonical and prose; another
considered them compatible. Acceptance stopped for an author decision, without
an automatic Canonical edit or review override. The author selected a prose-only
clarification. Candidate v6 then passed five new reviews and was accepted; only
one paragraph changed, all thirteen Canonical constraints remained unchanged,
and the chapter mirror is clean. Two chapters are now accepted; the rest of the
book and full-volume acceptance are still pending. A real 417-second specialist
call completed, beyond the core's former default idle cutoff.
Source quotes are checked exactly;
semantic correctness still requires review.

An additional concurrent-review regression brings the suite to 201 passing tests.
Two independent specialist services can review the same immutable candidate with
distinct run directories and category reports, without activating the candidate.
This does not change the default sequential CLI or weaken the acceptance gate.
Provider-side concurrency remains a separate production check.

GLM compatibility checkpoint: GLM-5.3/Flash/FlashX require thinking, so LG uses
enabled thinking with a 1,024-token budget and low output effort for this family.
Other models keep their existing mapping. This follows the
[model's forced-thinking constraint](https://docs.z.ai/guides/vlm/glm-5.3-flash)
and was verified against the configured GLM Anthropic endpoint with both plain
text and schema-tool output. Token-exhaustion diagnostics expose counts only.
All 204 regression tests passed. A real chapter draft then completed in about
80 seconds after two earlier token-exhaustion failures; it remains a candidate
until supervision and acceptance complete.

Latest production checkpoint: chapter 3 v4 passed five categories and normal CLI
acceptance, with one explicitly documented reconsideration of a reviewer clock
misreading. The original review was retained; neither Canonical nor the acceptance
gate was overridden. Two concurrent independent GLM reviews were exercised on
the real project. Three accepted chapters total 7,969 Chinese characters; their
published analyses and mirrors are current. The full-book goal remains open.

Chapter 4 subsequently passed all five specialists without a conflict and was
accepted as v4; its mirror is clean. Four accepted chapters total 10,274 Chinese
characters. Chapter 5 planning is underway; eight chapters and the volume audit
are still outstanding.

Real CLI browsing check on BeyondTheTide (four accepted chapters): all seven
`browse <topic> --json` commands returned valid arrays. Counts were characters 20,
events 24, storylines 6, timeline 12, world 30, chapters 12, analyses 20. These are
entry counts, not counts of distinct people or completed chapters. No conversation
file was created. A separate characters query in TheStartPoint left its existing
conversation file byte-for-byte unchanged and did not include BeyondTheTide's two
lead names. These checks are read-only and did not invoke a model.

The isolated wheel's actual `python -m lg_cli ... doctor` also passed database,
configuration, definitions, workflow, knowledge and pinned-runtime checks. It
reported the expected `core-tree` warning: vendored source verification is not
available inside a wheel-only installation. This is distinct from the checkout's
successful source-tree pin verification; the wheel does not bundle Codex source.

Offline packaging checkpoint: built the wheel with `--no-deps --no-build-isolation
--no-index` in LitIsLand and installed it into an isolated temporary target. Verified
imports came from that target, not the editable source checkout. The installed CLI
read BeyondTheTide chapter 1 as clean, loaded the bundled character specialist and
analysis response schema, and returned eight read-only character entries. All four
new service modules and `chapter-analysis.schema.json` were present in the wheel;
the removed legacy animation implementation was absent. No model calls or manuscript
edits were made by this check. Rebuilt and repeated isolated installation after
the timeout fix: the installed package selected `glm-5.3-flash` and generated
`model_providers.lg_anthropic.stream_idle_timeout_ms=900000` for this book's
900-second budget. Its chapter 2 mirror remained clean. Latest wheel SHA-256:
`b4239884e0531f555461a311f036cf5a9fadbd4483696dbf7e5cad9fa10fd7f5`.
This later GLM-fix wheel was installed into a fresh isolated target and its actual
request mapping was checked without a network request; imports were verified to
come from that installed package, not the checkout.

## Delivery checklist

Current-tree audit: `Library/AbstractLibrary/library_index.json` declares exactly
the five supervised categories (EventsLibrary, PayoffAngst, CharacterArc,
EmotionRhythm, Worldview). `Memes` is an empty directory, not an indexed library.
The full LitIsLand suite was rerun: 206 tests passed in 54.173 seconds.

A real `literary` subprocess in a 100x35 pseudo-terminal opened
`/browse characters`, entered a source-bearing detail, returned to the list with
Escape, returned to the conversation with Escape, and exited with `quit` (code 0).
Conversation file hashes and the set of run IDs were unchanged. The initial probe
used only one-second Escape intervals and did not exit naturally; it was terminated
and is not counted as a passing exit check. A repeat with three-second key gaps
passed all assertions. No model request was issued by browsing.

Editor roundtrip regression: a real changed chapter file exposed overly strict
EOF newline comparison. The parser now tolerates normalized trailing newlines
while preserving strict document markers, headings, order and outside-marker
text checks. Two added regressions bring the full suite to 206 passing tests.
The fresh wheel was installed offline into a separate temporary target; its
actual CLI imported an edited chapter as version 4, preserving versions 1-3.
Earlier steps verified database edits automatically mirror back to the file.
Package imports were confirmed to come from the installed target. Wheel SHA-256:
`310c77831d765da5b75a0dbab6bd2e312c2042cb7e98e68258a412fe316ffef7`.

Post-foreshadowing-fix wheel SHA-256:
`64a19b121d2bd8d99a8377a19a1dc5b6407a5da1b598b797809c697c7c109ada`.
Built offline from `lg-cli/`, installed into a fresh isolated target, and verified
to import from that target. Its context includes three labeled planned book
threads. Its real CLI imported another author edit as version 5 with all earlier
versions retained and a clean chapter mirror. Installed `doctor` passed every
available check, with only the documented wheel-only source-tree warning.

Latest book checkpoint: ten chapters accepted after five-category supervision,
24,520 Chinese characters and 50 current specialist reports. Ten source-version-
bound timeline checkpoints are stored separately from planned scene schedules.
The remaining two chapters and final volume audit are still outstanding.

Current external limitation: chapter 11's two explicit draft attempts, separated
by backoff, both returned GLM HTTP 429. Both runs are terminal, and neither created
a candidate. No fallback provider was selected. The book goal remains incomplete.
The bridge now distinguishes HTTP 429 rate-limit/quota failures from credential
configuration errors; a bounded numeric Retry-After is shown when supplied, while
provider response bodies and other header text remain redacted. This is diagnostic
only and does not add an automatic retry loop. Full native-enabled regression
suite after this fix: 209 tests passed in 53.791 seconds. The prior wheel hash
above predates the HTTP 429 diagnostic change.

Current post-429-fix wheel SHA-256:
`82af364df388a835c10ed8aeab379b6990e9eefd2a021609984dc3c002493f85`.
Built offline from `lg-cli/` into `/tmp/lg-rate-wheel-RwlAmf`, then installed into
an isolated `site` target. Imports were verified to come from that target. Four
targeted tests passed against the installed package (planned-thread isolation and
budget, actionable 429 diagnostics and error redaction). Its actual CLI reported
chapter 10's manuscript mirror clean. No live provider calls were made by these
installation checks. This verified package is not a claim of completed book delivery.

The next minimal availability check narrowed the live failure to GLM business
code 1113, not a generic transient throttle. The
[official error table](https://docs.bigmodel.cn/cn/faq/api-code) defines this as
insufficient balance. The bridge recognizes that exact code only for GLM and
prints a static instruction to check billing/plan access; provider error text is
still never shown. Other providers and unknown codes retain the generic 429
message. A regression covers string/integer codes, provider isolation and
redaction. This change requires a newer wheel than the previous hash above.

Post-1113-diagnostic verification: all 210 native-enabled tests passed in 56.322
seconds. An offline wheel built from `lg-cli/` has SHA-256
`750530fe811024c7a3e5873a92578c9df7b8f1a7f71b5e75104c6da17d543dd2`
at `/tmp/lg-billing-wheel-DIe3Hq/literarygiant_cli-0.3.0-py3-none-any.whl`.
It was installed into a fresh isolated target; import provenance was checked and
three HTTP-boundary regression tests passed against that installed package.
The real editable CLI still reports chapter 10 clean; `git diff --check` passes.
These local checks do not resolve the provider billing/entitlement rejection.

- [x] Category metadata and filtering in KnowledgeGateway.
- [x] Exclude source-locked details from reference matching and excerpts.
- [x] Explicit bidirectional chapter synchronization and conflict protection.
- [x] Read-only browsing with list/detail/back navigation.
- [x] Automatic safe chapter mirror refresh after manuscript changes.
- [x] Persistent per-book category analyses with source-version evidence.
- [x] Fixed specialist supervision integrated with chapter acceptance.
- [x] Full-volume review and stale-analysis invalidation.
- [x] Generate and verify the second complete book using the new pipeline.

### Additional GLM model verification (2026-09-23)

The author authorized three additional sandbox profiles: `glm-4.5-air`,
`glm-4.6v`, and `glm-4.1v-thinking-flashx`. All three passed minimal live text
requests on the existing GLM Anthropic endpoint. The first two also passed a
small structured streaming probe; the third failed structured final validation.
Small probes do not imply reliable long-form workflow behavior: real drafting
and specialist runs exposed continuity mistakes, escaped prose, malformed
structured arguments, and schema violations. Failed candidates remain separate
from accepted manuscript text.

Malformed JSON/non-object arguments to the structured-output tool now enter the
existing single bounded repair, without changing ordinary tool execution or
accepting invalid schema output. Specialist instructions explicitly distinguish
span IDs from host-resolved citations and require nullable fact IDs for ordinary
findings. All 211 native-enabled tests passed; 25 targeted tests also passed
against the isolated installed wheel. Wheel SHA-256:
`cd52aed479895eac222ab103bba47947dfc6384088c0f5bd5ee5353c6f53e06b`.
Package: `/tmp/lg-glm-final-wheel-KeRsYU/literarygiant_cli-0.3.0-py3-none-any.whl`.
The final prompt also includes the actual model-facing response schema after
historical reports, so their resolved-citation format cannot be mistaken for
the required model output. The final 211-test run took 57.600 seconds.
The per-book production log records subsequent manuscript acceptance and audits.

### Final workflow acceptance (2026-09-23)

This supersedes the incomplete production checkpoints above. The author delegated
adjudication of the final-chapter false positive. Its original report is retained
with an exact-source decision receipt; normal acceptance then succeeded without
changing Canonical rules or disabling other checks.

BeyondTheTide now has twelve accepted chapters (28,322 Chinese characters), sixty
current chapter reports and five current volume reports. All twelve chapter
mirrors are clean. MD, TXT and DOCX exports match the accepted scenes in order.
Three source-evidenced threads are paid and twelve timeline checkpoints are
canonical. The previous book's active manuscript hash is unchanged.

Final code: 217 native-enabled tests passed in LitIsLand. The isolated installed
wheel passed 37 targeted tests and an actual CLI chapter-status check. Seven real
read-only CLI views and a real PTY list/detail/back/exit session left conversations
and run IDs unchanged. Doctor passed; core and the global model default were not
changed. No new model calls are needed to reproduce these local checks.

Final wheel: `/tmp/lg-release-wheel-aRhMwe/literarygiant_cli-0.3.0-py3-none-any.whl`.
SHA-256: `c36d5473605a904c2e2dce5f9305688a48e9d0b91ad27b6692d100956ba1f3d3`.
Earlier wheel hashes above describe historical checkpoints, not this release.
See `docs/acceptance-audit.md` and the book's `DELIVERY.md` for acceptance evidence
and remaining model/editorial limitations.
