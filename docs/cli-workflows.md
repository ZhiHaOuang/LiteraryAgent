# LiteraryGiant CLI Workflows

`literary` is the primary command. `lg` and `literarygiant` are compatibility aliases.

## Start A Project

Use the current directory:

```bash
literary init
literary status
literary doctor
```

Or create an independent registered workspace:

```bash
literary project create "North City" --path ./north-city
literary project list
literary -C ./north-city project info
```

## Build The Story Bible

Ideas remain replaceable. `set` records an explicit author-confirmed Canonical fact.

```bash
literary bible add character Lin "A courier from the southern road" --state idea
literary bible set world gate-curfew "The North Gate closes at dusk"
literary bible list --state canonical
literary bible history <fact-id>
```

Model-generated durable facts are never promoted automatically:

```bash
literary bible curate world-bible "Extract stable rules, costs, and limits"
literary bible proposal list
literary bible proposal accept <proposal-id> --state canonical
literary bible proposal reject <proposal-id>
```

## Plan And Draft A Scene

```bash
literary chapter create chapter-1 "Chapter One" --number 1
literary scene create gate-refusal "The Refusal" \
  --chapter chapter-1 \
  --pov Lin \
  --goal "Enter the city" \
  --conflict "The guard refuses entry" \
  --end "Lin must reveal the sealed letter"

literary scene plan gate-refusal "Keep the letter secret until the last beat"
literary scene show gate-refusal
literary scene approve gate-refusal
literary scene draft gate-refusal
```

Planning and drafting stream `RunEvent` records. `--dry-run` persists the exact bounded prompt without invoking a model.
Planning fills only empty scene-card fields. Existing constraints, including the prose
length range, are preserved; ignored model changes are recorded in the run. Use
`literary scene set` for explicit changes. A requested plan length is not a prose target.

## Review And Accept Versions

Generated drafts and edits are candidates until explicitly accepted.
Inspect the actual candidate text: a model's summary or change log is not proof
that the requested corrections were applied.
Scoped edits preserve unselected text and reject copied surrounding paragraphs.
Revision prompts carry the selected document kind and scene viewpoint. An output
that exactly copies another long context document is rejected before saving a
candidate or memory proposals. This is a narrow guard, not a semantic guarantee.

```bash
literary version list gate-refusal
literary version show gate-refusal 2
literary version diff gate-refusal 1 2
literary version accept gate-refusal 2

literary edit gate-refusal --mode dialogue "Make each reply change leverage"
literary edit gate-refusal --mode light --source-version 2 --passage "The guard refused." "Rewrite only this sentence"
literary version reject gate-refusal 3
literary version restore gate-refusal 2
```

Available editing modes are `light`, `grammar`, `pacing`, `dialogue`, `imagery`, `conflict`, `pov`, `style`, and `rewrite`.

## Track And Check

```bash
literary timeline add "Day One" "Lin reaches North City" --order 001 --state canonical
literary timeline set <id> --event "Lin reaches North City at noon" --order 001-1200 --state canonical
literary foreshadowing add "Red Ribbon" "A ribbon hangs from the gate"
literary foreshadowing resolve <id> "The ribbon identifies the courier"

literary review consistency
literary review chapter chapter-1
literary review list
literary search "North Gate"
```

`timeline set` corrects or confirms an existing entry without duplicating it.
Omitted fields, source document, and tags remain unchanged. Confirm events only
after checking their accepted source text; candidate chronology stays `idea`.

Review commands persist doubts and evidence, but never modify prose or Canonical facts.
`review consistency` performs deterministic structural checks, not full semantic
review. For model-driven `check`, verify quoted passages against their actual
source versions and calculate time differences independently. A successful model
call is not proof that its findings are correct. For whole-book review, supply
the complete accepted text through stdin; bounded context excerpts are insufficient.

## Export

Only accepted/final manuscript scenes are exported unless `--include-drafts` is explicit.

```bash
literary export manuscript --format md
literary export manuscript --format txt
literary export manuscript --format docx
literary export bible --format md
literary export timeline --format md
literary export foreshadowing --format md
```

## Runs And Recovery

```bash
literary run list
literary run show <run-id> --events
literary run resume <run-id>
literary run adopt <run-id> outline-v1 --kind outline --title "Novel Outline"
literary run adopt <write-run-id> gate-refusal --kind scene --title "The Refusal"
```

Model/configuration/structured-output failures use nonzero exit codes and retain prompts, events, and completed stages under `.literarygiant/runs/`.
Generic declarative workflows support stage recovery. Scene planning, scene
drafting, document revision, and Bible curation runs do not yet share that resume
path: inspect their saved candidates before explicitly retrying a stopped call.

`run adopt` imports the exact completed artifact into this book's database with
run provenance. Repeating the same import is idempotent. A new document starts as
a draft; importing into an existing slug creates an inactive candidate version.
Use `--accept` only when deliberately activating it. Failed/planned runs and
artifacts outside this book cannot be adopted.

Generic workflows now read this book's database context as well as Markdown memory.
Canonical facts remain available even when the request has no matching keywords.
Recent stage outputs take precedence when the handoff budget is exceeded. Scene
planning, drafting, editing, and Bible curation load their named skills and subagent
prompts; curation proposes source-linked facts without changing Canonical memory.

The book's `[model].max_output_tokens` controls its output budget independently of
the global default. Structured Messages results get at most one model-driven JSON
repair after a schema failure, then must pass the original strict schema. A failed
repair remains a failed run, never an accepted manuscript.
