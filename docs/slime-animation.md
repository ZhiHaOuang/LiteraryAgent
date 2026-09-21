# Slime Animation Versions

The default `smooth` animation shares velocity across intermediate poses using
monotone cubic Hermite interpolation. Key poses, the 3.2-second cycle, 1.2-second
jump, 60 FPS sampling, crown mask, and face-clearance rules are unchanged.

## Immediate Rollback

Restart the CLI with the frozen pre-optimization animation:

```bash
LG_ANIMATION=legacy literary
```

Select the new version explicitly:

```bash
LG_ANIMATION=smooth literary
```

For a shell-session default, use `export LG_ANIMATION=legacy`; `unset LG_ANIMATION`
restores the default. These switches do not change credentials or story data.

## Preserved Baseline

`lg-cli/lg_cli/slime_animation_legacy.py` is an exact source copy taken before
continuity changes on 2026-09-21. Do not format or refactor this frozen file.

SHA-256:

```text
ab8430e04cd62274a1a1af664b21d376d6cc6de1f1074cc16c6903740caf291a
```

`tests/test_slime_versions.py` verifies the source checksum, all 192 rollback
frames, mode-separated caches, key-pose bounds, and velocity continuity.
Both implementations are included in the Python package. No git reset or
restoration of unrelated work is needed to roll back the animation.

## Comparison Previews

From the repository root, using the LitIsLand interpreter:

```bash
/opt/conda/envs/LitIsLand/bin/python scripts/preview_slime.py /root/.literarygiant/environments/sandbox/slime-comparison/legacy --mode legacy
/opt/conda/envs/LitIsLand/bin/python scripts/preview_slime.py /root/.literarygiant/environments/sandbox/slime-comparison/smooth --mode smooth
```

GIF timing has centisecond precision and viewers may clamp short delays. Judge
60 FPS playback in the actual terminal as well; duplicate pixel frames remain
normal at this resolution. More unique frames alone is not a continuity metric.
