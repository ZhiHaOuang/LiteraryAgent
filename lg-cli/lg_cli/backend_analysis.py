"""Source-bound specialist analysis through the shared workflow lifecycle."""
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from .book_analysis import BookAnalysisStore,CATEGORIES,fingerprint
from .project_store import ProjectStore
from .run_store import RunStore
from .workflow_io import WorkflowFailure,public_workflow_event


def prepare(config,params):
    store=ProjectStore(config.workspace)
    volume=params.get('volume',False)
    chapter=params['chapter']
    if not (volume and chapter=='__book__'):chapter=store.get_document(chapter,kind='part' if volume else 'chapter').slug
    categories=list(dict.fromkeys(params.get('categories') or ([params['category']] if 'category' in params else CATEGORIES)))
    if not categories or any(value not in CATEGORIES for value in categories):raise ValueError('Unknown analysis category')
    records=BookAnalysisStore(store)
    snapshot=records.volume_snapshot(chapter) if volume else records.snapshot(chapter)
    if not any(doc['content'].strip() for doc in snapshot['documents']):raise ValueError('Cannot analyze empty source text')
    value={'chapter':chapter,'categories':categories,'volume':volume,'sourceFingerprint':fingerprint(snapshot),
        'model':params.get('model'),'noReference':params.get('noReference',not config.enable_reference)}
    return {**value,'fingerprint':hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()}


def start(backend,key,params):
    selected=prepare(backend.config,params)
    if params.get('expectedFingerprint') is not None and params['expectedFingerprint']!=selected['fingerprint']:
        raise ValueError('Analysis preview changed; select the current source again')
    state=backend.session(key)
    chapter,categories=selected['chapter'],selected['categories']
    selection={'chapter':chapter,'categories':categories,'volume':selected['volume'],'noReference':selected['noReference'],
        **({'model':selected['model']} if selected['model'] else {})}
    result={};saved_attempts=set()
    def unchanged():
        if prepare(backend.config,selection)!=selected:raise ValueError('Analysis source changed; start a new analysis')
    async def approve(job=None):
        unchanged()
        details={'operation':'analysis',**selected,**({'category':categories[0]} if len(categories)==1 else {})}
        request=state['control'].approval_handler(details,run_id=state['workflow_run_id'])
        if job is None:decision=await request
        else:
            pending=asyncio.create_task(request);stopped=asyncio.create_task(job['stop_event'].wait())
            try:
                await asyncio.wait((pending,stopped),return_when=asyncio.FIRST_COMPLETED)
                if job['stop_requested']:raise asyncio.CancelledError()
                decision=await pending
            finally:
                pending.cancel();stopped.cancel()
                await asyncio.gather(pending,stopped,return_exceptions=True)
        if decision.get('decision')!='accept':raise asyncio.CancelledError()
        unchanged()
    def record(value):
        kind=value.get('type')
        if kind=='analysis.result':result.clear();result.update(value)
        elif kind in {'run.started','run.completed','run.failed','stage.started','stage.completed','stage.failed'}:
            public=public_workflow_event(value)
            run=state['workflow_run_id']
            backend.emit(key,'workflow/record',{'record':public},run_id=run)
            if kind.startswith('stage.'):
                backend.emit(key,'task/updated',{'taskId':run+':analysis:'+str(value.get('stage_id')),
                    'name':str(value.get('stage_id') or 'analysis'),'step':public.get('message',''),
                    'status':{'stage.started':'running','stage.completed':'completed','stage.failed':'failed'}[kind]},run_id=run)
    def outcome(code):
        if state['control'].status()['status']=='stopped':raise asyncio.CancelledError()
        if code:raise WorkflowFailure('Analysis workflow failed; inspect saved analysis runs',exit_code=code if code>0 else 1)
        unchanged()
        run=result.get('run_id')
        if not isinstance(run,str) or Path(run).name!=run or run in {'.','..'}:raise ValueError('Analysis returned no verified run')
        actual=RunStore(backend.config.workspace).load(run)
        records=BookAnalysisStore(ProjectStore(backend.config.workspace)).list(chapter,volume=selected['volume'])
        saved={value['library']:value for value in records}
        if (actual['command']!='analysis' or actual['status']!='completed' or actual['request']!=chapter
                or actual['completed_stages']!=categories or result.get('categories')!=categories
                or any(category not in saved or saved[category]['run_id']!=run or saved[category]['status']!='current'
                    or saved[category]['fingerprint']!=selected['sourceFingerprint'] for category in categories)):
            raise ValueError('Analysis result does not match the saved source and categories')
        blockers=[{'library':category,**finding} for category in categories for finding in saved[category]['report']['findings']
            if finding['kind']=='canon_conflict']
        status='blocked' if blockers else 'completed'
        if result.get('status')!=status:raise ValueError('Analysis final status does not match saved reports')
        public={'run_id':run,'status':status,'categories':categories,'blockers':blockers}
        attempt=(state['workflow_run_id'],state['control'].status().get('attempt',1))
        if attempt not in saved_attempts:
            item='workflow-assistant:'+attempt[0]+':analysis:'+str(attempt[1])
            text=json.dumps(public,ensure_ascii=False)
            backend.store.append(key,'assistant',text,run_id=attempt[0],item_id=item,workflow_run=run)
            backend.emit(key,'client/event',{'type':'item.updated','sessionId':key,'item':{
                'id':item,'runId':attempt[0],'kind':'assistant','title':'LG','text':text,'state':'completed'}})
            saved_attempts.add(attempt)
        return {'status':status,'analysisResult':public}
    def retry(job):
        if state['control'].job is not job:raise ValueError('Analysis retry owner is stale')
        result.clear()
        async def execute_retry():
            host=state['control'].session
            host.begin_work();animation=asyncio.create_task(host._animate_progress())
            try:
                await approve(job)
                return outcome(await state['control']._execute(job))
            except BaseException as exc:
                if state['control'].active:
                    job['status']='stopped' if isinstance(exc,asyncio.CancelledError) else 'failed'
                    state['control']._publish();state['control']._changed.set()
                raise
            finally:
                animation.cancel();host.end_work()
                await asyncio.gather(animation,return_exceptions=True)
        return backend.schedule(key,execute_retry,text='分析重试：'+chapter,kind='workflow',controlled_retry=True)
    async def execute():
        await approve()
        argv=[sys.executable,'-u','-m','lg_cli','-C',str(backend.config.workspace),'--backend-worker','--json',
            '--runtime-engine',backend.config.runtime_engine]
        if backend.config.environment:argv+=['--environment',backend.config.environment]
        if backend.config.runtime_manifest:argv+=['--runtime-manifest',str(backend.config.runtime_manifest)]
        if selected['model']:argv+=['--model',selected['model']]
        if selected['noReference']:argv.append('--no-reference')
        argv+=['analysis','volume' if selected['volume'] else 'chapter',chapter,'--expected-source-fingerprint',selected['sourceFingerprint']]
        for category in categories:argv+=['--category',category]
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        return outcome(await state['control'].run(argv,cwd=backend.config.workspace,env=env,structured=True,
            label='analysis '+chapter,on_record=record,retry_executor=retry))
    text='/analysis '+('volume ' if selected['volume'] else 'chapter ')+chapter
    response=backend.schedule(key,execute,text=text,kind='workflow')
    backend.store.append(key,'user',text,run_id=response['runId'],item_id='user:'+response['runId'])
    return response
