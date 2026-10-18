import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SessionStore } from '../src/host/SessionStore';
import { ClientMessageSchema } from '../shared/protocol';

test('interleaved items replace final text and ignore late deltas', () => {
  const store = new SessionStore('mock');
  store.apply({ type: 'session.snapshot', sessionId: 's', items: [] });
  store.apply({ type: 'run.started', sessionId: 's', runId: 'r', text: 'start' });
  for (const [itemId, delta] of [['a','你'],['b','第二条'],['a','好']]) store.apply({ type: 'assistant.delta', sessionId:'s', runId:'r', itemId, delta });
  store.apply({ type:'item.updated', sessionId:'s', item:{ id:'a',runId:'r',kind:'assistant',title:'LG',text:'你好！',state:'completed' } });
  store.apply({ type:'assistant.delta',sessionId:'s',runId:'r',itemId:'a',delta:'重复' });
  assert.deepEqual(store.state.items.filter(i=>i.kind==='assistant').map(i=>i.text), ['你好！','第二条']);
});
test('disconnect expires approval and preserves snapshot without success', () => {
  const store=new SessionStore('mock');
  store.apply({type:'session.snapshot',sessionId:'s',items:[]});
  store.apply({type:'run.started',sessionId:'s',runId:'r',text:'work'});
  store.apply({type:'approval.requested',sessionId:'s',approval:{id:'0',runId:'r',title:'write',details:'preview',choices:['accept','decline'],state:'pending'}});
  const rebuilt=store.snapshot(); assert.equal(rebuilt.approvals[0].state,'pending');
  rebuilt.approvals[0].state='resolved'; assert.equal(store.state.approvals[0].state,'pending');
  store.apply({type:'backend.status',status:'disconnected'});
  assert.equal(store.state.approvals[0].state,'expired'); assert.equal(store.state.run?.state,'interrupted');
});
test('cancel waits for backend and unrelated terminal events do not finish current run', () => {
  const store=new SessionStore('mock');store.apply({type:'session.snapshot',sessionId:'s',items:[]});
  store.apply({type:'run.started',sessionId:'s',runId:'r',text:'x'});
  store.state.run!.state='cancelling';
  store.apply({type:'run.finished',sessionId:'other',runId:'r',status:'completed'});
  assert.equal(store.state.run!.state,'cancelling');
  store.apply({type:'run.finished',sessionId:'s',runId:'r',status:'cancelled'});
  assert.equal(store.state.run!.state,'cancelled');
});
test('webview requests reject unknown commands and approval options', () => {
  assert.equal(ClientMessageSchema.safeParse({type:'exec',command:'touch file'}).success,false);
  assert.equal(ClientMessageSchema.safeParse({type:'approve',requestId:'x',decision:'always'}).success,false);
  assert.equal(ClientMessageSchema.safeParse({type:'run',text:'hello',credentials:'secret'}).success,false);
});

test('completed, cancelled and disconnected items cannot return to a pending state', () => {
  for (const ending of ['completed', 'cancelled', 'disconnected'] as const) {
    const store = new SessionStore('mock');
    store.apply({ type: 'session.snapshot', sessionId: 's', items: [] });
    store.apply({ type: 'run.started', sessionId: 's', runId: 'r', text: 'work' });
    const item = { id: 'tool', runId: 'r', kind: 'tool' as const, title: 'LG', text: 'preview', state: 'running' as const };
    store.apply({ type: 'item.updated', sessionId: 's', item });
    if (ending === 'disconnected') store.apply({ type: 'backend.status', status: 'disconnected' });
    else store.apply({ type: 'run.finished', sessionId: 's', runId: 'r', status: ending });
    const terminal = store.snapshot().items;
    for (const state of ['running', 'waiting'] as const) {
      store.apply({ type: 'item.updated', sessionId: 's', item: { ...item, state, text: 'late update' } });
    }
    store.apply({ type: 'assistant.delta', sessionId: 's', runId: 'r', itemId: 'tool', delta: 'late delta' });
    assert.deepEqual(store.state.items, terminal, ending);
  }
});

