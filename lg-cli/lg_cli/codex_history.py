"""Compatibility-only Codex history codec. Never starts/resumes a thread or model turn."""
import asyncio
import json
import os
from pathlib import Path

from .core_adapter import discover_core_commands


class CodexHistoryReader:
    METHODS={'initialize','thread/list','thread/read','thread/turns/list'}

    def __init__(self,config,home):
        self.config,self.home=config,home
        self.process=None
        self.counter=0

    async def __aenter__(self):
        if self.home.is_symlink():raise ValueError('Legacy LG history home cannot be a symlink')
        if not (self.config.runtime_manifest or os.environ.get('LG_CODEX_RUNTIME_MANIFEST')):
            raise ValueError('Legacy history requires a pinned runtime manifest')
        candidates=await asyncio.to_thread(discover_core_commands,probe=True,runtime_manifest=self.config.runtime_manifest)
        candidate=next((item for item in candidates if item.available),None)
        if candidate is None:raise ValueError('Legacy history needs the verified Codex runtime; set its manifest. Native writing remains available.')
        env={key:os.environ[key] for key in ('PATH','HOME','LANG','LC_ALL','TMPDIR','TEMP','SystemRoot','WINDIR') if key in os.environ}
        env['CODEX_HOME']=str(self.home)
        settings={'analytics.enabled':False,'feedback.enabled':False,'check_for_update_on_startup':False,
            'features.plugins':False,'features.apps':False,'features.recommended_plugins':False,
            'features.multi_agent':False,'features.multi_agent_v2':False,
            'model_provider':'lg_history','model_providers.lg_history.name':'LG History Codec',
            'model_providers.lg_history.base_url':'http://127.0.0.1:9','model_providers.lg_history.wire_api':'responses',
            'model_providers.lg_history.requires_openai_auth':False,'mcp_servers':{}}
        argv=[*candidate.command]
        for key,value in settings.items():argv+=['-c',key+'='+json.dumps(value)]
        argv+=['app-server','--listen','stdio://']
        self.process=await asyncio.create_subprocess_exec(*argv,cwd=self.config.workspace,env=env,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,limit=64*1024*1024)
        try:
            await self.request('initialize',{'clientInfo':{'name':'literarygiant_history_reader','version':'1'},
                'capabilities':{'experimentalApi':True}})
            await self.send({'method':'initialized','params':{}})
        except BaseException:
            await self.close();raise
        return self

    async def __aexit__(self,*_):await self.close()

    async def close(self):
        if self.process and self.process.returncode is None:
            try:self.process.terminate()
            except ProcessLookupError:return
            try:await asyncio.wait_for(self.process.wait(),5)
            except asyncio.TimeoutError:
                self.process.kill();await self.process.wait()

    async def send(self,value):
        self.process.stdin.write((json.dumps(value)+'\n').encode())
        await self.process.stdin.drain()

    async def request(self,method,params):
        if method not in self.METHODS:raise ValueError('History reader cannot execute runtime operations')
        self.counter+=1
        key=self.counter
        await self.send({'id':key,'method':method,'params':params})
        async def response():
            while line:=await self.process.stdout.readline():
                value=json.loads(line)
                if not isinstance(value,dict):raise ValueError('Legacy history codec returned invalid data')
                if value.get('id')==key and 'result' in value:return value['result']
                if value.get('id')==key and 'error' in value:
                    raise ValueError('Legacy history could not be read by the pinned compatibility codec')
                if 'id' in value and 'method' in value:
                    await self.send({'id':value['id'],'error':{'code':-32800,'message':'History-only reader rejects all execution requests'}})
            raise ValueError('Legacy history codec disconnected')
        try:return await asyncio.wait_for(response(),45)
        except (UnicodeError,json.JSONDecodeError):
            raise ValueError('Legacy history codec returned invalid data') from None

    def owned(self,value,thread=None):
        if not isinstance(value,dict) or not isinstance(value.get('id'),str) or not value['id'] or (thread is not None and value.get('id')!=thread):
            raise ValueError('Legacy thread identity changed')
        if not isinstance(value.get('cwd'),str) or Path(value['cwd']).resolve()!=self.config.workspace.resolve():
            raise ValueError('Legacy thread belongs to another book')
        if value.get('parentThreadId'):
            raise ValueError('Child-agent histories are not author conversations')
        return value

    async def list(self):
        records=[]
        cursor=None
        seen=set()
        while True:
            page=await self.request('thread/list',{'cwd':str(self.config.workspace),'limit':100,'cursor':cursor,
                'sourceKinds':['appServer','vscode'],'sortKey':'updated_at','modelProviders':[]})
            if not isinstance(page,dict) or not isinstance(page.get('data'),list):
                raise ValueError('Invalid legacy thread list')
            for value in page['data']:
                if not isinstance(value,dict) or not isinstance(value.get('cwd'),str):raise ValueError('Invalid legacy thread metadata')
                if Path(value['cwd']).resolve()==self.config.workspace.resolve() and not value.get('parentThreadId'):
                    records.append(self.owned(value))
            cursor=page.get('nextCursor')
            if cursor is None:return records
            if not isinstance(cursor,str) or cursor in seen:raise ValueError('Legacy thread list is incomplete')
            seen.add(cursor)

    async def read(self,thread):
        async def metadata(include):
            result=await self.request('thread/read',{'threadId':thread,'includeTurns':include})
            if not isinstance(result,dict):raise ValueError('Invalid legacy thread response')
            return self.owned(result.get('thread'),thread)
        before=await metadata(False)
        def signature(value):
            path=value.get('path')
            if path is None:return None
            if not isinstance(path,str) or not Path(path).resolve().is_relative_to(self.home.resolve()):
                raise ValueError('Legacy rollout points outside its LG history home')
            record=Path(path).stat()
            return path,record.st_ino,record.st_size,record.st_mtime_ns
        initial=signature(before)
        if before.get('historyMode')=='paginated':
            turns=[]
            cursor=None
            seen=set()
            ids=set()
            while True:
                page=await self.request('thread/turns/list',{'threadId':thread,'cursor':cursor,'limit':1,
                    'sortDirection':'asc','itemsView':'full'})
                if not isinstance(page,dict) or not isinstance(page.get('data'),list):raise ValueError('Invalid legacy turns page')
                for turn in page['data']:
                    if not isinstance(turn,dict) or not isinstance(turn.get('id'),str) or turn.get('itemsView')!='full' or turn.get('id') in ids:
                        raise ValueError('Legacy history is incomplete or repeats turns')
                    ids.add(turn['id']);turns.append(turn)
                cursor=page.get('nextCursor')
                if cursor is None:break
                if not isinstance(cursor,str) or cursor in seen:raise ValueError('Legacy history cursor repeated')
                seen.add(cursor)
        else:
            full=await metadata(True)
            turns=full.get('turns')
        if not isinstance(turns,list):raise ValueError('Invalid legacy turns')
        after=await metadata(False)
        if before.get('updatedAt')!=after.get('updatedAt') or initial!=signature(after):
            raise ValueError('Legacy conversation changed while reading; retry after stopping it there')
        return before,turns
