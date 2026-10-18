import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp} from 'node:fs/promises';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {resolve,join} from 'node:path';
import {tmpdir} from 'node:os';
import {LiteraryBackend,type LiteraryBackendSettings} from '../src/backend/LiteraryBackend';
import {SessionStore} from '../src/host/SessionStore';
import type {WritingAction} from '../shared/protocol';

class DecisionBackend extends LiteraryBackend{
  constructor(private fixture:LiteraryBackendSettings){super(fixture);}
  protected override launch(){return {command:this.fixture.python,
    args:[resolve('tests/fixtures/creative_decision_fixture.py'),this.fixture.workspace,
      this.fixture.runtimeEngine!,this.fixture.manifest],cwd:this.fixture.workspace,env:this.fixture.env,jsonRpc:true};}
}
async function until(predicate:()=>boolean){
  const end=Date.now()+45_000;
  while(!predicate()){
    if(Date.now()>end)throw Error('Creative-decision connector event timeout');
    await new Promise(r=>setTimeout(r,20));
  }
}

for(const engine of ['native','codex'] as const){
  test(`creative decisions ${engine}: exact options, separate creative approval, stale preview and empty withdrawal`,{
    timeout:240_000,skip:!process.env.LG_TEST_PYTHON||(engine==='codex'&&!process.env.LG_TEST_RUNTIME_MANIFEST)},async t=>{
    const workspace=await mkdtemp(join(tmpdir(),`lg-decision-${engine}-`));
    const settings:LiteraryBackendSettings={workspace,python:process.env.LG_TEST_PYTHON!,
      manifest:process.env.LG_TEST_RUNTIME_MANIFEST||'',runtimeEngine:engine,
      env:{...process.env,LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')}};
    const backend=new DecisionBackend(settings),store=new SessionStore('codex');
    backend.subscribe(event=>store.apply(event));
    t.after(async()=>{t.diagnostic(JSON.stringify({workspace,error:store.state.error,run:store.state.run}));await backend.dispose();});
    await backend.connect();const sessions=await backend.list();assert.equal(sessions.length,1);
    const session=sessions[0];await backend.resume(session.id);
    const inspectCode=String.raw`
import json,sys
from pathlib import Path
from dataclasses import asdict
from lg_cli.project_store import ProjectStore
from lg_cli.session_runtime import SessionRuntime
root=Path(sys.argv[1]);saved=ProjectStore(root);runtime=SessionRuntime(root,sys.argv[2]).read()
checkpoint=root/'.literarygiant'/'runtime'/'native'/(sys.argv[2]+'.json')
native=json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
calls=root/'model-calls.jsonl';errors=root/'provider-errors.json'
print(json.dumps({'decision':saved.decision_snapshot('ending-choice'),
 'documents':[asdict(d) for d in saved.list_documents(limit=1000)],
 'versions':[asdict(v) for v in saved.list_versions('ending-choice')],
 'chapters':{slug:{'document':asdict(saved.get_document(slug)),
   'versions':[asdict(v) for v in saved.list_versions(slug)]} for slug in ('one','two')},
 'canonical':[asdict(f) for f in saved.list_facts(state='canonical',limit=None)],'runtime':runtime,
 'checkpoint':{'schema':native.get('schema'),'thread':native.get('thread')},
 'coreHistory':any((root/'.literarygiant'/'codex-home'/'sessions').rglob('*.jsonl')),
 'calls':[json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else [],
 'providerErrors':json.loads(errors.read_text()) if errors.exists() else []},ensure_ascii=False))
`;
    async function python(code:string){
      const result=await promisify(execFile)(settings.python,['-c',code,workspace,session.id],{env:settings.env});
      return result.stdout;
    }
    async function inspect(){return JSON.parse(await python(inspectCode));}
    async function start(key:string,action:WritingAction){
      const detail=await backend.readEntry('pending',key);
      assert.ok(detail.actions.includes(action));
      const prompt=await backend.prepareWriting('pending',key,action,detail.fingerprint);
      const previous=store.state.run?.id;
      await backend.run(session.id,prompt);
      await until(()=>!!store.state.run&&store.state.run.id!==previous);
      await until(()=>store.state.approvals.some(a=>a.state==='pending')||!store.busy);
      const approval=store.state.approvals.find(a=>a.state==='pending');
      assert.ok(approval,'full_access operation permission must still ask for independent creative approval: '+JSON.stringify(store.state.items));
      const details=JSON.parse(approval.details!);
      if(action==='select'){
        assert.deepEqual(details.arguments,{slug:'ending-choice',expected_version_id:decision.version_id,
          options_version_number:2,option_id:'B'});
        assert.equal(details.preview.proposed_selection.options_version_id,decision.options_version_id);
        assert.equal(details.preview.proposed_selection.content,decision.options[1].content);
        assert.deepEqual(details.preview.proposed_selection.sources,decision.sources);
      }else{
        assert.equal(details.preview.action,'withdraw');assert.equal(details.preview.proposed_selection,null);
        assert.equal(details.preview.decision.selection.option_id,'B');
      }
      return {detail,approval};
    }
    async function finish(){
      await until(()=>!store.busy);
      assert.equal(store.state.run?.state,'completed',store.state.error||JSON.stringify(store.state.items));
    }
    const initial=await inspect(),decision=initial.decision;
    assert.equal(decision.version_number,2);assert.equal(decision.options_version_number,2);
    assert.equal(decision.selection,null);assert.equal(decision.status,'unselected');
    assert.equal(initial.runtime.mode,'full_access');assert.equal(initial.runtime.creative_mode,'ask');
    assert.equal(initial.canonical.length,1);assert.equal(decision.options.length,3);
    assert.equal(new Set(decision.options.map((option:any)=>option.content)).size,3);
    const optionKey=`decision-option:${decision.document_id}:2:B`,recordKey=`decision:${decision.document_id}`;
    const unchanged=(state:any)=>{
      assert.deepEqual(state.chapters,initial.chapters,'Deciding a plan cannot change manuscript or its version history');
      assert.deepEqual(state.canonical,initial.canonical,'Creative choices are plans, never Canonical facts');
      assert.deepEqual(state.providerErrors,[]);
    };
    const preview=await backend.readEntry('pending',optionKey);
    assert.match(preview.body,/第二组：主角留下揭穿阴谋。/);assert.deepEqual(preview.actions,['select']);
    assert.deepEqual((await inspect()).documents,initial.documents,'Reading a preview must not select it');

    const declined=await start(optionKey,'select');
    assert.equal(JSON.parse(declined.approval.details!).tool,'studio_select_decision');
    await backend.approve(declined.approval.id,'decline');await finish();
    let state=await inspect();unchanged(state);
    assert.deepEqual(state.documents,initial.documents);assert.deepEqual(state.versions,initial.versions);
    assert.ok(store.state.items.some(item=>item.kind==='tool'&&item.state==='failed'));
    await assert.rejects(backend.approve(declined.approval.id,'accept'),/过期|已回答/);

    const stale=await start(optionKey,'select');
    await python(String.raw`
import sys
from pathlib import Path
from lg_cli.project_store import ProjectStore
ProjectStore(Path(sys.argv[1])).create_document(kind='note',slug='author-note',title='作者并发备注',content='审批期间由作者添加。',state='accepted')
`);
    const afterAuthor=await inspect();
    assert.equal(afterAuthor.documents.length,initial.documents.length+1);
    assert.equal(afterAuthor.documents.find((doc:any)=>doc.slug==='author-note').content,'审批期间由作者添加。');
    await backend.approve(stale.approval.id,'accept');await finish();
    state=await inspect();unchanged(state);
    assert.deepEqual(state.documents,afterAuthor.documents);assert.deepEqual(state.versions,initial.versions);
    assert.ok(store.state.items.some(item=>item.kind==='tool'&&item.state==='failed'));
    await assert.rejects(backend.prepareWriting('pending',optionKey,'select',stale.detail.fingerprint),/过期|改变|changed|刷新|RPC -32000/);

    const selected=await start(optionKey,'select');
    assert.equal(JSON.parse(selected.approval.details!).tool,'studio_select_decision');
    await backend.approve(selected.approval.id,'accept');await finish();
    state=await inspect();unchanged(state);
    assert.equal(state.versions.length,3);assert.equal(state.decision.status,'selected');
    const otherDocuments=(value:any)=>value.documents.filter((doc:any)=>doc.kind!=='creative_decision');
    assert.deepEqual(otherDocuments(state),otherDocuments(afterAuthor));
    assert.equal(state.decision.selection.option_id,'B');
    assert.equal(state.decision.selection.options_version_id,decision.options_version_id);
    assert.equal(state.decision.selection.options_version_number,2);
    assert.equal(state.decision.selection.content,decision.options[1].content);
    assert.deepEqual(state.decision.selection.sources,decision.sources);
    assert.deepEqual(state.decision.options,decision.options);
    assert.deepEqual((await backend.readEntry('pending',optionKey)).actions,[]);
    const task=state.runtime.task.definition;
    assert.deepEqual(task,{kind:'decision',decisionAction:'select',document:'ending-choice',
      sourceVersion:decision.version_id,versionNumber:2,optionId:'B',maxWrites:1});
    assert.equal(state.runtime.mode,'full_access');assert.equal(state.runtime.creative_mode,'ask');
    assert.equal(state.runtime.engine,engine);assert.ok(state.runtime.thread_id);
    if(engine==='native'){
      assert.equal(state.checkpoint.schema,'lg.native-runtime.v1');
      assert.equal(state.checkpoint.thread,state.runtime.thread_id);assert.equal(state.coreHistory,false);
    }else{assert.equal(state.coreHistory,true);assert.equal(state.checkpoint.schema,null);}
    await assert.rejects(backend.approve(selected.approval.id,'accept'),/过期|已回答/);

    const withdrawal=await start(recordKey,'withdraw');
    assert.equal(JSON.parse(withdrawal.approval.details!).tool,'studio_withdraw_decision');
    await backend.approve(withdrawal.approval.id,'accept');await finish();
    state=await inspect();unchanged(state);
    assert.deepEqual(otherDocuments(state),otherDocuments(afterAuthor));
    assert.equal(state.versions.length,4);assert.equal(state.decision.status,'withdrawn');
    assert.equal(state.decision.selection,null,'Withdrawal must not fall back to an old option');
    assert.deepEqual(state.decision.options,decision.options);
    assert.equal(JSON.parse(state.versions.find((version:any)=>version.version_number===3).content).selection.option_id,'B');
    assert.equal(JSON.parse(state.versions.find((version:any)=>version.version_number===4).content).selection,null);
    const withdrawn=await backend.readEntry('pending',recordKey);
    assert.deepEqual(withdrawn.actions,[]);assert.match(withdrawn.body,/当前无正式选择/);
    assert.equal(state.calls.length,4,'Exactly one real MCP attempt for each prepared task');
    assert.deepEqual(state.calls.map((call:any)=>call.task.decisionAction),['select','select','select','withdraw']);
    assert.deepEqual(state.calls.slice(0,3).map((call:any)=>call.arguments),[1,2,3].map(()=>({slug:'ending-choice',
      expected_version_id:decision.version_id,options_version_number:2,option_id:'B'})));
    assert.deepEqual(state.calls[3].arguments,{slug:'ending-choice',expected_version_id:state.versions.find((version:any)=>version.version_number===3).id});
  });
}
