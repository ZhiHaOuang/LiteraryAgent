from __future__ import annotations

import tempfile
import json
import unittest
from unittest.mock import patch
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

    def _edited_file(self):
        status = self.sync.export('chapter-1')
        status.path.write_text(status.path.read_text().replace('Scene text', 'Imported once'))
        return status.path

    def test_index_failure_after_commit_recovers_without_another_version(self):
        path = self._edited_file()
        with patch('lg_cli.story_index.refresh_index', side_effect=OSError('index unavailable')):
            result = self.sync.import_file('chapter-1')
        self.assertEqual(result.status, 'committed-needs-sync')
        self.assertEqual(self.sync.status('chapter-1').status, 'committed-needs-sync')
        saved = self.store.get_document('scene-1')
        self.assertEqual(saved.content, 'Imported once')
        count = len(self.store.list_versions('scene-1'))
        recovered = ChapterSync(ProjectStore(self.root)).import_file('chapter-1')
        self.assertEqual(recovered.status, 'clean')
        self.assertEqual(self.store.get_document('scene-1').active_version_id, saved.active_version_id)
        self.assertEqual(len(self.store.list_versions('scene-1')), count)
        self.assertIn('Imported once', path.read_text())

    def test_baseline_failure_after_commit_is_recoverable(self):
        self._edited_file()
        with patch.object(self.sync, '_save_baseline', side_effect=OSError('baseline unavailable')):
            result = self.sync.import_file('chapter-1')
        self.assertEqual(result.status, 'committed-needs-sync')
        count = len(self.store.list_versions('scene-1'))
        self.assertEqual(ChapterSync(self.store).import_file('chapter-1').status, 'clean')
        self.assertEqual(len(self.store.list_versions('scene-1')), count)

    def test_recovery_preserves_later_editor_save_until_separate_import(self):
        path = self._edited_file()
        with patch.object(self.sync, '_save_baseline', side_effect=OSError('baseline unavailable')):
            self.sync.import_file('chapter-1')
        path.write_text(path.read_text().replace('Imported once', 'Later author save'))
        count = len(self.store.list_versions('scene-1'))
        self.assertEqual(ChapterSync(self.store).import_file('chapter-1').status, 'file-edited')
        self.assertEqual(self.store.get_document('scene-1').content, 'Imported once')
        self.assertEqual(len(self.store.list_versions('scene-1')), count)
        self.assertIn('Later author save', path.read_text())
        self.assertEqual(self.sync.import_file('chapter-1').status, 'clean')
        self.assertEqual(self.store.get_document('scene-1').content, 'Later author save')

    def test_recovery_refuses_changed_database_head_and_preserves_file(self):
        path = self._edited_file()
        with patch.object(self.sync, '_save_baseline', side_effect=OSError('baseline unavailable')):
            self.sync.import_file('chapter-1')
        saved = self.store.get_document('scene-1')
        with self.assertWarns(UserWarning):
            self.store.edit_active_document('scene-1', content='Other database edit',
                expected_version_id=saved.active_version_id, reason='Concurrent author')
        with self.assertRaisesRegex(ProjectStoreError, 'recovery|Recovery'):
            ChapterSync(self.store).import_file('chapter-1')
        self.assertEqual(self.store.get_document('scene-1').content, 'Other database edit')
        self.assertIn('Imported once', path.read_text())

    def test_failed_transaction_can_retry_but_must_not_claim_commit(self):
        self._edited_file()
        with patch.object(self.store, 'edit_chapter_documents', side_effect=OSError('before commit')):
            with self.assertRaisesRegex(OSError, 'before commit'):
                self.sync.import_file('chapter-1')
        self.assertEqual(self.store.get_document('scene-1').content, 'Scene text')
        self.assertEqual(ChapterSync(self.store).import_file('chapter-1').status, 'clean')
        self.assertEqual(len(self.store.list_versions('scene-1')), 3)

    def test_crash_after_database_commit_recovers_from_persisted_intent(self):
        self._edited_file()
        real_edit = self.store.edit_chapter_documents
        def interrupted(*args, **kwargs):
            real_edit(*args, **kwargs)
            raise KeyboardInterrupt()
        with patch.object(self.store, 'edit_chapter_documents', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.sync.import_file('chapter-1')
        count = len(self.store.list_versions('scene-1'))
        self.assertEqual(ChapterSync(ProjectStore(self.root)).import_file('chapter-1').status, 'clean')
        self.assertEqual(len(self.store.list_versions('scene-1')), count)

    def test_recovery_failure_and_export_never_replay_committed_content(self):
        self._edited_file()
        with patch('lg_cli.story_index.refresh_index', side_effect=OSError('index unavailable')):
            self.assertEqual(self.sync.import_file('chapter-1').status, 'committed-needs-sync')
            count = len(self.store.list_versions('scene-1'))
            self.assertEqual(ChapterSync(self.store).import_file('chapter-1').status, 'committed-needs-sync')
            self.assertEqual(len(self.store.list_versions('scene-1')), count)
        with self.assertRaisesRegex(ProjectStoreError, 'recovery'):
            self.sync.export('chapter-1')
        self.assertEqual(self.sync.import_file('chapter-1').status, 'clean')

    def test_changed_file_after_uncommitted_intent_is_not_silently_imported(self):
        path = self._edited_file()
        with patch.object(self.store, 'edit_chapter_documents', side_effect=OSError('before commit')):
            with self.assertRaises(OSError):
                self.sync.import_file('chapter-1')
        path.write_text(path.read_text().replace('Imported once', 'Different later edit'))
        with self.assertRaisesRegex(ProjectStoreError, 'recovery'):
            ChapterSync(self.store).import_file('chapter-1')
        self.assertEqual(self.store.get_document('scene-1').content, 'Scene text')
        self.assertIn('Different later edit', path.read_text())

    def test_retry_uses_one_file_snapshot_when_editor_saves_after_validation_read(self):
        path = self._edited_file()
        with patch.object(self.store, 'edit_chapter_documents', side_effect=OSError('before commit')):
            with self.assertRaises(OSError):
                self.sync.import_file('chapter-1')
        original_read = Path.read_text
        original_edit = self.store.edit_chapter_documents
        reads = []
        mutation_reads = []
        def concurrent_save(candidate, *args, **kwargs):
            text = original_read(candidate, *args, **kwargs)
            if candidate == path:
                reads.append(text)
                if len(reads) == 1:
                    candidate.write_text(text.replace('Imported once', 'Later editor save'))
            return text
        def edit(*args, **kwargs):
            mutation_reads.append(len(reads))
            return original_edit(*args, **kwargs)
        with patch.object(Path, 'read_text', concurrent_save), \
                patch.object(self.store, 'edit_chapter_documents', edit):
            result = self.sync.import_file('chapter-1')
        self.assertEqual(result.status, 'file-edited')
        self.assertEqual(mutation_reads, [1])
        self.assertEqual(self.store.get_document('scene-1').content, 'Imported once')
        self.assertIn('Later editor save', path.read_text())
        self.assertEqual(len(self.store.list_versions('scene-1')), 3)
        self.assertEqual(self.sync.import_file('chapter-1').status, 'clean')
        self.assertEqual(self.store.get_document('scene-1').content, 'Later editor save')

    def test_recovery_rejects_wrong_project_corruption_and_unproven_origin(self):
        self._edited_file()
        with patch.object(self.sync, '_save_baseline', side_effect=OSError('baseline unavailable')):
            self.sync.import_file('chapter-1')
        journal = next(self.sync.manifests.glob('*.import.json'))
        original = json.loads(journal.read_text())
        count = len(self.store.list_versions('scene-1'))
        variants = [{**original, 'project_id': 'other-book'},
                    {**original, 'before': [{}]},
                    {**original, 'origin': 'chapter-file:' + '0' * 32}]
        for value in variants:
            with self.subTest(value=value):
                journal.write_text(json.dumps(value))
                with self.assertRaisesRegex(ProjectStoreError, 'recovery'):
                    ChapterSync(self.store).import_file('chapter-1')
                self.assertEqual(len(self.store.list_versions('scene-1')), count)
        journal.write_text(json.dumps(original))
        self.assertEqual(self.sync.import_file('chapter-1').status, 'clean')

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
