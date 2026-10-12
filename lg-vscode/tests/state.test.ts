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
