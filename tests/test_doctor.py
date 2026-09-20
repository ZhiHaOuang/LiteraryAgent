from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.doctor import probe_provider, run_doctor
from lg_cli.project_store import ProjectStore

from .helpers import make_config


class DoctorTests(unittest.TestCase):
    def test_probe_without_key_does_not_launch_runtime(self):
        with tempfile.TemporaryDirectory() as raw, patch("lg_cli.doctor.CodexExecAdapter.run") as run:
            result = probe_provider(make_config(Path(raw), api_key=None))
            self.assertEqual(result.status, "FAIL")
            run.assert_not_called()

    def test_story_database_check_changes_from_warning_to_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            before = {check.name: check for check in run_doctor(make_config(root)).checks}
            self.assertEqual(before["story-db"].status, "WARN")

            store = ProjectStore(root)
            store.initialize(name="Doctor Test")
            store.create_document(kind="chapter", slug="chapter-1", title="Chapter One")

            after = {check.name: check for check in run_doctor(make_config(root)).checks}
            self.assertEqual(after["story-db"].status, "PASS")
            self.assertIn("project=Doctor Test", after["story-db"].message)
            self.assertIn("chapters=1", after["story-db"].message)


if __name__ == "__main__":
    unittest.main()
