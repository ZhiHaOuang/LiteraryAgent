"""One-invocation console presentation for the shared Literary Backend."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from .conversation_store import ConversationStore
from .events import RunEvent,EventType
from .terminal_backend import TerminalBackendClient
from .ui import RunEventRenderer

def options(args):
    result={}
    for option,key in [('dry_run','dryRun'),('raw','allowRaw'),('no_reference','noReference'),
            ('quiet','quiet'),('debug','debug'),('model','model'),('strategy','strategy'),('runtime_engine','runtimeEngine')]:
        value=getattr(args,option,None)
        if value:result[key]=value
    if getattr(args,'runtime_manifest',None):result['runtimeManifest']=str(args.runtime_manifest.expanduser().resolve())
    return result


def creative_params(args):
    if args.command in {'version','scene'} and getattr(args,args.command+'_command')=='accept':
        return {'action':'acceptVersion','document':args.reference,'version':args.version,'final':args.final,'review':True}
    if args.command=='scene' and args.scene_command=='approve':return {'action':'approveScene','document':args.reference}
    if args.command=='bible' and args.bible_command=='promote':return {'action':'promoteFact','factId':args.fact_id,'reason':args.reason}
    if args.command=='bible' and args.bible_command=='proposal' and args.proposal_command=='accept':
        return {'action':'acceptProposal','proposalId':args.proposal_id,'state':args.state}
    return None


def _emit_result(value,args):
    if getattr(args,'json',False):print(json.dumps(value,ensure_ascii=False))
    else:
        from .main import _print_workflow_result
        from .story_cli import _emit_story_result
        rendered=SimpleNamespace(**{'workflow':None,'latest_path':None,'artifact_path':None,'error':None,'text':'',**value})
        if value['type']=='story.result':_emit_story_result(rendered,args)
        else:_print_workflow_result(rendered,json_mode=False)
    return value['exit_code']


class ConsoleClient:
    def __init__(self,config,args):
        self.config,self.args=config,args
        self.required=[];self.last={}
        self.renderer=RunEventRenderer(json_mode=bool(getattr(args,'json',False)),quiet=bool(getattr(args,'quiet',False)),debug=bool(getattr(args,'debug',False)))
        key=getattr(args,'conversation',None) or ConversationStore(config.workspace).create()
        self.client=TerminalBackendClient(config,lambda *_:None,on_request=self.answer,control=None,conversation=key,on_event=self.event,
            on_notice=lambda text:print(text.rstrip(),file=sys.stderr) if not getattr(args,'json',False) else None)

    def event(self,event):
        method,params=event['method'],event['params']
        if method=='workflow/record':
            record=params['record']
            self.renderer(RunEvent(sequence=record.get('sequence',0),run_id=record.get('run_id',''),
                type=EventType(record['type']),workflow=record.get('workflow',''),timestamp=record.get('timestamp',''),
                stage_id=record.get('stage_id'),message=record.get('message',''),data=record.get('data',{})))
        elif method=='run/completed':self.last=params

    async def answer(self,method,details):
        fingerprint=details.get('fingerprint')
        if method=='lg/creativeApproval' and fingerprint and fingerprint==getattr(self.args,'approve_fingerprint',None):
            return {'decision':'accept'}
        value={'type':'approval.required','kind':method,'session_id':self.client.key,'preview':details}
        self.required.append(value)
        if getattr(self.args,'json',False):print(json.dumps(value,ensure_ascii=False))
        else:
            print('需要确认具体内容后再执行：\n'+json.dumps(details,ensure_ascii=False,indent=2),file=sys.stderr)
            if fingerprint:print('复核后在原命令前添加 --approve-fingerprint '+fingerprint,file=sys.stderr)
        return {'answers':{}} if method=='item/tool/requestUserInput' else {'decision':'decline'}

    async def start(self):
        await self.client.start()
        if getattr(self.args,'operation_mode',None):
            await self.client.set_permissions(self.args.operation_mode,self.client.runtime.read()['creative_mode'])

    async def workflow(self,params,*,resume=False):
        try:finished=await (self.client.resume_workflow(params) if resume else self.client.workflow(params))
        except FileNotFoundError:
            finished={'status':'failed','exitCode':5,'error':'Run not found.'}
        projected=(finished.get('result') or {}).get('workflowResult')
        if projected is None:
            projected={'type':'run.result','run_id':params.get('runId'),'workflow':params.get('command'),
                'status':'failed','text':'','error':finished.get('error','Workflow did not return a verified result.'),
                'exit_code':finished.get('exitCode',130 if finished['status']=='cancelled' else 1),'artifact_path':None,'latest_path':None}
        return _emit_result({**projected,'session_id':self.client.key},self.args)

    async def creative(self,params,*,emit=True):
        params={**params,**({'expectedFingerprint':self.args.approve_fingerprint} if getattr(self.args,'approve_fingerprint',None) else {})}
        finished=await self.client.creative(params)
        if self.required:return 2
        if finished['status']!='completed':
            print(finished.get('error','未采用修改。'),file=sys.stderr);return 130 if finished['status']=='cancelled' else 1
        value=finished['result']
        if emit:
            if getattr(self.args,'json',False):print(json.dumps(value,ensure_ascii=False))
            else:print('已完成指定创作操作。')
        return 0


async def _with_client(config,args,action):
    console=ConsoleClient(config,args)
    try:
        await console.start()
        try:return await action(console)
        except asyncio.CancelledError:
            await console.client.interrupt();return 130
    finally:await console.client.close()

def run_workflow(command,prompt,config,args):
    async def action(console):return await console.workflow({'command':command,'request':prompt,**options(args)})
    return asyncio.run(_with_client(config,args,action))


def run_agent(prompt,config,args):
    async def action(console):
        try:finished=await console.client.run(prompt)
        except ValueError:finished=console.last or {'status':'failed','error':'Agent did not complete.'}
        text='\n'.join(row['text'] for row in console.client.backend.store.read(console.client.key)
            if row['role']=='assistant' and row.get('run_id')==finished.get('runId'))
        code=2 if console.required else 0 if finished['status']=='completed' else 1
        return _emit_result({'type':'run.result','run_id':finished.get('runId'),'session_id':console.client.key,
            'workflow':'chat','status':'approval_required' if console.required else finished['status'],'text':text,
            'exit_code':code,'error':finished.get('error'),'artifact_path':None,'latest_path':None},args)
    return asyncio.run(_with_client(config,args,action))


def dispatch_story(args,config):
    if args.command=='analysis' and args.analysis_command in {'chapter','volume'}:
        return run_analysis(args,config)
    creative=creative_params(args)
    if creative:return asyncio.run(_with_client(config,args,lambda console:console.creative(creative)))
    story=(args.command=='edit' or args.command=='scene' and args.scene_command in {'plan','draft'}
        or args.command=='bible' and args.bible_command=='curate')
    if not story:return None
    if args.request_stdin:
        from .workflow_io import read_story_request
        if args.request or getattr(args,'passage',None) is not None:raise ValueError('Use stdin request or command-line text, not both')
        request=read_story_request(sys.stdin)
        if 'passage' in request and args.command!='edit':raise ValueError('Selected passage applies only to edit')
        args.request=[request['request']]
        if 'passage' in request:args.passage=request['passage']
    command='revise' if args.command=='edit' else 'bible-curate' if args.command=='bible' else 'scene-'+args.scene_command
    params={'command':command,'document':args.reference,'request':' '.join(args.request),**options(args)}
    if args.command=='edit':
        params['revisionMode']=args.mode
        if args.source_version is not None:params['sourceVersion']=args.source_version
        if args.passage is not None:params['passage']=args.passage
    return asyncio.run(_with_client(config,args,lambda console:console.workflow(params)))


def analysis_params(args):
    if getattr(args,'expected_source_fingerprint',None):raise ValueError('Source fingerprint is reserved for the owned analysis worker')
    return {'chapter':args.reference,'volume':args.analysis_command=='volume',
        **({'model':args.model} if getattr(args,'model',None) else {}),
        **({'noReference':True} if getattr(args,'no_reference',False) else {}),
        **({'categories':list(dict.fromkeys(args.category))} if args.category else {})}


def run_analysis(args,config):
    async def action(console):
        params=analysis_params(args)
        preview=await console.client.analysis_preview(params)
        expected=getattr(args,'approve_fingerprint',None) or preview['fingerprint']
        finished=await console.client.analysis({**params,'expectedFingerprint':expected},confirm_fingerprint=expected)
        if console.required:return 2
        if finished['status']!='completed':
            if finished['status']=='cancelled':return 130
            raise ValueError(finished.get('error','Analysis did not complete'))
        value=finished['result']['analysisResult']
        if args.json:print(json.dumps(value,ensure_ascii=False))
        else:print('Analysis run '+value['run_id']+': '+value['status'])
        return 1 if value['status']=='blocked' else 0
    return asyncio.run(_with_client(config,args,action))


def run_studio(args,config,arguments,preview):
    params={'operation':args.operation,'arguments':arguments,
        'expectedFingerprint':getattr(args,'approve_fingerprint',None) or preview['fingerprint']}
    async def action(console):
        fingerprint=params['expectedFingerprint'] if args.confirm or getattr(args,'approve_fingerprint',None) else None
        finished=await console.client.studio(params,confirm_fingerprint=fingerprint)
        if console.required:return 2
        if finished['status']!='completed':
            if finished['status']=='cancelled':return 130
            raise ValueError(finished.get('error','Studio operation did not complete'))
        print(json.dumps(finished['result']['value'],ensure_ascii=False,indent=2))
        return 0
    if preview['write']:return asyncio.run(_with_client(config,args,action))
    async def read():
        client=TerminalBackendClient.readonly(config)
        try:
            result=await client.studio(params)
            print(json.dumps(result['result']['value'],ensure_ascii=False,indent=2))
            return 0
        finally:await client.close()
    return asyncio.run(read())


def dispatch_run(args,config):
    async def execute():
        if args.run_command in {'list','show'}:
            client=TerminalBackendClient.readonly(config)
            try:
                if args.run_command=='list':
                    value=await client.list_runs(max(1,min(args.limit,200)))
                    if args.json:print(json.dumps(value,ensure_ascii=False))
                    else:
                        print('Recent LG runs' if value['runs'] else 'No LG runs found.')
                        for item in value['runs']:print('  '+item['run_id']+' '+item['status']+' '+item['command'])
                else:
                    try:value=await client.read_run(args.run_id,events=args.events)
                    except FileNotFoundError as exc:print(str(exc),file=sys.stderr);return 5
                    if args.json:print(json.dumps(value,ensure_ascii=False))
                    else:
                        from .main import _print_run_manifest
                        _print_run_manifest(value['run'])
                        for event in value.get('events',[]):print(str(event.get('sequence'))+' '+event['type']+' '+event.get('message',''))
                return 0
            finally:await client.close()
        async def action(console):
            if args.run_command=='resume':
                if args.expected_fingerprint:raise ValueError('Expected fingerprint is reserved for the owned worker')
                return await console.workflow({'runId':args.run_id,'confirmStopped':args.confirm_stopped,**options(args)},resume=True)
            finished=await console.client.import_run({'runId':args.run_id,'slug':args.slug,'kind':args.kind,'title':args.title})
            if finished['status']!='completed':print(finished.get('error','Import failed.'),file=sys.stderr);return 1
            value=finished['result']
            if args.accept:
                code=await console.creative({'action':'acceptVersion','document':value['slug'],'version':value['version'],'review':True},emit=False)
                if code:return code
                value['active']=True
            if args.json:print(json.dumps(value,ensure_ascii=False))
            else:print('Imported '+value['slug']+' v'+str(value['version']))
            return 0
        return await _with_client(config,args,action)
    return asyncio.run(execute())
