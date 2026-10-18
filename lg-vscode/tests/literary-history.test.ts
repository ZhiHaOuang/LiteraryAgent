import {test} from 'node:test';
import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import {LiteraryBackend} from '../src/backend/LiteraryBackend';
import {SessionStore} from '../src/host/SessionStore';

class HistoryBackend extends LiteraryBackend{
  constructor(private flag=''){super({python:process.execPath,manifest:'/fixture-only',workspace:process.cwd()});}
  protected override launch(){return {command:process.execPath,args:[resolve('tests/fixtures/history-pages-server.cjs'),this.flag],cwd:process.cwd(),jsonRpc:true};}
}
test('LG pages restore full Unicode history without starting a task',async()=>{
  const backend=new HistoryBackend(),store=new SessionStore('codex');backend.subscribe(e=>store.apply(e));
  try{
    await backend.connect();await backend.resume('thread');
    assert.equal(store.state.items[0].text,'中文🖋历史。'.repeat(50000));
    assert.equal(store.state.run,null);assert.equal(store.state.approvals.length,0);
  }finally{await backend.dispose();}
});
test('older LG service without page capability remains compatible',async()=>{
  const backend=new HistoryBackend('legacy');
  try{await backend.connect();assert.equal((await backend.read('thread'))[0].text,'保存的旧接口回复');}
  finally{await backend.dispose();}
});
for(const flag of ['repeat','token','cursor','missing','encoding','corrupt','expired']){
  test('malformed or expired history fails explicitly: '+flag,async()=>{
    const backend=new HistoryBackend(flag),store=new SessionStore('codex');backend.subscribe(e=>store.apply(e));
    try{
      await backend.connect();await assert.rejects(backend.read('thread'));
      assert.equal(store.state.items.length,0,'Incomplete pages must not become a history snapshot');
    }finally{await backend.dispose();}
  });
}
test('events arriving after frozen cursor survive snapshot publication',async()=>{
  const backend=new HistoryBackend('racing'),store=new SessionStore('codex');backend.subscribe(e=>store.apply(e));
  try{
    await backend.connect();await backend.resume('thread');
    assert.equal(store.state.items[0].text,'中文🖋历史。'.repeat(50000));
    assert.equal(store.state.run?.id,'live-run');
    assert.equal(store.state.items.find(i=>i.id==='live-answer')?.text,'后续内容');
  }finally{await backend.dispose();}
});
