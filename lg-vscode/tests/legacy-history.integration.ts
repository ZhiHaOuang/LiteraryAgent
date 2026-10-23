import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {LiteraryBackend,type LiteraryBackendSettings} from '../src/backend/LiteraryBackend';
import {SessionStore} from '../src/host/SessionStore';

class LegacyBackend extends LiteraryBackend{
  constructor(fixture:LiteraryBackendSettings){super(fixture);}
  protected override launch(){
    const spec=super.launch();
    return {...spec,args:[resolve('tests/fixtures/legacy_backend_fixture.py'),...spec.args.slice(2)]};
  }
}
async function until(predicate:()=>boolean){
  const end=Date.now()+30_000;
  while(!predicate()){
    if(Date.now()>end)throw Error('History continuation timeout');
    await new Promise(r=>setTimeout(r,20));
  }
}
for(const engine of ['native','codex'] as const)test('LG connector imports legacy dialogue and continues with '+engine+' after reconnect',{
  timeout:120_000,skip:!process.env.LG_TEST_PYTHON||(engine==='codex'&&!process.env.LG_TEST_RUNTIME_MANIFEST)},async t=>{
  const workspace=await mkdtemp(join(tmpdir(),'lg-legacy-extension-'));
  const settings:LiteraryBackendSettings={workspace,python:process.env.LG_TEST_PYTHON!,
    manifest:engine==='codex'?process.env.LG_TEST_RUNTIME_MANIFEST!:'',runtimeEngine:engine,
    env:{...process.env,LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')}};
  let backend=new LegacyBackend(settings);
  const store=new SessionStore('codex');
  const subscribe=()=>backend.subscribe(event=>store.apply(event));subscribe();
  t.after(async()=>{await backend.dispose();t.diagnostic(workspace);});
  await backend.connect();
  const old=(await backend.list())[0];assert.ok(old.id.startsWith('legacy-'));
  const preview=await backend.read(old.id);
  assert.deepEqual(preview.map(item=>item.kind),['user','notice','assistant']);
  assert.ok(preview[2].text.length>900_000);assert.doesNotMatch(JSON.stringify(preview),/PRIVATE_/);
  await backend.resume(old.id);
  const selected=store.state.sessionId!;assert.ok(selected.startsWith('imported-'));
  const metadata=JSON.parse(execFileSync(settings.python,['-c',
    'import json,sys; from pathlib import Path; from lg_cli.session_runtime import SessionRuntime; print(json.dumps(SessionRuntime(Path(sys.argv[1]),sys.argv[2]).read()))',
    workspace,selected],{env:settings.env,encoding:'utf8'}));
  assert.equal(metadata.mode,'ask');assert.equal(metadata.creative_mode,'ask');assert.equal(metadata.thread_id,null);
  assert.deepEqual(store.state.items,preview);assert.equal(store.busy,false);assert.equal(store.state.approvals.length,0);
  const sessions=await backend.list();assert.equal(sessions.length,1);assert.equal(sessions[0].id,selected);
  const before=store.state.run?.id;
  await backend.run(selected,'继续讨论规划');
  await until(()=>!!store.state.run&&store.state.run.id!==before&&!store.busy);
  assert.equal(store.state.run?.state,'completed');
  assert.equal(store.state.items.at(-1)?.text,engine==='codex'?'Codex 接续回复。':'Native 接续回复。');
  const history=await backend.read(selected);
  await backend.dispose();backend=new LegacyBackend(settings);subscribe();
  await backend.connect();await backend.resume(selected);assert.deepEqual(store.state.items,history);
  assert.equal(store.state.approvals.length,0);assert.equal((await backend.list()).length,1);
  const page=await backend.readLibrary({topic:'chapters',query:'原稿',offset:0});
  assert.match((await backend.readEntry('chapters',page.entries[0].key)).body,/作者的正式正文。/);
});
