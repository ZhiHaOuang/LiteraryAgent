import test from 'node:test';
import assert from 'node:assert/strict';
import {SessionStore} from '../src/host/SessionStore';
import {LibraryReader} from '../src/host/LibraryReader';
import {MockLibrary} from '../src/backend/MockLibrary';

const tick=()=>new Promise(r=>setImmediate(r));
test('library navigation keeps one read and only latest queued search; late results cannot replace current page',async()=>{
  const data=new MockLibrary(),store=new SessionStore('mock');
  let release!:()=>void;const blocked=new Promise<void>(r=>release=r);
  const calls:string[]=[];
  const reader=new LibraryReader({readLibrary:async q=>{calls.push(q.query);if(calls.length===1)await blocked;return data.page(q);},readEntry:(topic,key)=>data.detail(topic,key)},store);
  reader.select({topic:'characters',query:'',offset:0});
  for(let i=0;i<1000;i++)reader.select({topic:'characters',query:i===999?'维修学徒':String(i),offset:0});
  assert.equal(calls.length,1);release();await tick();await tick();
  assert.deepEqual(calls,['','维修学徒']);assert.equal(store.state.library.page?.entries.length,1);
  assert.equal(store.state.library.query.query,'维修学徒');reader.dispose();
});
test('saved-change invalidation disables old preview and disposal prevents late publication',async()=>{
  const data=new MockLibrary(),store=new SessionStore('mock');
  const reader=new LibraryReader({readLibrary:q=>data.page(q),readEntry:(topic,key)=>data.detail(topic,key)},store);
  reader.select({topic:'characters',query:'',offset:0});await tick();
  reader.open('characters','person:lin');await tick();
  const fingerprint=store.state.library.detail!.fingerprint;
  reader.invalidate();assert.equal(store.state.library.stale,true);
  data.commit('generate');reader.refresh();await tick();
  assert.notEqual(store.state.library.detail?.fingerprint,fingerprint);
  assert.equal(store.state.library.stale,false);reader.dispose();
  const sequence=store.state.sequence;reader.select({topic:'world',query:'',offset:0});await tick();
  assert.equal(store.state.sequence,sequence);
});
