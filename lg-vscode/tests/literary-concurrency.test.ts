import {test} from 'node:test';
import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import {LiteraryBackend} from '../src/backend/LiteraryBackend';
import {SessionStore} from '../src/host/SessionStore';

class ConcurrentBackend extends LiteraryBackend{
  constructor(private flag=''){super({python:process.execPath,manifest:'',workspace:process.cwd()});}
  protected override launch(){return {command:process.execPath,args:[resolve('tests/fixtures/concurrent-server.cjs'),this.flag],cwd:process.cwd(),jsonRpc:true};}
}
async function until(predicate:()=>boolean){
  const deadline=Date.now()+5000;
  while(!predicate()){
    if(Date.now()>deadline)throw Error('Concurrent fixture timeout');
    await new Promise(resolve=>setTimeout(resolve,10));
  }
}
test('LG connector keeps workflow ownership and matching approval after agent finishes',async()=>{
  const backend=new ConcurrentBackend(),store=new SessionStore('codex');backend.subscribe(event=>store.apply(event));
  try{
    await backend.connect();await backend.create();await backend.run('thread','work');
    await until(()=>store.state.run?.id==='workflow'&&store.state.approvals.length===1);
    assert.equal(store.busy,true);assert.equal(store.state.approvals[0].state,'pending');
    await assert.rejects(backend.run('thread','another run'),/停止/);
    await backend.approve('pw','accept');
    await until(()=>!store.busy);
    assert.equal(store.state.approvals[0].state,'resolved');
  }finally{await backend.dispose();}
});
test('LG restored active runs block new mutation after latest run completes',async()=>{
  const backend=new ConcurrentBackend('restore'),store=new SessionStore('codex');backend.subscribe(event=>store.apply(event));
  try{
    await backend.connect();await backend.resume('thread');
    await until(()=>store.state.run?.id==='workflow');
    assert.equal(store.busy,true);
    await assert.rejects(backend.create(),/停止/);
  }finally{await backend.dispose();}
});