test('restored waiting items can progress, and authoritative final details remain available', () => {
  const store = new SessionStore('mock');
  const item = { id: 'a', runId: 'r', kind: 'assistant' as const, title: 'LG', text: 'first', state: 'waiting' as const };
  store.apply({ type: 'session.snapshot', sessionId: 's', items: [item] });
  store.apply({ type: 'assistant.delta', sessionId: 's', runId: 'r', itemId: 'a', delta: ' second' });
  assert.equal(store.state.items[0].text, 'first second');
  store.apply({ type: 'run.finished', sessionId: 's', runId: 'r', status: 'completed' });
  store.apply({ type: 'item.updated', sessionId: 's', item: { ...item, state: 'completed', text: 'final text' } });
  assert.equal(store.state.items[0].text, 'final text');
  assert.equal(store.state.items[0].state, 'completed');
});

test('one concurrent run finishing preserves the other run and its approval',()=>{
  const store=new SessionStore('codex');store.apply({type:'session.snapshot',sessionId:'s',items:[]});
  for(const runId of ['workflow','agent'])store.apply({type:'run.started',sessionId:'s',runId,text:runId});
  store.apply({type:'approval.requested',sessionId:'s',approval:{id:'approve-workflow',runId:'workflow',
    title:'approve',details:'exact preview',choices:['accept','decline'],state:'pending'}});
  store.apply({type:'run.finished',sessionId:'s',runId:'agent',status:'completed'});
  assert.equal(store.busy,true);assert.equal(store.state.run?.id,'workflow');
  assert.equal(store.state.run?.state,'waiting');assert.equal(store.state.approvals[0].state,'pending');
  store.apply({type:'approval.resolved',sessionId:'s',requestId:'approve-workflow'});
  assert.equal(store.state.run?.state,'running');
  store.apply({type:'run.finished',sessionId:'s',runId:'workflow',status:'cancelled'});
  assert.equal(store.busy,false);
});
test('finishing older run cannot discard current questions or idle the session',()=>{
  const store=new SessionStore('codex');store.apply({type:'session.snapshot',sessionId:'s',items:[]});
  for(const runId of ['workflow','agent'])store.apply({type:'run.started',sessionId:'s',runId,text:runId});
  store.apply({type:'userInput.requested',sessionId:'s',request:{id:'q',runId:'agent',questions:[{id:'a',label:'direction',options:[]}]}});
  store.apply({type:'run.finished',sessionId:'s',runId:'workflow',status:'completed'});
  assert.equal(store.busy,true);assert.equal(store.state.inputs.length,1);
  store.apply({type:'backend.status',status:'disconnected'});
  assert.equal(store.busy,false);assert.equal(store.state.inputs.length,0);assert.equal(store.state.run?.state,'interrupted');
});
test('optional active run snapshot restores both lifetimes without replaying requests',()=>{
  const store=new SessionStore('codex');
  store.apply({type:'session.snapshot',sessionId:'s',items:[],activeRuns:[{id:'workflow',state:'running'},{id:'agent',state:'running'}]});
  store.apply({type:'run.finished',sessionId:'s',runId:'agent',status:'completed'});
  assert.equal(store.busy,true);assert.equal(store.state.run?.id,'workflow');
  assert.equal(store.state.approvals.length,0);
  store.apply({type:'session.snapshot',sessionId:'old-format',items:[]});
  assert.equal(store.busy,false);assert.equal(store.state.run,null);
});

test('retry lifecycle uses its own busy run and notice without duplicating author intent',()=>{
  const store=new SessionStore('codex');store.apply({type:'session.snapshot',sessionId:'s',items:[]});
  store.apply({type:'run.started',sessionId:'s',runId:'first',text:'只规划'});
  store.apply({type:'run.finished',sessionId:'s',runId:'first',status:'failed'});
  store.apply({type:'run.started',sessionId:'s',runId:'retry',text:'重试：只规划',source:'retry'});
  assert.equal(store.busy,true);assert.equal(store.state.run?.id,'retry');
  assert.equal(store.state.items.filter(item=>item.kind==='user').length,1);
  assert.equal(store.state.items.filter(item=>item.kind==='notice'&&item.title==='重试').length,1);
  store.apply({type:'run.finished',sessionId:'s',runId:'retry',status:'completed'});
  assert.equal(store.busy,false);
});
