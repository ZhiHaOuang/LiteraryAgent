"""Author/client task boundaries, enforced by the host before LG write approval."""
import copy
import uuid

import jsonschema

from .project_store import ProjectStore,ProjectStoreError

PLANNING_STAGES=('position','plan','world','scene')
UI_STAGES=('create',*PLANNING_STAGES,'draft','review','accept','overview')
TASK_SCHEMA={'type':'object','additionalProperties':False,'properties':{
    'kind':{'enum':['discussion','planning','draft','revision','restore','review','adopt','decision','knowledge','setting','overview','initialize']},
    'stage':{'enum':list(PLANNING_STAGES)},'document':{'type':'string','minLength':1,'maxLength':512},
    'sourceVersion':{'type':'integer','minimum':0},'versionNumber':{'type':'integer','minimum':1},
    'proposalId':{'type':'integer','minimum':1},'passage':{'type':'string','minLength':1,'maxLength':100000},
    'decisionAction':{'enum':['select','withdraw']},'optionId':{'type':'string','pattern':'^[A-Za-z0-9_-]{1,64}$'},
    'knowledgeAction':{'enum':['confirm','withdraw']},
    'sequence':{'type':'integer','minimum':1},'referenceKind':{'enum':['character','world','outline','note']},
    'allowRevision':{'type':'boolean'},
    'contextDocument':{'type':'string','minLength':1,'maxLength':512},'contextVersion':{'type':'integer','minimum':1},
    'maxWrites':{'type':'integer','minimum':1,'maximum':16}},'required':['kind']}


def stage_task(workspace,stage):
    if stage not in UI_STAGES:raise ValueError('Unknown literary task stage')
    if stage in PLANNING_STAGES:return {'kind':'planning','stage':stage}
    if stage in {'create','overview'}:return {'kind':'initialize' if stage=='create' else 'overview'}
    store=ProjectStore(workspace)
    saved=[d for d in store.list_documents(kind='chapter',limit=None) if d.state not in {'archived','rejected'}]
    chapters=[d for d in saved if d.sequence==1]
    if len(chapters)>1:raise ValueError('Choose the exact first chapter before starting this task')
    if not chapters:
        if saved:raise ValueError('Choose an exact chapter; the existing first chapter is not identified')
        if stage!='draft':raise ValueError('Choose an existing chapter and candidate first')
        return {'kind':'draft','document':'chapter-01','sourceVersion':0,'sequence':1}
    document=chapters[0]
    task={'kind':{'draft':'draft','review':'review','accept':'adopt'}[stage],
        'document':document.slug,'sourceVersion':document.active_version_id}
    if stage!='draft':
        candidates=[v for v in store.list_versions(document.id,limit=1000) if v.state=='candidate']
        if len(candidates)!=1:raise ValueError('Choose the exact saved candidate before review or adoption')
        task['versionNumber']=candidates[0].version_number
        if stage=='review':task['allowRevision']=True
    return task


