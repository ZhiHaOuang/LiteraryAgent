/** Low-level compatibility entry remains supported; this is not an Extension connector. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {resolve,join} from 'node:path';
import {tmpdir} from 'node:os';
import {z} from 'zod';
import {RpcTransport,type RpcMessage,type LaunchSpec} from '../src/backend/RpcTransport';
const object=z.record(z.unknown());
const record=(value:unknown)=>object.parse(value);
async function until(predicate:()=>boolean){
 const end=Date.now()+45000;
 while(!predicate()){if(Date.now()>end)throw Error('Core compatibility event timeout');await new Promise(r=>setTimeout(r,20));}
}
test('retained Core stdio entry: MCP decisions, stream, cancel and persisted history',{
 timeout:180000,skip:!process.env.LG_TEST_RUNTIME_MANIFEST||!process.env.LG_TEST_PYTHON},async t=>{
 const workspace=await mkdtemp(join(tmpdir(),'lg-core-compat-'));
 const spec:LaunchSpec={command:process.env.LG_TEST_PYTHON!,
  args:[resolve('tests/fixtures/app_server_fixture.py'),'--workspace',workspace,'--runtime-manifest',process.env.LG_TEST_RUNTIME_MANIFEST!],
  cwd:workspace,env:{...process.env,LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')}};
 const description=record(JSON.parse(execFileSync(spec.command,[...spec.args,'--describe'],{encoding:'utf8',cwd:workspace,env:spec.env})));
 assert.equal(description.workspace,workspace);
 const messages:RpcMessage[]=[],replyErrors:unknown[]=[];
 let decision:'accept'|'decline'='accept',approvalCount=0;
 let rpc:RpcTransport;
 const connect=async()=>{
  rpc=new RpcTransport(spec,message=>{
   messages.push(message);
   if(message.id!==undefined&&message.method){
    if(message.method==='mcpServer/elicitation/request'){
     approvalCount++;void rpc.respond(message.id,{action:decision,content:decision==='accept'?{confirm:true}:null,_meta:null}).catch(e=>replyErrors.push(e));
    }else void rpc.reject(message.id).catch(e=>replyErrors.push(e));
   }
  },()=>{});
  rpc.start();await rpc.request('initialize',{clientInfo:{name:'lg-compat-test',version:'0.3.0'},capabilities:{experimentalApi:true}});
  await rpc.notify('initialized',{});
 };
 t.after(async()=>{await rpc?.dispose();t.diagnostic(JSON.stringify({workspace,approvalCount}));});
 await connect();
 const thread=record(record(await rpc!.request('thread/start',{cwd:workspace,model:description.model||null,sandbox:'read-only',
  approvalPolicy:'on-request',approvalsReviewer:'user',baseInstructions:description.baseInstructions,ephemeral:false})).thread);
 const threadId=z.string().parse(thread.id);
 const call=(tool:string,args:unknown)=>rpc!.request('mcpServer/tool/call',{threadId,server:'lg_writing',tool,arguments:args});
 const content=(value:unknown)=>{
  const reply=record(value);if(reply.structuredContent)return record(reply.structuredContent);
  const blocks=z.array(object).parse(reply.content);return record(JSON.parse(String(blocks.find(b=>b.type==='text')?.text)));
 };
 await call('studio_initialize',{});assert.ok(approvalCount>0,'Initialization must cross elicitation');
 decision='decline';const declinedBefore=approvalCount;
 await call('update_book_overview',{overview:'这条简介不应保存。'});assert.equal(approvalCount,declinedBefore+1);
 assert.equal(content(await call('book_dashboard',{})).overview,'');
 decision='accept';await call('update_book_overview',{overview:'维修师在浮城寻找失灵闸门背后的真相。'});
 assert.match(String(content(await call('book_dashboard',{})).overview),/浮城/);
 const start=async(text:string)=>record(record(await rpc!.request('turn/start',{threadId,input:[{type:'text',text,text_elements:[]}]})).turn);
 const finished=(id:unknown)=>messages.some(m=>m.method==='turn/completed'&&record(record(m.params).turn).id===id);
 const text=await start('CASE:text');await until(()=>finished(text.id));
 assert.ok(messages.some(m=>m.method==='item/agentMessage/delta'));
 const slow=await start('CASE:slow');
 await until(()=>messages.some(m=>m.method==='turn/started'&&record(record(m.params).turn).id===slow.id));
 await rpc!.request('turn/interrupt',{threadId,turnId:slow.id});await until(()=>finished(slow.id));
 assert.equal(record(record(messages.find(m=>m.method==='turn/completed'&&record(record(m.params).turn).id===slow.id)!.params).turn).status,'interrupted');
 assert.equal(replyErrors.length,0);
 await rpc!.dispose();await connect();
 const listed=record(await rpc!.request('thread/list',{cwd:workspace,limit:100}));
 assert.ok(z.array(object).parse(listed.data).some(value=>value.id===threadId));
 await rpc!.request('thread/resume',{threadId,cwd:workspace,sandbox:'read-only',approvalPolicy:'on-request',baseInstructions:description.baseInstructions});
 const saved=record(record(await rpc!.request('thread/read',{threadId,includeTurns:false})).thread);
 const turns=saved.historyMode==='paginated'?await rpc!.request('thread/turns/list',{threadId,limit:100}):saved.turns;
 assert.ok(JSON.stringify(turns).includes('CASE:text'));
});
// Product candidate/diff/library/author-adoption checks now run through
// literary-backend.integration.ts and native-runtime.integration.ts.
