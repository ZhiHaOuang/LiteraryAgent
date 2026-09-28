"""Public request contract shared by transport, service and future TS clients."""
import jsonschema

TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 100000}
ID = {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,80}$'}
MODES = {'enum': ['ask', 'auto_review', 'full_access']}


def contract():
    definitions = {
        'initialize': ({}, []), 'session/list': ({}, []), 'session/create': ({}, []),
        'session/open': ({'sessionId': ID}, ['sessionId']),
        'session/read': ({'sessionId': ID}, ['sessionId']),
        'session/close': ({'sessionId': ID}, ['sessionId']),
        'session/rename': ({'sessionId': ID, 'title': {'type':'string','minLength':1,'maxLength':80}}, ['sessionId','title']),
        'events/read': ({'sessionId': ID,'after':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':1000}}, ['sessionId']),
        'permissions/set': ({'sessionId': ID,'mode': MODES,'creativeMode': MODES}, ['sessionId','mode']),
        'run/start': ({'sessionId': ID,'text':TEXT,'outputSchema':{'type':'object'}}, ['sessionId','text']),
        'run/steer': ({'sessionId': ID,'text':TEXT}, ['sessionId','text']),
        'run/cancel': ({'sessionId': ID}, ['sessionId']),
        'approval/resolve': ({'approvalId':ID,'result':{'type':'object'}}, ['approvalId','result']),
        'reference/contracts': ({}, []),
        'reference/validate': ({'category':TEXT,'card':{'type':'object'}}, ['category','card']),
        'workflow/start': ({'sessionId':ID,'category':TEXT,'chapter':TEXT}, ['sessionId','category','chapter']),
        'creative/apply': ({'sessionId':ID,'action':{'enum':['promoteFact','acceptVersion']},
            'factId':{'type':'integer','minimum':1},'document':TEXT,'version':{'type':'integer','minimum':1}}, ['sessionId','action']),
        'protocol/schema': ({}, []),
    }
    return {'version':'lg.backend.v1', 'methods': {name:{'type':'object','additionalProperties':False,
        'properties':fields,'required':required} for name,(fields,required) in definitions.items()}}


def validate(method, params):
    schemas = contract()['methods']
    if method not in schemas:
        raise ValueError('Unknown method: ' + method)
    jsonschema.validate(params, schemas[method])
    if method == 'creative/apply':
        required = ['factId'] if params['action'] == 'promoteFact' else ['document','version']
        if any(key not in params for key in required):
            raise ValueError('Missing creative action parameters: ' + ', '.join(required))
