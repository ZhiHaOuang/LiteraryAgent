// LG event-contract fixture: expiry is scoped by session and exact request ID.
const {createInterface}=require('node:readline');
let cursor=1;
const send=value=>process.stdout.write(JSON.stringify({jsonrpc:'2.0',...value})+'\n');
const event=(params,sessionId='thread')=>send({method:'event',params:{cursor:++cursor,sessionId,method:'client/event',params:{sessionId,...params}}});
const book={name:'fixture',overview:'',positioning:'',targetWords:0,writtenWords:0,acceptedWords:0,chapters:[],volumes:[],characters:[],relationships:[],settings:[],foreshadowing:[],tasks:[],pending:[],source:'backend'};
createInterface({input:process.stdin}).on('line',line=>{
 const m=JSON.parse(line),reply=result=>send({id:m.id,result});
 if(m.method==='initialize')reply({protocolVersion:'lg.backend.v1',workspace:process.cwd(),capabilities:['bookLibrary','clientEvents']});
 else if(m.method==='session/create')reply({sessionId:'thread'});
 else if(m.method==='session/snapshot')reply({sessionId:'thread',items:[],cursor:1});
 else if(m.method==='book/dashboard')reply(book);
 else if(m.method==='session/list')reply({sessions:[]});
 else if(m.method==='run/start'){
  reply({runId:'turn'});event({type:'run.started',runId:'turn',text:'test'});
  event({type:'approval.requested',approval:{id:'operation',runId:'turn',title:'Test approval',details:'preview',choices:['accept','decline'],state:'pending'}});
  event({type:'userInput.requested',request:{id:'question',runId:'turn',questions:[{id:'q',label:'Continue?',options:[]}]}});
  event({type:'approval.expired',requestId:'operation'},'another-thread');
  event({type:'approval.expired',requestId:'operation'});
  event({type:'userInput.resolved',requestId:'question'});
 }else throw Error('Expired requests must never be sent: '+m.method);
});
