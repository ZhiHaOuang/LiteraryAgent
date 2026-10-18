import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile } from 'node:fs/promises';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import { resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { LiteraryBackend, type LiteraryBackendSettings } from '../src/backend/LiteraryBackend';
import { SessionStore } from '../src/host/SessionStore';
import type { BackendEvent, Stage } from '../shared/protocol';

class FixtureBackend extends LiteraryBackend {
  constructor(private fixture:LiteraryBackendSettings){super(fixture);}
  protected override launch(){
    return {command:this.fixture.python,args:[resolve('tests/fixtures/app_server_fixture.py'),
      '--workspace',this.fixture.workspace,'--runtime-manifest',this.fixture.manifest,'--literary-backend'],
      cwd:this.fixture.workspace,env:this.fixture.env,jsonRpc:true};
  }
}
async function until(predicate:()=>boolean,ms=60_000){
  const start=Date.now();
  while(!predicate()){
    if(Date.now()-start>ms)throw Error('Timed out waiting for LG event');
    await new Promise(r=>setTimeout(r,25));
  }
}
test('LG connector through real pinned runtime: approval, candidates, diff, cancel and history',{
  timeout:240_000,skip:!process.env.LG_TEST_RUNTIME_MANIFEST||!process.env.LG_TEST_PYTHON},async t=>{
  const workspace=await mkdtemp(join(tmpdir(),'lg-unified-extension-'));
  const settings:LiteraryBackendSettings={workspace,python:process.env.LG_TEST_PYTHON!,manifest:process.env.LG_TEST_RUNTIME_MANIFEST!,
    env:{...process.env,LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')}};
  let backend=new FixtureBackend(settings);
  const store=new SessionStore('codex'),events:BackendEvent[]=[];
  const subscribe=()=>backend.subscribe(e=>{events.push(e);store.apply(e);});subscribe();
  t.after(async()=>{
    t.diagnostic(JSON.stringify({workspace,approvals:events.filter(e=>e.type==='approval.requested').length,
      terminalStates:events.filter(e=>e.type==='run.finished').map(e=>e.status)}));
    await backend.dispose();
  });
  await backend.connect();const session=await backend.create();
  async function turn(text:string,decision?:'accept'|'decline'){
    const previous=store.state.run?.id;
    await backend.run(session.id,text);
    await until(()=>!!store.state.run&&store.state.run.id!==previous);
    if(decision){
      await until(()=>store.state.approvals.some(a=>a.state==='pending')||!store.busy);
      const approval=store.state.approvals.find(a=>a.state==='pending');
      assert.ok(approval,'Expected host-owned LG approval');
      await backend.approve(approval.id,decision);
      await assert.rejects(backend.approve(approval.id,decision),/过期|已回答/);
    }
    await until(()=>!store.busy);
    assert.equal(store.state.run?.state,'completed',JSON.stringify(events.filter(e=>e.type==='error'||e.type==='run.finished')));
  }
  await turn('CASE:plan','accept');
  await turn('CASE:denied','decline');
  await assert.rejects(readFile(join(workspace,'ReferenceLibrary/bible/book-overview.json')));
  await turn('CASE:accepted','accept');
  await backend.refreshBook();assert.match(store.state.book.overview,/浮城/);
  await turn('CASE:reference','accept');
  const page=await backend.readLibrary({topic:'characters',query:'林砚',offset:0});
  const detail=await backend.readEntry('characters',page.entries[0].key);
  assert.match(detail.body,/隔离电源/);
  const before=store.state.run?.id;
  assert.match(await backend.prepareWriting('characters',detail.key,'generate',detail.fingerprint),/studio_reference/);
  assert.equal(store.state.run?.id,before);
  await turn('采用 lin v1。CASE:adoptref','accept');
  await assert.rejects(backend.prepareWriting('characters',detail.key,'generate',detail.fingerprint));
  await turn('整章修订原稿。CASE:edit','accept');
  assert.ok(store.state.items.some(i=>i.diff?.before==='作者已采纳的原稿。'&&i.diff.after==='Agent 提出的改稿候选。'&&!i.diff.applied));
  const chapters=await backend.readLibrary({topic:'chapters',query:'原稿',offset:0});
  assert.match((await backend.readEntry('chapters',chapters.entries[0].key)).body,/作者已采纳的原稿。/);
  await turn('恢复原稿 v1。CASE:restore','accept');
  await turn('CASE:text');assert.ok(events.some(e=>e.type==='assistant.delta'));
  assert.ok(events.filter(e=>e.type==='item.updated').every(e=>!JSON.stringify(e).includes('reasoning')));
  await backend.run(session.id,'CASE:slow');
  await until(()=>store.busy);await backend.cancel(session.id);
  await until(()=>!store.busy);assert.equal(store.state.run?.state,'cancelled');
  // A second turn must survive replacement of the cancelled runtime.
  await turn('CASE:text');
  const history=await backend.read(session.id);
  assert.ok(history.some(i=>i.diff?.after==='Agent 提出的改稿候选。'));
  const authors=history.filter(i=>i.kind==='user');
  assert.equal(authors.length,new Set(authors.map(i=>i.runId)).size);
  await backend.dispose();backend=new FixtureBackend(settings);subscribe();
  await backend.connect();assert.ok((await backend.list()).some(s=>s.id===session.id));
  await backend.resume(session.id);
  assert.deepEqual(store.state.items,history);assert.equal(store.state.run,null);
  const pending=await backend.readLibrary({topic:'pending',query:'',offset:0});
  const previous=store.snapshot().run?.id;
  await backend.run(session.id,'CASE:scopeedit','plan');
  await until(()=>!!store.state.run&&store.state.run.id!==previous&&!store.busy);
  assert.equal(store.snapshot().run?.state,'completed');
  assert.equal(store.state.approvals.filter(a=>a.state==='pending').length,0);
  const scopedItems=store.state.items.filter(item=>item.runId===store.state.run?.id);
  assert.ok(scopedItems.some(item=>item.kind==='tool'&&item.state==='failed'));
  assert.ok(!scopedItems.some(item=>item.diff));
  assert.equal((await backend.readLibrary({topic:'pending',query:'',offset:0})).total,pending.total);
  const fresh=await backend.readEntry('characters',detail.key);
  await turn(await backend.prepareWriting('characters',fresh.key,'generate',fresh.fingerprint),'accept');
  const candidates=await backend.readLibrary({topic:'pending',query:'林砚',offset:0});
  const candidate=candidates.entries.find(value=>value.key.startsWith('candidate:lin:'))!;
  assert.ok(candidate);
  const candidateDetail=await backend.readEntry('pending',candidate.key);
  await turn(await backend.prepareWriting('pending',candidate.key,'adopt',candidateDetail.fingerprint),'accept');
  assert.match((await backend.readEntry('characters',detail.key)).body,/更新候选：林砚/);
});


test('TASK MATRIX Codex: planning, exact selection, older-candidate review and separate author adoption',{
  timeout:240_000,skip:!process.env.LG_TEST_RUNTIME_MANIFEST||!process.env.LG_TEST_PYTHON},async t=>{
  const workspace=await mkdtemp(join(tmpdir(),'lg-matrix-Codex-'));
  const settings:LiteraryBackendSettings={workspace,python:process.env.LG_TEST_PYTHON!,manifest:process.env.LG_TEST_RUNTIME_MANIFEST!,
    env:{...process.env,LG_TEST_TASK_MATRIX:'1',LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')}};
  const backend=new FixtureBackend(settings),store=new SessionStore('codex');
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
  assert.equal(planned.engine,'codex');assert.ok(planned.thread);
  assert.equal(planned.coreHistory,true);assert.equal(planned.nativeCheckpoint.schema,null);
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
