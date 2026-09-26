from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from lg_cli.exporter import export_project
from lg_cli.project_store import ProjectStore


class ExporterTests(unittest.TestCase):
    def test_default_exports_belong_to_author_assets_not_agent_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Author exports")
            result = export_project(store, target="manuscript")
            self.assertEqual(result.path.parent, root / "exports")
            self.assertTrue(result.path.is_file())
            self.assertFalse(result.path.is_relative_to(store.root))

    def test_unaccepted_chapter_text_is_excluded_unless_requested(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Export states")
            store.create_document(kind="chapter", slug="draft", title="Draft",
                                  content="UNACCEPTED CHAPTER", state="draft")
            result = export_project(store, target="manuscript", output_path=root / "final.md")
            self.assertNotIn("UNACCEPTED CHAPTER", result.path.read_text())
            draft = export_project(store, target="manuscript", include_drafts=True,
                                   output_path=root / "draft.md")
            self.assertIn("UNACCEPTED CHAPTER", draft.path.read_text())
            store.accept_version("draft", 1)
            final = export_project(store, target="manuscript", output_path=root / "accepted.md")
            self.assertIn("UNACCEPTED CHAPTER", final.path.read_text())

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
