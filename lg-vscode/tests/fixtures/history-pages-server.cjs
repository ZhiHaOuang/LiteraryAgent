// A local LG peer for malformed-page and recovery races; never runs tools.
const {createInterface}=require('node:readline');
const {createHash}=require('node:crypto');
const flag=process.argv[2]||'';
const legacy=flag==='legacy';
const items=[{id:'saved',runId:'saved-run',kind:'assistant',title:'LG',state:'completed',text:legacy?'保存的旧接口回复':'中文🖋历史。'.repeat(50000)}];
const snapshot={sessionId:'thread',items,cursor:2};
const bytes=Buffer.from(JSON.stringify(snapshot));
const size=256*1024,pages=Math.ceil(bytes.length/size),digest=createHash('sha256').update(bytes).digest('hex');
let generation=0;
const send=value=>process.stdout.write(JSON.stringify({jsonrpc:'2.0',...value})+'\n');
const event=(cursor,params)=>send({method:'event',params:{cursor,sessionId:'thread',method:'client/event',params}});
createInterface({input:process.stdin}).on('line',line=>{
  const m=JSON.parse(line),p=m.params||{};
  const reply=result=>send({id:m.id,result});
  if(m.jsonrpc!=='2.0')throw Error('Expected JSON-RPC 2.0');
  if(m.method==='initialize')reply({protocolVersion:'lg.backend.v1',workspace:process.cwd(),capabilities:['bookLibrary','clientEvents',...legacy?[]:['pagedSnapshots']]});
  else if(m.method==='session/list')reply({sessions:[{sessionId:'thread',title:'保存的会话'}]});
  else if(m.method==='session/open')reply({sessionId:'thread'});
  else if(m.method==='session/snapshot'){
    if(!legacy)throw Error('Client must select paged history');
    reply(snapshot);
  }else if(m.method==='session/snapshotPage'){
    if(!p.snapshotId)generation++;
    const index=p.page||0;
    if(flag==='expired'&&index>0){send({id:m.id,error:{code:-32000,message:'Expired snapshot'}});return;}
    const chunk=Buffer.from(bytes.subarray(index*size,(index+1)*size));
    if(flag==='corrupt'&&index===1)chunk[0]^=1;
    const page={sessionId:'thread',snapshotId:'snapshot'+generation,cursor:2,page:index,pages,sha256:digest,
      data:chunk.toString('base64'),nextPage:index+1<pages?index+1:null};
    if(flag==='repeat'&&index===1)page.page=0;
    if(flag==='token'&&index===1)page.snapshotId='another-snapshot';
    if(flag==='cursor'&&index===1)page.cursor=3;
    if(flag==='missing'&&index===0)page.nextPage=null;
    if(flag==='encoding'&&index===0)page.data='*';
    if(flag==='racing'&&generation===2&&index===pages-1){
      event(3,{type:'run.started',sessionId:'thread',runId:'live-run',text:'恢复期间的新请求'});
      event(4,{type:'assistant.delta',sessionId:'thread',runId:'live-run',itemId:'live-answer',delta:'后续内容'});
    }
    reply(page);
  }else if(m.method==='book/dashboard')reply({name:'fixture',overview:'',positioning:'',targetWords:0,writtenWords:0,acceptedWords:0,
    chapters:[],volumes:[],characters:[],relationships:[],settings:[],foreshadowing:[],tasks:[],pending:[],source:'backend'});
  else throw Error('History recovery must not invoke other methods: '+m.method);
});
