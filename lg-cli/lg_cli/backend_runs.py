"""Public run records with private model events omitted and immutable transport pages."""
import hashlib
import json

from .run_store import RunStore

PUBLIC_EVENTS={'run.started','run.resumed','run.planned','run.completed','run.failed',
    'context.started','context.completed','stage.started','stage.completed','stage.failed','artifact.written'}
PUBLIC_FIELDS=('schema_version','run_id','workflow','command','request','status','created_at','updated_at',
    'completed_at','workspace','provider','model','resumed_from','completed_stages','artifact_path','latest_path')


def public_manifest(value,*,summary=False):
    if any(not isinstance(value[key],(str,type(None))) for key in PUBLIC_FIELDS
            if key in value and key!='completed_stages'):
        raise ValueError('Invalid public run metadata')
    if not isinstance(value.get('completed_stages',[]),list) or any(not isinstance(item,str) for item in value.get('completed_stages',[])):
        raise ValueError('Invalid completed stage list')
    result={key:value[key] for key in PUBLIC_FIELDS if key in value and (not summary or key!='request')}
    if summary:
        result['requestPreview']=' '.join((value.get('request') or '')[:240].split())[:60]
    if value.get('error'):
        result['error']='Run failed; inspect saved diagnostics for details.'
    return result


def list_runs(workspace,limit):
    return {'runs':[public_manifest(value,summary=True) for value in RunStore(workspace).list_runs(limit)]}


def prepare_run(workspace,session,run,*,include_events=False):
    store=RunStore(workspace)
    paths=[store._path(run,name) for name in ('run.json','events.jsonl')]
    def signatures():
        return [(path.stat().st_ino,path.stat().st_size,path.stat().st_mtime_ns) if path.exists() else None for path in paths]
    before=signatures()
    value={'run':public_manifest(store.load(run))}
    cursor=0
    if include_events:
        events=[]
        if paths[1].exists():
            with paths[1].open('rb') as source:
                while line:=source.readline(4*1024*1024+1):
                    if len(line)>4*1024*1024:
                        raise ValueError('Run event exceeds record limit; inspect saved diagnostics')
                    if not line.strip():continue
                    try:
                        event=json.loads(line)
                    except (ValueError,UnicodeError):
                        raise ValueError('Run event is invalid; inspect saved diagnostics') from None
                    if not isinstance(event,dict):
                        raise ValueError('Run event must be an object')
                    if type(event.get('sequence')) is int:
                        cursor=max(cursor,event['sequence'])
                    if event.get('type') in PUBLIC_EVENTS:
                        events.append({key:event[key] for key in ('sequence','timestamp','type','stage_id','message') if key in event})
        value['events']=events
    if signatures()!=before:
        raise ValueError('Run changed while reading; refresh before continuing')
    data=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
    return {'sessionId':'run:'+session+':'+run,'cursor':cursor,'data':data,
        'sha256':hashlib.sha256(data).hexdigest()}
