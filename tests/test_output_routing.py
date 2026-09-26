import tempfile
import unittest
from pathlib import Path

from lg_cli.knowledge import KnowledgeGateway
from lg_cli.output_writer import OUTPUT_DIRECTORIES, write_workflow_output
from lg_cli.project_store import ProjectStore
from lg_cli.book_analysis import BookAnalysisStore
from lg_cli.run_store import RunStore
from lg_cli.workflow_runner import WorkflowRunner
from tests.helpers import FakeAdapter, make_config


class OutputRoutingTests(unittest.TestCase):
    def test_analysis_reads_legacy_but_new_reviews_write_to_reference_library(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize()
            store.create_document(kind='chapter', slug='one', title='One', content='The gate closes at dusk.')
            records = BookAnalysisStore(store)
            report = {'summary': 'Reviewed', 'entries': [], 'findings': []}
            records.save(records.snapshot('one'), 'Worldview', report, 'old')
            source = records.root / 'one/Worldview.json'
            legacy = store.root / 'analysis/one/Worldview.json'
            legacy.parent.mkdir(parents=True)
            source.rename(legacy)
            original = legacy.read_bytes()
            records = BookAnalysisStore(store)
            self.assertEqual(records.list()[0]['run_id'], 'old')
            records.save(records.snapshot('one'), 'Worldview', report, 'new')
            self.assertEqual(records.list()[0]['run_id'], 'new')
            self.assertEqual(legacy.read_bytes(), original)
            self.assertTrue(source.is_file())

    def test_every_category_is_filed_on_write_even_with_legacy_config(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for command, category in OUTPUT_DIRECTORIES.items():
                result = write_workflow_output(root, command, 'Result',
                    output_root=root / '.literarygiant/output', run_id='run-1')
                self.assertEqual(result.output_path.parent, root / 'ReferenceLibrary' / category)
                self.assertEqual(result.latest_path.parent, result.output_path.parent)
                self.assertEqual(result.output_path.read_text(), 'Result\n')
            self.assertFalse((root / '.literarygiant/output').exists())
            self.assertFalse((root / '.literarygiant/assets.json').exists())

    def test_runtime_outputs_do_not_enter_references(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for command in ('chat', 'dry-run', 'code'):
                result = write_workflow_output(root, command, 'Execution only')
                self.assertTrue(result.output_path.is_relative_to(root / '.literarygiant'))
            self.assertFalse((root / 'ReferenceLibrary').exists())

    def test_unclassified_and_outside_paths_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for command, options in (('../escape', {}), ('outline', {'output_root': root.parent}),
                                     ('outline', {'run_id': '../escape'})):
                with self.assertRaises(ValueError):
                    write_workflow_output(root, command, 'Invalid', **options)
            self.assertEqual(list(root.iterdir()), [])

    def test_failed_workflow_retains_classified_specialist_work_as_provisional(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ProjectStore(root).initialize()
            result = WorkflowRunner(make_config(root), adapter=FakeAdapter(fail_at=4)).run('outline', 'Build a story')
            self.assertFalse(result.ok)
            outputs = list((root / 'ReferenceLibrary/bible/worlds/stages').rglob('*.md'))
            self.assertEqual(len(outputs), 1)
            self.assertIn('Status: provisional', outputs[0].read_text())
            metadata = RunStore(root).read_stage_metadata(result.run_id, 'world-pressure')
            self.assertEqual(Path(metadata['artifact_path']), outputs[0])
            self.assertFalse((root / 'ReferenceLibrary/plans/outlines/outline.latest.md').exists())

    def test_complete_outline_files_specialists_before_final_output_without_organizer(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ProjectStore(root).initialize()
            result = WorkflowRunner(make_config(root), adapter=FakeAdapter()).run('outline', 'Build a story')
            self.assertTrue(result.ok)
            self.assertEqual(result.artifact_path.parent, root / 'ReferenceLibrary/plans/outlines')
            for category in ('bible/worlds', 'bible/characters', 'plans/plots', 'reviews/reports', 'sources/research'):
                self.assertEqual(len(list((root / 'ReferenceLibrary' / category / 'stages' / result.run_id).glob('*.md'))), 1)
            self.assertFalse((root / '.literarygiant/output').exists())
            self.assertFalse((root / '.literarygiant/assets.json').exists())

    def test_reference_search_excludes_generated_drafts_and_partial_research(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_workflow_output(root, 'world', 'quasar invented rule')
            write_workflow_output(root, 'ref', 'quasar uncertain partial', run_id='run', stage_id='partial')
            gateway = KnowledgeGateway(make_config(root))
            self.assertEqual(gateway.search('quasar').hits, ())
            write_workflow_output(root, 'ref', 'quasar source mechanism')
            self.assertTrue(gateway.search('quasar').hits)
