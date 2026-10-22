import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {ClientMessageSchema,emptyBook,type AuthorTaskRequest,type BackendEvent} from '../shared/protocol';
import {TaskResult,LiteraryTasksPanel} from '../webview/LiteraryTasksPanel';
import {LiteraryBackend} from '../src/backend/LiteraryBackend';

const fingerprint='a'.repeat(64),operationId='b'.repeat(32);
test('bounded task messages reject unknown fields, invalid identities and unbounded options',()=>{
  const valid:AuthorTaskRequest[]=[
    {action:'start',options:{strategy:'polish',document:'one',request:'Polish this.',passage:'Exact.'}},
    {action:'select',taskId:'task-one',optionId:'2',fingerprint},
    {action:'advance',taskId:'task-one',fingerprint},
    {action:'reviewAdopt',document:'one',version:2},
    {action:'narrative',options:{audience:'character',holder_document:'hero',position:5,operation:'character_next_action'}},
    {action:'receipt',operationId},
  ];
  for(const request of valid)assert.equal(ClientMessageSchema.parse({type:'boundedTask',request}).type,'boundedTask');
  const invalid=[
    {type:'boundedTask',request:valid[0],approved:true},
    {type:'boundedTask',request:{...valid[1],approved:true}},
    {type:'boundedTask',request:{action:'select',taskId:'x',optionId:'2',fingerprint:'short'}},
    {type:'boundedTask',request:{action:'reviewAdopt',document:'one',version:0}},
    {type:'boundedTask',request:{action:'start',options:{strategy:'chapter',document:'one',request:'  '}}},
    {type:'boundedTask',request:{action:'narrative',options:{audience:'reader',position:1,
      operation:'reader_expectation',history:'FUTURE_SECRET'}}},
    {type:'boundedTask',request:{action:'receipt',operationId:'../database'}},
  ];
  for(const message of invalid)assert.equal(ClientMessageSchema.safeParse(message).success,false,JSON.stringify(message));
});

function render(structured:unknown,disabled=false){
  return renderToStaticMarkup(createElement(TaskResult,{structured,disabled,send:()=>{}}));
}
test('waiting direction renders separate choice and continue controls, stale source disables both',()=>{
  const state={runId:'task-one',status:'waiting_author',fingerprint,decision:{options:[
    {id:'1',title:'留守',summary:'保护现有线索'},{id:'2',title:'追踪',summary:'寻找外部证据'}]}};
  const html=render({literaryTask:state});
  assert.match(html,/等待你选择方向/);
  assert.match(html,/选择需要独立创作批准/);
  assert.equal((html.match(/选择此方向<\/button>/g)||[]).length,2);
  assert.match(html,/已选择，继续写作/);
  assert.doesNotMatch(html,/审读并申请采用/);
  const stale=render({literaryTask:{...state,sourceStale:true}});
  const controls=[...stale.matchAll(/<button([^>]*)>(选择此方向|已选择，继续写作)<\/button>/g)];
  assert.equal(controls.length,3);
  for(const button of controls)assert.match(button[1],/disabled/);
  assert.match(stale,/基础资料已经变化/);
});
test('candidate adoption remains explicit and unknown receipts never imply no commit',()=>{
  const candidate={runId:'task-one',status:'awaiting_review',candidate:{document:'one',versionNumber:2}};
  const html=render({literaryTask:candidate});
  assert.match(html,/生成任务没有自动采用正文/);
  assert.match(html,/审读并申请采用/);
  assert.match(render({literaryTask:candidate},true),/<button disabled="">审读并申请采用/);
  const unknown=render({operationReceipt:{operationId,status:'unknown',toolOutcome:'unknown',derivedFiles:'not_attested'}});
  assert.match(unknown,/提交结果尚不能确定/);
  assert.doesNotMatch(unknown,/数据库修改未提交/);
  assert.match(unknown,/取消不会撤销已经提交的修改/);
  assert.match(unknown,/查询最新提交状态/);
  assert.match(render({operationReceipt:{operationId,status:'committed',toolOutcome:'failed',derivedFiles:'not_attested'}}),/数据库修改已提交/);
});
test('unsupported inference explains narrow guarantee and never offers manuscript adoption',()=>{
  const html=render({narrativeResult:{status:'unsupported',reasons:['strict_mode_requires_native_responses']}});
  assert.match(html,/当前执行器不支持这项保证/);
  assert.match(html,/Native Responses/);
  assert.match(html,/覆盖不完整/);
  assert.doesNotMatch(html,/<button/);
  const panel=renderToStaticMarkup(createElement(LiteraryTasksPanel,{disabled:true,send:()=>{}}));
  assert.match(panel,/有限写作任务与认知推演/);
  assert.match(panel,/指定选区润色/);
  assert.match(panel,/准确选区/);
});

