import assert from 'node:assert/strict';
import test from 'node:test';
import type { Snapshot } from '../shared/protocol';
import { SessionStore } from '../src/host/SessionStore';
import { LatestSnapshotSender } from '../src/host/LatestSnapshotSender';

test('slow renderer receives bounded full snapshots including latest approval and terminal state', async()=>{
  const store=new SessionStore('mock');
  const delivered:Snapshot[]=[];
  let release:()=>void=()=>{};
  const first=new Promise<void>(resolve=>{release=resolve;});
  const sender=new LatestSnapshotSender(async snapshot=>{
    delivered.push(snapshot);
    if(delivered.length===1)await first;
  });
  sender.offer(store.snapshot());
  for(let sequence=1;sequence<=10000;sequence++){
    store.state.sequence=sequence;
    sender.offer(store.snapshot());
  }
  store.state.run={id:'final',state:'completed'};
  store.state.approvals=[{id:'approval',runId:'final',title:'Confirm',details:'Exact operation',choices:['accept','decline'],state:'resolved'}];
  sender.offer(store.snapshot());
  assert.equal(delivered.length,1);
  release();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(delivered.length,2);
  assert.equal(delivered[1].sequence,10000);
  assert.equal(delivered[1].run?.state,'completed');
  assert.equal(delivered[1].approvals[0].state,'resolved');
  sender.dispose();
});

test('disposal drops pending snapshots and a failed send does not block the latest snapshot',async()=>{
  const store=new SessionStore('mock');
  for(const dispose of [true,false]){
    let reject:(error:Error)=>void=()=>{};
    const blocked=new Promise<void>((_,r)=>{reject=r;});
    const delivered:number[]=[];
    const sender=new LatestSnapshotSender(snapshot=>{
      delivered.push(snapshot.sequence);
      return delivered.length===1?blocked:undefined;
    });
    store.state.sequence=0;sender.offer(store.snapshot());
    store.state.sequence=1;sender.offer(store.snapshot());
    if(dispose)sender.dispose();
    reject(new Error('Renderer closed'));await new Promise(resolve=>setImmediate(resolve));
    assert.deepEqual(delivered,dispose?[0]:[0,1]);
    sender.dispose();
  }
});
