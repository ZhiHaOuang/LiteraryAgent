"""The maintenance generator must preserve the current runtime definitions."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DefinitionMaterializerTests(unittest.TestCase):
    def setUp(self):
        checkout = Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory(prefix="lg-definition-materializer-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        self.script = self.root / "scripts/materialize_lg_definitions.py"
        shutil.copyfile(checkout / "scripts/materialize_lg_definitions.py", self.script)
        for name in ("lg-skills", "lg-subagents"):
            shutil.copytree(checkout / name, self.root / name)
        self.baseline = self._snapshot()

    def _snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def _run(self, *arguments):
        return subprocess.run(
            [sys.executable, str(self.script), *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _remove_existing_contracts(self):
        skill = self.root / "lg-skills/memory-updater/SKILL.md"
        text = skill.read_text(encoding="utf-8")
        self.assertIn("Notes and proposals are not authoritative canon.", text)
        text = "\n".join(
            line for line in text.split("\n")
            if not line.startswith("   ")
        )
        skill.write_text(text, encoding="utf-8")
        director = self.root / "lg-subagents/director/output.schema.json"
        schema = json.loads(director.read_text(encoding="utf-8"))
        self.assertIn("book_overview", schema["required"])
        del schema["properties"]["book_overview"]
        schema["required"].remove("book_overview")
        director.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_check_current_definitions_is_read_only(self):
        result = self._run("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._snapshot(), self.baseline)

    def test_generation_is_byte_for_byte_idempotent(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._snapshot(), self.baseline)
        again = self._run()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self._snapshot(), self.baseline)

    def test_check_rejects_changed_contracts_without_writing(self):
        self._remove_existing_contracts()
        changed = self._snapshot()
        result = self._run("--check")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("lg-skills/memory-updater/SKILL.md", result.stdout)
        self.assertIn("lg-subagents/director/output.schema.json", result.stdout)
        self.assertEqual(self._snapshot(), changed)

    def test_generation_restores_contracts_and_check_passes(self):
        self._remove_existing_contracts()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._snapshot(), self.baseline)
        checked = self._run("--check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertEqual(self._snapshot(), self.baseline)


if __name__ == "__main__":
    unittest.main()