test('validated inference separates evidence and uncertainty without claiming correctness',()=>{
  const value={schema:'lg.narrative-execution.v2',status:'completed',resultReleased:true,
    evidenceValidation:{contract:'visible_record_exact_quote.v1',status:'validated'},
    analysis:{inference:'人物可能等待。',evidence:[{record_id:7,quote:'<门已关闭>'}],uncertainty:'目的尚不明确。'}};
  const html=render({narrativeResult:value});
  for(const text of ['推断','登记证据','不确定性','记录 7','引用有效不代表推理正确'])assert.ok(html.includes(text));
  assert.match(html,/&lt;门已关闭&gt;/);
  assert.doesNotMatch(html,/旧版输出|<button/);
  const rejected=render({narrativeResult:{...value,status:'failed',resultReleased:false,
    evidenceValidation:{contract:'visible_record_exact_quote.v1',status:'rejected'},
    reasons:['invalid_evidence'],text:'UNVERIFIED_OUTPUT_SECRET'}});
  assert.match(rejected,/输出引用未通过校验/);
  assert.doesNotMatch(rejected,/人物可能等待|UNVERIFIED_OUTPUT_SECRET|门已关闭/);
  const malformed=render({narrativeResult:{...value,analysis:{...value.analysis,evidence:[]},text:'UNVERIFIED_OUTPUT_SECRET'}});
  assert.match(malformed,/结果结构未通过检查/);
  assert.doesNotMatch(malformed,/UNVERIFIED_OUTPUT_SECRET|人物可能等待/);
});

test('legacy inference remains readable and explicitly lacks evidence validation',()=>{
  for(const schema of [undefined,'lg.narrative-execution.v1']){
    const html=render({narrativeResult:{schema,status:'completed',text:'此前的推演。'}});
    assert.match(html,/旧版输出：未校验证据引用/);
    assert.match(html,/此前的推演/);
    assert.doesNotMatch(html,/已核对引用|<button/);
  }
});

