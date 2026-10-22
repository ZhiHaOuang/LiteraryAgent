import json
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from scripts.compare_literary_models import Budget, MODELS, execute_case, known_cost, main, metrics, reviewed_pending_jobs, safe_response, worst_cost
from tests.helpers import make_config


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_budget_reserves_full_request_before_send_and_blocks_uncertainty(self):
        budget = Budget(self.root, Decimal('1'), Decimal(0))
        record = budget.begin('first', 'glm-4.5-air', {'input': 'synthetic'})
        persisted = json.loads((self.root / 'budget.json').read_text())
        self.assertEqual(persisted['requests'][0]['state'], 'intent_before_send')
        with self.assertRaises(ValueError):
            budget.begin('second', 'glm-4.5-air', {})
        budget.finish(record, {}, transport_complete=False)
        self.assertEqual(Decimal(record['heldCNY']), worst_cost('glm-4.5-air'))
        with self.assertRaises(ValueError):
            budget.begin('retry', 'glm-4.5-air', {})

    def test_no_network_is_allowed_over_budget_or_input_limit(self):
        budget = Budget(self.root, Decimal('.1'), Decimal(0))
        with self.assertRaises(ValueError):
            budget.begin('over', 'glm-4.6v', {})
        self.assertEqual(budget.records, [])
        with self.assertRaises(ValueError):
            budget.begin('too-large', 'glm-4.6v', {'x': 'a' * 65000})
        self.assertEqual(budget.records, [])

    def test_known_schema_rejection_keeps_full_reservation_and_does_not_weaken_output(self):
        budget = Budget(self.root, Decimal('5'), Decimal(0))
        record = budget.begin('schema-rejected', 'glm-4.6v', {})
        reserve = record['heldCNY']
        budget.finish(record, {'input_tokens': 1900, 'output_tokens': 1338}, transport_complete=False, host_schema_rejected=True)
        self.assertFalse(budget.pending)
        self.assertEqual(record['state'], 'host_schema_rejected')
        self.assertEqual(record['heldCNY'], reserve)
        self.assertIsNone(record['catalogueEstimateCNY'])
        budget.begin('different-task', 'glm-4.6v', {})

    def test_explicit_continuation_refuses_retry_and_dropped_prior_reservation(self):
        import hashlib
        previous = self.root / 'previous';previous.mkdir()
        raw = json.dumps({'heldCNY': '1.0856268', 'requests': [{'label': 'glm-4.5-air/P/lg'}]}).encode()
        (previous / 'budget.json').write_bytes(raw)
        (previous / 'plan.json').write_text(json.dumps({'cases':[{'id':'P'}], 'models':MODELS, 'maxOutputTokens':4096}))
        manifest = {'previousOutput': str(previous), 'previousBudgetSha256': hashlib.sha256(raw).hexdigest(),
                    'carriedReserveCNY': '1.0856268', 'neverReplaySentRequests': True,
                    'jobs': [['zhipu-air', 'P', 'baseline'], ['zhipu-vision', 'P', 'lg'], ['zhipu-vision', 'P', 'baseline']]}
        path = self.root / 'review.json';path.write_text(json.dumps(manifest))
        jobs, reserve, _ = reviewed_pending_jobs(path, [{'id': 'P'}])
        self.assertEqual(len(jobs), 3)
        self.assertEqual(reserve, Decimal('1.0856268'))
        manifest['jobs'].append(['zhipu-air', 'P', 'lg']);path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): reviewed_pending_jobs(path, [{'id': 'P'}])
        manifest['jobs'].pop();manifest['carriedReserveCNY']='0';path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): reviewed_pending_jobs(path, [{'id': 'P'}])

    def test_cost_and_requested_output_caps_include_thinking_usage(self):
        self.assertEqual(known_cost('glm-4.5-air', 1000, 500), Decimal('.0038'))
        budget = Budget(self.root, Decimal('5'), Decimal(0))
        record = budget.begin('bad-count', 'glm-4.6v', {})
        budget.finish(record, {'input_tokens': 10, 'output_tokens': 4097}, transport_complete=True)
        self.assertTrue(budget.pending)

    def test_private_reasoning_is_absent_from_saved_contract(self):
        value = safe_response({'model': 'glm-4.6v', 'status': 'completed', 'usage': {'output_tokens': 99},
            'encrypted_content': 'PRIVATE', 'output': [
                {'type': 'reasoning', 'content': [{'type': 'reasoning_text', 'text': 'PRIVATE'}]},
                {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'candidate'}]}]})
        self.assertNotIn('PRIVATE', json.dumps(value))
        self.assertEqual(value['output'][0]['content'][0]['text'], 'candidate')

    def test_plan_only_creates_no_books_or_model_calls_and_refuses_replay(self):
        output = self.root / 'new-plan'
        self.assertEqual(main(['--output', str(output)]), 0)
        self.assertFalse((output / 'synthetic-books').exists())
        self.assertEqual(json.loads((output / 'budget.json').read_text())['requests'], [])
        with self.assertRaises(ValueError):
            main(['--output', str(output), '--run'])

    def test_exact_outside_selection_change_is_detected(self):
        case = {'kind': 'polish', 'source': '前文。选中。后文。', 'passage': '选中。', 'forbiddenExact': []}
        self.assertTrue(metrics(case, '前文。修改。后文。')['outsideSelectionPreserved'])
        self.assertFalse(metrics(case, '前文被改。修改。后文。')['outsideSelectionPreserved'])

    def test_real_http_r5_and_baseline_accounting_do_not_adopt_or_log_secrets(self):
        seen = []
        owner = self
        self.invalid_schema = False
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(request)
                schema = ((request.get('text') or {}).get('format') or {}).get('schema', {})
                if 'revision_markdown' in schema.get('properties', {}):
                    payload = {'summary': 'synthetic revision', 'revision_markdown': '修改。',
                        'change_log': ['synthetic change'], 'memory_proposals': [], 'risks': [], 'book_overview': '合成故事。'}
                    if owner.invalid_schema:
                        payload.pop('summary')
                        payload.pop('risks')
                    text = json.dumps(payload, ensure_ascii=False)
                else:
                    text = '前文。修改。后文。'
                response = {'id': 'synthetic-response', 'model': 'glm-4.5-air', 'status': 'completed',
                    'usage': {'input_tokens': 100, 'output_tokens': 50}, 'output': [
                        {'type': 'reasoning', 'content': [{'type': 'reasoning_text', 'text': 'PRIVATE_CHAIN'}]},
                        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}]}
                events = [{'type': 'response.output_text.delta', 'delta': text}, {'type': 'response.completed', 'response': response}]
                self.send_response(200);self.send_header('Content-Type', 'text/event-stream');self.end_headers()
                for event in events:
                    self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n').encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        worker = threading.Thread(target=server.serve_forever, daemon=True);worker.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), worker.join(timeout=2)))
        case = {'id': 'SYNTHETIC', 'title': 'Test', 'kind': 'polish', 'source': '前文。选中。后文。',
                'passage': '选中。', 'request': '润色指定选段，其他文字逐字保持。', 'constraints': ['只改指定段'], 'forbiddenExact': []}
        budget = Budget(self.root / 'budget', Decimal('5'), Decimal(0))
        for arm in ('lg', 'baseline'):
            folder = self.root / 'outputs' / arm
            workspace = self.root / 'books' / arm
            config = replace(make_config(workspace), provider='responses-compatible', protocol='responses', runtime_engine='native',
                             default_model='glm-4.5-air', base_url=f'http://127.0.0.1:{server.server_port}', max_output_tokens=4096)
            result = execute_case(case, arm, folder, config, budget)
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['activeManuscriptUnchanged'])
            self.assertTrue(result['metrics']['outsideSelectionPreserved'])
            output = (folder / 'model-output.json').read_text()
            self.assertNotIn('PRIVATE_CHAIN', output)
            self.assertNotIn(config.api_key, (folder / 'model-input.json').read_text())
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(budget.records), 2)
        self.assertFalse(budget.pending)
        self.assertEqual([r['usage']['input_tokens'] for r in budget.records], [100, 100])
        self.invalid_schema = True
        rejected_folder = self.root / 'outputs' / 'rejected'
        rejected_workspace = self.root / 'books' / 'rejected'
        config = replace(config, workspace=rejected_workspace, memory_path=rejected_workspace/'.literarygiant/memory',
                         output_path=rejected_workspace/'.literarygiant/output', reference_path=rejected_workspace/'ReferenceLibrary')
        rejected = execute_case(case, 'lg', rejected_folder, config, budget)
        self.assertFalse(rejected['ok'])
        self.assertTrue(rejected['activeManuscriptUnchanged'])
        self.assertEqual(budget.records[-1]['state'], 'host_schema_rejected')
        self.assertTrue(budget.records[-1]['fullReservationRetained'])
        self.assertFalse(budget.pending)
        self.assertEqual(len(seen), 3)
