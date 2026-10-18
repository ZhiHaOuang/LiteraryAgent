import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp} from 'node:fs/promises';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {LiteraryBackend,type LiteraryBackendSettings} from '../src/backend/LiteraryBackend';
import {SessionStore} from '../src/host/SessionStore';

class LargeHistoryBackend extends LiteraryBackend{
  constructor(private fixture:LiteraryBackendSettings){super(fixture);}
  protected override launch(){return {command:this.fixture.python,args:[resolve('tests/fixtures/large-history-backend.py'),this.fixture.workspace],
    cwd:this.fixture.workspace,env:this.fixture.env,jsonRpc:true};}
}
test('actual Python service restores a single history item exceeding 4 MiB through LG connector',{
  timeout:60_000,skip:!process.env.LG_TEST_PYTHON},async t=>{
  const workspace=await mkdtemp(join(tmpdir(),'lg-large-history-'));
  const backend=new LargeHistoryBackend({workspace,python:process.env.LG_TEST_PYTHON!,manifest:'/fixture-only',
    env:{...process.env,LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')}});
  const store=new SessionStore('codex');backend.subscribe(e=>store.apply(e));
  t.after(async()=>{await backend.dispose();t.diagnostic(workspace);});
  await backend.connect();const sessions=await backend.list();assert.equal(sessions.length,1);
  await backend.resume(sessions[0].id);
  assert.equal(store.state.items.length,1);
  assert.equal(store.state.items[0].text,'中文正文。🖋\n"原稿"'.repeat(160000));
  assert.equal(store.state.run,null);assert.equal(store.state.approvals.length,0);
  assert.equal(store.state.connection,'ready');
});
