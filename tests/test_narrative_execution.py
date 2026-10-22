"""Strict registered-input projection crosses a real local Responses HTTP boundary."""
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lg_cli.narrative_execution import OUTPUT_SCHEMA_PATH, prepare, run
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config


class NarrativeExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ProjectStore(self.root)
        self.store.initialize(name='GLOBAL_SECRET')
        self.holder = self.store.create_document(kind='character', slug='holder', title='PROFILE_SECRET',
            content='PROFILE_SECRET', state='accepted')
        self.other = self.store.create_document(kind='character', slug='other', title='Other')
        self.source = self.store.create_document(kind='chapter', slug='source', title='SOURCE_SECRET',
            content='SOURCE_SECRET', state='accepted')
        self.known = self.statement('known', 'NEEDED_EARLY_CLUE', start=0)
        self.statement('future', 'FUTURE_SECRET', start=20)
        self.statement('other-known', 'OTHER_SECRET', holder=self.other.id)
        self.store.set_fact(category='world', key='truth', value='CANONICAL_SECRET')
        memory = self.root/'.literarygiant/memory'
        memory.mkdir(parents=True, exist_ok=True)
        (memory/'STYLE_GUIDE.md').write_text('MEMORY_SECRET')
        history = self.root/'.literarygiant/conversations'
        history.mkdir(parents=True, exist_ok=True)
        (history/'old.jsonl').write_text('HISTORY_SECRET')
        self.seen, self.during, self.tool_output = [], None, False
        self.response_text = None
        owner = self
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                owner.seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                if owner.during: owner.during()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                statement = json.loads(owner.seen[-1]['input'][0]['content'])['visible_statements'][0]
                value = {'inference':'Provisional action based on '+statement['content'],
                    'evidence':[{'record_id':statement['record_id'],'quote':statement['content']}],
                    'uncertainty':'Goals and situation are not fully registered.'}
                text = owner.response_text if owner.response_text is not None else json.dumps(value)
                output = [{'type':'function_call','name':'read_document','call_id':'forbidden',
                    'arguments':'{}'}] if owner.tool_output else [
                    {'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}]
                events = [{'type':'response.output_text.delta','delta':text},
                    {'type':'response.completed','response':{'status':'completed','output':[
                        *output]}}]
                try:
                    for event in events:
                        self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n').encode())
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(('127.0.0.1',0), Provider)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)
        self.config = replace(make_config(self.root), runtime_engine='native', protocol='responses',
            default_model='local-narrative', project_instructions='INSTRUCTIONS_SECRET',
            base_url=f'http://127.0.0.1:{self.server.server_port}')
        self.arguments = dict(audience='character', position=5, holder_document=self.holder.id,
            operation='character_next_action')

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(timeout=2)

    def statement(self, slug, content, *, start=0, holder=None, audience='character', sources=None):
        record = self.store.propose_knowledge(slug=slug, title='TITLE_SECRET',
            topic={'audience':audience,'holder_document_id':None if audience=='reader' else holder or self.holder.id,
                'key':slug,'from_position':start},
            statement={'content':content,'stance':'believes' if audience=='character' else 'knows',
                'until_position':None,'sources':sources or []}, rationale='RATIONALE_SECRET', expected_version_id=0)
        return self.store.confirm_knowledge(slug, proposal_version_number=record['proposal_version_number'],
            expected_version_id=record['version_id'])

    def test_real_payload_includes_needed_record_and_excludes_all_other_inputs(self):
        before = self.store.get_document(self.source.id)
        with patch.dict('os.environ', {'LG_WORKFLOW_GUIDANCE':'GUIDANCE_SECRET'}), \
                patch('lg_cli.execution_adapter.CodexExecAdapter', side_effect=AssertionError('No Codex')):
            result = run(self.config, **self.arguments)
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['provisional'])
        self.assertTrue(result['requestSent'])
        self.assertTrue(result['resultReleased'])
        self.assertEqual(result['evidenceValidation'],
            {'contract':'visible_record_exact_quote.v1','status':'validated'})
        self.assertEqual(result['analysis']['evidence'],[{'record_id':self.store.get_document('known').id,
            'quote':'NEEDED_EARLY_CLUE'}])
        self.assertEqual(result['budget']['unit'],'host_prompt_and_instructions_characters')
        self.assertEqual(len(self.seen), 1)
        payload = json.dumps(self.seen[0])
        self.assertIn('NEEDED_EARLY_CLUE', payload)
        for secret in ('GLOBAL_SECRET','PROFILE_SECRET','SOURCE_SECRET','FUTURE_SECRET','OTHER_SECRET',
                       'CANONICAL_SECRET','MEMORY_SECRET','HISTORY_SECRET','INSTRUCTIONS_SECRET',
                       'RATIONALE_SECRET','TITLE_SECRET','GUIDANCE_SECRET'):
            self.assertNotIn(secret, payload)
        self.assertEqual(self.seen[0]['tools'], [])
        self.assertEqual(self.seen[0]['text']['format']['schema'],json.loads(OUTPUT_SCHEMA_PATH.read_text()))
        self.assertTrue(self.seen[0]['text']['format']['strict'])
        self.assertEqual(len(self.seen[0]['input']), 1)
        self.assertEqual(self.store.get_document(self.source.id), before)

    def test_reader_uses_reading_axis_and_cannot_bind_character(self):
        self.statement('reader', 'NEEDED_READER_CLUE', audience='reader')
        args = dict(audience='reader', position=1, operation='reader_expectation')
        preview = prepare(self.config, **args)
        self.assertEqual(preview['status'], 'ready')
        self.assertFalse(preview['requestSent'])
        self.assertFalse(preview['resultReleased'])
        self.assertEqual(preview['binding']['axis'], 'reading')
        result = run(self.config, **args)
        self.assertEqual(result['status'], 'completed')
        self.assertIn('NEEDED_READER_CLUE', json.dumps(self.seen))
        self.assertNotIn('NEEDED_EARLY_CLUE', json.dumps(self.seen))
        with self.assertRaises(ValueError): prepare(self.config, holder_document=self.holder.id, **args)

    def test_free_text_and_mismatched_operation_never_reach_model(self):
        with self.assertRaises(ValueError): run(self.config, question='FUTURE_SECRET', **self.arguments)
        with self.assertRaises(ValueError): prepare(self.config, **{**self.arguments,'operation':'reader_expectation'})
        self.assertEqual(self.seen, [])

    def test_empty_truncated_and_oversized_context_do_not_call_model(self):
        self.assertEqual(run(self.config, **{**self.arguments,'position':-1})['status'], 'insufficient_context')
        self.statement('second', 'Another visible clue')
        self.assertEqual(run(self.config, limit=1, **self.arguments)['status'], 'insufficient_context')
        self.assertEqual(run(self.config, max_input_chars=500, **self.arguments)['status'], 'insufficient_context')
        self.assertEqual(self.seen, [])

    def test_stale_registered_sources_stop_instead_of_silent_omission(self):
        self.statement('sourced', 'SOURCED_CLUE', sources=[{'document_id':self.source.id,
            'version_id':self.source.active_version_id}])
        self.store.edit_active_document(self.source.id, content='Changed',
            expected_version_id=self.source.active_version_id, reason='author')
        result = run(self.config, **self.arguments)
        self.assertEqual(result['status'], 'insufficient_context')
        self.assertIn('source_needs_review', result['reasons'])
        self.assertEqual(self.seen, [])

    def test_changed_preview_and_changed_during_generation_discard_result(self):
        preview = prepare(self.config, **self.arguments)
        self.statement('new', 'New visible clue')
        self.assertEqual(run(self.config, expected_fingerprint=preview['fingerprint'],
            **self.arguments)['status'], 'stale')
        self.assertEqual(self.seen, [])
        self.during = lambda: self.store.withdraw_knowledge('known', expected_version_id=self.known['version_id'])
        result = run(self.config, **self.arguments)
        self.assertEqual(result['status'], 'stale')
        self.assertNotIn('text', result)

    def test_cancel_before_and_during_request_never_publishes_text(self):
        self.assertEqual(run(self.config, cancelled=lambda:True, **self.arguments)['status'], 'cancelled')
        self.assertEqual(self.seen, [])
        cancelled = threading.Event()
        self.during = cancelled.set
        result = run(self.config, cancelled=cancelled.is_set, **self.arguments)
        self.assertEqual(result['status'], 'cancelled')
        self.assertNotIn('text', result)

    def test_unsupported_runtime_and_protocol_do_not_fallback(self):
        for config in (replace(self.config,runtime_engine='codex'), replace(self.config,protocol='anthropic')):
            self.assertEqual(run(config, **self.arguments)['status'], 'unsupported')
        self.assertEqual(self.seen, [])

    def test_preview_binds_provider_endpoint_model_and_output_budget(self):
        preview = prepare(self.config, self.arguments)
        for changes in ({'default_model':'other-model'}, {'provider':'another-provider'},
                        {'base_url':'http://127.0.0.1:9'}, {'max_output_tokens':100}):
            with self.subTest(changes=changes):
                result = run(replace(self.config, **changes), self.arguments,
                    expected_fingerprint=preview['fingerprint'])
                self.assertEqual(result['status'], 'stale')
                self.assertEqual(result['reasons'], ['preview_changed'])
        self.assertEqual(self.seen, [])

    def test_unavailable_tool_output_is_rejected_without_publishing_stream(self):
        self.tool_output = True
        result = run(self.config, self.arguments)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('text', result)
        self.assertEqual(len(self.seen), 1)
        self.assertTrue(result['requestSent'])
        self.assertFalse(result['resultReleased'])

    def test_http_unknown_evidence_id_is_rejected_without_publication_or_retry(self):
        self.response_text = json.dumps({'inference':'根据登记线索，人物可能先等待。',
            'evidence':[{'record_id':999999,'quote':'NEEDED_EARLY_CLUE'}],
            'uncertainty':'没有登记人物目标，不能确定行动。'})
        result = run(self.config, self.arguments)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['reasons'], ['invalid_evidence'])
        self.assertTrue(result['requestSent'])
        self.assertFalse(result['resultReleased'])
        self.assertNotIn('text', result)
        self.assertNotIn('analysis', result)
        self.assertEqual(len(self.seen), 1)

    def test_http_future_other_observer_and_forged_quotes_are_rejected(self):
        known = self.store.get_document('known').id
        cases = [
            {'record_id':self.store.get_document('future').id,'quote':'FUTURE_SECRET'},
            {'record_id':self.store.get_document('other-known').id,'quote':'OTHER_SECRET'},
            {'record_id':known,'quote':'FABRICATED_OUTPUT_SECRET'},
            {'record_id':known,'quote':'needed_early_clue'},
            {'record_id':float(known),'quote':'NEEDED_EARLY_CLUE'},
        ]
        for evidence in cases:
            with self.subTest(evidence=evidence):
                self.response_text = json.dumps({'inference':'UNVERIFIED_OUTPUT_SECRET',
                    'evidence':[evidence],'uncertainty':'Still provisional.'})
                result = run(self.config,self.arguments)
                self.assertEqual(result['status'],'failed')
                self.assertEqual(result['reasons'],['invalid_evidence'])
                self.assertFalse(result['resultReleased'])
                self.assertNotIn('text',result)
                self.assertNotIn('analysis',result)
                self.assertNotIn('UNVERIFIED_OUTPUT_SECRET',json.dumps(result))
        self.assertEqual(len(self.seen),len(cases), 'Invalid evidence must not trigger retries')

    def test_http_empty_invalid_and_unbounded_structure_never_publishes(self):
        valid = {'inference':'A provisional inference.','evidence':[{
            'record_id':self.store.get_document('known').id,'quote':'NEEDED_EARLY_CLUE'}],
            'uncertainty':'Missing situation.'}
        cases = ['', 'not JSON', json.dumps({**valid,'evidence':[]}),
            json.dumps({**valid,'evidence':[{'record_id':True,'quote':'NEEDED_EARLY_CLUE'}]}),
            json.dumps({**valid,'evidence':[{'record_id':valid['evidence'][0]['record_id'],'quote':'  '}]}),
            json.dumps({**valid,'inference':'  '}),json.dumps({**valid,'uncertainty':'  '}),
            json.dumps({**valid,'inference':'x'*4001}),json.dumps({**valid,'extra':'not allowed'})]
        for text in cases:
            with self.subTest(text=text[:60]):
                self.response_text=text
                result=run(self.config,self.arguments)
                self.assertEqual(result['status'],'failed')
                self.assertFalse(result['resultReleased'])
                self.assertNotIn('text',result)
                self.assertNotIn('analysis',result)
        self.assertEqual(len(self.seen),len(cases))

    def test_output_schema_and_contract_are_bound_to_preview(self):
        preview=prepare(self.config,self.arguments)
        changed=json.loads(OUTPUT_SCHEMA_PATH.read_text())
        changed['properties']['inference']['maxLength']=3999
        local=self.root/'changed-output-schema.json'
        local.write_text(json.dumps(changed))
        for changes in ({'OUTPUT_SCHEMA_PATH':local},{'EVIDENCE_CONTRACT':'new-contract'}):
            with self.subTest(changes=changes),patch.multiple('lg_cli.narrative_execution',**changes):
                result=run(self.config,self.arguments,expected_fingerprint=preview['fingerprint'])
                self.assertEqual(result['status'],'stale')
                self.assertEqual(result['reasons'],['preview_changed'])
        self.assertEqual(self.seen,[])

    def test_exact_partial_quote_is_accepted_without_claiming_inference_correctness(self):
        self.response_text=json.dumps({'inference':'A deliberately unproven inference.',
            'evidence':[{'record_id':self.store.get_document('known').id,'quote':'EARLY_CLUE'}],
            'uncertainty':'A valid quote does not prove this inference.'})
        result=run(self.config,self.arguments)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['analysis']['evidence'][0]['quote'],'EARLY_CLUE')
        self.assertTrue(any('not inference correctness' in limit for limit in result['limitations']))
