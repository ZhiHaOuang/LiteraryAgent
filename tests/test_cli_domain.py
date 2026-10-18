import json
import unittest
import signal
import subprocess
import sys
import threading
import tempfile
import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.conversation_store import ConversationStore
from lg_cli.book_analysis import BookAnalysisStore
from lg_cli.session_runtime import SessionRuntime
from tests import test_cli_backend as fixture


class DomainCLITests(unittest.TestCase):
    setUp=fixture.CLIBackendTests.setUp
    invoke=fixture.CLIBackendTests.invoke
    initialize=fixture.CLIBackendTests.initialize
    provider=fixture.CLIBackendTests.provider

    def input(self,value):
        path=self.root/'studio-input.json';path.write_text(json.dumps(value,ensure_ascii=False));return str(path)

    def test_studio_preview_leaves_empty_workspace_and_confirm_uses_backend(self):
        code,stdout,stderr=self.invoke('studio','initialize','--json')
        self.assertEqual(code,2,stderr+stdout);self.assertEqual(list(self.root.iterdir()),[])
        preview=json.loads(stdout)
        code,stdout,stderr=self.invoke('--approve-fingerprint',preview['fingerprint'],'studio','initialize','--json')
        self.assertEqual(code,0,stderr+stdout);self.assertTrue(json.loads(stdout)['initialized'])
        self.assertEqual(len(ConversationStore(self.root).list()),1)

    def test_studio_confirm_preserves_candidate_and_cannot_bypass_review(self):
        self.initialize()
        original=self.store.create_document(kind='chapter',slug='prose',title='正文',content='作者正式正文。',state='accepted')
        path=self.input({'slug':'prose','title':'正文','content':'只保存候选。','sequence':1,
            'expected_version_id':original.active_version_id,'reason':'明确作者输入'})
        code,stdout,stderr=self.invoke('studio','draft','--input',path,'--confirm','--json')
        self.assertEqual(code,0,stderr+stdout)
        candidate=json.loads(stdout)['version']
        path=self.input({'slug':'prose','version_number':candidate['version_number'],'expected_version_id':original.active_version_id})
        code,stdout,stderr=self.invoke('studio','accept','--input',path,'--confirm','--json')
        self.assertEqual(code,1,stderr+stdout);self.assertIn('Missing or stale',stdout)
        self.assertEqual(self.store.get_document('prose').content,'作者正式正文。')

    def test_real_analysis_chapter_and_volume_keep_final_json_and_reports(self):
        from tests.test_execution_adapter import specimen
        self.initialize();self.store.create_document(kind='chapter',slug='chapter',title='Chapter',content='守卫关上城门。',state='accepted')
        seen=self.provider(lambda value:json.dumps(specimen(value['text']['format']['schema']),ensure_ascii=False))
        for args,count in ((('chapter','chapter','--category','Worldview','--category','Worldview'),1),
                (('volume','__book__','--category','EventsLibrary'),2)):
            code,stdout,stderr=self.invoke(*(['--model','analysis-model'] if count==1 else []),'analysis',*args,'--json')
            self.assertEqual(code,0,stderr+stdout)
            result=json.loads(stdout.splitlines()[-1]);self.assertEqual(result['status'],'completed')
            self.assertEqual(len(seen),count)
        self.assertEqual([value['model'] for value in seen],['analysis-model','synthetic-cli'])
        self.assertEqual(BookAnalysisStore(self.store).list('chapter')[0]['status'],'current')
        self.assertEqual(BookAnalysisStore(self.store).list('__book__',volume=True)[0]['status'],'current')

    def test_analysis_private_provider_failure_and_worker_marker_are_rejected(self):
        self.initialize();self.store.create_document(kind='chapter',slug='chapter',title='Chapter',content='原文')
        self.provider(lambda _:'PRIVATE_PROVIDER_BODY')
        code,stdout,stderr=self.invoke('analysis','chapter','chapter','--category','Worldview','--json')
        self.assertEqual(code,1,stderr+stdout);self.assertNotIn('PRIVATE_PROVIDER_BODY',stdout+stderr)
        code,stdout,_=self.invoke('--backend-worker','studio','initialize','--confirm','--json')
        self.assertEqual(code,1);self.assertIn('owned worker',stdout)

    def test_analysis_blockers_keep_legacy_exit_code_and_verified_result(self):
        from lg_cli.book_analysis import source_spans
        self.initialize();self.store.create_document(kind='chapter',slug='chapter',title='Chapter',content='城门打开了。',state='accepted')
        fact=self.store.add_fact(category='world',key='gate',value='城门永不打开。',state='canonical')
        span=next(iter(source_spans(BookAnalysisStore(self.store).snapshot('chapter'))))
        report={'summary':'原文违反正式约束','entries':[],'findings':[{'kind':'canon_conflict','explanation':'城门打开与正式规则矛盾。',
            'fact_id':fact.id,'evidence':[{'span_id':span}]}]}
        self.provider(lambda _:json.dumps(report,ensure_ascii=False))
        code,stdout,stderr=self.invoke('analysis','chapter','chapter','--category','Worldview','--json')
        self.assertEqual(code,1,stderr+stdout)
        result=json.loads(stdout.splitlines()[-1]);self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['blockers'][0]['fact_id'],fact.id)
        self.assertEqual(self.store.get_document('chapter').content,'城门打开了。')

    def test_analysis_sigint_retains_completed_report_and_releases_session(self):
        from tests.test_execution_adapter import specimen
        self.initialize();self.store.create_document(kind='chapter',slug='chapter',title='Chapter',content='守卫守住城门。',state='accepted')
        held=threading.Event();release=threading.Event();calls=[]
        def reply(value):
            calls.append(value)
            if len(calls)==2:held.set();release.wait(20)
            return json.dumps(specimen(value['text']['format']['schema']),ensure_ascii=False)
        self.provider(reply)
        key=ConversationStore(self.root).create()
        process=subprocess.Popen([sys.executable,'-u',str(Path(fixture.__file__).resolve()),self.provider_endpoint,
            '-C',str(self.root),'--runtime-engine','native','--conversation',key,'analysis','chapter','chapter',
            '--category','Worldview','--category','EventsLibrary','--json'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertTrue(held.wait(20),'Second category did not reach the local fixture')
            process.send_signal(signal.SIGINT)
            stdout,stderr=process.communicate(timeout=12)
            self.assertEqual(process.returncode,130,stdout+stderr)
            records=BookAnalysisStore(self.store).list('chapter')
            self.assertEqual([item['library'] for item in records],['Worldview'])
            runtime=SessionRuntime(self.root,key);runtime.acquire();runtime.release()
        finally:
            release.set()
            if process.poll() is None:process.kill();process.communicate(timeout=5)

    def test_interactive_studio_and_analysis_share_one_backend_and_present_once(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from lg_cli.main import interactive_loop
        from lg_cli.config import load_config
        from lg_cli.terminal_input import LiteraryInput
        from tests.test_execution_adapter import specimen
        self.initialize()
        original=self.store.create_document(kind='chapter',slug='chapter',title='Chapter',content='作者正式正文。',state='accepted')
        self.provider(lambda value:json.dumps(specimen(value['text']['format']['schema']),ensure_ascii=False))
        config=replace(load_config(self.root,environment='cli-domain-interactive-no-profile'),runtime_engine='native',
            provider='openai',protocol='responses',api_key='synthetic-key',default_model='synthetic-cli',
            base_url=self.provider_endpoint,enable_reference=False,max_stage_context_chars=200000)
        path=self.input({'slug':'chapter','title':'Chapter','content':'待审候选。','sequence':1,
            'expected_version_id':original.active_version_id,'reason':'明确作者请求'})
        with create_pipe_input() as pipe:
            session=LiteraryInput(self.root/'history',input=pipe,output=DummyOutput(),usage_path=self.root/'usage.json')
            original_run=session.run_command
            async def run_command(argv,**options):
                if argv[1:4]==['-u','-m','lg_cli']:
                    argv=[sys.executable,'-u',str(Path(fixture.__file__).resolve()),self.provider_endpoint,*argv[4:]]
                return await original_run(argv,**options)
            def run(handler,*_,**__):
                async def exercise():
                    try:
                        await handler('/studio draft --input '+path)
                        self.assertEqual(len(ConversationStore(self.root).list()),0)
                        await handler('/studio draft --input '+path+' --confirm')
                        await handler('/analysis chapter chapter --category Worldview')
                        self.assertEqual(self.store.get_document('chapter').content,'作者正式正文。')
                        sessions=ConversationStore(self.root).list();self.assertEqual(len(sessions),1)
                        self.assertEqual([row['role'] for row in ConversationStore(self.root).read(sessions[0][0])],['user','assistant']*2)
                        self.assertEqual(session.transcript.count('"categories": ["Worldview"]'),1)
                    finally:await session.shutdown_handler()
                    return 0
                return asyncio.run(exercise())
            with patch('lg_cli.main.LiteraryInput',return_value=session),patch('lg_cli.main.load_config',return_value=config),\
                    patch.object(session,'run',run),patch.object(session,'run_command',run_command),\
                    patch.object(session,'ask',side_effect=AssertionError('Exact author command requested another approval')):
                self.assertEqual(interactive_loop(config),0)


class TerminalRecordOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_callback_owns_worker_records_before_terminal_presentation(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from lg_cli.terminal_input import LiteraryInput
        with tempfile.TemporaryDirectory() as raw,create_pipe_input() as pipe:
            root=Path(raw);records=[]
            session=LiteraryInput(root/'history',input=pipe,output=DummyOutput(),usage_path=root/'usage.json')
            code=await session.run_command([sys.executable,'-c',
                'import json; print(json.dumps({"type":"error","error":"PRIVATE_WORKER_BODY"}))'],
                structured=True,on_record=records.append)
            self.assertEqual(code,0)
            self.assertEqual(records[0]['error'],'PRIVATE_WORKER_BODY')
            self.assertNotIn('PRIVATE_WORKER_BODY',session.transcript)
