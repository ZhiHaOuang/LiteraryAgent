// Product history uses the LG snapshot contract; Core pagination is service-owned.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import {LiteraryBackend} from '../src/backend/LiteraryBackend';
import {SessionStore} from '../src/host/SessionStore';
class HistoryBackend extends LiteraryBackend {
 constructor(){super({python:process.execPath,manifest:'',workspace:process.cwd()});}
 protected override launch(){return {command:process.execPath,args:[resolve('tests/fixtures/history-pages-server.cjs')],cwd:process.cwd(),jsonRpc:true};}
}
test('listing, reading and reopening paged history are stable and never start a task',{timeout:10000},async()=>{
 const backend=new HistoryBackend(),store=new SessionStore('codex');backend.subscribe(e=>store.apply(e));
 try{
  await backend.connect();assert.deepEqual((await backend.list()).map(s=>s.id),['thread']);
  const first=await backend.read('thread');assert.deepEqual(await backend.read('thread'),first);
  await backend.resume('thread');assert.deepEqual(store.state.items,first);
  assert.equal(store.state.run,null);assert.equal(store.state.error,null);assert.equal(store.state.approvals.length,0);
 }finally{await backend.dispose();}
});
// Corrupt/repeated/incomplete pages, cursor races and older LG services are
// exercised in literary-history.test.ts at this same production boundary.
