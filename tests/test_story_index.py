import json
import tempfile
import unittest
from pathlib import Path

from lg_cli.project_store import ProjectStore
from lg_cli.story_index import build_index, save_artifact_index, validate_graph


def example_graph():
    return {'characters': [{'id':'lin','name':'林舟','role':'protagonist','summary':'循着信件追查失踪者'},
        {'id':'xu','name':'许岚','role':'ally','summary':'保管关键证据'}],
        'events':[{'id':'letter','label':'收到来信','summary':'信中提到旧码头','planned':False},
            {'id':'dock','label':'追查码头','summary':'下一章将前往码头','planned':True}],
        'relationships':[{'source':'lin','target':'xu','label':'共同调查'}],
        'links':[{'source':'letter','target':'dock','label':'线索引向'}]}


class StoryIndexTests(unittest.TestCase):
    def test_index_never_writes_through_an_external_reference_symlink(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); book = root/'book'; outside = root/'external'
            book.mkdir(); outside.mkdir()
            (book/'ReferenceLibrary').symlink_to(outside, target_is_directory=True)
            source = book/'draft.md'; source.write_text('draft')
            with self.assertRaises(ValueError):
                save_artifact_index(book, source, example_graph())
            self.assertEqual(list(outside.iterdir()), [])

    def test_existing_character_documents_and_version_checked_analyses_are_used(self):
        from lg_cli.book_analysis import BookAnalysisStore
        with tempfile.TemporaryDirectory() as raw:
            store=ProjectStore(Path(raw));store.initialize()
            store.create_document(kind='character',slug='lin',title='林舟',content='追查旧案',tags=['protagonist'])
            chapter=store.create_document(kind='chapter',slug='one',title='雨夜',content='林舟收到一封信。')
            records=BookAnalysisStore(store)
            report={'summary':'来信','entries':[{'key':'letter','label':'收到来信','analysis':'调查开始',
                'evidence':[{'document_id':chapter.id,'version_id':chapter.active_version_id,'quote':'收到一封信'}]}], 'findings':[]}
            from tests.helpers import reference_card
            report['entries'][0]['instance_card'] = reference_card('EventsLibrary', stored=True)
            records.save(records.snapshot('one'),'EventsLibrary',report,'old-run')
            index=build_index(store)
            self.assertEqual(index['characters'][0]['role'],'主角')
            self.assertEqual(index['events'][0]['label'],'收到来信')
            store.edit_active_document('one',content='正文已经改写。',expected_version_id=chapter.active_version_id,reason='author')
            self.assertEqual(build_index(store)['events'],[])

    def test_generated_outline_graph_survives_adoption(self):
        from dataclasses import replace
        from tests.helpers import FakeAdapter, make_config
        from lg_cli.init_project import init_workspace
        from lg_cli.workflow_runner import WorkflowRunner
        from lg_cli.run_store import RunStore
        class GraphAdapter(FakeAdapter):
            def run(self, **kwargs):
                result = super().run(**kwargs)
                payload = json.loads(result.output_text)
                payload['story_index'] = example_graph()
                return replace(result, output_text=json.dumps(payload))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); init_workspace(root)
            result = WorkflowRunner(make_config(root), adapter=GraphAdapter()).run('outline','Plan story')
            self.assertTrue(result.ok, result.error)
            store = ProjectStore(root)
            self.assertEqual(len(build_index(store)['characters']),2)
            adopted = RunStore(root).adopt(result.run_id, slug='plan',kind='outline',title='完整规划',accept=True)
            version = store.get_version('plan',adopted['version'])
            self.assertEqual(version.metadata['story_index'],example_graph())
            self.assertTrue(any(c['evidence'].get('state') == 'accepted' for c in build_index(store)['characters']))

    def test_version_bound_graph_invalidates_after_manual_edit(self):
        with tempfile.TemporaryDirectory() as raw:
            store=ProjectStore(Path(raw)); store.initialize(name='长夜')
            doc=store.create_document(kind='chapter',slug='one',title='来信',content='原文',metadata={'story_index':example_graph()})
            data=build_index(store)
            self.assertEqual(data['characters'][0]['name'],'林舟')
            self.assertEqual(data['characters'][0]['evidence']['version'],1)
            projected=store.workspace/'ReferenceLibrary/indices/story.json'
            self.assertTrue(projected.exists())
            store.edit_active_document(doc.id,content='手动修改',expected_version_id=doc.active_version_id,reason='author')
            self.assertEqual(build_index(store)['characters'],[])
            self.assertEqual(json.loads(projected.read_text())['missing'],1)
            candidate=store.create_version(doc.id,content='新版',metadata={'story_index':example_graph()})
            self.assertEqual(build_index(store)['characters'],[])
            store.accept_version(doc.id,candidate.version_number)
            data=build_index(store)
            self.assertEqual(data['characters'][0]['evidence']['state'],'accepted')
            self.assertEqual(data['characters'][0]['evidence']['version'],3)

    def test_artifact_graph_uses_source_hash(self):
        with tempfile.TemporaryDirectory() as raw:
            store=ProjectStore(Path(raw));store.initialize()
            path=Path(raw)/'ReferenceLibrary/plans/outline.md'
            path.parent.mkdir(parents=True);path.write_text('outline')
            save_artifact_index(Path(raw),path,example_graph())
            self.assertEqual(len(build_index(store)['relationships']),1)
            path.write_text('changed')
            self.assertEqual(build_index(store)['relationships'],[])
            self.assertEqual(build_index(store)['missing'],1)

    def test_invalid_edges_cannot_commit_a_version(self):
        with tempfile.TemporaryDirectory() as raw:
            store=ProjectStore(Path(raw));store.initialize()
            doc=store.create_document(kind='chapter',slug='one',title='One',content='original')
            graph=example_graph();graph['relationships'][0]['target']='missing'
            with self.assertRaises(ValueError):
                store.edit_active_document(doc.id,content='bad',expected_version_id=doc.active_version_id,
                    reason='bad graph',story_index=graph)
            self.assertEqual(store.get_document(doc.id).content,'original')
