from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lg_cli.dashboard import render_dashboard
from lg_cli.project_store import ProjectStore

from .helpers import make_config


class DashboardTests(unittest.TestCase):
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
            self.assertIn("not initialized", text)
            self.assertIn('literary project create "My Novel" --path .', text)

    def test_dashboard_summarizes_story_state_and_next_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Northern City")
            chapter = store.create_document(kind="chapter", slug="chapter-1", title="Chapter One")
            scene = store.create_scene(slug="arrival", title="Arrival", chapter=chapter.id)
            store.set_scene_plan(scene.document.id, "1. Reach the gate.")
            store.set_fact(category="world", key="gate", value="The gate closes at dusk")

            text = render_dashboard(make_config(root), mode="interactive", skills_count=15, agents_count=12)
            self.assertIn("Northern City", text)
            self.assertIn("1 chapter", text)
            self.assertIn("1 Canonical facts", text)
            self.assertIn("literary scene list", text)


if __name__ == "__main__":
    unittest.main()
