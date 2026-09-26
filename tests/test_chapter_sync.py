from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lg_cli.chapter_sync import ChapterSync
from lg_cli.project_store import ProjectStore, ProjectStoreError


class ChapterSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root)
        self.store.initialize(name="Sync test")
        self.store.create_document(kind="chapter", slug="chapter-1", title="First", content="Opening\n")
        self.store.create_scene(slug="scene-1", title="Scene", chapter="chapter-1")
        self.store.create_version("scene-1", content="Scene text", state="accepted", activate=True)
        self.sync = ChapterSync(self.store)

    def test_round_trip_preserves_versions_and_imports_author_edits(self) -> None:
        status = self.sync.export("chapter-1")
        self.assertEqual(status.status, "clean")
        self.sync.import_file("chapter-1")
        self.assertEqual(len(self.store.list_versions("chapter-1")), 1)
        status.path.write_text(status.path.read_text().replace("Scene text", "Author revision"))
        self.assertEqual(self.sync.status("chapter-1").status, "file-edited")
        self.assertIn("+Author revision", self.sync.diff("chapter-1"))
        with self.assertRaises(ProjectStoreError):
            self.sync.export("chapter-1")
        self.assertEqual(self.sync.import_file("chapter-1").status, "clean")
        self.assertEqual(self.store.get_document("scene-1").content, "Author revision")
        self.assertEqual(self.store.get_version("scene-1", 2).content, "Scene text")
        self.sync.import_file("chapter-1")
        self.assertEqual(len(self.store.list_versions("scene-1")), 3)
        self.assertEqual(self.store.get_document("chapter-1").content, "Opening\n")

    def test_conflict_never_overwrites_either_side(self) -> None:
        status = self.sync.export("chapter-1")
        external = status.path.read_text().replace("Opening", "External")
        status.path.write_text(external)
        with self.assertWarns(UserWarning):
            self.store.create_version("scene-1", content="Database edit", activate=True)
        self.assertEqual(self.sync.status("chapter-1").status, "conflict")
        for action in (self.sync.export, self.sync.import_file):
            with self.assertRaises(ProjectStoreError):
                action("chapter-1")
        self.assertEqual(status.path.read_text(), external)
        self.assertEqual(self.store.get_document("chapter-1").content, "Opening\n")
        self.assertEqual(self.store.get_document("scene-1").content, "Database edit")

    def test_import_accepts_editor_normalized_final_newlines(self) -> None:
        for index, ending in enumerate(("", "\n", "\n\n\n")):
            with self.subTest(ending=repr(ending)):
                status = self.sync.export("chapter-1")
                current = self.store.get_document("scene-1").content
                replacement = f"Author revision {index}"
                edited = status.path.read_text().replace(current, replacement).rstrip("\n") + ending
                status.path.write_text(edited)
                self.assertEqual(self.sync.import_file("chapter-1").status, "clean")
                self.assertEqual(self.store.get_document("scene-1").content, replacement)
                self.assertEqual(self.store.get_document("chapter-1").content, "Opening\n")
                self.assertEqual(status.path.read_text(), edited)
                count = len(self.store.list_versions("scene-1"))
                self.sync.import_file("chapter-1")
                self.assertEqual(len(self.store.list_versions("scene-1")), count)

    def test_eof_tolerance_does_not_allow_changed_heading_or_outside_text(self) -> None:
        status = self.sync.export("chapter-1")
        original = status.path.read_text().rstrip("\n")
        for invalid in (original.replace("# First", "# Different"),
                        original + "\nText outside the markers",
                        original.replace("# First\n\n", "# First\n\nOutside text\n\n")):
            with self.subTest(invalid=invalid):
                status.path.write_text(invalid)
                with self.assertRaises(ProjectStoreError):
                    self.sync.import_file("chapter-1")
                self.assertEqual(self.store.get_document("scene-1").content, "Scene text")
                self.assertEqual(status.path.read_text(), invalid)

    def test_new_scene_automatically_refreshes_clean_files(self) -> None:
        self.sync.export("chapter-1")
        self.store.create_scene(slug="scene-2", title="Second scene", chapter="chapter-1")
        self.assertEqual(self.sync.status("chapter-1").status, "clean")
        self.sync.import_file("chapter-1")
        self.assertEqual(self.sync.export("chapter-1").status, "clean")

    def test_changed_markers_and_untracked_files_are_preserved(self) -> None:
        status = self.sync.export("chapter-1")
        invalid = status.path.read_text().replace("<!-- lg:document", "<!-- broken")
        status.path.write_text(invalid)
        with self.assertRaises(ProjectStoreError):
            self.sync.import_file("chapter-1")
        untracked = status.path.with_name("chapter-2.md")
        untracked.write_text("Author-owned file")
        with self.assertWarns(UserWarning):
            self.store.create_document(kind="chapter", slug="chapter-2", title="Second")
        with self.assertRaises(ProjectStoreError):
            self.sync.export("chapter-2")
        self.assertEqual(untracked.read_text(), "Author-owned file")

    def test_acceptance_refreshes_mirror_without_accepting_other_candidates(self) -> None:
        version = self.store.create_version("scene-1", content="New candidate")
        path = self.sync.status("chapter-1").path
        self.assertNotIn("New candidate", path.read_text())
        self.store.accept_version("scene-1", version.version_number)
        self.assertIn("New candidate", path.read_text())
        self.assertEqual(self.sync.status("chapter-1").status, "clean")

    def test_database_transaction_checks_all_versions_before_any_change(self) -> None:
        chapter = self.store.get_document("chapter-1")
        scene = self.store.get_document("scene-1")
        with self.assertRaises(ProjectStoreError):
            self.store.edit_chapter_documents(
                "chapter-1", expected_versions={chapter.id: chapter.active_version_id, scene.id: -1},
                contents={chapter.id: "Must not persist", scene.id: "Nor this"},
            )
        self.assertEqual(self.store.get_document("chapter-1").content, "Opening\n")

    def test_another_project_cannot_use_copied_sync_baseline(self) -> None:
        original = self.sync.export("chapter-1")
        other = ProjectStore(self.root / "other")
        other.initialize(name="Other book")
        other.create_document(kind="chapter", slug="chapter-1", title="Other")
        other_sync = ChapterSync(other)
        other_sync.export("chapter-1")
        manifest = next(self.sync.manifests.glob("*.json"))
        next(other_sync.manifests.glob("*.json")).write_text(manifest.read_text())
        with self.assertRaises(ProjectStoreError):
            other_sync.import_file("chapter-1")
        self.assertEqual(self.sync.status("chapter-1").path, original.path)


if __name__ == "__main__":
    unittest.main()
