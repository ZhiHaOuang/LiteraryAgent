"""Bounded JSONL records and private stdin requests for owned CLI workflows."""
import codecs
import json


class WorkflowFailure(ValueError):
    def __init__(self,message,*,exit_code=1,result=None):
        super().__init__(message)
        self.exit_code=exit_code
        self.result=result


def public_workflow_event(value):
    fields=('sequence','run_id','workflow','timestamp','type','stage_id','message')
    result={key:value[key] for key in fields if key in value}
    data=value.get('data') or {}
    result['data']={key:data[key] for key in ('index','total','restored','artifact_path','latest_path','command','resumed_from') if key in data}
    if value.get('type') in {'stage.failed','run.failed'}:
        result['message']='Workflow failed; inspect the saved run diagnostics.'
    return result


def public_workflow_result(value,*,code,missing_credentials=False):
    fields=('type','run_id','workflow','operation','status','text','document','version','artifact_path','latest_path','exit_code')
    result={key:value[key] for key in fields if key in value}
    result['exit_code']=code
    if code:result['text']=''
    result['error']=None if not code else ('No API key is configured. LiteraryGiant is API-only.' if missing_credentials
        else 'Workflow failed; inspect the saved run diagnostics.')
    return result


def read_story_request(stream):
    """Validate the private request envelope before any story service writes."""
    import jsonschema
    value = stream.read(4 * 1024 * 1024 + 1)
    if len(value.encode('utf-8')) > 4 * 1024 * 1024:
        raise ValueError('Story request exceeds size limit')
    try:
        request = json.loads(value)
    except ValueError:
        raise ValueError('Story request must be a JSON object') from None
    try:
        jsonschema.validate(request,{'type':'object','additionalProperties':False,'required':['request'],
            'properties':{'request':{'type':'string','maxLength':4 * 1024 * 1024},
                'passage':{'type':'string','minLength':1,'maxLength':100000}}})
    except jsonschema.ValidationError:
        raise ValueError('Invalid story request fields') from None
    return request


class WorkflowStream:
    def __init__(self, callback):
        self.callback = callback
        self.decoder = codecs.getincrementaldecoder('utf-8')()
        self.pending = ''

    def feed(self, data):
        try:
            self.pending += self.decoder.decode(data)
        except UnicodeError:
            raise ValueError('Workflow output is not valid UTF-8') from None
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n',1)
            self._line(line)
        if len(self.pending.encode()) > 4 * 1024 * 1024:
            raise ValueError('Workflow output record exceeds size limit')

    def _line(self,line):
        if not line.strip():
            return
        if len(line.encode()) > 4 * 1024 * 1024:
            raise ValueError('Workflow output record exceeds size limit')
        try:
            record = json.loads(line)
        except ValueError:
            raise ValueError('Workflow output record is invalid; inspect saved run diagnostics') from None
        if not isinstance(record,dict):
            raise ValueError('Workflow output must contain JSON objects')
        self.callback(record)

    def end(self):
        try:
            self.pending += self.decoder.decode(b'',final=True)
        except UnicodeError:
            raise ValueError('Workflow output is truncated') from None
        if self.pending:
            self._line(self.pending)
            self.pending = ''


async def feed_request(process,text):
    try:
        process.stdin.write(text.encode('utf-8'))
        await process.stdin.drain()
    except (BrokenPipeError,ConnectionResetError):
        pass
    finally:
        process.stdin.close()
