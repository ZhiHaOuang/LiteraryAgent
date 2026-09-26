# Slime Animation

The default `smooth` animation shares velocity across intermediate poses using
monotone cubic Hermite interpolation. Key poses, the 3.2-second cycle, 1.2-second
jump, 60 FPS sampling, crown mask, and face-clearance rules are unchanged.

## Archived Version

Only the smooth implementation is shipped. The old duplicate implementation and
`LG_ANIMATION` mode switch were removed with author approval. The entire earlier
UI and both animation implementations are preserved in GitHub commit `b6afbf7aa`.
To inspect that snapshot without resetting or overwriting current work:

```bash
git worktree add --detach ../LiteraryAgent-ui-snapshot b6afbf7aa
```

The new worktree is an inspection copy, not an automatic change to the installed
`literary` command. Restore/install from it deliberately when a rollback is needed.
`tests/test_slime_animation.py` verifies velocity continuity, pose bounds, facial
clearance, crown geometry, timing, and cache isolation from caller mutation.
`tests/test_dashboard.py` locks all 192 pixel frames and rendered panels at three
widths to the pre-cleanup appearance.

## Preview

From the repository root, using the LitIsLand interpreter:

```bash
/opt/conda/envs/LitIsLand/bin/python scripts/preview_slime.py /root/.literarygiant/environments/sandbox/slime-preview
```

GIF timing has centisecond precision and viewers may clamp short delays. Judge
60 FPS playback in the actual terminal as well; duplicate pixel frames remain
normal at this resolution. More unique frames alone is not a continuity metric.
