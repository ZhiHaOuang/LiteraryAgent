"""Read/import LG-owned legacy conversations, with no story mutation or runtime replay."""
import asyncio
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .codex_history import CodexHistoryReader
from .credentials import environment_dir


def identifier(home,thread):
    return 'legacy-'+hashlib.sha256((str(home.resolve())+'\n'+thread).encode()).hexdigest()[:32]


def imported_id(key):return 'imported-'+key.removeprefix('legacy-')


def _safe_under(path,root):
    try:relative=path.relative_to(root)
    except ValueError:return False
    current=root
    for part in relative.parts:
        current=current/part
        if current.is_symlink():return False
    return path.resolve().is_relative_to(root.resolve())


def project_history(key,thread,turns):
    items,records=[],[]
    for turn in turns:
        if not isinstance(turn,dict) or not isinstance(turn.get('id'),str) or not isinstance(turn.get('items'),list):
            raise ValueError('Legacy turn projection is incomplete')
        run='legacy-turn:'+hashlib.sha256((key+turn['id']).encode()).hexdigest()[:32]
        for index,old in enumerate(turn['items']):
            if not isinstance(old,dict):raise ValueError('Invalid legacy item')
            kind=old.get('type')
            if kind=='reasoning':continue
            item_id='legacy-item:'+hashlib.sha256((run+str(old.get('id',index))).encode()).hexdigest()[:32]
            if kind=='userMessage':
                content=old.get('content')
                if not isinstance(content,list) or any(not isinstance(block,dict) for block in content):raise ValueError('Invalid legacy user message')
                text='\n'.join(block['text'] for block in content if block.get('type')=='text' and isinstance(block.get('text'),str))
                item={'id':item_id,'runId':run,'kind':'user','title':'作者','text':text,'state':'completed'}
            elif kind=='agentMessage':
                if not isinstance(old.get('text'),str):raise ValueError('Invalid legacy assistant message')
                item={'id':item_id,'runId':run,'kind':'assistant','title':'LG','text':old['text'],
                    'state':'cancelled' if turn.get('status')=='inProgress' else 'completed'}
            else:
                item={'id':item_id,'runId':run,'kind':'notice','title':'历史 '+str(kind or '输出'),
                    'text':'旧运行记录已保留；工具和审批不会重新执行。','state':'completed'}
            items.append(item)
            if item['kind'] in {'user','assistant'}:
                records.append({'role':item['kind'],'text':item['text'],'run_id':run,'item_id':item_id,
                    'legacy':True,'timestamp':str(thread.get('updatedAt',''))})
    data=json.dumps({'items':items,'sourceThread':thread['id']},ensure_ascii=False,sort_keys=True).encode()
    return {'items':items,'records':records,'sha256':hashlib.sha256(data).hexdigest(),
        'title':str(thread.get('name') or thread.get('preview') or '旧 Extension 会话')[:80]}


class LegacySessions:
    def __init__(self,config,*,reader_factory=CodexHistoryReader):
        self.config,self.reader_factory=config,reader_factory
        self.catalog={}

    def homes(self):
        roots=[self.config.workspace/'.literarygiant'/'codex-home']
        subscriptions=environment_dir(self.config.environment or 'sandbox')/'codex-auth'
        if _safe_under(subscriptions,Path.home()) and subscriptions.exists():
            roots.extend(path for path in sorted(subscriptions.iterdir()) if path.is_dir() and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',path.name))
        return [path for path in roots if _safe_under(path,self.config.workspace if path==roots[0] else Path.home()) and (path/'state_5.sqlite').is_file()
            and not (path/'state_5.sqlite').is_symlink()]

    async def list(self,store):
        warning='旧 Extension 历史仍保留；部分历史读取失败。请核对 LG 配置中的固定 Runtime manifest 后重新连接；切换执行器不会修复历史读取。'
        bound=set()
        for path in store.root.glob('*.runtime.json'):
            try:
                if path.is_symlink():raise ValueError('Invalid LG runtime metadata')
                value=json.loads(path.read_text())
                if not isinstance(value,dict) or not isinstance(value.get('engine_threads',{}),dict):raise ValueError('Invalid LG runtime metadata')
                threads=[value.get('thread_id'),*value.get('engine_threads',{}).values()]
                if any(item is not None and not isinstance(item,str) for item in threads):raise ValueError('Invalid LG runtime metadata')
                bound.update(item for item in threads if item)
            except (OSError,ValueError,TypeError):
                return [],warning
        records=[]
        warning=None
        self.catalog={}
        for home in self.homes():
            try:
                if self.reader_factory is CodexHistoryReader:
                    with closing(sqlite3.connect((home/'state_5.sqlite').as_uri()+'?mode=ro',uri=True)) as index:
                        possible=[thread for thread,cwd in index.execute('SELECT id,cwd FROM threads WHERE archived=0')
                            if Path(cwd).resolve()==self.config.workspace.resolve() and thread not in bound
                            and not store.path(imported_id(identifier(home,thread))).exists()]
                    if not possible:continue
                async with self.reader_factory(self.config,home) as reader:
                    threads=await reader.list()
                for thread in threads:
                    key=identifier(home,thread['id'])
                    self.catalog[key]=(home,thread['id'])
                    if thread['id'] in bound or store.path(imported_id(key)).exists():continue
                    records.append({'sessionId':key,'title':'旧 Extension：'+str(thread.get('name') or thread.get('preview') or 'LG 会话')[:60]})
            except (OSError,ValueError,TimeoutError,sqlite3.Error):
                warning='旧 Extension 历史仍保留；部分历史读取失败。请核对 LG 配置中的固定 Runtime manifest 后重新连接；切换执行器不会修复历史读取。'
        return records,warning

    async def snapshot(self,key,store):
        if key not in self.catalog:
            await self.list(store)
        if key not in self.catalog:raise ValueError('Legacy conversation is missing or belongs to another book')
        home,thread=self.catalog[key]
        async with self.reader_factory(self.config,home) as reader:
            meta,turns=await reader.read(thread)
        if Path(meta['cwd']).resolve()!=self.config.workspace.resolve() or meta['id']!=thread or meta.get('parentThreadId'):
            raise ValueError('Legacy conversation ownership changed')
        result=await asyncio.to_thread(project_history,key,meta,turns)
        return {**result,'source':{'kind':'codex-history','threadId':thread,'home':str(home),'sha256':result['sha256']}}
