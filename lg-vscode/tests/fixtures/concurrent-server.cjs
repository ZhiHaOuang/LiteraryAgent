// Deterministic LG peer for concurrent ownership; no model or writing tools.
const {createInterface}=require('node:readline');
const restore=process.argv[2]==='restore';
let cursor=1,started=false;
const send=value=>process.stdout.write(JSON.stringify({jsonrpc:'2.0',...value})+'\n');
const event=params=>send({method:'event',params:{cursor:++cursor,sessionId:'thread',method:'client/event',params:{sessionId:'thread',...params}}});
const book={name:'fixture',overview:'',positioning:'',targetWords:0,writtenWords:0,acceptedWords:0,
  chapters:[],volumes:[],characters:[],relationships:[],settings:[],foreshadowing:[],tasks:[],pending:[],source:'backend'};
createInterface({input:process.stdin}).on('line',line=>{
  const m=JSON.parse(line),p=m.params||{},reply=result=>send({id:m.id,result});
  if(m.method==='initialize')reply({protocolVersion:'lg.backend.v1',workspace:process.cwd(),capabilities:['bookLibrary','clientEvents']});
  else if(m.method==='session/create'||m.method==='session/open')reply({sessionId:'thread'});
  else if(m.method==='session/close')reply({});
  else if(m.method==='session/snapshot')reply({sessionId:'thread',items:[],cursor:1,
    activeRuns:restore?[{id:'workflow',state:'running'},{id:'agent',state:'running'}]:[]});
  else if(m.method==='book/dashboard'){
    reply(book);
    if(restore&&!started){started=true;event({type:'run.finished',runId:'agent',status:'completed'});}
  }else if(m.method==='run/start'){
    if(started)throw Error('Client must remain busy until workflow ends');
    started=true;reply({runId:'agent'});
    event({type:'run.started',runId:'workflow',text:'planning'});
    event({type:'run.started',runId:'agent',text:'direction'});
    event({type:'approval.requested',approval:{id:'pw',runId:'workflow',title:'exact approval',details:'preview',choices:['accept','decline'],state:'pending'}});
    event({type:'run.finished',runId:'agent',status:'completed'});
  }else if(m.method==='approval/resolve'){
    if(p.approvalId!=='pw')throw Error('Wrong approval owner');
    reply({});event({type:'approval.resolved',requestId:'pw'});
    event({type:'run.finished',runId:'workflow',status:'completed'});
  }else throw Error('Unexpected LG method: '+m.method);
});
