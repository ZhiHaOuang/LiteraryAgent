"""Thinking Orbs working/20 geometry, adapted to terminal braille pixels.

MIT © 2026 Jakub Antalik. See resources/thinking-orbs-LICENSE.txt.
Upstream: Jakubantalik/thinking-orbs@de85557ca220332586d070d8788c0e1d6e877a0d
Source: src/engine/orbits.ts, core.ts, profiles.ts and presets.ts.
Only the rasterizer and orange presentation differ from the original geometry.
"""
from math import sin, cos, acos, sqrt, floor, pi
from functools import lru_cache


def _hash(a, b):
    value = sin(a * 12.9898 + b * 78.233) * 43758.5453
    return value - floor(value)


def working_dots(seconds):
    size, t = 20, seconds * 3.9
    radius, rs = size / 2 * .82, (size / 300) ** .6
    yaw, tilt = t * .12, .3
    dots = []
    # Upstream scaleCounts(.238): round(12*.238)=3, round(40*.238)=10.
    # `particles` is deliberately not a scalable count key upstream.
    for orb in range(3):
        h1, h2, h3 = (_hash(orb, seed) for seed in (1.7, 5.2, 8.9))
        ro, th, phi = radius * (.45 + .52 * h1), h1 * 2 * pi, acos(2 * h2 - 1)
        nx, ny, nz = sin(phi) * cos(th), cos(phi), sin(phi) * sin(th)
        ul = max(1e-6, sqrt(ny * ny + nx * nx))
        ux, uy = -ny / ul, nx / ul
        vx, vy, vz = -nz * uy, nz * ux, nx * uy - ny * ux
        speed = (.25 + .55 * h3) * (1 if h3 > .5 else -1)
        for ghost, count in ((True, 10), (False, 3)):
            for k in range(count):
                a = k / count * 2 * pi + (0 if ghost else t * speed + h2 * 6)
                x = (ux * cos(a) + vx * sin(a)) * ro
                y = (uy * cos(a) + vy * sin(a)) * ro
                z = vz * sin(a) * ro
                x1, z1 = x * cos(yaw) + z * sin(yaw), -x * sin(yaw) + z * cos(yaw)
                y1, z2 = y * cos(tilt) - z1 * sin(tilt), y * sin(tilt) + z1 * cos(tilt)
                depth = (z2 / ro + 1) / 2
                r = .9 * 2.4 * rs if ghost else (1.2 + 1.6 * depth) * 2.4 * rs
                white = .72 if ghost else .3 - .22 * depth
                alpha = .5 * (.4 + .6 * depth) if ghost else 1
                dots.append((10 + x1, 10 - y1, z2, max(.3, r), white, alpha))
    return sorted(dots, key=lambda dot: dot[2])


@lru_cache(maxsize=128)
def terminal_frame(tick):
    """8×8 dot raster → four braille columns, two terminal rows."""
    dots = working_dots(tick / 8)
    pixels = [[False] * 8 for _ in range(8)]
    for y in range(8):
        for x in range(8):
            ink = 0
            # Supersampling preserves small fast particles at terminal resolution.
            for sy in (.25, .75):
                for sx in (.25, .75):
                    px, py, sample = (x + sx) * 2.5, (y + sy) * 2.5, 0
                    for dx, dy, _, r, white, alpha in dots:
                        if (px-dx)**2 + (py-dy)**2 <= r*r:
                            sample = sample * (1-alpha) + (1-white) * alpha
                    ink += sample / 4
            pixels[y][x] = ink >= .12
    bits = ((0, 3), (1, 4), (2, 5), (6, 7))
    rows = []
    for row in range(2):
        chars = []
        for col in range(4):
            mask = sum(1 << bits[y][x] for y in range(4) for x in range(2)
                if pixels[row * 4 + y][col * 2 + x])
            chars.append(chr(0x2800 + mask) if mask else ' ')
        rows.append(''.join(chars))
    return '\n'.join(rows)