def normalize_definition(definition):
    definition=copy.deepcopy(definition)
    jsonschema.validate(definition,TASK_SCHEMA)
    kind=definition['kind']
    allowed={'kind','maxWrites'}
    allowed|={'planning':{'stage','referenceKind','document','sourceVersion','contextDocument','contextVersion'},
        'draft':{'document','sourceVersion','sequence'},'revision':{'document','sourceVersion','passage','versionNumber'},
        'restore':{'document','sourceVersion','versionNumber'},
        'review':{'document','sourceVersion','versionNumber','allowRevision'},'adopt':{'document','sourceVersion','versionNumber','proposalId'},
        'decision':{'document','sourceVersion','decisionAction','versionNumber','optionId'},
        'knowledge':{'document','sourceVersion','knowledgeAction','versionNumber'},
        'setting':set()}.get(kind,set())
    if set(definition)-allowed:raise ValueError('Task fields do not match its deliverable')
    if kind=='planning':definition.setdefault('stage','plan')
    definition.setdefault('maxWrites',8 if kind=='planning' else 5 if kind=='review' else 1)
    if kind=='decision':
        if not {'document','sourceVersion','decisionAction'}<=set(definition) or definition['sourceVersion']==0:
            raise ValueError('Decision task requires its exact saved record head and action')
        if definition['maxWrites']!=1:raise ValueError('Decision task permits exactly one transition')
        selected={'versionNumber','optionId'}
        if definition['decisionAction']=='select' and not selected<=set(definition):
            raise ValueError('Decision selection requires the exact options version and option ID')
        if definition['decisionAction']=='withdraw' and set(definition)&selected:
            raise ValueError('Decision withdrawal cannot select an option')
    if kind=='knowledge':
        if not {'document','sourceVersion','knowledgeAction'}<=set(definition) or definition['sourceVersion']==0:
            raise ValueError('Knowledge task requires its exact saved record head and action')
        if definition['maxWrites']!=1:raise ValueError('Knowledge task permits exactly one transition')
        if definition['knowledgeAction']=='confirm' and 'versionNumber' not in definition:
            raise ValueError('Knowledge confirmation requires the exact proposal version')
        if definition['knowledgeAction']=='withdraw' and 'versionNumber' in definition:
            raise ValueError('Knowledge withdrawal cannot confirm a proposal')
    if kind in {'draft','revision','restore','review'} or kind=='adopt' and 'proposalId' not in definition:
        if not {'document','sourceVersion'}<=set(definition):raise ValueError('Task requires an exact document and sourceVersion')
    if kind in {'restore','review','adopt'} and 'proposalId' not in definition and 'versionNumber' not in definition:
        raise ValueError('Task requires the exact candidate versionNumber')
    if 'document' in definition and 'sourceVersion' not in definition:raise ValueError('Task requires sourceVersion')
    if ('contextDocument' in definition)!=('contextVersion' in definition):raise ValueError('Planning context requires an exact source version')
    if kind=='revision' and definition['sourceVersion']==0:raise ValueError('Revision requires an existing source version')
    if 'proposalId' in definition:
        if kind!='adopt' or set(definition)&{'document','sourceVersion','versionNumber'}:
            raise ValueError('Proposal adoption cannot name another target')
    return definition


def bind_task(workspace,definition):
    definition=normalize_definition(definition)
    kind=definition['kind']
    store=ProjectStore(workspace)
    if 'contextDocument' in definition:
        context=store.get_document(definition['contextDocument'])
        if context.active_version_id!=definition['contextVersion']:raise ValueError('Planning context version changed')
        definition['contextDocument']=context.slug
    if 'proposalId' in definition:
        proposals={p.id:p for p in store.list_proposals(limit=1000)}
        if definition['proposalId'] not in proposals:raise ValueError('Task proposal is missing')
    if 'document' in definition:
        if 'sourceVersion' not in definition:raise ValueError('Task requires sourceVersion')
        try:document=store.get_document(definition['document'])
        except ProjectStoreError:
            if kind not in {'draft','planning'} or definition['sourceVersion']!=0:raise
            if kind=='draft' and 'sequence' not in definition:raise ValueError('A new chapter task requires its sequence')
        else:
            if document.active_version_id!=definition['sourceVersion']:raise ValueError('Task source version changed')
            definition['document']=document.slug
            if kind in {'draft','revision','restore'} and document.kind not in {'chapter','scene','note'}:
                raise ValueError('Prose task requires a manuscript document')
            source=store.get_version(document.id,definition['versionNumber']) if 'versionNumber' in definition else document
            if kind=='revision' and source.state in {'archived','rejected'}:raise ValueError('Selected revision source is no longer available')
            passage=definition.get('passage')
            if passage:
                start=source.content.find(passage)
                if start<0 or source.content.find(passage,start+1)>=0:
                    raise ValueError('Selected passage must occur exactly once in the current source')
    if kind=='decision':
        arguments={'expected_version_id':definition['sourceVersion']}
        if definition['decisionAction']=='select':
            store.decision_selection_preview(definition['document'],options_version_number=definition['versionNumber'],
                option_id=definition['optionId'],**arguments)
        else:store.decision_withdrawal_preview(definition['document'],**arguments)
    if kind=='knowledge':
        arguments={'expected_version_id':definition['sourceVersion']}
        if definition['knowledgeAction']=='confirm':
            store.knowledge_confirmation_preview(definition['document'],
                proposal_version_number=definition['versionNumber'],**arguments)
        else:store.knowledge_withdrawal_preview(definition['document'],**arguments)
    return {'schema':'lg.task.v1','id':uuid.uuid4().hex,'definition':definition,'grants':0,'overviewGrants':0,'revisionGrants':0}


