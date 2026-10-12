import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MockBackend } from '../src/backend/MockBackend';
import { SessionStore } from '../src/host/SessionStore';
const until=async (condition:()=>boolean)=>{ const end=Date.now()+3000;while(!condition()){if(Date.now()>end)throw Error('timeout');await new Promise(r=>setTimeout(r,5));} };
test('mock rejects duplicate approvals and can restore finished history',async()=>{
  const backend=new MockBackend(0),store=new SessionStore('mock');backend.subscribe(e=>store.apply(e));
  try{await backend.connect();const session=await backend.create();await backend.run(session.id,'hello');
    await until(()=>store.state.approvals.length>0);const request=store.state.approvals[0];
    await backend.approve(request.id,'decline');await assert.rejects(backend.approve(request.id,'accept'));
    await until(()=>store.state.run?.state==='completed');assert.equal(store.state.items.at(-1)?.state,'cancelled');
    const before=store.state.items.map(i=>i.text);await backend.resume(session.id);assert.deepEqual(store.state.items.map(i=>i.text),before);
  }finally{await backend.dispose();}
});
test('mock cancellation while awaiting approval never applies tool',async()=>{
  const backend=new MockBackend(0),store=new SessionStore('mock');backend.subscribe(e=>store.apply(e));
  try{await backend.connect();const s=await backend.create();await backend.run(s.id,'work');await until(()=>store.state.approvals.length>0);
    await backend.cancel(s.id);await until(()=>store.state.run?.state==='cancelled');assert.equal(store.state.approvals[0].state,'expired');
    assert.equal(store.state.items.some(i=>i.kind==='tool'&&i.state==='completed'),false);
  }finally{await backend.dispose();}
});

test('mock preserves the visible partial reply after cancellation and history restoration',async()=>{
  const backend=new MockBackend(10),store=new SessionStore('mock');backend.subscribe(e=>store.apply(e));
  try {
    await backend.connect();const session=await backend.create();await backend.run(session.id,'/slow');
    await until(()=>store.state.items.some(i=>i.kind==='assistant'&&i.text.length>0));
    const visible=store.state.items.find(i=>i.kind==='assistant')!.text;
    await backend.cancel(session.id);await until(()=>store.state.run?.state==='cancelled');
    const final=store.snapshot().items;
    assert.equal(final.find(i=>i.kind==='assistant')?.text,visible);
    assert.equal(final.find(i=>i.kind==='assistant')?.state,'cancelled');
    assert.equal(final.some(i=>i.kind==='tool'),false);
    await backend.create();await backend.resume(session.id);
    assert.deepEqual(store.state.items,final);
    assert.equal(store.state.run,null);
    assert.equal(store.state.approvals.length,0);
  } finally {await backend.dispose();}
});
