import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JsonLines, type RpcMessage } from '../src/backend/RpcTransport';
import { normalizeItem, errorCategory } from '../src/backend/normalize';

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
test('MCP structured result is preserved; private reasoning is not exposed', () => {
  const item=normalizeItem({id:'a',type:'mcpToolCall',server:'lg_writing',tool:'studio_save',result:{content:[{type:'text',text:'Saved'}],structuredContent:{profileVersion:2}}},'run',true)!;
  assert.equal(item.text,'Saved');assert.deepEqual(item.structured,{arguments:undefined,result:{profileVersion:2}});
  assert.equal(normalizeItem({id:'b',type:'reasoning',content:['private']},'run',true),undefined);
  assert.match(normalizeItem({id:'c',type:'futureOutput'},'run',true)!.text,/只读/);
});
test('FastMCP JSON text exposes real candidate artifacts and readonly diff',()=>{
  const data={artifact:'ReferenceLibrary/drafts/candidate.md',version:{version_number:2},diff:{path:'ReferenceLibrary/drafts/candidate.md',before:'旧稿',after:'修订稿',applied:false}};
  const base={id:'candidate',type:'mcpToolCall',server:'lg_writing',tool:'studio_draft'};
  const item=normalizeItem({...base,result:{content:[{type:'text',text:JSON.stringify(data)}]}},'run',true)!;
  assert.deepEqual(item.files,[data.artifact]);assert.deepEqual(item.diff,data.diff);
  assert.deepEqual(item.structured,{arguments:undefined,result:data});
  const plain=normalizeItem({...base,result:{content:[{type:'text',text:'not JSON'}]}},'run',true)!;
  assert.equal(plain.text,'not JSON');assert.equal(plain.diff,undefined);assert.equal(plain.files,undefined);
  const malformed=normalizeItem({...base,result:{content:[{type:'text',text:JSON.stringify({artifact:7,diff:{...data.diff,applied:'false'}})}]}},'run',true)!;
  assert.equal(malformed.diff,undefined);assert.equal(malformed.files,undefined);
});
test('known bridge failures are actionable without forwarding provider error bodies',()=>{
  assert.equal(errorCategory({codexErrorInfo:'other',message:'response failed: upstream tool input is not valid JSON; PRIVATE_SENTINEL'}),'bridgeInvalidToolJson');
  assert.equal(errorCategory({codexErrorInfo:'other',message:'upstream output token limit reached before completion; PRIVATE_SENTINEL'}),'bridgeOutputLimit');
  assert.equal(errorCategory({codexErrorInfo:'other',message:'PRIVATE_SENTINEL'}),'other');
});

test('tool JSON diagnostics retain only fixed labels and bounded numeric positions',()=>{
  const prefix='upstream tool input is not valid JSON; json_error=';
  assert.equal(errorCategory({message:prefix+'missing_comma, offset=17, chars=25; PRIVATE_SENTINEL'}),
    'bridgeInvalidToolJson / missing_comma / 17:25');
  for(const suffix of ['PRIVATE_SENTINEL, offset=17, chars=25',
    'missing_comma, offset=26, chars=25', 'missing_comma, offset=-1, chars=25',
    'missing_comma, offset=1, chars=999999999']) {
    assert.equal(errorCategory({message:prefix+suffix}), 'bridgeInvalidToolJson');
  }
});
