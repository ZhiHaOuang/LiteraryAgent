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

## Review And Accept Versions

Generated drafts and edits are candidates until explicitly accepted.

```bash
literary version list gate-refusal
literary version show gate-refusal 2
literary version diff gate-refusal 1 2
literary version accept gate-refusal 2

literary edit gate-refusal --mode dialogue "Make each reply change leverage"
literary version reject gate-refusal 3
literary version restore gate-refusal 2
```

Available editing modes are `light`, `grammar`, `pacing`, `dialogue`, `imagery`, `conflict`, `pov`, `style`, and `rewrite`.

## Track And Check

```bash
literary timeline add "Day One" "Lin reaches North City" --order 001 --state canonical
literary foreshadowing add "Red Ribbon" "A ribbon hangs from the gate"
literary foreshadowing resolve <id> "The ribbon identifies the courier"

literary review consistency
literary review chapter chapter-1
literary review list
literary search "North Gate"
```

Review commands persist doubts and evidence, but never modify prose or Canonical facts.

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
```

Model/configuration/structured-output failures use nonzero exit codes and retain prompts, events, and completed stages under `.literarygiant/runs/`.