def check_operation(workspace,scope,details):
    """Return the grant class; no approval mode can override this boundary."""
    if scope.get('schema')!='lg.task.v1':raise ValueError('Invalid literary task scope')
    definition=scope['definition'];kind=definition['kind']
    if 'contextDocument' in definition and ProjectStore(workspace).get_document(definition['contextDocument']).active_version_id!=definition['contextVersion']:
        raise ValueError('Planning context version changed; refresh the task')
    tool=details.get('tool');args=details.get('arguments',{})
    if not isinstance(args,dict):raise ValueError('Invalid scoped tool arguments')
    tools={'discussion':set(),'planning':{'studio_save','studio_reference','studio_decision_options','studio_knowledge_propose'},
        'draft':{'create_chapter','studio_draft','edit_manuscript','update_book_overview'},
        'revision':{'edit_manuscript','studio_draft','update_book_overview'},
        'restore':{'restore_manuscript','update_book_overview'},
        'review':{'studio_review'},'adopt':{'studio_accept','studio_adopt_reference','studio_accept_proposal'},
        'decision':{'studio_select_decision' if definition.get('decisionAction')=='select' else 'studio_withdraw_decision'},
        'knowledge':{'studio_knowledge_confirm' if definition.get('knowledgeAction')=='confirm' else 'studio_knowledge_withdraw'},
        'setting':{'propose_setting','studio_knowledge_propose'},'overview':{'update_book_overview'},'initialize':{'studio_initialize'}}[kind]
    if kind=='review' and definition.get('allowRevision'):tools|={'edit_manuscript','studio_draft','update_book_overview'}
    if tool not in tools:raise ValueError('Tool is outside the author task scope; no write authorized')
    grant='overviewGrants' if tool=='update_book_overview' and kind in {'draft','revision','restore','review'} else 'revisionGrants' if kind=='review' and tool in {'edit_manuscript','studio_draft'} else 'grants'
    generated=scope.get('revisionGrants',0) if kind=='review' else scope['grants']
    if grant=='overviewGrants' and not generated:raise ValueError('Generate the requested candidate before updating its overview')
    if scope.get(grant,0)>=(1 if grant!='grants' else definition.get('maxWrites',1)):
        raise ValueError('Task stopping point reached; a new author task is required')
    if kind=='decision':
        if args.get('slug')!=definition['document'] or args.get('expected_version_id')!=definition['sourceVersion']:
            raise ValueError('Creative decision is outside the exact author task target')
        store=ProjectStore(workspace)
        if definition['decisionAction']=='select':
            if args.get('options_version_number')!=definition['versionNumber'] or args.get('option_id')!=definition['optionId']:
                raise ValueError('Creative option is outside the exact author task selection')
            store.decision_selection_preview(definition['document'],options_version_number=definition['versionNumber'],
                option_id=definition['optionId'],expected_version_id=definition['sourceVersion'])
        else:store.decision_withdrawal_preview(definition['document'],expected_version_id=definition['sourceVersion'])
        return grant
    if kind=='knowledge':
        if args.get('slug')!=definition['document'] or args.get('expected_version_id')!=definition['sourceVersion']:
            raise ValueError('Knowledge operation is outside the exact author task target')
        store=ProjectStore(workspace)
        if definition['knowledgeAction']=='confirm':
            if args.get('proposal_version_number')!=definition['versionNumber']:
                raise ValueError('Knowledge proposal is outside the exact author task selection')
            store.knowledge_confirmation_preview(definition['document'],proposal_version_number=definition['versionNumber'],
                expected_version_id=definition['sourceVersion'])
        else:store.knowledge_withdrawal_preview(definition['document'],expected_version_id=definition['sourceVersion'])
        return grant
    if kind=='planning':
        if tool=='studio_save' and args.get('stage')!=definition['stage']:raise ValueError('Planning stage is outside the task scope')
        updates={'position':{'positioning','targetWords','tasks','pending'},
            'plan':{'targetWords','volumes','foreshadowing','tasks','pending'},
            'world':{'characters','relationships','settings','foreshadowing','tasks','pending'},
            'scene':{'foreshadowing','tasks','pending'}}[definition['stage']]
        if tool=='studio_save' and (not isinstance(args.get('updates'),dict) or set(args['updates'])-updates):
            raise ValueError('Profile updates are outside the planning task')
        kinds={'character','world','note'} if definition['stage']=='world' else {'outline','note'}
        if definition.get('referenceKind'):kinds={definition['referenceKind']}
        if tool=='studio_reference' and args.get('kind') not in kinds:raise ValueError('Reference kind is outside the planning task')
    if tool=='studio_accept_proposal':
        if args.get('proposal_id')!=definition.get('proposalId'):raise ValueError('Proposal is outside the task scope')
        return grant
    if 'proposalId' in definition:raise ValueError('Only the selected proposal may be adopted')
    if 'document' in definition and tool!='update_book_overview':
        if args.get('slug')!=definition['document']:raise ValueError('Document is outside the task scope')
        store=ProjectStore(workspace)
        try:document=store.get_document(definition['document'])
        except ProjectStoreError:
            if definition['sourceVersion']!=0:raise
            document=None
        actual=document.active_version_id if document else 0
        if actual!=definition['sourceVersion']:raise ValueError('Task source version changed; refresh the task')
        if tool in {'studio_draft','edit_manuscript','restore_manuscript','studio_reference','studio_decision_options','studio_knowledge_propose','studio_accept','studio_adopt_reference'} and args.get('expected_version_id')!=actual:
            raise ValueError('Tool source version does not match the author task')
        if tool=='create_chapter' and actual!=0:raise ValueError('Task target already exists')
        if 'sequence' in definition and args.get('sequence')!=definition['sequence']:raise ValueError('Chapter sequence is outside the task scope')
        if 'versionNumber' in definition and tool in {'restore_manuscript','studio_review','studio_accept','studio_adopt_reference'} and args.get('version_number')!=definition['versionNumber']:
            raise ValueError('Candidate version is outside the task scope')
        source=store.get_version(document.id,definition['versionNumber']) if kind=='revision' and 'versionNumber' in definition else document
        if kind=='revision' and source.state in {'archived','rejected'}:raise ValueError('Selected revision source is no longer available')
        if definition.get('passage'):
            passage=definition['passage'];start=source.content.find(passage)
            if start<0:raise ValueError('Selected passage changed')
            before=source.content[:start];after=source.content[start+len(passage):]
            body=args.get('content')
            if not isinstance(body,str) or not body.startswith(before) or not body.endswith(after) or len(body)<len(before)+len(after):
                raise ValueError('Revision changes text outside the selected passage')
    return grant


def scope_prompt(scope):
    import json
    return ('Host-enforced author task; tool permission never expands it. Inherit this scope for continue/steer. '
        'Stop at its grant limit; report incomplete work rather than starting another task.\n'
        +json.dumps({'taskId':scope['id'],**scope['definition'],'grantsUsed':scope['grants']},ensure_ascii=False))


def scope_summary(scope):
    definition=scope['definition']
    label={'discussion':'讨论与读取','planning':'规划资料','draft':'正文候选','revision':'正文修订候选','restore':'历史版本恢复候选',
        'review':'候选审读','adopt':'指定候选采用','decision':'创作方向精确选择' if definition.get('decisionAction')=='select' else '撤回创作方向选择',
        'knowledge':'精确认知提案确认' if definition.get('knowledgeAction')=='confirm' else '撤回认知记录确认',
        'setting':'设定提案','overview':'作品概况','initialize':'当前作品初始化'}[definition['kind']]
    target='：'+definition['document'] if definition.get('document') else ''
    passage='；只修改指定选区，保留其余原文' if definition.get('passage') else ''
    return label+target+passage+'。完成指定范围后停止；操作权限不会扩大本轮任务。'
