"""Fixed author workbench operations shared by the CLI and Backend."""
import copy
import hashlib
import json

import jsonschema

OPERATIONS={'status':'studio_status','dashboard':'book_dashboard','initialize':'studio_initialize',
    'save':'studio_save','draft':'studio_draft','review-context':'studio_review_context','review':'studio_review',
    'accept':'studio_accept','overview':'update_book_overview','library':'book_library','entry':'book_entry',
    'action-context':'book_action_context','reference':'studio_reference','adopt-reference':'studio_adopt_reference',
    'accept-proposal':'studio_accept_proposal','decision-options':'studio_decision_options',
    'select-decision':'studio_select_decision','withdraw-decision':'studio_withdraw_decision',
    'decision':'studio_decision','source-impact':'studio_source_impact',
    'knowledge':'studio_knowledge','knowledge-view':'studio_knowledge_view',
    'knowledge-propose':'studio_knowledge_propose','knowledge-confirm':'studio_knowledge_confirm',
    'knowledge-withdraw':'studio_knowledge_withdraw'}
READ_ONLY=frozenset({'status','dashboard','review-context','library','entry','action-context','decision','source-impact',
    'knowledge','knowledge-view'})


async def prepare(config,operation,arguments):
    from .writing_mcp import create_server
    from .mcp_approval import operation_details,project_fingerprint
    if operation not in OPERATIONS:raise ValueError('Unknown studio operation')
    tools={tool.name:tool for tool in await create_server(config).list_tools()}
    schema=tools[OPERATIONS[operation]].inputSchema
    try:jsonschema.validate(arguments,{**schema,'additionalProperties':False})
    except jsonschema.ValidationError:raise ValueError('Invalid studio operation arguments') from None
    normalized={key:copy.deepcopy(value['default']) for key,value in schema.get('properties',{}).items() if 'default' in value}
    normalized.update(copy.deepcopy(arguments))
    details=operation_details(config.workspace,OPERATIONS[operation],normalized)
    book=project_fingerprint(config.workspace)
    digest=hashlib.sha256(json.dumps({'operation':operation,'details':details,'book':book},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {'operation':operation,'tool':OPERATIONS[operation],'arguments':normalized,'write':operation not in READ_ONLY,
        'details':details,'bookFingerprint':book,'fingerprint':digest}


async def invoke(server,tool,arguments):
    value=await server.call_tool(tool,arguments)
    return value[1] if isinstance(value,tuple) else json.loads(value[0].text)
