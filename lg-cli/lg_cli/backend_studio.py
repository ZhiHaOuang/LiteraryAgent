"""Explicit author workbench calls use the service's existing approval broker."""
import asyncio
import json

from .studio_operations import prepare,invoke


async def call(backend,key,params):
    from .writing_mcp import create_server
    from .mcp_approval import project_fingerprint
    prepared=await prepare(backend.config,params['operation'],params['arguments'])
    if params.get('expectedFingerprint') is not None and params['expectedFingerprint']!=prepared['fingerprint']:
        raise ValueError('Studio preview changed; review the exact operation again')
    if not prepared['write']:
        return await invoke(create_server(backend.config),prepared['tool'],prepared['arguments'])
    state=backend.session(key)
    async def approve(details):
        if details!=prepared['details'] or project_fingerprint(backend.config.workspace)!=prepared['bookFingerprint']:
            raise ValueError('Studio source changed before approval; refresh the preview')
        result=await state['control'].approval_handler({**details,'fingerprint':prepared['fingerprint']},
            run_id=state['run_id'],author_action=True)
        if result.get('decision')!='accept':raise asyncio.CancelledError()
        return result
    async def execute():
        server=create_server(backend.config,approval_handler=approve)
        value=await invoke(server,prepared['tool'],prepared['arguments'])
        run=state['run_id'];item_id='command-assistant:'+run
        text=json.dumps(value,ensure_ascii=False)
        backend.store.append(key,'assistant',text,run_id=run,item_id=item_id)
        item={'id':item_id,'runId':run,'kind':'tool','title':'LG · '+prepared['tool'],'text':text,'state':'completed',
            'structured':{'arguments':prepared['arguments'],'result':value}}
        if isinstance(value,dict):
            if isinstance(value.get('artifact'),str):item['files']=[value['artifact']]
            if isinstance(value.get('diff'),dict):item['diff']=value['diff']
        backend.emit(key,'client/event',{'type':'item.updated','sessionId':key,'item':item})
        return {'value':value}
    text='/studio '+params['operation']
    response=backend.schedule(key,execute,text=text)
    backend.store.append(key,'user',text,run_id=response['runId'],item_id='user:'+response['runId'])
    return response
