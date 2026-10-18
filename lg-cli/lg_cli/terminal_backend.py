"""Terminal client of the same service used by the Extension JSON-RPC transport."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid

from .backend import Backend


class TerminalBackendClient:
    def __init__(self, config, view, *, on_request, control, conversation, agent_factory=None, on_notice=lambda _: None,route_tasks=True,task_router=None,on_event=lambda _:None):
        self.view, self.on_request, self.control, self.key = view, on_request, control, conversation
        self.on_notice = on_notice
        self.on_event=on_event
        self.route_tasks=route_tasks
        self.dialogs, self.results = {}, {}
        self.closed = False
        self.backend = Backend(config, self._event, agent_factory=agent_factory,
            control_factory=(lambda _: control) if control is not None else None,task_router=task_router)

    @classmethod
    def readonly(cls,config):
        return cls(config,lambda *_:None,on_request=None,control=None,conversation='reader-'+uuid.uuid4().hex)

    @property
    def runtime(self):
        return self.backend.session(self.key)['agent'].runtime

    @property
    def thread_id(self):
        return self.backend.session(self.key)['agent'].thread_id

    async def start(self):
        await self.backend.dispatch('session/open', {'sessionId': self.key})

    def _event(self, event):
        self.on_event(event)
        method, params = event['method'], event['params']
        if method == 'client/event' and str(params.get('item',{}).get('id','')).startswith(('workflow-assistant:','command-assistant:')):
            return  # The terminal's existing command renderer already presents this result.
        item=params.get('item',{}) if method=='client/event' else {}
        if item.get('kind')=='notice' and str(item.get('id','')).startswith(('task-scope:','task-interpretation:')):
            self.on_notice(item.get('title','')+'：'+item.get('text','')+'\n')
            return
        if method == 'client/event':
            self.view('lg.event', params)
        elif method == 'run/completed':
            self.results[params['runId']] = params
        elif method == 'approval/requested':
            key = params['approvalId']
            self.dialogs[key] = asyncio.create_task(self._answer(key, params))
        elif method in {'approval/cancelled', 'approval/resolved'}:
            if params.get('automatic') and params.get('reason'):
                self.on_notice(('创作审查：' if params.get('kind') == 'creative' else '操作审查：') + params['reason'] + '\n')
            task = self.dialogs.get(params.get('approvalId'))
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()

    async def _answer(self, key, params):
        kind = params['kind']
        method = {'permissions': 'lg/permissionApproval', 'operation': 'lg/operationApproval',
            'creative': 'lg/creativeApproval'}.get(kind, kind)
        try:
            result = await self.on_request(method, params['details'])
            await self.backend.dispatch('approval/resolve', {'approvalId': key, 'result': result})
        except asyncio.CancelledError:
            raise
        except Exception:
            # Dialog or validation failures cannot grant the waiting operation.
            pending = self.backend.approvals.pending.get(key)
            if pending and not pending[0].done():
                if kind == 'item/tool/requestUserInput':
                    pending[0].cancel()
                else:
                    self.backend.approvals.resolve(key, {'decision': 'cancel'})
        finally:
            self.dialogs.pop(key, None)

    async def run(self, text):
        result = await self.backend.dispatch('run/start', {'sessionId': self.key, 'text': text,'routeTask':self.route_tasks})
        await self.backend.tasks[result['runId']][1]
        finished = self.results.pop(result['runId'])
        if finished['status'] == 'failed':
            raise ValueError(finished.get('error') or '主 Agent 执行失败。')
        if finished['status'] in {'cancelled', 'interrupted'}:
            raise asyncio.CancelledError()
        return finished

    async def steer(self, text):
        return await self.backend.dispatch('run/steer', {'sessionId': self.key, 'text': text})

    async def workflow(self,params):
        result = await self.backend.dispatch('workflow/run',{'sessionId':self.key,**params})
        await self.backend.tasks[result['runId']][1]
        return self.results.pop(result['runId'])

    async def analysis_preview(self,params):
        return await self.backend.dispatch('analysis/preview',params)

    async def _owned_call(self,method,params,confirm_fingerprint=None):
        previous=self.on_request
        consumed=False
        async def answer(method,details):
            nonlocal consumed
            if (not consumed and confirm_fingerprint and method in {'lg/creativeApproval','lg/operationApproval'}
                    and details.get('fingerprint')==confirm_fingerprint):
                consumed=True
                return {'decision':'accept'}
            return await previous(method,details) if previous else {'decision':'decline'}
        self.on_request=answer
        try:
            response=await self.backend.dispatch(method,{'sessionId':self.key,**params})
            await self.backend.tasks[response['runId']][1]
            return self.results.pop(response['runId'])
        finally:self.on_request=previous

    async def analysis(self,params,*,confirm_fingerprint=None):
        return await self._owned_call('workflow/start',params,confirm_fingerprint)

    async def studio(self,params,*,confirm_fingerprint=None):
        from .studio_operations import READ_ONLY
        if params['operation'] in READ_ONLY:
            value=await self.backend.dispatch('studio/call',params)
            return {'status':'completed','result':{'value':value}}
        return await self._owned_call('studio/call',params,confirm_fingerprint)

    async def resume_workflow(self,params):
        result=await self.backend.dispatch('workflow/resume',{'sessionId':self.key,**params})
        await self.backend.tasks[result['runId']][1]
        return self.results.pop(result['runId'])

    async def list_runs(self,limit=20):
        return await self.backend.dispatch('run/list',{'limit':limit})

    async def read_run(self,run,*,events=False):
        params={'sessionId':self.key,'runId':run,'events':events}
        first=await self.backend.dispatch('run/readPage',params)
        chunks=[]
        page=first
        index=0
        if type(first.get('pages')) is not int or first['pages']<1:
            raise ValueError('Invalid run page count')
        while True:
            if (page.get('sessionId')!=self.key or page.get('runId')!=run or page.get('page')!=index
                    or any(page.get(key)!=first.get(key) for key in ('snapshotId','pages','sha256','cursor'))):
                raise ValueError('Run pages changed identity')
            chunks.append(base64.b64decode(page['data'],validate=True))
            expected=index+1 if index+1<first['pages'] else None
            if page.get('nextPage')!=expected:
                raise ValueError('Run pages are incomplete')
            if expected is None:break
            index=expected
            page=await self.backend.dispatch('run/readPage',{'sessionId':self.key,'runId':run,
                'snapshotId':first['snapshotId'],'page':index})
        data=b''.join(chunks)
        if hashlib.sha256(data).hexdigest()!=first['sha256']:
            raise ValueError('Run page checksum mismatch')
        return json.loads(data)

    async def creative(self,params):
        result = await self.backend.dispatch('creative/apply',{'sessionId':self.key,**params})
        await self.backend.tasks[result['runId']][1]
        return self.results.pop(result['runId'])

    async def import_run(self,params):
        result=await self.backend.dispatch('run/import',{'sessionId':self.key,**params})
        await self.backend.tasks[result['runId']][1]
        return self.results.pop(result['runId'])

    async def interrupt(self):
        return await self.backend.dispatch('run/cancel', {'sessionId': self.key})

    async def set_permissions(self, mode, creative):
        return await self.backend.dispatch('permissions/set',
            {'sessionId': self.key, 'mode': mode, 'creativeMode': creative})

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            await self.backend.close()
        finally:
            tasks = list(self.dialogs.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
