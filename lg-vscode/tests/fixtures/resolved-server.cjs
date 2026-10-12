// Local JSONL peer for request-expiry races; no model, shell commands or writes.
const {createInterface} = require('node:readline');
const send = value => process.stdout.write(JSON.stringify(value) + '\n');
const paginated=process.argv.includes('--paginated');
const thread={id:'thread',cwd:process.cwd(),historyMode:paginated?'paginated':'legacy',turns:[]};
const turn=n=>({id:'turn-'+n,status:'completed',itemsView:process.argv.includes('--summary')?'summary':'full',items:[{id:'answer-'+n,type:'agentMessage',text:'saved answer '+n}]});
if (process.argv.includes('--describe')) {
  send({schema:1, workspace:process.cwd(), model:'fixture', runtimeVersion:'codex-cli 0.154.0', baseInstructions:''});
} else {
  createInterface({input:process.stdin}).on('line', line => {
    const m = JSON.parse(line);
    const reply = result => send({id:m.id,result});
    if (m.method === 'initialize') reply({});
    else if (m.method === 'thread/list') reply(paginated?{data:[{...thread,id:m.params.cursor?'new':'old',name:'Saved session'}],nextCursor:m.params.cursor?null:'list-page-2'}:{data:[],nextCursor:null});
    else if (m.method === 'thread/start') reply({thread:{id:'thread',cwd:process.cwd()}});
    else if (m.method === 'thread/read') {
      if(paginated&&m.params.includeTurns)send({id:m.id,error:{code:-32602,message:'Paginated history requires the list endpoint'}});
      else reply({thread});
    }
    else if (m.method === 'thread/resume') reply({thread});
    else if (m.method === 'thread/turns/list') reply({
      data:m.params.cursor?[turn(1),turn(2)]:[turn(1)],
      nextCursor:!m.params.cursor||process.argv.includes('--repeat-cursor')?'turn-page-2':null,
      backwardsCursor:null,
    });
    else if (m.method === 'mcpServer/tool/call') reply({isError:false,content:[],structuredContent:{
      name:'fixture', overview:'', positioning:'', targetWords:1000000, writtenWords:0, acceptedWords:0,
      chapters:[],volumes:[],characters:[],relationships:[],settings:[],foreshadowing:[],tasks:[],pending:[],source:'backend'
    }});
    else if (m.method === 'turn/start') {
      reply({turn:{id:'turn'}});
      send({method:'turn/started',params:{threadId:'thread',turn:{id:'turn'}}});
      send({id:0,method:'item/commandExecution/requestApproval',params:{threadId:'thread',turnId:'turn',command:'fixture'}});
      send({id:'0',method:'item/tool/requestUserInput',params:{threadId:'thread',turnId:'turn',questions:[{id:'q',question:'Continue?',options:[]}]}});
      send({method:'serverRequest/resolved',params:{threadId:'another-thread',requestId:0}});
      send({method:'serverRequest/resolved',params:{threadId:'thread',requestId:0}});
      send({method:'serverRequest/resolved',params:{threadId:'thread',requestId:0}});
      send({method:'serverRequest/resolved',params:{threadId:'thread',requestId:'0'}});
    } else if (!m.method) {
      // An expired request must not receive any reply from the client.
      send({method:'error',params:{threadId:'thread',error:{codexErrorInfo:'other'}}});
    }
  });
}
