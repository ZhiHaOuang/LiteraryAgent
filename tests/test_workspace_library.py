import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lg_cli.book_home import snapshot, panel_entries
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from lg_cli.workspace_library import library, entry, writing_context, _stamp
from lg_cli.writing_mcp import create_server
from tests.helpers import make_config, reference_card


class WorkspaceLibraryTests(unittest.TestCase):
    def test_library_and_entry_reject_changes_during_materialization(self):
        from lg_cli.workspace_library import _entries
        for read_detail in (False, True):
            with self.subTest(detail=read_detail), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                store = ProjectStore(root)
                store.initialize(name='读取一致性')
                document = store.create_document(kind='chapter', slug='one', title='原稿',
                    content='读取前的已采纳正文。', state='accepted')
                config = make_config(root)
                key = library(config, 'chapters')['entries'][0]['key']
                def concurrent_edit(*args):
                    rows = _entries(*args)
                    store.edit_active_document('one', content='作者读取期间保存的新正文。',
                        expected_version_id=document.active_version_id, reason='并发保存')
                    return rows
                with patch('lg_cli.workspace_library._entries', concurrent_edit):
                    with self.assertRaisesRegex(ValueError, 'changed while reading'):
                        entry(config, 'chapters', key) if read_detail else library(config, 'chapters')
                self.assertIn('作者读取期间保存', store.get_document('one').content)

    def test_scene_review_and_adoption_follow_parent_chapter_supervision(self):
        async def exercise(root):
            init_workspace(root);config=make_config(root);store=ProjectStore(root);server=create_server(config)
            chapter=store.create_document(kind='chapter',slug='first',title='第一章',content='',state='candidate',sequence=1)
            scene=store.create_scene(slug='rescue',title='闸门救援',chapter=chapter.id)
            candidate=store.create_version(scene.document.id,content='林砚先切断电源，再打开闸门救人。',state='candidate',activate=False,reason='test original scene')
            async def call(name,**arguments):
                result=await server.call_tool(name,arguments)
                return result[1] if isinstance(result,tuple) else json.loads(result[0].text)
            context=await call('studio_review_context',slug='rescue',version_number=candidate.version_number)
            self.assertEqual(context['snapshot']['chapter'],'first')
            span=next(iter(context['source_spans']))
            reports={category:{'summary':'动作因果有原文依据。','entries':[{'instance_card':reference_card(category),'key':'rescue','label':'救援选择','analysis':'人物选择先断电，避免救援时触电。','evidence':[{'span_id':span}]}],'findings':[]} for category in context['schemas']}
            with self.assertRaises(Exception):await call('studio_accept',slug='rescue',version_number=candidate.version_number,expected_version_id=scene.document.active_version_id)
            await call('studio_review',slug='rescue',version_number=candidate.version_number,source_fingerprint=context['sourceFingerprint'],reports=reports)
            result=await call('studio_accept',slug='rescue',version_number=candidate.version_number,expected_version_id=scene.document.active_version_id)
            self.assertTrue(result['accepted']);self.assertEqual(store.get_document('rescue').state,'accepted')
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'LITERARYGIANT_REGISTRY_HOME': raw}):asyncio.run(exercise(Path(raw)))

    def test_cli_people_sources_pagination_search_and_saved_updates(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'LITERARYGIANT_REGISTRY_HOME': raw}):
            root = Path(raw);init_workspace(root);config = make_config(root);store = ProjectStore(root)
            for i in range(30):
                store.create_document(kind='character', slug=f'person-{i}', title=f'人物{i:02}',
                                      content=f'角色资料{i:02}，来源是原创测试规划。', state='candidate')
            before = _stamp(config)
            first = library(config, 'characters');second = library(config, 'characters', offset=24)
            self.assertEqual(first['total'], 30);self.assertEqual(len(first['entries']), 24)
            self.assertEqual(len(second['entries']), 6)
            self.assertFalse({e['key'] for e in first['entries']} & {e['key'] for e in second['entries']})
            selected = entry(config, 'characters', first['entries'][0]['key'])
            cli = dict(panel_entries(snapshot(config), 'characters'))
            self.assertEqual(selected['body'], cli[selected['title']])
            self.assertIn('第 1 版', selected['body']);self.assertIn('候选资料',selected['body'])
            self.assertEqual(before, _stamp(config), 'Reading must not modify book state')
            found = library(config, 'characters', query='人物07')['entries'][0]
            preview = entry(config, 'characters', found['key'])
            prompt = writing_context(config, 'characters', found['key'], 'generate', preview['fingerprint'])['prompt']
            self.assertIn('studio_reference', prompt);self.assertIn('不自动采用', prompt)
            doc = store.get_document('person-7')
            store.edit_active_document(doc.slug, content='CLI 中修改后的最新人物总结。', expected_version_id=doc.active_version_id, reason='simulate CLI update')
            updated = library(config, 'characters', query='人物07')['entries'][0]
            self.assertEqual(updated['key'],found['key'],'Summary changes must preserve the selected person')
            self.assertIn('CLI 中修改后的', entry(config, 'characters', updated['key'])['body'])
            with self.assertRaisesRegex(ValueError, 'changed after preview'):
                writing_context(config, 'characters', found['key'], 'generate', preview['fingerprint'])
            with self.assertRaises(ValueError):library(config, '../secrets')

    def test_reference_candidates_adoption_and_proposal_preview_use_cli_store(self):
        async def exercise(root):
            init_workspace(root);config = make_config(root);store = ProjectStore(root);server = create_server(config)
            async def call(name, **arguments):
                result = await server.call_tool(name, arguments)
                return result[1] if isinstance(result, tuple) else json.loads(result[0].text)
            saved = await call('studio_reference', kind='character', slug='lin', title='林砚', content='人物资料初稿。', expected_version_id=0, reason='author request')
            self.assertTrue((root/saved['artifact']).is_file())
            old = store.get_document('lin')
            updated = await call('studio_reference', kind='character', slug='lin', title='林砚', content='人物总结更新：先切断电源再救人。', expected_version_id=old.active_version_id, reason='author update')
            self.assertEqual(store.get_document('lin').content, '人物资料初稿。')
            self.assertEqual(store.summary()['canonical_facts'], 0)
            preview = await call('book_entry', topic='pending', key='candidate:lin:2')
            self.assertIn('先切断电源', preview['body'])
            await call('studio_adopt_reference', slug='lin', version_number=2, expected_version_id=old.active_version_id)
            self.assertEqual(store.get_document('lin').state, 'accepted')
            self.assertEqual(store.get_document('lin').content, updated['version']['content'])
            with self.assertRaises(Exception):
                await call('studio_adopt_reference', slug='lin', version_number=2, expected_version_id=old.active_version_id)
            proposal = store.propose_fact(category='world', key='供能', value='需定时检修', rationale='作者提出')
            detail = entry(config, 'pending', f'proposal:{proposal.id}')
            self.assertIn('当前记录', detail['body']);self.assertIn('提案预览', detail['body'])
            await call('studio_accept_proposal', proposal_id=proposal.id, expected_fingerprint=detail['fingerprint'])
            self.assertEqual(store.list_facts()[0].value, '需定时检修')
            self.assertEqual(store.list_proposals(), [])
            self.assertEqual(store.get_version('lin', 1).content, saved['version']['content'])
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'LITERARYGIANT_REGISTRY_HOME': raw}):
            asyncio.run(exercise(Path(raw)))

    def test_declined_reference_and_proposal_actions_never_mutate(self):
        async def exercise(root):
            init_workspace(root);config = make_config(root);store = ProjectStore(root)
            server = create_server(config, require_elicitation=True)
            from lg_cli.mcp_approval import READ_TOOLS, WRITE_TOOLS
            self.assertEqual({t.name for t in await server.list_tools()},READ_TOOLS|WRITE_TOOLS)
            async def decline(**_):return SimpleNamespace(action='decline',data=None)
            with patch.object(server,'get_context',return_value=SimpleNamespace(elicit=decline)):
                with self.assertRaisesRegex(ValueError,'not approved'):
                    await server.call_tool('studio_reference',dict(kind='character',slug='lin',title='林砚',content='不得保存',expected_version_id=0,reason='test'))
            self.assertEqual(store.list_documents(kind='character'),[])
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'LITERARYGIANT_REGISTRY_HOME': raw}):
            asyncio.run(exercise(Path(raw)))
