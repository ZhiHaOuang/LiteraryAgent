"""Host-bound expert work uses local models, real MCP lifecycle and no Codex."""
import asyncio
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.init_project import init_workspace
from lg_cli.native_delegation import NAME
from lg_cli.project_store import ProjectStore
from lg_cli.task_scope import bind_task
from tests.helpers import make_config
from tests.test_execution_adapter import specimen
from tests.test_native_runtime import answer


def delegate(experts=('continuity','critic'), **extra):
    return {'type':'function_call','call_id':'review-call','name':NAME,
        'arguments':json.dumps({'experts':list(experts),**extra})}


class NativeDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        init_workspace(self.root)
        self.store=ProjectStore(self.root)
        self.original=self.store.create_document(kind='chapter',slug='one',title='原稿',
            content='作者已采纳的原稿。',state='accepted',sequence=1)
        self.candidate=self.store.create_version('one',content='候选：林砚先隔离电源，再救出工友。',
            expected_version_id=self.original.active_version_id)
        self.definition={'kind':'review','document':'one','sourceVersion':self.original.active_version_id,
            'versionNumber':self.candidate.version_number}
        self.config=replace(make_config(self.root),runtime_engine='native',protocol='responses',
            default_model='main-model',critic_model='expert-model',timeout_seconds=15)
        self.events=[];self.backend=Backend(self.config,self.events.append)
        self.key=(await self.backend.dispatch('session/create',{}))['sessionId']
        self.forbid=patch('lg_cli.live_agent.LiveAgent',side_effect=AssertionError('No Codex'))
        self.forbid.start()

    async def asyncTearDown(self):
        await self.backend.close();self.forbid.stop();self.tmp.cleanup()

    async def run_review(self):
        return await self.backend.run(self.key,'请独立审读这个候选。',task=self.definition)

    async def finish(self, request):
        await self.backend.tasks[request['runId']][1]
        return next(e['params'] for e in reversed(self.events)
            if e['method']=='run/completed' and e['params']['runId']==request['runId'])

    def runtime(self):return self.backend.session(self.key)['agent']

    def results(self, runtime=None):
        return [json.loads(i['output']) for i in (runtime or self.runtime()).messages
            if i.get('type')=='function_call_output' and i.get('call_id')=='review-call']

    async def wait_for(self, predicate):
        for _ in range(500):
            if predicate():return
            await asyncio.sleep(.01)
        self.fail('Fixture did not reach the expected lifecycle state')

    def model(self, *, calls=None, expert=None, main_call=None):
        async def generate(_model, request, delta):
            if calls is not None:calls.append(request)
            if request['model']=='expert-model':
                if expert:return await expert(request,delta)
                delta('PRIVATE_EXPERT_DELTA')
                return answer(json.dumps(specimen(request['text']['format']['schema']),ensure_ascii=False))
            if any(i.get('type')=='function_call_output' for i in request['input']):
                return answer('主 Agent 综合意见。')
            return {'status':'completed','output':main_call or [delegate()]}
        return generate

    async def test_two_real_http_experts_run_concurrently_without_authority_or_history(self):
        requests=[];barrier=threading.Barrier(2);overlapped=[]
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(request)
                if request['model']=='expert-model':
                    barrier.wait(timeout=5);overlapped.append(True)
                    response=answer(json.dumps(specimen(request['text']['format']['schema']),ensure_ascii=False))
                elif any(i.get('type')=='function_call_output' for i in request['input']):
                    response=answer('主 Agent 综合意见。')
                else:response={'status':'completed','output':[delegate()]}
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                self.wfile.write(('data: '+json.dumps({'type':'response.completed','response':response})+'\n\n').encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        await self.backend.close()
        self.config=replace(self.config,base_url=f'http://127.0.0.1:{server.server_port}')
        self.backend=Backend(self.config,self.events.append);await self.backend.open(self.key)
        self.backend.store.append(self.key,'user','PRIVATE_PARENT_HISTORY')
        override=self.root/'.literarygiant/subagents/critic';override.mkdir(parents=True)
        (override/'agent.toml').write_text('prompt="/PRIVATE_MISSING_FILE"\ntools=["shell","write_file","delegate_review"]')
        before={str(p):p.read_bytes() for p in (self.root/'ReferenceLibrary').rglob('*') if p.is_file()}
        try:
            self.assertEqual((await self.finish(await self.run_review()))['status'],'completed')
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close();thread.join(timeout=2)
        experts=[r for r in requests if r['model']=='expert-model']
        self.assertEqual(len(requests),4);self.assertEqual(len(overlapped),2)
        for request in experts:
            self.assertEqual(request['tools'],[])
            self.assertNotIn('PRIVATE_PARENT_HISTORY',json.dumps(request))
            self.assertIn(self.candidate.content,json.dumps(request,ensure_ascii=False))
            self.assertLessEqual(request['max_output_tokens'],4096)
        self.assertEqual(self.store.get_document('one').active_version_id,self.original.active_version_id)
        self.assertEqual(len(self.store.list_versions('one')),2)
        self.assertEqual(self.store.list_facts(state='canonical'),[])
        self.assertEqual(before,{str(p):p.read_bytes() for p in (self.root/'ReferenceLibrary').rglob('*') if p.is_file()})
        self.assertEqual([r['text'] for r in self.backend.store.read(self.key) if r['role']=='assistant'],['主 Agent 综合意见。'])
        notices=[e['params']['item'] for e in self.events if e['method']=='client/event'
            and e['params'].get('type')=='item.updated' and e['params']['item'].get('structured',{}).get('task')]
        self.assertEqual({i['title'] for i in notices if i['state']=='completed'},{'continuity','critic'})
        result=self.results()[0]['structuredContent']
        self.assertTrue(result['provisional']);self.assertEqual(result['status'],'completed')
        self.assertEqual(self.runtime().runtime.read()['task']['grants'],0)

    async def test_extra_target_or_model_arguments_fail_before_experts(self):
        for extra in ({'document':'other-book'},{'model':'unapproved'},{'version':99},{'tools':['shell']}):
            calls=[]
            with patch('lg_cli.native_model.NativeModel.generate',self.model(calls=calls,main_call=[delegate(**extra)])):
                result=await self.finish(await self.run_review())
            self.assertEqual(result['status'],'failed');self.assertEqual(len(calls),1)
            self.assertFalse(self.runtime().pending)

    async def test_scene_candidate_has_explicit_host_target_and_real_state(self):
        from lg_cli.book_analysis import fingerprint
        from lg_cli.studio import review_source
        scene = self.store.create_scene(slug='gate', title='城门', chapter='one').document
        candidate = self.store.create_version(scene.id, content='候选场景：维修师检查城门。')
        self.definition = {'kind': 'review', 'document': scene.slug,
            'sourceVersion': scene.active_version_id, 'versionNumber': candidate.version_number}
        shared = review_source(self.store, scene.slug, candidate.version_number)
        calls = []
        with patch('lg_cli.native_model.NativeModel.generate', self.model(calls=calls)):
            self.assertEqual((await self.finish(await self.run_review()))['status'], 'completed')
        experts = [request for request in calls if request['model'] == 'expert-model']
        self.assertEqual(len(experts), 2)
        for request in experts:
            payload = json.loads(request['input'][0]['content'])
            self.assertEqual(payload['target'], {'documentId': scene.id, 'slug': scene.slug,
                'kind': 'scene', 'versionId': candidate.id, 'versionNumber': candidate.version_number,
                'state': 'candidate', 'activeVersionId': scene.active_version_id})
            target = next(doc for doc in payload['snapshot']['documents'] if doc['id'] == scene.id)
            self.assertEqual((target['version_id'], target['state'], target['content']),
                             (candidate.id, 'candidate', candidate.content))
            self.assertTrue(any(doc['id'] == self.original.id for doc in payload['snapshot']['documents']))
        self.assertEqual(self.results()[0]['structuredContent']['sourceFingerprint'], fingerprint(shared))
        self.assertEqual(review_source(self.store, scene.slug, candidate.version_number), shared)
        self.assertEqual(self.store.get_document(scene.id).active_version_id, scene.active_version_id)

    async def test_canonical_change_invalidates_both_expert_results(self):
        changed = False
        async def expert(request, delta):
            nonlocal changed
            if not changed:
                changed = True
                self.store.add_fact(category='world', key='gate', value='作者确认城门规则已变。', state='canonical')
            return answer(json.dumps(specimen(request['text']['format']['schema'])))
        with patch('lg_cli.native_model.NativeModel.generate', self.model(expert=expert)):
            await self.finish(await self.run_review())
        result = self.results()[0]['structuredContent']
        self.assertFalse(result['sourceCurrent'])
        self.assertEqual([child['state'] for child in result['experts']], ['failed', 'failed'])
        self.assertTrue(all(child['result'] is None for child in result['experts']))

    async def test_rejected_candidate_invalidates_both_expert_results(self):
        changed = False
        async def expert(request, delta):
            nonlocal changed
            if not changed:
                changed = True
                self.store.reject_version('one', self.candidate.version_number)
            return answer(json.dumps(specimen(request['text']['format']['schema'])))
        with patch('lg_cli.native_model.NativeModel.generate', self.model(expert=expert)):
            await self.finish(await self.run_review())
        result = self.results()[0]['structuredContent']
        self.assertFalse(result['sourceCurrent'])
        self.assertEqual([child['state'] for child in result['experts']], ['failed', 'failed'])
        self.assertEqual(self.store.get_document('one').active_version_id, self.original.active_version_id)

    async def test_same_active_version_state_change_invalidates_expert_target(self):
        from lg_cli.book_analysis import BookAnalysisStore, CATEGORIES, fingerprint
        from lg_cli.studio import review_source
        version = self.store.get_version('one', self.original.active_version_number)
        records = BookAnalysisStore(self.store)
        base = records.snapshot('one')
        final_snapshot = records.candidate_snapshot('one', version, final=True)
        for category in CATEGORIES:
            records.save(final_snapshot, category, {'summary': 'Synthetic review.', 'entries': [], 'findings': []},
                         'synthetic-final-review', candidate_id=version.id, base_snapshot=base)
        self.definition['versionNumber'] = version.version_number
        before = fingerprint(review_source(self.store, 'one', version.version_number))
        changed = False
        async def expert(request, delta):
            nonlocal changed
            if not changed:
                changed = True
                self.store.accept_version('one', version.version_number, final=True)
            return answer(json.dumps(specimen(request['text']['format']['schema'])))
        with patch('lg_cli.native_model.NativeModel.generate', self.model(expert=expert)):
            await self.finish(await self.run_review())
        self.assertEqual(self.store.get_document('one').active_version_id, self.original.active_version_id)
        self.assertEqual(self.store.get_version('one', version.version_number).state, 'final')
        self.assertEqual(fingerprint(review_source(self.store, 'one', version.version_number)), before)
        result = self.results()[0]['structuredContent']
        self.assertFalse(result['sourceCurrent'])
        self.assertEqual([child['state'] for child in result['experts']], ['failed', 'failed'])

    async def test_tool_is_not_available_without_a_review_task(self):
        calls=[]
        with patch('lg_cli.native_model.NativeModel.generate',self.model(calls=calls)):
            result=await self.finish(await self.backend.run(self.key,'讨论当前作品'))
        self.assertEqual(result['status'],'failed')
        self.assertNotIn(NAME,[t['name'] for t in calls[0]['tools']]);self.assertEqual(len(calls),1)

    async def test_expert_tool_requests_and_invalid_schema_are_private_failures(self):
        async def expert(request,delta):
            delta('PRIVATE_EXPERT_DELTA')
            if 'Expert role: continuity' in request['instructions']:
                return {'status':'completed','output':[{'type':'function_call','name':'shell','call_id':'bad','arguments':'{}'}]}
            return answer('PRIVATE_PROVIDER_BODY')
        with patch('lg_cli.native_model.NativeModel.generate',self.model(expert=expert)):
            self.assertEqual((await self.finish(await self.run_review()))['status'],'completed')
        result=self.results()[0]
        self.assertTrue(result['isError'])
        self.assertEqual([c['state'] for c in result['structuredContent']['experts']],['failed','failed'])
        self.assertNotIn('PRIVATE_',json.dumps(self.events))
        self.assertNotIn('PRIVATE_',self.runtime().checkpoint.read_text())

    async def test_repeated_expert_cannot_reset_parent_turn_budget(self):
        calls=[]
        base=self.model(calls=calls,main_call=[delegate(('critic',))])
        async def model(_model,request,delta):
            if request['model']=='main-model' and any(i.get('type')=='function_call_output' for i in request['input']):
                call=delegate(('critic',));call['call_id']='repeat'
                return {'status':'completed','output':[call]}
            return await base(_model,request,delta)
        with patch('lg_cli.native_model.NativeModel.generate',model):
            result=await self.finish(await self.run_review())
        self.assertEqual(result['status'],'failed');self.assertIn('budget',result['error'])
        self.assertEqual(len([r for r in calls if r['model']=='expert-model']),1)

    async def test_task_change_discards_expert_results(self):
        changed=False
        async def expert(request,delta):
            nonlocal changed
            if not changed:
                changed=True
                self.runtime().runtime.update(task=bind_task(self.root,self.definition))
            return answer(json.dumps(specimen(request['text']['format']['schema'])))
        with patch('lg_cli.native_model.NativeModel.generate',self.model(expert=expert)):
            await self.finish(await self.run_review())
        result=self.results()[0]['structuredContent']
        self.assertEqual(result['status'],'failed')
        self.assertFalse(result['sourceCurrent'])
        self.assertTrue(all(c['result'] is None for c in result['experts']))

    async def test_cancel_preserves_completed_expert_and_continues_without_replay(self):
        calls=[];waiting=asyncio.Event()
        async def expert(request,delta):
            if 'Expert role: critic' in request['instructions']:
                waiting.set();await asyncio.Event().wait()
            return answer(json.dumps(specimen(request['text']['format']['schema'])))
        with patch('lg_cli.native_model.NativeModel.generate',self.model(calls=calls,expert=expert)):
            request=await self.run_review();await asyncio.wait_for(waiting.wait(),5)
            await self.wait_for(lambda:any(c['state']=='completed' for r in self.runtime().delegations.records.values() for c in r['children']))
            original_runtime=self.runtime()
            await self.backend.cancel(self.key)
            self.assertEqual((await self.finish(request))['status'],'cancelled')
            self.assertFalse(original_runtime.pending)
            result=self.results(original_runtime)[0]['structuredContent']
            self.assertEqual(result['status'],'cancelled')
            self.assertEqual([c['state'] for c in result['experts']],['completed','cancelled'])
            await self.finish(await self.backend.run(self.key,'只总结已有意见'))
        self.assertEqual(len([r for r in calls if r['model']=='expert-model']),2)
        self.assertEqual(len(self.results()),1)

    async def test_mixed_pending_write_remains_unresolved_after_cancel(self):
        waiting=asyncio.Event()
        async def expert(*_):waiting.set();await asyncio.Event().wait()
        write={'type':'function_call','call_id':'possible-write','name':'update_book_overview',
            'arguments':json.dumps({'text':'这句话不得保存。'})}
        with patch('lg_cli.native_model.NativeModel.generate',self.model(expert=expert,main_call=[delegate(),write])):
            request=await self.run_review();await asyncio.wait_for(waiting.wait(),5)
            original_runtime=self.runtime()
            await self.backend.cancel(self.key);await self.finish(request)
        self.assertEqual(original_runtime.pending,['possible-write'])
        self.assertEqual(json.loads(original_runtime.checkpoint.read_text())['pending'],['possible-write'])
        with patch('lg_cli.native_model.NativeModel.generate',side_effect=AssertionError('No replay')):
            result=await self.finish(await self.backend.run(self.key,'继续'))
        self.assertEqual(result['status'],'failed');self.assertIn('unresolved',result['error'])

    async def test_crash_recovery_settles_read_only_pending_once_without_model(self):
        waiting=asyncio.Event()
        async def expert(*_):waiting.set();await asyncio.Event().wait()
        with patch('lg_cli.native_model.NativeModel.generate',self.model(expert=expert)):
            request=await self.run_review();await asyncio.wait_for(waiting.wait(),5)
            checkpoint=self.runtime().checkpoint;crash=checkpoint.read_text()
            await self.backend.cancel(self.key);await self.finish(request)
        await self.backend.close();checkpoint.write_text(crash)
        self.backend=Backend(self.config,self.events.append);await self.backend.open(self.key)
        with patch('lg_cli.native_model.NativeModel.generate',side_effect=AssertionError('No recovery request')):
            await self.runtime().start()
        self.assertFalse(self.runtime().pending);self.assertEqual(len(self.results()),1)
        self.assertEqual(self.results()[0]['structuredContent']['status'],'cancelled')
        await self.backend.close();self.backend=Backend(self.config,self.events.append);await self.backend.open(self.key)
        with patch('lg_cli.native_model.NativeModel.generate',side_effect=AssertionError('No duplicate request')):
            await self.runtime().start()
        self.assertEqual(len(self.results()),1)
        saved=json.loads(crash);saved['messages'][-1]['name']='edit_manuscript'
        await self.backend.close();checkpoint.write_text(json.dumps(saved))
        self.backend=Backend(self.config,self.events.append);await self.backend.open(self.key)
        with patch('lg_cli.native_model.NativeModel.generate',side_effect=AssertionError('No forged recovery')):
            with self.assertRaisesRegex(ValueError,'identity'):
                await self.runtime().start()

    async def test_queued_experts_settle_without_clearing_prior_uncertain_write(self):
        from lg_cli.book_analysis import fingerprint, source_spans
        from lg_cli.studio import review_source
        from tests.helpers import reference_card
        calls=[]
        source=review_source(self.store,'one',self.candidate.version_number)
        report={'summary':'审查救援选择。','entries':[{'instance_card':reference_card('CharacterArc'),
            'key':'choice','label':'救援选择','analysis':'人物隔离电源后救援。',
            'evidence':[{'span_id':next(iter(source_spans(source)))}]}],'findings':[]}
        write={'type':'function_call','call_id':'prior-write','name':'studio_review',
            'arguments':json.dumps({'slug':'one','version_number':self.candidate.version_number,
                'source_fingerprint':fingerprint(source),'reports':{'CharacterArc':report}})}
        with patch('lg_cli.native_model.NativeModel.generate',self.model(calls=calls,main_call=[write,delegate()])):
            run=await self.run_review()
            await self.wait_for(lambda:bool(self.backend.approvals.pending))
            original_runtime=self.runtime()
            self.assertTrue(all(c['state']=='waiting' for r in original_runtime.delegations.records.values() for c in r['children']))
            await self.backend.cancel(self.key);await self.finish(run)
        self.assertEqual(len(calls),1)
        self.assertEqual(original_runtime.pending,['prior-write'])
        result=self.results(original_runtime)[0]['structuredContent']
        self.assertEqual([c['state'] for c in result['experts']],['cancelled','cancelled'])
        with patch('lg_cli.native_model.NativeModel.generate',side_effect=AssertionError('No replay')):
            resumed=await self.finish(await self.backend.run(self.key,'继续'))
        self.assertEqual(resumed['status'],'failed');self.assertIn('unresolved',resumed['error'])

    async def test_checkpoint_failure_blocks_later_turns(self):
        from lg_cli.output_writer import _atomic_text
        def save(path,text):
            value=json.loads(text)
            if not value['pending'] and any(m.get('type')=='function_call_output' for m in value['messages']):
                raise OSError('PRIVATE_FILESYSTEM_ERROR')
            return _atomic_text(path,text)
        with patch('lg_cli.native_model.NativeModel.generate',self.model()),patch('lg_cli.native_runtime._atomic_text',save):
            result=await self.finish(await self.run_review())
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('PRIVATE_',json.dumps(self.events))
        with patch('lg_cli.native_model.NativeModel.generate',side_effect=AssertionError('No unsafe continuation')):
            result=await self.finish(await self.backend.run(self.key,'继续'))
        self.assertEqual(result['status'],'failed');self.assertIn('checkpoint',result['error'])

    async def test_source_change_never_publishes_a_premature_completed_task(self):
        waiting=asyncio.Event();release=asyncio.Event()
        async def expert(request,delta):
            if 'Expert role: critic' in request['instructions']:
                waiting.set();await release.wait()
            return answer(json.dumps(specimen(request['text']['format']['schema'])))
        with patch('lg_cli.native_model.NativeModel.generate',self.model(expert=expert)):
            run=await self.run_review();await asyncio.wait_for(waiting.wait(),5)
            await self.wait_for(lambda:any(c['state']=='completed' for r in self.runtime().delegations.records.values() for c in r['children']))
            self.store.create_version('one',content='作者重新修订了正式稿。',state='accepted',activate=True,
                expected_version_id=self.original.active_version_id)
            release.set();await self.finish(run)
        result=self.results()[0]['structuredContent']
        self.assertFalse(result['sourceCurrent'])
        self.assertEqual([c['state'] for c in result['experts']],['failed','failed'])
        notices=[e['params']['item'] for e in self.events if e['method']=='client/event'
            and e['params'].get('type')=='item.updated' and e['params']['item'].get('structured',{}).get('task')]
        self.assertFalse(any(i['state']=='completed' for i in notices))
        self.assertEqual({i['title'] for i in notices if i['state']=='failed'},{'continuity','critic'})

    async def test_parent_timeout_settles_experts_without_replay(self):
        calls=[]
        async def expert(request,delta):
            if 'Expert role: critic' in request['instructions']:
                await asyncio.Event().wait()
            return answer(json.dumps(specimen(request['text']['format']['schema'])))
        self.backend.config=replace(self.config,timeout_seconds=1)
        self.runtime().config=self.backend.config
        with patch('lg_cli.native_model.NativeModel.generate',self.model(calls=calls,expert=expert)):
            result=await self.finish(await self.run_review())
            self.assertEqual(result['status'],'failed')
            self.assertFalse(self.runtime().pending)
            self.assertEqual(len(self.results()),1)
            self.assertEqual(self.results()[0]['structuredContent']['status'],'cancelled')
            self.assertEqual((await self.finish(await self.backend.run(self.key,'总结已返回意见')))['status'],'completed')
        self.assertEqual(len([r for r in calls if r['model']=='expert-model']),2)

    async def test_late_expert_cannot_overwrite_cancelled_checkpoint(self):
        waiting=asyncio.Event();returned=[]
        async def expert(request,delta):
            waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                try:await asyncio.sleep(.02)
                except asyncio.CancelledError:pass
                returned.append(True)
                return answer(json.dumps(specimen(request['text']['format']['schema'])))
        with patch('lg_cli.native_model.NativeModel.generate',self.model(expert=expert)):
            run=await self.run_review();await asyncio.wait_for(waiting.wait(),5)
            original_runtime=self.runtime()
            await self.backend.cancel(self.key);await self.finish(run)
        self.assertTrue(returned)
        result=self.results(original_runtime)[0]['structuredContent']
        self.assertEqual([c['state'] for c in result['experts']],['cancelled','cancelled'])
        self.assertTrue(all(c['result'] is None for c in result['experts']))
        saved=json.loads(original_runtime.checkpoint.read_text())
        outputs=[i for i in saved['messages'] if i.get('type')=='function_call_output']
        self.assertEqual(len(outputs),1);self.assertFalse(saved['pending']);self.assertFalse(saved['delegations'])
        self.assertEqual(json.loads(outputs[0]['output'])['structuredContent'],result)

    async def test_context_budget_includes_instructions_and_schema(self):
        from lg_cli.book_analysis import source_spans
        from lg_cli.studio import review_source
        candidate=self.store.create_version('one',content='文'*1200,
            expected_version_id=self.original.active_version_id)
        self.definition['versionNumber']=candidate.version_number
        source=review_source(self.store,'one',candidate.version_number)
        payload=json.dumps({'snapshot':source,'source_spans':source_spans(source),'focus':''},ensure_ascii=False)
        self.assertLess(len(payload),4000)
        self.backend.config=replace(self.config,max_stage_context_chars=4000)
        calls=[]
        with patch('lg_cli.native_model.NativeModel.generate',self.model(calls=calls)):
            result=await self.finish(await self.run_review())
        self.assertEqual(result['status'],'failed');self.assertIn('context budget',result['error'])
        self.assertEqual(len(calls),1)
