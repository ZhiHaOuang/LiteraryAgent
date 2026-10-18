"""Canonical changes require current provenance, even after author approval."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.mcp_approval import operation_details
from lg_cli.project_store import ProjectStore, ProjectStoreError
from lg_cli.workspace_library import library, entry, writing_context
from lg_cli.writing_mcp import create_server
from tests.helpers import make_config


class ProposalSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ProjectStore(self.root)
        self.store.initialize(name='提案来源')
        self.source = self.store.create_document(kind='chapter', slug='one', title='来源',
            content='闸门由定时供能驱动。', state='accepted')
        self.config = make_config(self.root)

    def proposal(self, version=None):
        return self.store.propose_fact(category='world', key='闸门供能', value='定时供能',
            source_document='one', source_version=version, rationale='基于来源原文')

    def test_current_source_is_bound_even_when_version_is_omitted(self):
        proposal = self.proposal()
        self.assertEqual(proposal.source_version_id, self.source.active_version_id)
        self.assertEqual(self.store.proposal_source_status(proposal.id)['status'], 'current')
        fact = self.store.accept_proposal(proposal.id)
        self.assertEqual((fact.state, fact.source_version_id), ('canonical', self.source.active_version_id))

    def test_pending_candidate_can_only_supply_canon_after_separate_adoption(self):
        version = self.store.create_version('one', content='候选文本的新事实。',
            expected_version_id=self.source.active_version_id)
        proposal = self.proposal(version.version_number)
        before = self.store.summary()
        with self.assertRaisesRegex(ProjectStoreError, 'source is pending'):
            self.store.accept_proposal(proposal.id)
        self.assertEqual(self.store.summary(), before)
        self.assertEqual(self.store.list_proposals()[0].status, 'proposed')
        preview = entry(self.config, 'pending', f'proposal:{proposal.id}')
        self.assertIn('先采纳来源', preview['body'])
        self.assertEqual(preview['actions'], [])
        with self.assertRaisesRegex(ValueError, 'not available'):
            writing_context(self.config, 'pending', preview['key'], 'adopt', preview['fingerprint'])
        self.store.accept_version('one', version.version_number)
        self.assertEqual(self.store.list_facts(state='canonical'), [])
        self.assertEqual(self.store.accept_proposal(proposal.id).source_version_id, version.id)

    def test_old_source_is_rejected_even_with_a_fresh_library_preview(self):
        proposal = self.proposal()
        self.store.edit_active_document('one', content='作者修订为人工驱动。',
            expected_version_id=self.source.active_version_id, reason='修订正文')
        preview = entry(self.config, 'pending', f'proposal:{proposal.id}')
        self.assertIn('来源已被新版本替换', preview['body'])
        self.assertEqual(preview['actions'], [])
        with self.assertRaisesRegex(ProjectStoreError, 'source is stale'):
            self.store.accept_proposal(proposal.id)
        with self.assertRaisesRegex(ValueError, 'source is stale'):
            operation_details(self.root, 'studio_accept_proposal', {'proposal_id': proposal.id})
        self.assertEqual(self.store.list_facts(), [])
        self.assertEqual(self.store.list_proposals()[0].id, proposal.id)

    def test_rejected_or_archived_sources_never_promote(self):
        for state in ('rejected', 'archived'):
            with self.subTest(state=state):
                source = self.store.create_document(kind='note', slug=state, title=state,
                    content='无效来源', state=state)
                proposal = self.store.propose_fact(category='world', key=state, value='无效事实',
                    source_document=source.id)
                with self.assertRaisesRegex(ProjectStoreError, 'source is unavailable'):
                    self.store.accept_proposal(proposal.id)
        self.assertEqual(self.store.list_facts(), [])

    def test_canonical_creation_and_promotion_share_the_source_gate(self):
        candidate = self.store.create_version('one', content='未采用来源',
            expected_version_id=self.source.active_version_id)
        for operation in (self.store.add_fact, self.store.set_fact):
            with self.assertRaisesRegex(ProjectStoreError, 'source is pending'):
                operation(category='world', key='供能', value='候选', state='canonical',
                    source_document='one', source_version=candidate.version_number)
        idea = self.store.add_fact(category='world', key='供能', value='候选',
            source_document='one', source_version=candidate.version_number)
        with self.assertRaisesRegex(ProjectStoreError, 'source is pending'):
            self.store.promote_fact(idea.id)
        self.assertEqual(self.store.get_fact(idea.id).state, 'idea')
        self.assertEqual(len(self.store.fact_history(idea.id)), 1)

    def test_independent_author_design_remains_separately_confirmable(self):
        proposal = self.store.propose_fact(category='world', key='世界规则', value='作者独立设定')
        self.assertEqual(self.store.proposal_source_status(proposal.id)['status'], 'unbound')
        self.assertIn('adopt', entry(self.config, 'pending', f'proposal:{proposal.id}')['actions'])
        self.assertEqual(self.store.accept_proposal(proposal.id).value, '作者独立设定')

    def test_source_check_holds_a_write_transaction_against_another_connection(self):
        proposal = self.proposal()
        original = self.store._require_current_source
        def check(connection, document_id, version_id):
            original(connection, document_id, version_id)
            other = sqlite3.connect(self.store.database_path, timeout=0)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    other.execute('BEGIN IMMEDIATE')
            finally:
                other.close()
        with patch.object(self.store, '_require_current_source', check):
            self.store.accept_proposal(proposal.id)
        self.assertEqual(self.store.get_document('one').active_version_id, self.source.active_version_id)

    def test_mcp_extraction_records_source_and_missing_version_does_not_rebase_on_adoption(self):
        async def execute():
            server = create_server(self.config)
            result = await server.call_tool('propose_setting', {'category': 'world', 'key': '闸门供能',
                'value': '定时供能', 'rationale': '原文提取', 'source_document': 'one'})
            return result[1] if isinstance(result, tuple) else json.loads(result[0].text)
        proposal = asyncio.run(execute())
        self.assertEqual(proposal['source_version_id'], self.source.active_version_id)
        self.store.edit_active_document('one', content='新来源',
            expected_version_id=self.source.active_version_id, reason='作者保存')
        with self.assertRaisesRegex(ProjectStoreError, 'source is stale'):
            self.store.accept_proposal(proposal['id'])
        self.assertEqual(self.store.list_facts(), [])
