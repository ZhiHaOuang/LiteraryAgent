from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.auth_ui import profile_label
from lg_cli.book_assets import asset_root, organize_book, resolve_artifact
from lg_cli.book_analysis import BookAnalysisStore
from lg_cli.config import load_config
from lg_cli.init_project import init_workspace, MEMORY_FILES
from lg_cli.knowledge import KnowledgeGateway
from lg_cli.memory import read_memory_context
from lg_cli.project_browser import analysis_text
from lg_cli.project_store import ProjectStore
from lg_cli.run_store import RunStore
from lg_cli.terminal_input import BookReaderLexer
from prompt_toolkit.document import Document
from tests.helpers import make_config


class BookAssetTests(unittest.TestCase):
    def test_lazy_bible_does_not_hide_existing_project_learnings(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            learning = root / '.learnings/notes.md'
            learning.parent.mkdir()
            learning.write_text('Existing author lesson')
            context = read_memory_context(root)
            self.assertEqual(context.found_count, 1)
            self.assertIn('Existing author lesson', context.to_prompt_text())

    def test_interrupted_migration_can_resume_without_overwriting(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ProjectStore(root).initialize()
            source = root / '.literarygiant/memory/ERRORS.md'
            source.parent.mkdir()
            source.write_text('Correction')
            plan = organize_book(root)
            (root / '.literarygiant/assets.json').write_text(json.dumps(plan))
            target = root / plan['moves'][0]['to']
            target.parent.mkdir(parents=True)
            source.rename(target)
            self.assertEqual(organize_book(root, apply=True)['status'], 'complete')
            self.assertEqual(target.read_text(), 'Correction')

    def test_migration_rejects_symlinks_and_preserves_existing_references_on_init(self):
        with tempfile.TemporaryDirectory() as raw, patch('lg_cli.init_project.ProjectRegistry'):
            root = Path(raw)
            refs = root / 'ReferenceLibrary'
            refs.mkdir()
            original = refs / 'notes.md'
            original.write_text('Existing research')
            init_workspace(root)
            self.assertEqual(original.read_text(), 'Existing research')
            (refs / 'linked.md').symlink_to(original)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                organize_book(root, apply=True)
            self.assertTrue(original.is_file())

    def test_reader_distinguishes_headings_evidence_and_metadata(self):
        lines = BookReaderLexer().lex_document(Document('# Title\n## Evidence\n> quote\nSource: v2\nProse'))
        self.assertEqual(lines(0), [('class:reader.title', 'Title')])
        self.assertEqual(lines(1)[0][0], 'class:reader.heading')
        self.assertEqual(lines(2)[0][0], 'class:reader.quote')
        self.assertEqual(lines(3)[0][0], 'class:reader.meta')
        self.assertEqual(lines(4), [('', 'Prose')])

    def test_profiles_show_provider_model_and_disambiguate_accounts(self):
        profile = {"provider": "deepseek", "model": "deepseek-flash"}
        data = {"active": "deepseek", "profiles": {"deepseek": profile}}
        self.assertEqual(profile_label("deepseek", profile, data), "* DeepSeek | deepseek-flash")
        data['profiles']['backup'] = profile
        self.assertEqual(profile_label("backup", profile, data), "DeepSeek | deepseek-flash [backup]")

    def test_migration_preserves_bytes_and_resolves_history_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Book")
            memory = store.root / "memory"
            memory.mkdir()
            (memory / "STORY_BIBLE.md").write_text(MEMORY_FILES['STORY_BIBLE.md'])
            (memory / "ERRORS.md").write_text("Author correction")
            source = store.root / "output/candidates/old.md"
            source.parent.mkdir(parents=True)
            source.write_text("Candidate")
            runs = RunStore(root)
            handle = runs.create(workflow="draft", command="write", request="test", provider="fake", model="fake")
            runs.finalize(handle, status="completed", artifact_path=source)
            original_run = handle.manifest_path.read_bytes()
            references = root / "ReferenceLibrary"
            references.mkdir()
            (references / "production-plan.json").write_text('{}')
            (references / "research.md").write_text("navigation source")
            (store.root / "analysis").mkdir()
            plan = organize_book(root)
            self.assertEqual(len(plan['moves']), 5)
            self.assertTrue(source.exists())
            done = organize_book(root, apply=True)
            self.assertEqual(done, organize_book(root, apply=True))
            self.assertFalse(source.exists())
            self.assertEqual(Path(resolve_artifact(root, str(source))).read_text(), "Candidate")
            self.assertTrue(Path(runs.load(handle.run_id)['artifact_path']).is_file())
            self.assertEqual(handle.manifest_path.read_bytes(), original_run)
            self.assertEqual(asset_root(root, 'memory'), references / 'bible')
            self.assertEqual(BookAnalysisStore(store).root, references / 'analyses')
            self.assertTrue((references / 'archive/placeholders/STORY_BIBLE.md').is_file())
            self.assertTrue((references / 'plans/production-plan.json').is_file())
            self.assertTrue((references / 'sources/imported/research.md').is_file())
            config = make_config(root)
            self.assertEqual(len(KnowledgeGateway(config).search('navigation').hits), 1)
            self.assertEqual(KnowledgeGateway(config).search('Candidate').hits, ())

    def test_conflicting_destination_stops_before_any_move(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ProjectStore(root).initialize()
            source = root / '.literarygiant/memory/ERRORS.md'
            source.parent.mkdir()
            source.write_text('Original')
            target = root / 'ReferenceLibrary/bible/ERRORS.md'
            target.parent.mkdir(parents=True)
            target.write_text('Other')
            with self.assertRaisesRegex(ValueError, 'Destination exists'):
                organize_book(root, apply=True)
            self.assertEqual(source.read_text(), 'Original')
            self.assertEqual(target.read_text(), 'Other')
            self.assertFalse((root / '.literarygiant/assets.json').exists())

    def test_new_book_is_lazy_and_legacy_config_redirects_after_migration(self):
        with tempfile.TemporaryDirectory() as raw, patch('lg_cli.init_project.ProjectRegistry'):
            root = Path(raw)
            init_workspace(root)
            self.assertFalse((root / '.literarygiant/output').exists())
            self.assertFalse((root / 'ReferenceLibrary/bible/STORY_BIBLE.md').exists())
            self.assertTrue((root / 'ReferenceLibrary/README.md').exists())
            (root / '.literarygiant/config.toml').write_text(
                '[paths]\nmemory=".literarygiant/memory"\noutput=".literarygiant/output"\n')
            config = load_config(root, environment='sandbox')
            self.assertEqual(config.memory_path, root / 'ReferenceLibrary/bible')
            self.assertEqual(config.output_path, root / 'ReferenceLibrary/drafts')

    def test_analysis_reader_keeps_evidence_and_adjudication_visible(self):
        evidence = [{'document_id': 1, 'version_id': 2, 'quote': 'Exact source quote'}]
        record = {'chapter': 'one', 'library': 'Worldview', 'status': 'current', 'run_id': 'test',
                  'report': {'summary': 'Summary', 'entries': [{'label': 'Rule', 'analysis': 'Explanation', 'evidence': evidence}],
                             'findings': [{'kind': 'canon_conflict', 'explanation': 'Claim', 'evidence': evidence}]},
                  'adjudications': [{'finding_index': 1, 'reviewer': 'Author', 'reason': 'False positive'}]}
        text = analysis_text(record)
        self.assertIn('## Summary', text)
        self.assertIn('document 1 / version 2', text)
        self.assertIn('[adjudicated]', text)
        self.assertIn('False positive', text)
        self.assertNotIn('"document_id":', text)
