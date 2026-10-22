import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JsonLines, type RpcMessage } from '../src/backend/RpcTransport';
import {RpcTransport} from '../src/backend/RpcTransport';

test('JSONL preserves split UTF-8, coalesced frames and request id zero', () => {
  const messages: RpcMessage[] = [], parser = new JsonLines(m=>messages.push(m));
  const bytes = Buffer.from(JSON.stringify({id:0,method:'审批',params:{text:'星港'}})+'\n'+JSON.stringify({id:'x',result:{}})+'\n');
  for(const byte of bytes)parser.push(Buffer.from([byte]));parser.end();
  assert.equal(messages[0].id,0);assert.equal(messages[0].method,'审批');assert.equal(messages[1].id,'x');
});
test('framing rejects truncated, oversized, invalid UTF-8 and ambiguous responses', () => {
  const parser = new JsonLines(()=>{},16);parser.push(Buffer.from('{"id":1'));
  assert.throws(()=>parser.end());
  assert.throws(()=>new JsonLines(()=>{},2).push(Buffer.from('123')));
  assert.throws(()=>new JsonLines(()=>{}).push(Buffer.from([0xff,10])));
  assert.throws(()=>new JsonLines(()=>{}).push(Buffer.from('{"id":1,"result":{},"error":{}}\n')));
});
test('RPC error bodies and stderr are not exposed to the client',async()=>{
 const script=`require('node:readline').createInterface({input:process.stdin}).on('line',line=>{
  const m=JSON.parse(line);process.stderr.write('PRIVATE_STDERR');
  console.log(JSON.stringify({id:m.id,error:{code:-32000,message:'PRIVATE_PROVIDER_BODY'}}));
 });`;
 const messages:RpcMessage[]=[],closed:string[]=[];
 const rpc=new RpcTransport({command:process.execPath,args:['-e',script],cwd:process.cwd()},m=>messages.push(m),s=>closed.push(s));
 try{rpc.start();await assert.rejects(rpc.request('fixture',{}),error=>{
  assert.equal((error as Error).message,'app-server 请求失败（RPC -32000）');return true;
 });assert.equal(messages.length,0);assert.ok(!JSON.stringify(closed).includes('PRIVATE_'));}
 finally{await rpc.dispose();}
});
// Runtime item privacy/diff conversion is checked against the real Python
// service in presentation.integration.ts, not a second frontend normalizer.
