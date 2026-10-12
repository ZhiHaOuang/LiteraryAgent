import {test} from 'node:test';
import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import {CodexBackend} from '../src/backend/CodexBackend';
import {SessionStore} from '../src/host/SessionStore';

class HistoryBackend extends CodexBackend {
  constructor(private fixtureFlag='') {super({python:process.execPath,manifest:'/unused-fixture',workspace:process.cwd()});}
  protected launch(){return {command:process.execPath,args:[resolve('tests/fixtures/resolved-server.cjs'),'--paginated',this.fixtureFlag],cwd:process.cwd()};}
}
test('paginated history restores every page once without starting a task',{timeout:10000},async()=>{
  const backend=new HistoryBackend(),store=new SessionStore('codex');
  backend.subscribe(e=>store.apply(e));
  try {
    await backend.connect();
    assert.deepEqual((await backend.list()).map(s=>s.id),['old','new']);
    assert.deepEqual((await backend.read('thread')).map(i=>i.text),['saved answer 1','saved answer 2']);
    await backend.resume('thread');
    assert.deepEqual(store.state.items.map(i=>i.text),['saved answer 1','saved answer 2']);
    assert.equal(store.state.run,null);assert.equal(store.state.error,null);
    assert.equal(store.state.approvals.length,0);
  } finally {await backend.dispose();}
});
for(const [flag,message]of [['--repeat-cursor',/重复返回历史游标/],['--summary',/完整历史内容/]] as const){
  test('incomplete history fails explicitly: '+flag,{timeout:10000},async()=>{
    const backend=new HistoryBackend(flag);
    try {await backend.connect();await assert.rejects(backend.read('thread'),message);}
    finally {await backend.dispose();}
  });
}
