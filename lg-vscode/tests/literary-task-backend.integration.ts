import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,existsSync,readFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {execFileSync} from 'node:child_process';
import {LiteraryBackend} from '../src/backend/LiteraryBackend';
import type {BackendEvent,TaskOptions} from '../shared/protocol';
import {buildClarifiedPolishRequest} from '../webview/LiteraryTasksPanel';

const python=process.env.LG_TEST_PYTHON;
if(!python)throw Error('Set LG_TEST_PYTHON to the verified Python environment; this integration must not silently skip.');
test('frontend connector crosses real Backend approval and Native HTTP before returning a candidate',async()=>{
  const workspace=mkdtempSync(join(tmpdir(),'lg-task-real-backend-'));
  const repo=resolve('..'),fixture=resolve('tests/fixtures/literary_task_backend.py');
  const env={...process.env,PYTHONPATH:join(repo,'lg-cli')+':'+repo,LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')};
  class RealBackend extends LiteraryBackend{
    protected override launch(){return {command:python!,args:[fixture,workspace],cwd:workspace,env,jsonRpc:true};}
  }
  const backend=new RealBackend({python:python!,manifest:'',workspace}),events:BackendEvent[]=[];
  backend.subscribe(event=>events.push(event));
  async function until(predicate:()=>boolean){
    const deadline=Date.now()+12000;
    while(!predicate()){
      assert.ok(Date.now()<deadline,'Timed out waiting for real Backend event: '+JSON.stringify(events.slice(-3)));
      await new Promise(resolve=>setTimeout(resolve,10));
    }
  }
  try{
    await backend.connect();const session=await backend.create();
    await backend.authorTask(session.id,{action:'start',options:{strategy:'polish',document:'one',
      request:'Polish exactly this passage.',passage:'SELECTED.'}});
    await until(()=>events.some(event=>event.type==='approval.requested'));
    const approval=events.find(event=>event.type==='approval.requested');
    assert.ok(approval?.type==='approval.requested');
    assert.equal(existsSync(join(workspace,'http-requests.jsonl')),false,'No model request before operation approval');
    await backend.approve(approval.approval.id,'accept');
    await until(()=>events.some(event=>event.type==='run.finished'));
    const finished=events.find(event=>event.type==='run.finished');
    assert.ok(finished?.type==='run.finished');
    assert.equal(finished.status,'completed');
    const taskEvent=events.find(event=>event.type==='item.updated'&&
      !!(event.item.structured as {literaryTask?:unknown})?.literaryTask);
    assert.ok(taskEvent?.type==='item.updated');
    const task=(taskEvent.item.structured as {literaryTask:{runId:string;status:string;candidate:{versionNumber:number}}}).literaryTask;
    assert.equal(task.status,'awaiting_review');
    const requests=readFileSync(join(workspace,'http-requests.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));
    assert.equal(requests.length,1);
    assert.deepEqual(requests[0].tools,[]);
    const inspect=`import json,sys\nfrom pathlib import Path\nfrom lg_cli.project_store import ProjectStore\ns=ProjectStore(Path(sys.argv[1]))\nprint(json.dumps({'active':s.get_document('one').content,'candidate':s.get_version('one',int(sys.argv[2])).content}))`;
    const saved=JSON.parse(execFileSync(python!,['-c',inspect,workspace,String(task.candidate.versionNumber)],{env,encoding:'utf8'}));
    assert.deepEqual(saved,{active:'Prefix. SELECTED. Suffix.',candidate:'Prefix. REVISED. Suffix.'});
    await until(()=>events.some(event=>event.type==='book.updated'));
    await backend.authorTask(session.id,{action:'read',taskId:task.runId});
    assert.ok(events.some(event=>event.type==='item.updated'&&event.item.id==='bounded-read:'+task.runId));
  }finally{await backend.dispose();}
});

test('no-progress polish preserves its budget while a clarified task requires a separate real Backend approval',async()=>{
  const workspace=mkdtempSync(join(tmpdir(),'lg-task-clarification-backend-'));
  const repo=resolve('..'),fixture=resolve('tests/fixtures/literary_task_backend.py');
  const env={...process.env,PYTHONPATH:join(repo,'lg-cli')+':'+repo,LITERARYGIANT_REGISTRY_HOME:join(workspace,'registry')};
  class RealBackend extends LiteraryBackend{
    protected override launch(){return {command:python!,args:[fixture,workspace,'no-progress-first'],cwd:workspace,env,jsonRpc:true};}
  }
  const backend=new RealBackend({python:python!,manifest:'',workspace}),events:BackendEvent[]=[];
  backend.subscribe(event=>events.push(event));
  const requests=()=>existsSync(join(workspace,'http-requests.jsonl'))?
    readFileSync(join(workspace,'http-requests.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line)):[];
  async function until(predicate:()=>boolean){
    const deadline=Date.now()+12000;
    while(!predicate()){
      assert.ok(Date.now()<deadline,'Timed out waiting for clarification Backend event: '+JSON.stringify(events.slice(-3)));
      await new Promise(resolve=>setTimeout(resolve,10));
    }
  }
  type Task={runId:string;status:string;reason?:string;modelCalls:number;limits:{maxModelCalls:number;maxInputChars:number};
    clarification?:{question:string;options:TaskOptions};candidate?:{versionNumber:number}};
  function resultSince(index:number){
    const event=events.slice(index).find(event=>event.type==='item.updated'&&
      !!(event.item.structured as {literaryTask?:unknown})?.literaryTask);
    assert.ok(event?.type==='item.updated');
    return (event.item.structured as {literaryTask:Task}).literaryTask;
  }
  try{
    await backend.connect();const session=await backend.create();
    const options:TaskOptions={strategy:'polish',document:'one',request:'Keep the dialogue and all existing facts.',
      passage:'SELECTED.',maxModelCalls:1,maxInputChars:12000};
    await backend.authorTask(session.id,{action:'start',options});
    await until(()=>events.some(event=>event.type==='approval.requested'));
    const firstApproval=events.find(event=>event.type==='approval.requested');
    assert.ok(firstApproval?.type==='approval.requested');
    assert.equal(requests().length,0);
    await backend.approve(firstApproval.approval.id,'accept');
    await until(()=>events.some(event=>event.type==='run.finished'));
    const stopped=resultSince(0);
    assert.equal(stopped.status,'stopped');
    assert.equal(stopped.reason,'no_textual_progress');
    assert.equal(stopped.modelCalls,1);
    assert.equal(stopped.candidate,undefined);
    assert.deepEqual(stopped.clarification?.options,options);
    assert.equal(requests().length,1);
    const savedPath=join(workspace,'.literarygiant','runs',stopped.runId,'literary-task.json');
    const saved=readFileSync(savedPath);
    await backend.authorTask(session.id,{action:'read',taskId:stopped.runId});
    assert.equal(requests().length,1,'Reading the clarification does not repeat the old model request');

    const marker=events.length;
    const clarified=buildClarifiedPolishRequest(stopped.clarification!.options,'Adjust only the pauses between the actions.');
    await backend.authorTask(session.id,clarified);
    await until(()=>events.slice(marker).some(event=>event.type==='approval.requested'));
    const secondApproval=events.slice(marker).find(event=>event.type==='approval.requested');
    assert.ok(secondApproval?.type==='approval.requested');
    assert.notEqual(secondApproval.approval.id,firstApproval.approval.id);
    assert.equal(requests().length,1,'Supplementing the request is not permission to execute it');
    await backend.approve(secondApproval.approval.id,'accept');
    await until(()=>events.slice(marker).some(event=>event.type==='run.finished'));
    const candidate=resultSince(marker);
    assert.equal(candidate.status,'awaiting_review');
    assert.notEqual(candidate.runId,stopped.runId);
    assert.equal(candidate.modelCalls,1);
    assert.equal(candidate.limits.maxInputChars,12000);
    assert.equal(candidate.limits.maxModelCalls,1);
    const wire=requests();
    assert.equal(wire.length,2);
    assert.deepEqual(wire[0].tools,[]);assert.deepEqual(wire[1].tools,[]);
    assert.ok(JSON.stringify(wire[1]).includes(JSON.stringify(clarified.options.request).slice(1,-1)),
      'The original request plus the exact author clarification reaches the actual Native HTTP request');
    assert.deepEqual(readFileSync(savedPath),saved,'New task leaves the stopped task and its spent budget unchanged');
    const inspect=`import json,sys\nfrom pathlib import Path\nfrom lg_cli.project_store import ProjectStore\ns=ProjectStore(Path(sys.argv[1]))\nprint(json.dumps({'active':s.get_document('one').content,'versions':len(s.list_versions('one')),'candidate':s.get_version('one',int(sys.argv[2])).content}))`;
    const inspected=JSON.parse(execFileSync(python!,['-c',inspect,workspace,String(candidate.candidate!.versionNumber)],{env,encoding:'utf8'}));
    assert.deepEqual(inspected,{active:'Prefix. SELECTED. Suffix.',versions:2,candidate:'Prefix. REVISED. Suffix.'});
  }finally{await backend.dispose();}
});
