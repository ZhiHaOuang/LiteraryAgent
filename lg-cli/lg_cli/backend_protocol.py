"""Public request contract shared by transport, service and future TS clients."""
import jsonschema
from .task_scope import TASK_SCHEMA,UI_STAGES
from .studio_operations import OPERATIONS

TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 100000}
ID = {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,80}$'}
MODES = {'enum': ['ask', 'auto_review', 'full_access']}
LIBRARY_TOPIC = {'enum': ['characters', 'relationships', 'chapters', 'outlines', 'events', 'timeline',
    'world', 'storylines', 'analyses', 'bible', 'pending', 'personal', 'usage']}
LIBRARY_KEY = {'type': 'string', 'minLength': 1, 'maxLength': 512}
STORY_COMMANDS = {'scene-plan','scene-draft','revise','bible-curate'}
REVISION_MODES = ['light','grammar','pacing','dialogue','imagery','conflict','pov','style','rewrite']


def contract():
    from .narrative_execution import INPUT_SCHEMA as NARRATIVE_INPUT
    from .literary_tasks import INPUT_SCHEMA as TASK_INPUT
    fingerprint = {'type':'string','pattern':'^[0-9a-f]{64}$'}
    definitions = {
        'initialize': ({}, []), 'session/list': ({}, []), 'session/create': ({}, []),
        'runtime/identity': ({'verifyManifest':{'type':'boolean'}}, []),
        'operation/read': ({'operationId':{'type':'string','pattern':'^[0-9a-f]{32}$'}}, ['operationId']),
        'task/preview': ({'options':TASK_INPUT}, ['options']),
        'task/start': ({'sessionId':ID,'options':TASK_INPUT,'expectedFingerprint':fingerprint}, ['sessionId','options']),
        'task/read': ({'taskId':ID}, ['taskId']),
        'task/advance': ({'sessionId':ID,'taskId':ID,'expectedFingerprint':fingerprint}, ['sessionId','taskId']),
        'task/select': ({'sessionId':ID,'taskId':ID,'optionId':TEXT,'expectedFingerprint':fingerprint},
            ['sessionId','taskId','optionId','expectedFingerprint']),
        'narrative/preview': ({'options':NARRATIVE_INPUT}, ['options']),
        'narrative/run': ({'sessionId':ID,'options':NARRATIVE_INPUT,'expectedFingerprint':fingerprint}, ['sessionId','options']),
        'sources/check': ({'document':TEXT,'version':{'type':'integer','minimum':1}}, ['document']),
        'session/open': ({'sessionId': ID}, ['sessionId']),
        'session/read': ({'sessionId': ID}, ['sessionId']),
        'session/snapshot': ({'sessionId': ID}, ['sessionId']),
        'session/snapshotPage': ({'sessionId': ID, 'snapshotId': ID,
            'page': {'type': 'integer', 'minimum': 0}}, ['sessionId']),
        'session/close': ({'sessionId': ID}, ['sessionId']),
        'session/rename': ({'sessionId': ID, 'title': {'type':'string','minLength':1,'maxLength':80}}, ['sessionId','title']),
        'events/read': ({'sessionId': ID,'after':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':1000}}, ['sessionId']),
        'permissions/set': ({'sessionId': ID,'mode': MODES,'creativeMode': MODES}, ['sessionId','mode']),
        'run/start': ({'sessionId': ID,'text':TEXT,'outputSchema':{'type':'object'},'task':TASK_SCHEMA,
            'stage':{'enum':list(UI_STAGES)},'routeTask':{'type':'boolean'},'authorText':TEXT}, ['sessionId','text']),
        'run/steer': ({'sessionId': ID,'text':TEXT}, ['sessionId','text']),
        'run/cancel': ({'sessionId': ID}, ['sessionId']),
        'approval/resolve': ({'approvalId':ID,'result':{'type':'object'}}, ['approvalId','result']),
        'reference/contracts': ({}, []),
        'reference/validate': ({'category':TEXT,'card':{'type':'object'}}, ['category','card']),
        'analysis/preview': ({'chapter':TEXT,'category':TEXT,'categories':{'type':'array','minItems':1,'maxItems':5,'uniqueItems':True,'items':TEXT},
            'volume':{'type':'boolean'},'model':{'type':'string','minLength':1,'maxLength':200},'noReference':{'type':'boolean'}}, ['chapter']),
        'workflow/start': ({'sessionId':ID,'category':TEXT,'chapter':TEXT,
            'categories':{'type':'array','minItems':1,'maxItems':5,'uniqueItems':True,'items':TEXT},
            'volume':{'type':'boolean'},'model':{'type':'string','minLength':1,'maxLength':200},'noReference':{'type':'boolean'},
            'expectedFingerprint':{'type':'string','pattern':'^[0-9a-f]{64}$'}}, ['sessionId','chapter']),
        'studio/preview': ({'operation':{'enum':list(OPERATIONS)},'arguments':{'type':'object'}}, ['operation','arguments']),
        'studio/call': ({'sessionId':ID,'operation':{'enum':list(OPERATIONS)},'arguments':{'type':'object'},
            'expectedFingerprint':{'type':'string','pattern':'^[0-9a-f]{64}$'}}, ['operation','arguments']),
        'run/list': ({'limit':{'type':'integer','minimum':1,'maximum':200}}, []),
        'run/readPage': ({'sessionId':ID,'runId':ID,'events':{'type':'boolean'},'snapshotId':ID,
            'page':{'type':'integer','minimum':0}}, ['sessionId','runId']),
        'workflow/resume': ({'sessionId':ID,'runId':ID,'dryRun':{'type':'boolean'},'allowRaw':{'type':'boolean'},
            'quiet':{'type':'boolean'},'debug':{'type':'boolean'},'noReference':{'type':'boolean'},
            'model':{'type':'string','minLength':1,'maxLength':200},'strategy':{'enum':['staged','single-pass']},
            'runtimeEngine':{'enum':['codex','native']},'runtimeManifest':{'type':'string','minLength':1,'maxLength':4096},
            'confirmStopped':{'type':'boolean'}}, ['sessionId','runId']),
        'run/import': ({'sessionId':ID,'runId':ID,'slug':{'type':'string','minLength':1,'maxLength':512},
            'kind':{'enum':['outline','world','character','note','research','style','chapter','scene']},
            'title':{'type':'string','minLength':1,'maxLength':512}}, ['sessionId','runId','slug','kind','title']),
        'workflow/run': ({'sessionId':ID,'command':{'enum':['chat','outline','world','character','plot','ref','check','write',*sorted(STORY_COMMANDS)]},
            'request':{'type':'string','maxLength':100000},'dryRun':{'type':'boolean'},'allowRaw':{'type':'boolean'},
            'quiet':{'type':'boolean'},'debug':{'type':'boolean'},
            'noReference':{'type':'boolean'},'model':{'type':'string','minLength':1,'maxLength':200},
            'strategy':{'enum':['staged','single-pass']},'runtimeEngine':{'enum':['codex','native']},
            'runtimeManifest':{'type':'string','minLength':1,'maxLength':4096},
            'document':TEXT,'revisionMode':{'enum':REVISION_MODES},
            'sourceVersion':{'type':'integer','minimum':1},'passage':TEXT}, ['sessionId','command','request']),
        'creative/apply': ({'sessionId':ID,'action':{'enum':['promoteFact','acceptProposal','acceptVersion','approveScene']},
            'factId':{'type':'integer','minimum':1},'document':TEXT,'version':{'type':'integer','minimum':1},
            'proposalId':{'type':'integer','minimum':1},'state':{'enum':['canonical','idea']},'reason':{'type':'string','maxLength':100000},
            'expectedFingerprint':{'type':'string','pattern':'^[0-9a-f]{64}$'},
            'final':{'type':'boolean'},'review':{'type':'boolean'}}, ['sessionId','action']),
        'protocol/schema': ({}, []),
        'book/dashboard': ({}, []),
        'library/list': ({'topic': LIBRARY_TOPIC, 'query': {'type': 'string', 'maxLength': 120},
            'offset': {'type': 'integer', 'minimum': 0}}, ['topic']),
        'library/read': ({'topic': LIBRARY_TOPIC, 'key': LIBRARY_KEY}, ['topic', 'key']),
        'library/actionContext': ({'topic': LIBRARY_TOPIC, 'key': {'type': 'string', 'maxLength': 512},
            'action': {'enum': ['generate', 'review', 'adopt', 'select', 'withdraw', 'confirm-knowledge', 'withdraw-knowledge']},
            'fingerprint': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
            ['topic', 'key', 'action', 'fingerprint']),
    }
    return {'version':'lg.backend.v1', 'methods': {name:{'type':'object','additionalProperties':False,
        'properties':fields,'required':required} for name,(fields,required) in definitions.items()}}


def validate(method, params):
    schemas = contract()['methods']
    if method not in schemas:
        raise ValueError('Unknown method: ' + method)
    jsonschema.validate(params, schemas[method])
    if method in {'analysis/preview','workflow/start'} and 'category' in params and 'categories' in params:
        raise ValueError('Choose category or categories, not both')
    if method in {'studio/preview','studio/call'}:
        from .studio_operations import OPERATIONS,READ_ONLY
        if params['operation'] not in OPERATIONS:raise ValueError('Unknown studio operation')
        if method=='studio/call' and params['operation'] not in READ_ONLY and 'sessionId' not in params:
            raise ValueError('Studio writes require an owned session')
    if method=='run/start' and 'task' in params and 'stage' in params:
        raise ValueError('Choose either an explicit task or a UI stage')
    if method=='run/start' and params.get('routeTask') and ('task' in params or 'stage' in params):
        raise ValueError('Task interpretation applies only to natural-language requests')
    if method == 'workflow/run':
        story = params['command'] in STORY_COMMANDS
        if story and 'document' not in params:
            raise ValueError('Story workflow requires a document')
        if params['command']=='revise' and 'revisionMode' not in params:
            raise ValueError('Revision requires an explicit revisionMode')
        revision_fields = {'revisionMode','sourceVersion','passage'}
        if params['command']!='revise' and revision_fields.intersection(params):
            raise ValueError('Revision options apply only to revise')
        if not story and 'document' in params:
            raise ValueError('Document applies only to a story workflow')
        if story and any(params.get(key) for key in ('allowRaw','noReference','strategy')):
            raise ValueError('Story workflows do not support reference or strategy overrides')
    if method == 'creative/apply':
        if params['action']!='acceptVersion' and {'final','review'}.intersection(params):
            raise ValueError('Final and review apply only to acceptVersion')
        if params['action']!='acceptProposal' and {'proposalId','state'}.intersection(params):raise ValueError('Proposal fields apply only to acceptProposal')
        if params['action']!='promoteFact' and 'reason' in params:raise ValueError('Reason applies only to promoteFact')
        required = {'promoteFact':['factId'],'acceptProposal':['proposalId'],'acceptVersion':['document','version'],'approveScene':['document']}[params['action']]
        if any(key not in params for key in required):
            raise ValueError('Missing creative action parameters: ' + ', '.join(required))
