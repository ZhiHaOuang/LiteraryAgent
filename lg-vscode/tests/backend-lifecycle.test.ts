import {test} from 'node:test';
import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import {CodexBackend} from '../src/backend/CodexBackend';
import {RpcTransport} from '../src/backend/RpcTransport';
import {SessionStore} from '../src/host/SessionStore';
import type {BackendEvent} from '../shared/protocol';

class FixtureBackend extends CodexBackend {
  protected launch() { return {command:process.execPath,args:[resolve('tests/fixtures/resolved-server.cjs')],cwd:process.cwd()}; }
}

test('server resolution expires only matching requests and blocks late approval/input replies', {timeout:10000}, async()=>{
  const backend = new FixtureBackend({python:process.execPath,manifest:'/unused-fixture',workspace:process.cwd()});
  const store = new SessionStore('codex');
  const events:BackendEvent[]=[];
  let finish!:()=>void;
  const resolved = new Promise<void>(r=>{finish=r;});
  backend.subscribe(event=>{
    events.push(event);store.apply(event);
    if(event.type==='userInput.resolved')finish();
  });
  try {
    await backend.connect();await backend.create();await backend.run('thread','test');await resolved;
    assert.equal(store.state.approvals.length,1);
    assert.equal(store.state.approvals[0].state,'expired');
    assert.equal(store.state.inputs.length,0);
    assert.equal(store.state.run?.state,'running');
    assert.equal(events.filter(e=>e.type==='approval.expired').length,1);
    const input=events.find(e=>e.type==='userInput.requested');
    assert.ok(input?.type==='userInput.requested');
    await assert.rejects(backend.approve(store.state.approvals[0].id,'accept'),/过期/);
    await assert.rejects(backend.answer(input.request.id,{q:'yes'}),/过期/);
    await backend.list(); // Flush the peer before checking for unexpected replies.
    assert.equal(events.filter(e=>e.type==='error').length,0);
  } finally {await backend.dispose();}
});

test('expired transport request cannot emit a response',{timeout:10000},async()=>{
  let received!:()=>void;
  const request=new Promise<void>(resolve=>{received=resolve;});
  const transport=new RpcTransport({command:process.execPath,args:[resolve('tests/fixtures/resolved-server.cjs')],cwd:process.cwd()},message=>{
    if(message.id===0&&message.method)received();
  },()=>{});
  try {
    transport.start();
    await transport.request('turn/start',{});await request;
    transport.expire(0);
    await assert.rejects(transport.respond(0,{decision:'accept'}),/过期/);
    // String and numeric request IDs remain distinct.
    await transport.respond('0',{answers:{q:{answers:['yes']}}});
  } finally {await transport.dispose();}
});
