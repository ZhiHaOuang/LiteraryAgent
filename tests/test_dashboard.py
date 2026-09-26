from __future__ import annotations

import io
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from pathlib import Path

from lg_cli.dashboard import render_dashboard, render_status_bar
from lg_cli.slime_animation import slime_mark, FRAME_INTERVAL, FRAME_COUNT

from .helpers import make_config


class DashboardTests(unittest.TestCase):
    def test_animation_has_fixed_dimensions_and_stable_panel(self):
        import os

        frames = [
            slime_mark(index * FRAME_INTERVAL + 0.001).plain
            for index in range(FRAME_COUNT)
        ]
        self.assertGreaterEqual(len(set(frames)), 40)
        for frame in frames:
            self.assertEqual(len(frame.splitlines()), 11)
            self.assertEqual({len(line) for line in frame.splitlines()}, {30})
        with (
            tempfile.TemporaryDirectory() as raw,
            patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": "1"}),
        ):
            panels = [
                render_dashboard(
                    make_config(Path(raw)),
                    mode="interactive",
                    skills_count=0,
                    agents_count=0,
                    animation_time=index * FRAME_INTERVAL + 0.001,
                )
                for index in range(FRAME_COUNT)
            ]
            self.assertEqual(len({len(panel.splitlines()) for panel in panels}), 1)
            self.assertEqual(len({panel.splitlines()[-1] for panel in panels}), 1)

    def test_uninitialized_dashboard_suggests_project_creation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                text = render_dashboard(
                    make_config(Path(raw)),
                    mode="interactive",
                    skills_count=15,
                    agents_count=12,
                )
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("What shall we create today?", text)
            self.assertNotIn("LG | LG", text)
            self.assertNotIn("Commands", text)

    def test_status_bar_is_compact_and_fits_width(self):
        import os
        from rich.cells import cell_len

        with patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": "1"}):
            config = make_config(Path("/tmp/my-novel"))
            for width in (40, 80, 120):
                text = render_status_bar(config, width)
                self.assertLessEqual(len(text.splitlines()), 7)
                self.assertTrue(text.splitlines()[0].startswith("╭"))
                self.assertTrue(text.splitlines()[-1].startswith("╰"))
                self.assertIn("/tmp/my-novel", text)
                self.assertIn("LiteraryGiant", text)
                self.assertTrue(
                    all(cell_len(line) <= width for line in text.splitlines())
                )

    def test_welcome_animation_and_existing_widths_are_unchanged(self):
        import hashlib
        import json
        import os
        from lg_cli.slime_animation import frame_pixels

        with patch.dict(
            os.environ,
            {"TERM": "xterm-256color", "NO_COLOR": "1", "LG_ANIMATION": "smooth"},
        ):
            pixels = [frame_pixels(i) for i in range(FRAME_COUNT)]
            self.assertEqual(
                hashlib.sha256(json.dumps(pixels).encode()).hexdigest(),
                "b26814d52e95cd611bf23e4e3a28b64588eab2d064dfdfd2777864e1f1d4e0be",
            )
            config = make_config(Path("/tmp/lg-visual-fixture"))
            panels = [
                render_dashboard(
                    config,
                    mode="interactive",
                    skills_count=0,
                    agents_count=0,
                    terminal_width=min(88, w),
                    animation_time=i / 60 + 0.001,
                    compact=c,
                )
                for w in (40, 80, 120)
                for c in (False, True)
                for i in range(FRAME_COUNT)
            ]
            self.assertEqual(
                hashlib.sha256(json.dumps(panels).encode()).hexdigest(),
                "b2ecd88570909469c0478d715fa9be876ecfd5503159de0c1fd5beca5182fee9",
            )

    def test_welcome_fits_narrow_terminal_without_color(self):
        import os
        from rich.cells import cell_len

        with tempfile.TemporaryDirectory() as raw:
            for width in (24, 40, 80, 120):
                with (
                    self.subTest(width=width),
                    patch(
                        "lg_cli.dashboard.shutil.get_terminal_size",
                        return_value=os.terminal_size((width, 24)),
                    ),
                    patch.dict(os.environ, {"NO_COLOR": "1"}),
                ):
                    text = render_dashboard(
                        make_config(Path(raw)),
                        mode="interactive",
                        skills_count=15,
                        agents_count=12,
                    )
                    self.assertNotIn("\x1b", text)
                    self.assertTrue(
                        all(cell_len(line) <= width for line in text.splitlines())
                    )

    def test_pixel_mark_and_plain_terminal_fallback(self):
        import os

        with tempfile.TemporaryDirectory() as raw:
            for terminal in ("xterm-256color", "dumb"):
                with (
                    self.subTest(terminal=terminal),
                    patch.dict(os.environ, {"TERM": terminal, "NO_COLOR": "1"}),
                ):
                    text = render_dashboard(
                        make_config(Path(raw)),
                        mode="interactive",
                        skills_count=15,
                        agents_count=12,
                    )
                    if terminal == "dumb":
                        self.assertTrue(text.isascii())
                    else:
                        self.assertIn("\u2584", text)
                        self.assertIn("\u2588", text)

    def test_deepseek_display_name_does_not_change_model_id(self):
        from dataclasses import replace
        import os

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.dict(os.environ, {"TERM": "xterm-256color"}),
        ):
            config = replace(
                make_config(Path(raw)),
                provider="deepseek",
                default_model="deepseek-flash",
            )
            text = render_dashboard(
                config, mode="interactive", skills_count=15, agents_count=12
            )
            self.assertIn("DeepSeek Flash", text)
            self.assertEqual(config.default_model, "deepseek-flash")
            lines = text.splitlines()
            title = next(i for i, line in enumerate(lines) if "What shall" in line)
            model = next(i for i, line in enumerate(lines) if "DeepSeek Flash" in line)
            self.assertEqual(model - title - 1, 12)


if __name__ == "__main__":
    unittest.main()
