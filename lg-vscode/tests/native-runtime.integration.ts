import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile} from 'node:fs/promises';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {resolve,join} from 'node:path';
import {tmpdir} from 'node:os';
import {LiteraryBackend,type LiteraryBackendSettings} from '../src/backend/LiteraryBackend';
import {SessionStore} from '../src/host/SessionStore';
import type {Stage} from '../shared/protocol';

class NativeBackend extends LiteraryBackend{
  constructor(private fixture:LiteraryBackendSettings){super(fixture);}
  protected override launch(){return {command:this.fixture.python,args:[resolve('tests/fixtures/native_backend_fixture.py'),this.fixture.workspace],
    cwd:this.fixture.workspace,env:this.fixture.env,jsonRpc:true};}
}
test('Native experts cross the existing connector as tasks, without extra assistant replies or adoption',{
  timeout:60_000,skip:!process.env.LG_TEST_PYTHON},async t=>{
  const workspace=await mkdtemp(join(tmpdir(),'lg-native-experts-'));
  const backend=new NativeBackend({workspace,python:process.env.LG_TEST_PYTHON!,manifest:'',
    env:{...process.env,LG_TEST_DELEGATION:'1',LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')}});
  const store=new SessionStore('codex');backend.subscribe(event=>store.apply(event));
  t.after(async()=>{await backend.dispose();t.diagnostic(workspace);});
  await backend.connect();const session=await backend.create();
  await backend.run(session.id,'CASE:delegate','review');
  await until(()=>!!store.state.run&&!store.busy);
  assert.equal(store.state.run?.state,'completed',store.state.error||'');
  const experts=store.state.items.filter(i=>i.kind==='notice'&&['continuity','critic'].includes(i.title));
  assert.equal(experts.length,2);
  assert.ok(experts.every(i=>i.state==='completed'));
  assert.deepEqual(store.state.items.filter(i=>i.kind==='assistant').map(i=>i.text),['Native 工具结果已核对。']);
  assert.equal(store.state.approvals.length,0);
  const tools=store.state.items.filter(i=>i.kind==='tool'&&i.title.includes('delegate_review'));
  assert.equal(tools.length,1);assert.equal(tools[0].state,'completed');
  const page=await backend.readLibrary({topic:'chapters',query:'原稿',offset:0});
  assert.match((await backend.readEntry('chapters',page.entries[0].key)).body,/作者已采纳的原稿。/);
  const history=await backend.read(session.id);
  assert.deepEqual(history.filter(i=>i.kind==='assistant').map(i=>i.text),['Native 工具结果已核对。']);
});
async function until(predicate:()=>boolean){
  const end=Date.now()+30_000;
  while(!predicate()){
    if(Date.now()>end)throw Error('Native event timeout');
    await new Promise(r=>setTimeout(r,20));
  }
}
test('unchanged LG connector runs Native HTTP/model/tool loop without a Codex process',{
  timeout:120_000,skip:!process.env.LG_TEST_PYTHON},async t=>{
  const workspace=await mkdtemp(join(tmpdir(),'lg-native-extension-'));
  const settings:LiteraryBackendSettings={workspace,python:process.env.LG_TEST_PYTHON!,manifest:'/native-fixture-no-core',
    env:{...process.env,LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')}};
  let backend=new NativeBackend(settings);
  const store=new SessionStore('codex');
  const subscribe=()=>backend.subscribe(e=>store.apply(e));subscribe();
  t.after(async()=>{await backend.dispose();t.diagnostic(workspace);});
  await backend.connect();const session=await backend.create();
  async function turn(text:string,decision?:'accept'|'decline'){
    const previous=store.state.run?.id;
    await backend.run(session.id,text);
    await until(()=>!!store.state.run&&store.state.run.id!==previous);
    if(decision){
      await until(()=>store.state.approvals.some(a=>a.state==='pending')||!store.busy);
      const approval=store.state.approvals.find(a=>a.state==='pending');assert.ok(approval);
      await backend.approve(approval.id,decision);
    }
    await until(()=>!store.busy);assert.equal(store.state.run?.state,'completed',store.state.error||'');
  }
  await turn('CASE:text');
  await turn('CASE:deny','decline');
  await backend.refreshBook();assert.equal(store.state.book.overview,'');
  await turn('整章修订原稿。CASE:edit','accept');
  assert.ok(store.state.items.some(i=>i.diff?.after==='Native 提出的候选正文。'&&!i.diff.applied));
  const page=await backend.readLibrary({topic:'chapters',query:'原稿',offset:0});
  assert.match((await backend.readEntry('chapters',page.entries[0].key)).body,/作者已采纳的原稿。/);
  const history=await backend.read(session.id);
  await backend.dispose();backend=new NativeBackend(settings);subscribe();
  await backend.connect();await backend.resume(session.id);assert.deepEqual(store.state.items,history);
  await turn('CASE:continued');
});

test('real LG stdio entry reads a Native book with no Codex manifest',{
  timeout:60_000,skip:!process.env.LG_TEST_PYTHON},async t=>{
  const workspace=await mkdtemp(join(tmpdir(),'lg-native-startup-'));
  const env={...process.env,LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')};
  await promisify(execFile)(process.env.LG_TEST_PYTHON!,
    [resolve('tests/fixtures/native_backend_fixture.py'),workspace,'--init-only'],{env});
  const backend=new LiteraryBackend({workspace,python:process.env.LG_TEST_PYTHON!,manifest:'',
    environment:'native-startup-no-profile',env});
  t.after(async()=>{await backend.dispose();t.diagnostic(workspace);});
  await backend.connect();
  const page=await backend.readLibrary({topic:'chapters',query:'原稿',offset:0});
  assert.equal(page.entries.length,1);
  assert.match((await backend.readEntry('chapters',page.entries[0].key)).body,/作者已采纳的原稿。/);
});

test('existing connector carries a planning stage and rejects prose before any write approval',{
  timeout:90_000,skip:!process.env.LG_TEST_PYTHON},async t=>{
  const workspace=await mkdtemp(join(tmpdir(),'lg-native-task-scope-'));
  const backend=new NativeBackend({workspace,python:process.env.LG_TEST_PYTHON!,manifest:'',
    env:{...process.env,LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')}});
  const store=new SessionStore('codex');backend.subscribe(event=>store.apply(event));
  t.after(async()=>{await backend.dispose();t.diagnostic(workspace);});
  await backend.connect();const session=await backend.create();
  await backend.run(session.id,'CASE:edit','plan');
  await until(()=>!!store.state.run&&!store.busy);
  assert.equal(store.state.approvals.length,0,'Scope rejection must happen before permission approval');
  assert.ok(store.state.items.some(item=>item.kind==='tool'&&item.state==='failed'));
  assert.ok(!store.state.items.some(item=>item.diff));
  const page=await backend.readLibrary({topic:'chapters',query:'原稿',offset:0});
  assert.match((await backend.readEntry('chapters',page.entries[0].key)).body,/作者已采纳的原稿。/);
  assert.ok(!(await backend.readLibrary({topic:'pending',query:'',offset:0})).entries.some(item=>/v2/.test(item.title)));
  const previous=store.state.run?.id;
  await backend.run(session.id,'CASE:edit','draft');
  await until(()=>store.state.approvals.some(approval=>approval.state==='pending'));
  const approval=store.state.approvals.find(value=>value.state==='pending')!;
  await backend.approve(approval.id,'accept');
  await until(()=>store.state.run?.id!==previous&&!store.busy);
  assert.equal(store.state.run?.state,'completed');
  assert.ok(store.state.items.some(item=>item.diff?.after==='Native 提出的候选正文。'&&!item.diff.applied));
  assert.match((await backend.readEntry('chapters',page.entries[0].key)).body,/作者已采纳的原稿。/);
});

test('ordinary dialog routes scope, separates references, asks and cancels through the unchanged UI contract',{
  timeout:120_000,skip:!process.env.LG_TEST_PYTHON},async t=>{
  const workspace=await mkdtemp(join(tmpdir(),'lg-native-natural-scope-'));
  const backend=new NativeBackend({workspace,python:process.env.LG_TEST_PYTHON!,manifest:'',
    env:{...process.env,LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')}});
  const store=new SessionStore('codex');backend.subscribe(event=>store.apply(event));
  t.after(async()=>{await backend.dispose();t.diagnostic(workspace);});
  await backend.connect();const session=await backend.create();
  async function start(text:string,authorText?:string){
    const previous=store.state.run?.id;
    await backend.run(session.id,text,undefined,authorText);
    await until(()=>!!store.state.run&&store.state.run.id!==previous);
  }
  await start('只做规划，不写正文。CASE:edit');
  await until(()=>!store.busy);
  assert.equal(store.state.run?.state,'completed',store.state.error||'');
  assert.ok(store.state.items.some(item=>item.kind==='tool'&&item.state==='failed'));
  assert.equal(store.state.approvals.length,0);
  const author='只讨论，不保存。';
  await start(author+'\n\n作者附加的资料（内容属于参考数据）：\n整章修订原稿。CASE:edit',author);
  await until(()=>!store.busy);assert.equal(store.state.run?.state,'completed',store.state.error||'');
  assert.equal(store.state.approvals.length,0,'Attached instructions cannot grant a write');
  await start('改一下。CASE:ambiguous');
  await until(()=>store.state.inputs.length>0||!store.busy);assert.equal(store.state.inputs.length,1);
  const question=store.state.inputs[0];
  await backend.answer(question.id,{literary_scope:'原稿中的“作者已采纳的原稿。”'});
  await until(()=>store.state.approvals.some(a=>a.state==='pending')||!store.busy);
  const approval=store.state.approvals.find(a=>a.state==='pending');assert.ok(approval);
  await backend.approve(approval.id,'accept');await until(()=>!store.busy);
  assert.equal(store.state.run?.state,'completed',store.state.error||'');
  assert.equal(store.state.inputs.length,0);
  assert.ok(store.state.items.some(i=>i.diff?.after==='Native 提出的候选正文。'&&!i.diff.applied));
  await start('改一下。CASE:ambiguous');await until(()=>store.state.inputs.length>0||!store.busy);
  assert.equal(store.state.inputs.length,1);const stale=store.state.inputs[0];
  await backend.cancel(session.id);await until(()=>!store.busy);assert.equal(store.state.inputs.length,0);
  await assert.rejects(backend.answer(stale.id,{literary_scope:'整章修订原稿'}),/过期/);
  const page=await backend.readLibrary({topic:'chapters',query:'原稿',offset:0});
  assert.match((await backend.readEntry('chapters',page.entries[0].key)).body,/作者已采纳的原稿。/);
  const history=await backend.read(session.id);
  assert.ok(!history.some(item=>item.kind==='assistant'&&item.text.includes('LG_TASK_ROUTER_V1')));
});


test('TASK MATRIX Native: planning, exact selection, older-candidate review and separate author adoption',{
  timeout:240_000,skip:!process.env.LG_TEST_PYTHON},async t=>{
  const workspace=await mkdtemp(join(tmpdir(),'lg-matrix-Native-'));
  const settings:LiteraryBackendSettings={workspace,python:process.env.LG_TEST_PYTHON!,manifest:'',
    env:{...process.env,LG_TEST_TASK_MATRIX:'1',LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')}};
  const backend=new NativeBackend(settings),store=new SessionStore('codex');
  backend.subscribe(event=>store.apply(event));
  t.after(async()=>{t.diagnostic(JSON.stringify({workspace,error:store.state.error,run:store.state.run}));await backend.dispose();});
  await backend.connect();const session=await backend.create();
  // Read actual SQLite state and actual reports; the provider never writes reports itself.
  async function inspect(){
    const result=await promisify(execFile)(settings.python,['-c',String.raw`
import json,sys
from pathlib import Path
from dataclasses import asdict
from lg_cli.project_store import ProjectStore
from lg_cli.book_analysis import BookAnalysisStore,fingerprint
from lg_cli.session_runtime import SessionRuntime
root=Path(sys.argv[1]); store=ProjectStore(root); records=BookAnalysisStore(store)
doc=store.get_document('one')
runtime=SessionRuntime(root,sys.argv[2]).read()
checkpoint=root/'.literarygiant'/'runtime'/'native'/(sys.argv[2]+'.json')
native=json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
reports={str(path.relative_to(records.root)):json.loads(path.read_text()) for path in sorted(records.root.rglob('*.json'))} if records.root.exists() else {}
print(json.dumps({'documents':[asdict(d) for d in store.list_documents(limit=1000)],'document':asdict(doc),'versions':[asdict(v) for v in store.list_versions('one')],
 'other':asdict(store.get_document('two')),'canonical':[asdict(f) for f in store.list_facts(state='canonical',limit=None)],
 'reports':reports,'plans':[p.read_text() for p in sorted((root/'ReferenceLibrary'/'plans').rglob('*.md'))],
 'candidateFingerprint':fingerprint(records.candidate_snapshot('one',store.get_version('one',2))) if len(store.list_versions('one'))>1 else None,
 'task':runtime.get('task'),'engine':runtime.get('engine'),'thread':runtime.get('thread_id'),
 'nativeCheckpoint':{'schema':native.get('schema'),'thread':native.get('thread')},
 'coreHistory':any((root/'.literarygiant'/'codex-home'/'sessions').rglob('*.jsonl'))},ensure_ascii=False))
`,workspace,session.id],{env:settings.env});
    return JSON.parse(result.stdout);
  }
  async function start(text:string,stage?:Stage){
    const previous=store.state.run?.id;
    await backend.run(session.id,text,stage);
    await until(()=>!!store.state.run&&store.state.run.id!==previous);
  }
  async function approval(){
    await until(()=>store.state.approvals.some(a=>a.state==='pending')||!store.busy);
    const value=store.state.approvals.find(a=>a.state==='pending');
    assert.ok(value,'Expected an actual host-owned approval: '+JSON.stringify(store.state.items));
    return value;
  }
  async function finish(){
    await until(()=>!store.busy);
    assert.equal(store.state.run?.state,'completed',store.state.error||JSON.stringify(store.state.items));
  }
  async function approved(text:string,stage?:Stage){
    await start(text,stage);const value=await approval();await backend.approve(value.id,'accept');await finish();
    return value.id;
  }
  const initial=await inspect(),original='前文保持不动。\n\n待修改片段。\n\n后文保持不动。';
  assert.equal(initial.document.content,original);
  assert.equal(initial.canonical.length,1);
  const unchanged=(state:any)=>{
    assert.deepEqual(state.canonical,initial.canonical,'Model operations cannot change any Canonical field');
    assert.deepEqual(state.other,initial.other,'Unselected chapter must remain byte-for-byte unchanged');
  };
  await approved('MATRIX:plan 只做全书规划，不写正文。','plan');
  const planned=await inspect();unchanged(planned);
  assert.equal(planned.engine,'native');assert.ok(planned.thread);
  assert.equal(planned.nativeCheckpoint.schema,'lg.native-runtime.v1');
  assert.equal(planned.nativeCheckpoint.thread,planned.thread);assert.equal(planned.coreHistory,false);
  assert.ok(planned.plans.some((content:string)=>content.includes('MATRIX_PLAN')));
  assert.deepEqual(planned.document,initial.document);assert.deepEqual(planned.versions,initial.versions);
  const candidateA=original.replace('待修改片段。','第一次选区候选。');
  const candidateB=original.replace('待修改片段。','第二次选区候选。');
  await approved('把 one 中“待修改片段。”改成第一版，只改此选区。MATRIX:revision-a');
  let state=await inspect();unchanged(state);
  assert.deepEqual(state.document,initial.document);
  assert.equal(state.versions.length,2);
  assert.equal(state.versions.find((v:any)=>v.version_number===2).content,candidateA);
  assert.equal(state.task.definition.kind,'revision');
  assert.equal(state.task.definition.sourceVersion,initial.document.active_version_id);
  assert.equal(state.task.definition.passage,'待修改片段。');
  await approved('把 one 中“待修改片段。”改成不同的第二版，只改此选区。MATRIX:revision-b');
  state=await inspect();unchanged(state);
  assert.deepEqual(state.document,initial.document);
  assert.equal(state.versions.length,3);
  const v2=state.versions.find((v:any)=>v.version_number===2),v3=state.versions.find((v:any)=>v.version_number===3);
  assert.equal(v2.content,candidateA);assert.equal(v3.content,candidateB);assert.notEqual(v2.id,v3.id);
  for(const version of [v2,v3]){
    assert.equal(version.state,'candidate');assert.equal(version.metadata.base_version_id,initial.document.active_version_id);
  }
  assert.ok(store.state.items.some(item=>item.diff?.before===original&&item.diff.after===candidateA&&!item.diff.applied));
  async function prepare(action:'review'|'adopt'){
    const detail=await backend.readEntry('pending','candidate:one:2');
    return {detail,prompt:await backend.prepareWriting('pending',detail.key,action,detail.fingerprint)};
  }
  const beforeReview=await inspect();
  await approved((await prepare('review')).prompt);
  const reviewed=await inspect();unchanged(reviewed);
  assert.deepEqual(reviewed.document,initial.document);assert.deepEqual(reviewed.versions,beforeReview.versions);
  assert.deepEqual(reviewed.documents,beforeReview.documents);
  assert.equal(reviewed.task.definition.kind,'review');assert.equal(reviewed.task.definition.versionNumber,2);
  assert.equal(reviewed.task.definition.sourceVersion,initial.document.active_version_id);
  assert.notEqual(reviewed.task.definition.allowRevision,true);
  const reviewItems=store.state.items.filter(item=>item.runId===store.state.run?.id);
  assert.ok(reviewItems.some(item=>item.kind==='tool'&&item.state==='failed'),'Review scope must reject attempted prose');
  assert.equal(reviewItems.filter(item=>item.diff).length,0);
  const reviewApprovals=store.state.approvals.filter(value=>value.runId===store.state.run?.id);
  assert.equal(reviewApprovals.length,1);assert.equal(JSON.parse(reviewApprovals[0].details).tool,'studio_review');
  const reportEntries=Object.entries(reviewed.reports) as [string,any][];
  assert.equal(reportEntries.length,5);
  for(const [path,report] of reportEntries){
    assert.ok(path.startsWith(`candidates/${v2.id}/`),path);
    assert.equal(report.fingerprint,reviewed.candidateFingerprint);
    assert.ok(report.contract_fingerprint);
    assert.match(report.report.summary,/MATRIX_REVIEW_V2/);
    assert.ok(report.report.entries.length>0);
    for(const entry of report.report.entries){
      assert.ok(entry.evidence.length>0);
      assert.ok(entry.evidence.every((e:any)=>e.version_id===v2.id&&candidateA.includes(e.quote)));
    }
  }
  assert.ok(!reportEntries.some(([path])=>path.startsWith(`candidates/${v3.id}/`)));
  const source=JSON.parse(await readFile(join(workspace,'matrix-review-source.json'),'utf8'));
  assert.equal(source.snapshot.documents[0].version_id,v2.id);
  assert.equal(source.snapshot.documents[0].content,candidateA);
  assert.equal(source.sourceFingerprint,reviewed.candidateFingerprint);
  // Adoption is a fresh author action, distinct from saving a review.
  await start((await prepare('adopt')).prompt);
  const declined=await approval();assert.equal(declined.title,'创作采用待审批');
  assert.match(declined.details,/studio_accept/);
  const adoptionPreview=JSON.parse(declined.details);
  assert.equal(adoptionPreview.arguments.slug,'one');assert.equal(adoptionPreview.arguments.version_number,2);
  assert.equal(adoptionPreview.arguments.expected_version_id,initial.document.active_version_id);
  await backend.approve(declined.id,'decline');await finish();
  await assert.rejects(backend.approve(declined.id,'accept'),/过期|已回答/);
  state=await inspect();unchanged(state);assert.deepEqual(state.document,initial.document);
  assert.deepEqual(state.versions,reviewed.versions);assert.deepEqual(state.reports,reviewed.reports);
  assert.deepEqual(state.documents,reviewed.documents);
  const staleContext=await prepare('adopt');await start(staleContext.prompt);
  const stale=await approval();
  // This is an independent, explicit author edit, not an Agent-side mutation.
  await promisify(execFile)(settings.python,['-c',String.raw`
import sys
from pathlib import Path
from lg_cli.project_store import ProjectStore
ProjectStore(Path(sys.argv[1])).create_document(kind='note',slug='author-concurrent-note',title='作者并发备注',content='作者在审批等待期间添加的备注。',state='accepted')
`,workspace],{env:settings.env});
  const afterAuthor=await inspect();
  assert.equal(afterAuthor.documents.length,reviewed.documents.length+1);
  assert.equal(afterAuthor.documents.find((doc:any)=>doc.slug==='author-concurrent-note').content,'作者在审批等待期间添加的备注。');
  await backend.approve(stale.id,'accept');await finish();
  const afterStale=await inspect();unchanged(afterStale);
  assert.deepEqual(afterStale.document,afterAuthor.document);assert.deepEqual(afterStale.versions,afterAuthor.versions);
  assert.deepEqual(afterStale.reports,afterAuthor.reports);assert.deepEqual(afterStale.documents,afterAuthor.documents);
  assert.ok(store.state.items.some(item=>item.runId===store.state.run?.id&&item.kind==='tool'&&item.state==='failed'));
  await assert.rejects(backend.approve(stale.id,'accept'),/过期|已回答/);
  await assert.rejects(backend.prepareWriting('pending',staleContext.detail.key,'adopt',staleContext.detail.fingerprint));
  const acceptedId=await approved((await prepare('adopt')).prompt);
  const accepted=await inspect();unchanged(accepted);
  assert.equal(accepted.document.active_version_id,v2.id);assert.equal(accepted.document.content,candidateA);
  assert.equal(accepted.versions.length,3);assert.equal(accepted.versions.find((v:any)=>v.id===v3.id).state,'candidate');
  const published=(Object.entries(accepted.reports) as [string,any][]).filter(([path])=>!path.startsWith('candidates/'));
  assert.equal(published.length,5);
  for(const [,report] of published){
    assert.equal(report.fingerprint,accepted.candidateFingerprint);
    assert.ok(report.report.entries.every((entry:any)=>entry.evidence.every((e:any)=>e.version_id===v2.id)));
  }
  await assert.rejects(backend.approve(acceptedId,'accept'),/过期|已回答/);
  assert.deepEqual(await inspect(),accepted,'Duplicate approval response must have no additional effect');
});
