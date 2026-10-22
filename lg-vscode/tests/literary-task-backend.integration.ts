import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,existsSync,readFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {execFileSync} from 'node:child_process';
import {LiteraryBackend} from '../src/backend/LiteraryBackend';
import type {BackendEvent} from '../shared/protocol';

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
