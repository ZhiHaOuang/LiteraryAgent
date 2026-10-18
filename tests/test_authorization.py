"""Regression coverage for Agent proposals, trusted author edits and adoption preconditions."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.project_store import ProjectStore, ProjectStoreError
from lg_cli.writing_mcp import create_server
from tests.helpers import make_config


class AuthorControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ProjectStore(self.root)
        self.store.initialize()
        self.original = self.store.create_document(kind='chapter', slug='one', title='一',
            content='已采纳正文', state='accepted')

    def call(self, name, **arguments):
        async def execute():
            server = create_server(make_config(self.root))
            result = await server.call_tool(name, arguments)
            return result[1] if isinstance(result, tuple) else json.loads(result[0].text)
        return asyncio.run(execute())

    def test_agent_edit_and_restore_preserve_accepted_text_until_adoption(self):
        revision = self.call('edit_manuscript', slug='one', content='Agent 建议稿',
            expected_version_id=self.original.active_version_id, reason='局部修改',
            book_overview='人物寻找遗失的信件。')
        self.assertEqual((revision['state'], revision['applied']), ('candidate', False))
        self.assertEqual(self.store.get_document('one'), self.original)
        restored = self.call('restore_manuscript', slug='one', version_number=1,
            expected_version_id=self.original.active_version_id)
        self.assertEqual(restored['state'], 'candidate')
        self.assertEqual(self.store.get_document('one'), self.original)
        adopted = self.call('studio_accept', slug='one', version_number=revision['version_number'],
            expected_version_id=self.original.active_version_id)
        self.assertTrue(adopted['accepted'])
        self.assertEqual(self.store.get_document('one').content, 'Agent 建议稿')
        self.assertEqual(self.store.get_version('one', 1).content, '已采纳正文')

    def test_author_edit_remains_direct_and_invalidates_older_candidate(self):
        candidate = self.store.create_version('one', content='旧基线建议',
            expected_version_id=self.original.active_version_id)
        author_edit = self.store.edit_active_document('one', content='作者亲自修改',
            expected_version_id=self.original.active_version_id, reason='编辑器保存')
        with self.assertRaisesRegex(ProjectStoreError, 'older active version'):
            self.store.accept_version('one', candidate.version_number,
                expected_version_id=author_edit.id)
        self.assertEqual(self.store.get_document('one').content, '作者亲自修改')
        self.assertEqual(self.store.get_version('one', candidate.version_number).state, 'candidate')

    def test_change_between_tool_preview_and_candidate_transaction_is_rejected(self):
        original_create = ProjectStore.create_version
        def concurrent_edit(store, *args, **kwargs):
            self.store.edit_active_document('one', content='另一个作者保存',
                expected_version_id=self.original.active_version_id, reason='并发编辑')
            return original_create(store, *args, **kwargs)
        with patch.object(ProjectStore, 'create_version', concurrent_edit):
            with self.assertRaisesRegex(Exception, 'changed since it was read'):
                self.call('studio_draft', slug='one', title='一', content='过期建议',
                    sequence=1, expected_version_id=self.original.active_version_id, reason='建议')
        self.assertEqual(self.store.get_document('one').content, '另一个作者保存')
        self.assertEqual(len(self.store.list_versions('one')), 2)
        self.assertFalse((self.root / 'ReferenceLibrary/drafts/candidates').exists())

    def test_change_between_adoption_preview_and_transaction_is_rejected(self):
        candidate = self.store.create_version('one', content='建议',
            expected_version_id=self.original.active_version_id)
        original_accept = ProjectStore.accept_version
        def concurrent_edit(store, *args, **kwargs):
            self.store.edit_active_document('one', content='批准后的外部修改',
                expected_version_id=self.original.active_version_id, reason='并发编辑')
            return original_accept(store, *args, **kwargs)
        with patch.object(ProjectStore, 'accept_version', concurrent_edit):
            with self.assertRaisesRegex(Exception, 'changed after preview'):
                self.call('studio_accept', slug='one', version_number=candidate.version_number,
                    expected_version_id=self.original.active_version_id)
        self.assertEqual(self.store.get_document('one').content, '批准后的外部修改')
        self.assertEqual(self.store.get_version('one', candidate.version_number).state, 'candidate')

    def test_new_agent_chapter_is_candidate(self):
        created = self.call('create_chapter', slug='two', title='二', content='新章候选',
            sequence=2, book_overview='人物寻找遗失的信件。')
        self.assertEqual(created['state'], 'candidate')
        self.assertEqual(self.store.get_version('two', 1).state, 'candidate')
        self.assertEqual(self.store.get_document('one'), self.original)

    def test_adoption_approval_previews_saved_candidate_before_any_write(self):
        from types import SimpleNamespace
        from lg_cli.mcp_approval import operation_details
        candidate = self.store.create_version('one', content='具体候选正文',
            expected_version_id=self.original.active_version_id)
        arguments = {'slug': 'one', 'version_number': candidate.version_number,
            'expected_version_id': self.original.active_version_id}
        details = operation_details(self.root, 'studio_accept', arguments)
        self.assertEqual(details['preview']['version_id'], candidate.id)
        self.assertIn('具体候选正文', details['preview']['diff'])
        self.assertIn('已采纳正文', details['preview']['diff'])
        self.assertEqual(self.store.get_document('one'), self.original)
        messages = []
        async def decline(**request):
            messages.append(request['message'])
            return SimpleNamespace(action='decline', data=None)
        server = create_server(make_config(self.root), require_elicitation=True)
        with patch.object(server, 'get_context', return_value=SimpleNamespace(elicit=decline)):
            with self.assertRaisesRegex(ValueError, 'not approved'):
                asyncio.run(server.call_tool('studio_accept', arguments))
        self.assertIn('具体候选正文', messages[0])
        self.assertEqual(self.store.get_document('one'), self.original)
