from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from lg_cli.exporter import export_project
from lg_cli.project_store import ProjectStore


class ExporterTests(unittest.TestCase):
    def test_manuscript_exports_accepted_text_to_all_formats(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Export Test")
            chapter = store.create_document(kind="chapter", slug="chapter-1", title="Chapter 1")
            scene = store.create_scene(slug="arrival", title="Arrival", chapter=chapter.id)
            version = store.create_version(
                scene.document.id,
                content="Lin reached the city at dusk.",
                state="candidate",
            )
            store.accept_version(scene.document.id, version.version_number)

            markdown = export_project(
                store,
                target="manuscript",
                format="md",
                output_path=root / "manuscript.md",
            )
            plain = export_project(
                store,
                target="manuscript",
                format="txt",
                output_path=root / "manuscript.txt",
            )
            docx = export_project(
                store,
                target="manuscript",
                format="docx",
                output_path=root / "manuscript.docx",
            )

            self.assertIn("# Chapter 1", markdown.path.read_text(encoding="utf-8"))
            self.assertIn("Lin reached the city", plain.path.read_text(encoding="utf-8"))
            with zipfile.ZipFile(docx.path) as archive:
                self.assertIn("word/document.xml", archive.namelist())
                document_xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("Lin reached the city", document_xml)


if __name__ == "__main__":
    unittest.main()
