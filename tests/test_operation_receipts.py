"""Commit receipts must survive lost results without replaying formal writes."""
import asyncio
import json
import tempfile
import subprocess
import sys
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.project_store import ProjectStore, ProjectStoreError
from tests.helpers import make_config


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ProjectStore(self.root)
        self.store.initialize()
        self.original = self.store.create_document(kind='chapter', slug='one', title='One',
            content='Original', state='accepted')
        self.candidate = self.store.create_version('one', content='Candidate',
            expected_version_id=self.original.active_version_id)
        self.identity = uuid.uuid4().hex
        self.details = {'tool': 'studio_accept', 'arguments': {'slug': 'one',
            'version_number': self.candidate.version_number,
            'expected_version_id': self.original.active_version_id}}

    def scope(self):
        from lg_cli.operation_receipts import operation_scope
        return operation_scope(self.root, self.identity, self.details, 'a' * 64)

    def receipt(self):
        from lg_cli.operation_receipts import lookup
        return lookup(self.root, self.identity)

    def adopt(self):
        return self.store.accept_version('one', self.candidate.version_number,
            expected_version_id=self.original.active_version_id)

    def test_post_commit_exception_preserves_exact_effect_and_forbids_replay(self):
        with self.assertRaisesRegex(OSError, 'lost result'):
            with self.scope():
                self.adopt()
                raise OSError('lost result')
        result = self.receipt()
        self.assertEqual(result['status'], 'committed')
        self.assertEqual(result['effects'][0]['versionId'], self.candidate.id)
        self.assertFalse(result['replayAllowed'])
        self.assertEqual(result['toolOutcome'], 'failed')
        self.assertEqual(result['derivedFiles'], 'not_attested')
        with self.assertRaisesRegex(ValueError, 'already'):
            with self.scope():
                self.fail('Must not replay')

    def test_transaction_failure_is_not_committed_and_rolls_back_receipt(self):
        with self.assertRaises(ProjectStoreError):
            with self.scope():
                self.store.accept_version('one', self.candidate.version_number, expected_version_id=-1)
        self.assertEqual(self.receipt()['status'], 'not_committed')
        self.assertEqual(self.receipt()['effects'], [])
        self.assertEqual(self.store.get_document('one').active_version_id, self.original.active_version_id)

    def test_receipt_write_failure_rolls_back_formal_state(self):
        with patch('lg_cli.operation_receipts.record_transaction', side_effect=OSError('receipt unavailable')):
            with self.assertRaises(OSError):
                with self.scope():
                    self.adopt()
        self.assertEqual(self.receipt()['status'], 'not_committed')
        self.assertEqual(self.store.get_document('one').active_version_id, self.original.active_version_id)

    def test_unknown_and_legacy_operations_cannot_claim_noncommit(self):
        self.assertEqual(self.receipt()['status'], 'unknown')
        from lg_cli.operation_receipts import operation_scope
        with operation_scope(self.root, None, self.details, 'a' * 64):
            self.adopt()
        self.assertEqual(self.receipt()['status'], 'unknown')

    def test_same_id_changed_request_is_rejected(self):
        with self.scope():
            self.adopt()
        self.details['arguments']['version_number'] = 1
        with self.assertRaisesRegex(ValueError, 'binding'):
            with self.scope():
                self.fail('Changed binding must not execute')

    def test_second_transaction_rolls_back_without_hiding_first_commit(self):
        with self.assertRaisesRegex(ValueError, 'second write transaction'):
            with self.scope():
                self.adopt()
                self.store.add_fact(category='world', key='extra', value='Must not commit', state='canonical')
        self.assertEqual(self.receipt()['status'], 'committed')
        self.assertEqual(len(self.receipt()['effects']), 1)
        self.assertEqual(self.receipt()['toolOutcome'], 'failed')
        self.assertEqual(self.store.list_facts(), [])

    def test_process_exit_keeps_pending_unknown_and_committed_proven(self):
        code = '''import json, os, sys
from pathlib import Path
from lg_cli.operation_receipts import operation_scope
from lg_cli.project_store import ProjectStore
root, identity, details, commit = sys.argv[1:]
details = json.loads(details)
with operation_scope(Path(root), identity, details, "a" * 64):
    if commit == "yes":
        args = details["arguments"]
        ProjectStore(Path(root)).accept_version(args["slug"], args["version_number"], expected_version_id=args["expected_version_id"])
    os._exit(9)
'''
        for commit in ('no', 'yes'):
            self.identity = uuid.uuid4().hex
            process = subprocess.run([sys.executable, '-B', '-c', code, str(self.root), self.identity,
                json.dumps(self.details), commit], env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'lg-cli')},
                capture_output=True, timeout=10)
            self.assertEqual(process.returncode, 9, process.stderr.decode())
            self.assertEqual(self.receipt()['status'], 'committed' if commit == 'yes' else 'unknown')
            self.assertEqual(self.receipt()['toolOutcome'], 'unknown')

    def test_proposal_precondition_survives_receipt_registration(self):
        from lg_cli.operation_receipts import lookup
        from lg_cli.workspace_library import _stamp
        from lg_cli.writing_mcp import create_server
        proposal = self.store.propose_fact(category='world', key='rule', value='Author approved rule')
        config = make_config(self.root)
        async def approve(details):
            return {'decision': 'accept', 'operationId': self.identity}
        async def call():
            server = create_server(config, approval_handler=approve)
            return await server.call_tool('studio_accept_proposal', {
                'proposal_id': proposal.id, 'expected_fingerprint': _stamp(config)})
        asyncio.run(call())
        self.assertEqual(self.store.get_proposal(proposal.id).status, 'accepted')
        result = lookup(self.root, self.identity)
        self.assertEqual(result['status'], 'committed')
        self.assertEqual(result['effects'][0]['proposalId'], proposal.id)

    def test_lookup_during_commit_cannot_report_false_noncommit(self):
        from lg_cli import operation_receipts
        original = operation_receipts._read
        injected = False
        def complete_between_reads(path, identity):
            nonlocal injected
            value = original(path, identity)
            if not injected:
                injected = True
                with self.scope():
                    self.adopt()
            return value
        with patch.object(operation_receipts, '_read', complete_between_reads):
            self.assertEqual(self.receipt()['status'], 'committed')

    def test_decision_and_knowledge_tools_record_the_exact_new_head(self):
        from lg_cli.writing_mcp import create_server
        from tests.test_creative_decision_tools import options_arguments
        from tests.test_narrative_knowledge_tools import proposal_arguments
        self.store.save_decision_options(**options_arguments(self.original))
        holder = self.store.create_document(kind='character', slug='ada', title='Ada')
        self.store.propose_knowledge(**proposal_arguments(holder))
        async def execute(tool, args):
            self.identity = uuid.uuid4().hex
            async def approve(details):
                return {'decision': 'accept', 'operationId': self.identity}
            await create_server(make_config(self.root), approval_handler=approve).call_tool(tool, args)
        for tool, slug in [('studio_select_decision', 'crossroads'), ('studio_withdraw_decision', 'crossroads'),
                           ('studio_knowledge_confirm', 'gate-belief'), ('studio_knowledge_withdraw', 'gate-belief')]:
            with self.subTest(tool=tool):
                document = self.store.get_document(slug)
                args = {'slug': slug, 'expected_version_id': document.active_version_id}
                if tool == 'studio_select_decision':
                    args.update(options_version_number=1, option_id='rescue')
                if tool == 'studio_knowledge_confirm':
                    args['proposal_version_number'] = 1
                asyncio.run(execute(tool, args))
                self.assertEqual(self.receipt()['status'], 'committed')
                self.assertEqual(self.receipt()['effects'][0]['versionId'], self.store.get_document(slug).active_version_id)
                effect = self.receipt()['effects'][0]
                self.assertEqual(effect['state'], 'draft')
                self.assertEqual(effect['domainStatus'], {
                    'studio_select_decision': 'selected', 'studio_withdraw_decision': 'withdrawn',
                    'studio_knowledge_confirm': 'confirmed', 'studio_knowledge_withdraw': 'withdrawn'}[tool])
                self.assertNotIn('content', effect)

    def test_missing_operation_id_preserves_legacy_tool_behavior(self):
        from lg_cli.writing_mcp import create_server
        async def approve(details):
            return {'decision': 'accept'}
        async def execute():
            return await create_server(make_config(self.root), approval_handler=approve).call_tool(
                self.details['tool'], self.details['arguments'])
        asyncio.run(execute())
        self.assertEqual(self.store.get_document('one').active_version_id, self.candidate.id)
        self.assertEqual(self.receipt()['status'], 'unknown')

    def test_reads_inside_scope_do_not_create_commit_evidence(self):
        with self.scope():
            self.store.get_document('one')
            self.store.list_versions('one')
            self.store.get_version('one', self.candidate.version_number)
        self.assertEqual(self.receipt()['status'], 'not_committed')
        self.assertEqual(self.receipt()['effects'], [])

    def test_reference_adoption_has_one_exact_effect(self):
        from lg_cli.writing_mcp import create_server
        reference = self.store.create_document(kind='world', slug='world', title='World',
            content='Original world', state='accepted')
        candidate = self.store.create_version('world', content='Proposed world', expected_version_id=reference.active_version_id)
        async def approve(details):
            return {'decision': 'accept', 'operationId': self.identity}
        async def execute():
            await create_server(make_config(self.root), approval_handler=approve).call_tool('studio_adopt_reference', {
                'slug': 'world', 'version_number': candidate.version_number, 'expected_version_id': reference.active_version_id})
        asyncio.run(execute())
        self.assertEqual(self.receipt()['status'], 'committed')
        self.assertEqual(self.receipt()['effects'], [{'kind': 'document', 'documentId': reference.id,
            'slug': 'world', 'versionId': candidate.id, 'state': 'accepted'}])

    def test_proposal_effect_excludes_unchanged_same_key_idea(self):
        from lg_cli.operation_receipts import operation_scope
        idea = self.store.add_fact(category='world', key='rule', value='Unrelated idea', state='idea')
        proposal = self.store.propose_fact(category='world', key='rule', value='Confirmed rule')
        details = {'tool': 'studio_accept_proposal', 'arguments': {'proposal_id': proposal.id}}
        with operation_scope(self.root, self.identity, details, 'a' * 64):
            actual = self.store.accept_proposal(proposal.id)
        self.assertEqual(self.receipt()['effects'][0]['facts'], [{'factId': actual.id, 'state': 'canonical'}])
        self.assertEqual(self.store.get_fact(idea.id), idea)


class NativeReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_commit_then_lost_delivery_cancel_is_queryable(self):
        from lg_cli.backend import Backend
        from lg_cli.native_tools import NativeTools
        from lg_cli.operation_receipts import lookup
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize()
            original = store.create_document(kind='chapter', slug='one', title='One',
                content='Original', state='accepted')
            candidate = store.create_version('one', content='Approved candidate',
                expected_version_id=original.active_version_id)
            config = replace(make_config(root), runtime_engine='native', protocol='responses',
                default_model='synthetic', timeout_seconds=15)
            backend = Backend(config)
            delivered = asyncio.Event()
            identities = []
            real_call = NativeTools.call
            async def lost_delivery(tools, name, arguments):
                result = await real_call(tools, name, arguments)
                if getattr(result, 'isError', False):
                    raise AssertionError(str(result))
                delivered.set()
                await asyncio.Event().wait()
            async def model(_model, request, delta):
                return {'status': 'completed', 'output': [{'type': 'function_call', 'call_id': 'adopt',
                    'name': 'studio_accept', 'arguments': json.dumps({'slug': 'one',
                        'version_number': candidate.version_number, 'expected_version_id': original.active_version_id})}]}
            try:
                key = (await backend.dispatch('session/create', {}))['sessionId']
                control = backend.session(key)['control']
                real_approve = control.approval_handler
                async def approve(*args, **kwargs):
                    result = await real_approve(*args, **kwargs)
                    identity = result.get('operationId') or uuid.uuid4().hex
                    identities.append(identity)
                    return {**result, 'operationId': identity}
                control.approval_handler = approve
                await backend.dispatch('permissions/set', {'sessionId': key, 'mode': 'full_access',
                    'creativeMode': 'full_access'})
                with patch('lg_cli.native_model.NativeModel.generate', model), \
                        patch.object(NativeTools, 'call', lost_delivery), \
                        patch('lg_cli.live_agent.LiveAgent', side_effect=AssertionError('No Codex')):
                    run = await backend.run(key, 'Adopt the exact candidate', task={'kind': 'adopt',
                        'document': 'one', 'sourceVersion': original.active_version_id,
                        'versionNumber': candidate.version_number})
                    await asyncio.wait_for(delivered.wait(), 10)
                    self.assertEqual(lookup(root, identities[-1])['status'], 'committed')
                    await backend.cancel(key)
                    await backend.tasks[run['runId']][1]
                self.assertEqual(store.get_document('one').active_version_id, candidate.id)
                self.assertEqual(lookup(root, identities[-1])['effects'][0]['versionId'], candidate.id)
                with patch('lg_cli.native_model.NativeModel.generate', side_effect=AssertionError('No replay')):
                    retry = await backend.run(key, 'Continue')
                    await backend.tasks[retry['runId']][1]
                self.assertEqual(len(store.list_versions('one')), 2)
            finally:
                await backend.close()
