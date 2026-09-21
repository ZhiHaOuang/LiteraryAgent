"""A cached, pixel-art welcome animation; no model or external assets involved."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise

from rich.text import Text

from . import slime_animation_legacy as legacy

WIDTH, HEIGHT = 30, 22  # Two square pixels per terminal row; ground meets the bottom.
FRAME_INTERVAL = 1 / 60
DURATION = 3.2
FRAME_COUNT = 192
BODY = "#dc795f"
CROWN = "#e8b866"


@dataclass(frozen=True)
class Pose:
    x: float
    bottom: float
    width: float
    height: float
    crown_gap: float
    lean: float


# Authored contact poses, in seconds. Ease between them, never reset position.
_KEYS = (
    (0.00, Pose(20, 22, 16, 8, 0, 0)),
    (0.30, Pose(20, 22, 16, 8, 0, -0.05)),
    (0.43, Pose(19.5, 22, 18, 6.5, 0, -0.12)),
    (0.50, Pose(19, 22, 17, 7, 0, -0.18)),
    (0.65, Pose(18, 19.8, 14, 10, 0, -0.22)),
    (0.80, Pose(16.7, 18, 14, 10, 0, -0.18)),
    (0.95, Pose(15.6, 16.8, 15, 9, 0, -0.12)),
    (1.10, Pose(14.7, 16, 17, 8, 0, -0.05)),
    (1.25, Pose(13.7, 16.6, 16, 9, 0.5, 0.03)),
    (1.45, Pose(12.3, 18.5, 14, 10, 1.5, 0.1)),
    (1.65, Pose(11.1, 21, 15, 9, 3, 0.1)),
    (1.70, Pose(11, 22, 19, 6, 4, 0)),
    (1.80, Pose(11, 22, 15, 9, 1.8, -0.08)),
    (1.95, Pose(11, 22, 16, 8, 0, 0)),
    (2.05, Pose(11, 22, 16, 8, 0.6, 0)),
    (2.20, Pose(11, 22, 16, 8, 0, 0)),
    (2.80, Pose(20, 22, 16, 8, 0, 0)),
    (3.20, Pose(20, 22, 16, 8, 0, 0)),
)

# Original three-tier crown, represented as top/bottom pixels per cell.
_CROWN_ROWS = ("_  _  _", "#__#__#", "^#####^")
_CROWN_PIXELS = frozenset(
    (x, row * 2 + half)
    for row, line in enumerate(_CROWN_ROWS)
    for x, char in enumerate(line)
    for half in range(2)
    if char == "#" or (char == "^" and half == 0) or (char == "_" and half == 1)
)
# The original broad silhouette, before face cutouts.
_BODY_SPANS = ((4, 11), (3, 12), (2, 13), (1, 14), (0, 15), (0, 15), (1, 14), (2, 13))


def animation_mode() -> str:
    mode = os.environ.get("LG_ANIMATION", "smooth")
    if mode not in {"smooth", "legacy"}:
        raise ValueError("LG_ANIMATION must be smooth or legacy")
    return mode


@lru_cache(maxsize=1)
def _tangents() -> tuple[Pose, ...]:
    intervals = [b[0] - a[0] for a, b in pairwise(_KEYS)]
    fields = {}
    for field in Pose.__dataclass_fields__:
        slopes = [
            (getattr(b[1], field) - getattr(a[1], field)) / h
            for (a, b), h in zip(pairwise(_KEYS), intervals)
        ]
        values = [0.0]
        for i in range(1, len(_KEYS) - 1):
            before, after = slopes[i - 1], slopes[i]
            if before * after <= 0:
                values.append(0.0)
            else:
                # Monotone Hermite tangents: shared velocity at each key, no overshoot.
                w1 = 2 * intervals[i] + intervals[i - 1]
                w2 = intervals[i] + 2 * intervals[i - 1]
                values.append((w1 + w2) / (w1 / before + w2 / after))
        fields[field] = [*values, 0.0]
    return tuple(
        Pose(*(fields[field][i] for field in Pose.__dataclass_fields__))
        for i in range(len(_KEYS))
    )


def pose_at(seconds: float, *, mode: str | None = None) -> Pose:
    selected = mode or animation_mode()
    if selected == "legacy":
        original = legacy.pose_at(seconds)
        return Pose(*(getattr(original, field) for field in Pose.__dataclass_fields__))
    if selected != "smooth":
        raise ValueError("Unknown animation mode")
    t = max(0.0, seconds) % DURATION
    tangents = _tangents()
    for i, ((start, a), (end, b)) in enumerate(pairwise(_KEYS)):
        if t < end:
            u = (t - start) / (end - start)
            h00 = 2 * u**3 - 3 * u**2 + 1
            h10 = u**3 - 2 * u**2 + u
            h01 = -2 * u**3 + 3 * u**2
            h11 = u**3 - u**2
            return Pose(
                *(
                    min(
                        max(getattr(a, field), getattr(b, field)),
                        max(
                            min(getattr(a, field), getattr(b, field)),
                            h00 * getattr(a, field)
                            + h01 * getattr(b, field)
                            + (end - start)
                            * (
                                h10 * getattr(tangents[i], field)
                                + h11 * getattr(tangents[i + 1], field)
                            ),
                        ),
                    )
                    for field in Pose.__dataclass_fields__
                )
            )
    return _KEYS[0][1]


def frame_index(seconds: float | None) -> int:
    return (
        0
        if seconds is None
        else int((max(0.0, seconds) % DURATION) / FRAME_INTERVAL + 1e-9) % FRAME_COUNT
    )


def _body_row(pose: Pose, y: int) -> tuple[int, int]:
    width, height = round(pose.width), round(pose.height)
    top = round(pose.bottom) - round(pose.height)
    row = y - top
    if not 0 <= row < height:
        return WIDTH, -1
    a, b = _BODY_SPANS[min(7, row * 8 // height)]
    left = round(pose.x) - width // 2 + round(pose.lean * (height - 1 - row))
    return left + (a * width + 15) // 16, left + ((b + 1) * width + 15) // 16 - 1


def face_pixels(pose: Pose) -> tuple[tuple[int, int], ...]:
    top = round(pose.bottom) - round(pose.height)
    preferred_y = top + max(2, round(pose.height * 0.38))
    # Move the face as a unit, preserving eye spacing and a one-pixel body rim.
    for eye_y in range(preferred_y, round(pose.bottom) - 3):
        offsets = (
            (-4, eye_y),
            (-4, eye_y + 1),
            (0, eye_y),
            (0, eye_y + 1),
            (-2, eye_y + 2),
        )
        lower, upper = -WIDTH, WIDTH
        for dx, y in offsets:
            for row in (y - 1, y, y + 1):
                left, right = _body_row(pose, row)
                lower = max(lower, left + 1 - dx)
                upper = min(upper, right - 1 - dx)
        if lower <= upper:
            center = min(upper, max(lower, round(pose.x)))
            return tuple((center + dx, y) for dx, y in offsets)
    raise ValueError("Slime pose cannot contain the face with a safe pixel rim")


def frame_pixels(index: int) -> tuple[tuple[str | None, ...], ...]:
    return _frame_pixels(index % FRAME_COUNT, animation_mode())


@lru_cache(maxsize=FRAME_COUNT * 2)
def _frame_pixels(index: int, mode: str) -> tuple[tuple[str | None, ...], ...]:
    if mode == "legacy":
        return legacy.frame_pixels(index)
    pose = pose_at(index * FRAME_INTERVAL, mode=mode)
    pixels: list[list[str | None]] = [[None] * WIDTH for _ in range(HEIGHT)]
    height = round(pose.height)
    top = round(pose.bottom) - height
    for y in range(height):
        left, right = _body_row(pose, top + y)
        for x in range(left, right + 1):
            pixels[top + y][x] = BODY
    for x, y in face_pixels(pose):
        pixels[y][x] = None

    # Translate the exact mask without rotation/resampling or silhouette changes.
    crown_x = round(pose.x) - 4 + round(pose.lean * (height - 1))
    crown_y = top - round(pose.crown_gap) - 6
    for x, y in _CROWN_PIXELS:
        pixels[crown_y + y][crown_x + x] = CROWN
    return tuple(tuple(row) for row in pixels)


@lru_cache(maxsize=FRAME_COUNT * 2)
def _frame_text(index: int, mode: str) -> Text:
    pixels = _frame_pixels(index, mode)
    result = Text()
    for row in range(0, HEIGHT, 2):
        for top, bottom in zip(pixels[row], pixels[row + 1]):
            if top == bottom:
                result.append("\u2588" if top else " ", style=top or "")
            elif top and bottom:
                result.append("\u2580", style=f"{top} on {bottom}")
            else:
                result.append("\u2580" if top else "\u2584", style=top or bottom or "")
        if row < HEIGHT - 2:
            result.append("\n")
    return result


def prepare_frames() -> None:
    mode = animation_mode()
    for index in range(FRAME_COUNT):
        _frame_text(index, mode)


def slime_mark(seconds: float | None = None) -> Text:
    return _frame_text(frame_index(seconds), animation_mode()).copy()
