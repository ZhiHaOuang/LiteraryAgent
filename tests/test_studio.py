import asyncio
import json
import os
import tempfile
import unittest
import jsonschema
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from lg_cli.writing_mcp import create_server
from lg_cli.mcp_approval import ApprovingMCP, WriteConfirmation
from tests.helpers import make_config, reference_card


class StudioTests(unittest.TestCase):
    def test_full_shared_workflow_and_stale_review_guard(self):
        async def exercise(root):
            server = create_server(make_config(root))
            async def call(name, **args):
                result = await server.call_tool(name, args)
                if isinstance(result, tuple):
                    return result[1]
                return json.loads(result[0].text)
            await call('studio_initialize')
            version = 0
            for stage in ('position', 'plan', 'world', 'scene'):
                result = await call('studio_save', stage=stage, content='原创测试资料：维修师在封闭浮城寻找停电原因。',
                    updates={'positioning': '原创机械悬疑', 'targetWords': 1000000, 'volumes': ['第一卷：停电之夜']}, expected_profile_version=version)
                version = result['profileVersion']
            store = ProjectStore(root)
            self.assertTrue(list((root/'ReferenceLibrary/plans').rglob('*.md')))
            self.assertTrue(list((root/'ReferenceLibrary/bible').rglob('*.md')))
            draft = await call('studio_draft', slug='first', title='停电之夜', content='钟塔停了。许栎握住发烫的铜柄，决定先救困在闸门后的陌生人。', sequence=1, expected_version_id=0, reason='原创流程测试')
            active = store.get_document('first').active_version_id
            with self.assertRaises(Exception):
                await call('studio_accept', slug='first', version_number=1, expected_version_id=active)
            context = await call('studio_review_context', slug='first', version_number=1)
            span = next(iter(context['source_spans']))
            reports = {category: {'summary': '测试审阅：行动动机存在原文依据。', 'entries': [{
                'instance_card': reference_card(category), 'key': 'choice', 'label': '救援选择',
                'analysis': '人物选择承受风险救援他人。', 'evidence': [{'span_id': span}]}], 'findings': []}
                for category in context['schemas']}
            tools = await server.list_tools()
            review_schema = next(tool.inputSchema for tool in tools if tool.name == 'studio_review')
            jsonschema.Draft202012Validator.check_schema(review_schema)
            arguments = dict(slug='first', version_number=1,
                source_fingerprint=context['sourceFingerprint'], reports=reports)
            jsonschema.validate(arguments, review_schema)
            for invalid in ({}, {'Unknown': reports['CharacterArc']},
                            {'CharacterArc': {'summary': 'Missing required evidence and entries'}}):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate({**arguments, 'reports': invalid}, review_schema)
            for index, (category, report) in enumerate(reports.items()):
                saved = await call('studio_review', slug='first', version_number=1,
                    source_fingerprint=context['sourceFingerprint'], reports={category: report})
                self.assertEqual(saved['reviewed'], [category])
                if index < len(reports) - 1:
                    with self.assertRaisesRegex(Exception, 'Missing or stale .* candidate review'):
                        await call('studio_accept', slug='first', version_number=1, expected_version_id=active)
                    self.assertEqual(store.get_version('first', 1).state, 'candidate')
                    self.assertEqual((await call('book_dashboard'))['acceptedWords'], 0)
            accepted = await call('studio_accept', slug='first', version_number=1, expected_version_id=active)
            self.assertTrue(accepted['accepted'])
            with self.assertRaises(Exception):
                await call('studio_accept', slug='first', version_number=1, expected_version_id=active)
            overview = await call('book_dashboard')
            self.assertEqual(overview['targetWords'], 1000000)
            self.assertGreater(overview['acceptedWords'], 0)
            self.assertEqual(overview['acceptedWords'], overview['writtenWords'])
            self.assertEqual(overview['source'], 'backend')
            revision = await call('studio_draft', slug='first', title='停电之夜', content='钟塔停了。许栎先切断电源，随后拉开闸门救人。', sequence=1, expected_version_id=active, reason='增加动作因果')
            self.assertEqual(store.get_document('first').content, draft['version']['content'])
            context2 = await call('studio_review_context', slug='first', version_number=revision['version']['version_number'])
            store.set_fact(category='world', key='gate', value='闸门通电时不能触摸', state='canonical')
            with self.assertRaises(Exception):
                await call('studio_review', slug='first', version_number=revision['version']['version_number'], source_fingerprint=context2['sourceFingerprint'], reports=reports)
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'LITERARYGIANT_REGISTRY_HOME': raw}):
            asyncio.run(exercise(Path(raw)))

    def test_decline_and_changed_project_never_write(self):
        async def exercise(root):
            init_workspace(root)
            server = create_server(make_config(root), require_elicitation=True)
            self.assertIsInstance(server, ApprovingMCP)
            async def decline(**_):
                return SimpleNamespace(action='decline', data=None)
            with patch.object(server, 'get_context', return_value=SimpleNamespace(elicit=decline)):
                with self.assertRaisesRegex(ValueError, 'not approved'):
                    await server.call_tool('update_book_overview', {'text': '不得写入'})
            self.assertFalse((root/'ReferenceLibrary/bible/book-overview.json').exists())
            async def stale(**_):
                ProjectStore(root).add_fact(category='world', key='changed', value='changed')
                return SimpleNamespace(action='accept', data=WriteConfirmation(confirm=True))
            with patch.object(server, 'get_context', return_value=SimpleNamespace(elicit=stale)):
                with self.assertRaisesRegex(ValueError, 'changed during approval'):
                    await server.call_tool('update_book_overview', {'text': '不得写入'})
            self.assertFalse((root/'ReferenceLibrary/bible/book-overview.json').exists())
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'LITERARYGIANT_REGISTRY_HOME': raw}):
            asyncio.run(exercise(Path(raw)))