// This peer tests connector routing only. It is not a Python Backend or model fixture.
const fixture=String.raw`
const fs=require('node:fs');
const [log,capability,book]=process.argv.slice(1);
require('node:readline').createInterface({input:process.stdin}).on('line',line=>{
  const req=JSON.parse(line);fs.appendFileSync(log,JSON.stringify(req)+'\n');
  const p=req.params||{};let result;
  switch(req.method){
    case 'initialize':result={protocolVersion:'lg.backend.v1',workspace:process.cwd(),runtime:'native',
      capabilities:['bookLibrary','clientEvents',...(capability==='yes'?['literaryTasks']:[])]};break;
    case 'session/create':result={sessionId:'session-one'};break;
    case 'session/snapshot':result={sessionId:p.sessionId,cursor:0,items:[],activeRuns:[]};break;
    case 'book/dashboard':result=JSON.parse(book);break;
    case 'task/preview':result={status:'ready',fingerprint:'a'.repeat(64)};break;
    case 'narrative/preview':result=p.options.audience==='reader'?{status:'unsupported',
      reasons:['strict_mode_requires_native_responses']}:{status:'ready',fingerprint:'a'.repeat(64)};break;
    case 'task/read':result={runId:p.taskId,status:'waiting_author',fingerprint:'a'.repeat(64)};break;
    case 'operation/read':result={operationId:p.operationId,status:'unknown',toolOutcome:'unknown',derivedFiles:'not_attested'};break;
    case 'sources/check':result={coverage:'known_direct_associations_only',automaticChanges:false};break;
    default:result={sessionId:'session-one',runId:'protocol-only-run'};
  }
  console.log(JSON.stringify({jsonrpc:'2.0',id:req.id,result}));
});`;
class ProtocolBackend extends LiteraryBackend{
  constructor(private log:string,private capability=true){super({python:process.execPath,manifest:'',workspace:process.cwd()});}
  protected override launch(){return {command:process.execPath,args:['-e',fixture,this.log,this.capability?'yes':'no',
    JSON.stringify({...emptyBook(),source:'backend'})],cwd:process.cwd(),jsonRpc:true};}
}
test('connector routes previews, exact selection, independent adoption and read-only receipts',async()=>{
  const log=join(mkdtempSync(join(tmpdir(),'lg-task-rpc-')),'requests.jsonl');
  const backend=new ProtocolBackend(log),events:BackendEvent[]=[];
  backend.subscribe(event=>events.push(event));
  const requests=()=>readFileSync(log,'utf8').trim().split('\n').map(line=>JSON.parse(line));
  try{
    await backend.connect();const session=await backend.create();
    const options={strategy:'chapter' as const,document:'one',request:'Compare choices.',plot_disagreement:true};
    await backend.authorTask(session.id,{action:'start',options});
    await backend.authorTask(session.id,{action:'select',taskId:'task-one',optionId:'2',fingerprint});
    await backend.authorTask(session.id,{action:'advance',taskId:'task-one',fingerprint});
    await backend.authorTask(session.id,{action:'reviewAdopt',document:'one',version:2});
    await backend.authorTask(session.id,{action:'read',taskId:'task-one'});
    await backend.authorTask(session.id,{action:'receipt',operationId});
    await backend.authorTask(session.id,{action:'sources',document:'one',version:1});
    const calls=requests();
    const start=calls.find(item=>item.method==='task/start');
    assert.deepEqual(start.params,{sessionId:session.id,options,expectedFingerprint:fingerprint});
    assert.ok(calls.findIndex(item=>item.method==='task/preview')<calls.findIndex(item=>item.method==='task/start'));
    assert.deepEqual(calls.find(item=>item.method==='task/select').params,{sessionId:session.id,taskId:'task-one',expectedFingerprint:fingerprint,optionId:'2'});
    assert.deepEqual(calls.find(item=>item.method==='creative/apply').params,{sessionId:session.id,action:'acceptVersion',document:'one',version:2,review:true});
    assert.equal(calls.filter(item=>item.method==='creative/apply').length,1);
    assert.ok(events.some(event=>event.type==='item.updated'&&(event.item.structured as {operationReceipt?:unknown})?.operationReceipt));
    const before=requests().length;
    await assert.rejects(backend.authorTask('other-session',{action:'read',taskId:'task-one'}),/会话不匹配/);
    assert.equal(requests().length,before);
  }finally{await backend.dispose();}
});
test('unsupported narrative preview and missing capability never dispatch model work',async()=>{
  for(const capability of [true,false]){
    const log=join(mkdtempSync(join(tmpdir(),'lg-task-capability-')),'requests.jsonl');
    const backend=new ProtocolBackend(log,capability),events:BackendEvent[]=[];
    backend.subscribe(event=>events.push(event));
    try{
      await backend.connect();const session=await backend.create();
      const command:AuthorTaskRequest={action:'narrative',options:{audience:'reader',position:1,operation:'reader_expectation'}};
      if(capability){
        await backend.authorTask(session.id,command);
        assert.ok(events.some(event=>event.type==='item.updated'&&(event.item.structured as {narrativeResult?:unknown})?.narrativeResult));
      }else await assert.rejects(backend.authorTask(session.id,command),/尚不支持有限任务/);
      const calls=readFileSync(log,'utf8').trim().split('\n').map(line=>JSON.parse(line));
      assert.equal(calls.some(item=>item.method==='narrative/run'),false);
      assert.equal(calls.some(item=>item.method==='task/start'),false);
    }finally{await backend.dispose();}
  }
});
