import { isAbsolute, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { z } from 'zod';
import { ActiveRunSchema, BackendEventSchema, BookSchema, ItemSchema, LibraryPageSchema, LibraryDetailSchema,
  type Approval, type BackendEvent, type InputRequest, type Item, type Session,
  type LibraryQuery, type LibraryTopic, type WritingAction, type Stage } from '../../shared/protocol';
import { EventBackend, type AgentBackend } from './AgentBackend';
import { RpcTransport, type LaunchSpec, type RpcMessage } from './RpcTransport';

export type LiteraryBackendSettings = {python:string;manifest:string;workspace:string;environment?:string;env?:NodeJS.ProcessEnv;runtimeEngine?:'codex'|'native'};
const SessionReply = z.object({sessionId:z.string().min(1)});
const JournalRecord = z.object({cursor:z.number().int().positive(),sessionId:z.string(),method:z.string(),params:z.unknown()});
const SnapshotReply = z.object({sessionId:z.string(),items:z.array(ItemSchema),cursor:z.number().int().nonnegative(),
  activeRuns:z.array(ActiveRunSchema).optional()});
const SnapshotPage = z.object({sessionId:z.string(),snapshotId:z.string().min(1),cursor:z.number().int().nonnegative(),
  page:z.number().int().nonnegative(),pages:z.number().int().positive(),sha256:z.string().regex(/^[0-9a-f]{64}$/),
  data:z.string().max(350_000),nextPage:z.number().int().nonnegative().nullable()});

/** The React host consumes LG events; runtime-specific messages never reach this connector. */
export class LiteraryBackend extends EventBackend implements AgentBackend {
  private rpc?:RpcTransport;
  private sessionId='';
  private active=new Set<string>();
  private starting=false;
  private cursors=new Map<string,number>();
  private pending=new Map<string,Approval|InputRequest>();
  private bookRefresh?:Promise<void>;
  private pagedSnapshots=false;
  private taskScopes=false;
  private naturalTaskRouting=false;
  private boundedTasks=false;
  private preparedTask?:{prompt:string;task:unknown};
  private recovering=false;
  private deferred:RpcMessage[]=[];
  constructor(private settings:LiteraryBackendSettings){super();}
  protected launch():LaunchSpec {
    const {python,manifest,workspace,environment,env,runtimeEngine}=this.settings;
    if(!isAbsolute(python))throw Error('请指定绝对 Python 路径');
    if(manifest&&!isAbsolute(manifest))throw Error('runtime manifest 必须是绝对路径');
    const args=['-m','lg_cli.backend_stdio','--workspace',workspace];
    if(manifest)args.push('--runtime-manifest',manifest);
    if(environment)args.push('--environment',environment);
    if(runtimeEngine)args.push('--runtime-engine',runtimeEngine);
    return {command:python,args,cwd:workspace,env,jsonRpc:true};
  }
  async connect(){
    this.emit({type:'backend.status',status:'connecting'});
    try{
      this.rpc=new RpcTransport(this.launch(),message=>{
        try{this.message(message);}catch{this.emit({type:'error',message:'LG Backend 事件格式不兼容，连接已关闭'});void this.dispose();}
      },reason=>{
        this.active.clear();this.starting=false;this.pending.clear();
        this.emit({type:'backend.status',status:'disconnected',error:reason});
      });
      this.rpc.start();
      const info=z.object({protocolVersion:z.literal('lg.backend.v1'),workspace:z.string(),capabilities:z.array(z.string()),
        runtime:z.enum(['codex','native']).optional()})
        .parse(await this.request('initialize',{}));
      if(resolve(info.workspace)!==resolve(this.settings.workspace)||!['bookLibrary','clientEvents'].every(c=>info.capabilities.includes(c)))
        throw Error('LG Backend 的作品目录或能力不匹配');
      if(this.settings.runtimeEngine&&info.runtime!==this.settings.runtimeEngine)
        throw Error('LG Backend 未使用指定执行器，连接已关闭；请更新 LG Python 包并核对配置');
      this.pagedSnapshots=info.capabilities.includes('pagedSnapshots');
      this.taskScopes=info.capabilities.includes('taskScopes');
      this.naturalTaskRouting=info.capabilities.includes('naturalTaskRouting');
      this.boundedTasks=info.capabilities.includes('literaryTasks');
      this.emit({type:'backend.status',status:'ready'});
    }catch(error){await this.rpc?.dispose();throw error instanceof z.ZodError?Error('LG Backend 协议不兼容'):error;}
  }
  private request(method:string,params:unknown){
    if(!this.rpc)return Promise.reject(Error('LG Backend 尚未连接'));
    return this.rpc.request(method,params);
  }
  async list():Promise<Session[]>{
    const result=z.object({sessions:z.array(z.object({sessionId:z.string(),title:z.string()})),legacyWarning:z.string().optional()})
      .parse(await this.request('session/list',{}));
    if(result.legacyWarning)this.emit({type:'error',message:result.legacyWarning});
    return result.sessions.map(s=>({id:s.sessionId,title:s.title}));
  }
  private idle(){if(this.active.size||this.starting)throw Error('请先停止当前任务');}
  private async select(sessionId:string){
    this.preparedTask=undefined;
    this.sessionId=sessionId;this.pending.clear();
    this.recovering=true;
    try{
      const result=await this.snapshot(sessionId);
      this.cursors.set(sessionId,result.cursor);
      this.active.clear();for(const run of result.activeRuns??[])this.active.add(run.id);
      this.emit({type:'session.snapshot',sessionId,items:result.items,activeRuns:result.activeRuns});
    }finally{
      this.recovering=false;
      const deferred=this.deferred.splice(0);
      for(const message of deferred)this.message(message);
    }
    await this.refreshBook();
  }
  async create():Promise<Session>{
    this.idle();
    if(this.sessionId)await this.request('session/close',{sessionId:this.sessionId});
    const result=SessionReply.parse(await this.request('session/create',{}));
    await this.select(result.sessionId);
    return {id:result.sessionId,title:'LG 创作会话'};
  }
  async read(sessionId:string):Promise<Item[]>{
    return (await this.snapshot(sessionId)).items;
  }
  private async snapshot(sessionId:string){
    if(!this.pagedSnapshots){
      const result=SnapshotReply.parse(await this.request('session/snapshot',{sessionId}));
      if(result.sessionId!==sessionId)throw Error('LG 返回的会话不匹配');
      return result;
    }
    const first=SnapshotPage.parse(await this.request('session/snapshotPage',{sessionId}));
    const chunks:Buffer[]=[];
    let current=first;
    for(let index=0;index<first.pages;index++){
      if(current.sessionId!==sessionId||current.snapshotId!==first.snapshotId||current.cursor!==first.cursor||
        current.sha256!==first.sha256||current.pages!==first.pages||current.page!==index||
        current.nextPage!==(index+1<first.pages?index+1:null))throw Error('历史分页顺序或快照不一致，恢复已停止');
      const chunk=Buffer.from(current.data,'base64');
      if(chunk.toString('base64')!==current.data)throw Error('历史分页编码无效');
      chunks.push(chunk);
      if(current.nextPage!==null)current=SnapshotPage.parse(await this.request('session/snapshotPage',
        {sessionId,snapshotId:first.snapshotId,page:current.nextPage}));
    }
    const bytes=Buffer.concat(chunks);
    if(createHash('sha256').update(bytes).digest('hex')!==first.sha256)throw Error('历史快照校验失败，恢复已停止');
    const result=SnapshotReply.parse(JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes)));
    if(result.sessionId!==sessionId||result.cursor!==first.cursor)throw Error('历史快照标识不匹配');
    return result;
  }
  async resume(sessionId:string){
    this.idle();
    // Verify the target before releasing ownership of the current session.
    await this.read(sessionId);
    if(this.sessionId&&this.sessionId!==sessionId)await this.request('session/close',{sessionId:this.sessionId});
    const result=SessionReply.parse(await this.request('session/open',{sessionId}));
    await this.select(result.sessionId);
  }
  async run(sessionId:string,text:string,stage?:Stage,authorText?:string){
    this.idle();
    if(sessionId!==this.sessionId)throw Error('会话不匹配');
    this.starting=true;
    const prepared=this.preparedTask;this.preparedTask=undefined;
    const taskOptions=this.taskScopes?(stage?{stage}:prepared?.prompt===text?{task:prepared.task}:{}):{};
    const routeOptions=!stage&&prepared?.prompt!==text&&this.naturalTaskRouting?{routeTask:true,...(authorText?{authorText}:{})}:{};
    try{await this.request('run/start',{sessionId,text,...taskOptions,...routeOptions});}finally{this.starting=false;}
  }
  async cancel(sessionId:string){
    if(sessionId!==this.sessionId)throw Error('会话不匹配');
    await this.request('run/cancel',{sessionId});
  }
  async authorTask(sessionId:string,command:import('../../shared/protocol').AuthorTaskRequest){
    this.idle();
    if(sessionId!==this.sessionId)throw Error('会话不匹配');
    if(!this.boundedTasks)throw Error('当前 LG Python 包尚不支持有限任务，请先核对运行版本');
    const show=(field:string,value:unknown,title:string,key:string)=>{
      this.emit({type:'item.updated',sessionId,item:{id:'bounded-read:'+key,runId:'bounded-read:'+key,
        kind:'notice',title,text:'已读取当前保存状态',state:'completed',structured:{[field]:value}}});
    };
    if(command.action==='read'){
      show('literaryTask',await this.request('task/read',{taskId:command.taskId}),'文学任务状态',command.taskId);return;
    }
    if(command.action==='sources'){
      show('sourceRevalidation',await this.request('sources/check',{document:command.document,
        ...(command.version?{version:command.version}:{})}),'来源复核 · 已知直接关联',command.document);return;
    }
    if(command.action==='receipt'){
      show('operationReceipt',await this.request('operation/read',{operationId:command.operationId}),
        '正式提交状态',command.operationId);return;
    }
    this.starting=true;
    try{
      if(command.action==='start'){
        const preview=z.object({status:z.string(),fingerprint:z.string()}).passthrough()
          .parse(await this.request('task/preview',{options:command.options}));
        if(preview.status!=='ready'){show('literaryTask',preview,'文学任务未开始','preview');return;}
        await this.request('task/start',{sessionId,options:command.options,expectedFingerprint:preview.fingerprint});
      }else if(command.action==='narrative'){
        const preview=z.object({status:z.string(),fingerprint:z.string().optional()}).passthrough()
          .parse(await this.request('narrative/preview',{options:command.options}));
        if(preview.status!=='ready'){show('narrativeResult',preview,'有限认知推演未开始','narrative');return;}
        await this.request('narrative/run',{sessionId,options:command.options,expectedFingerprint:preview.fingerprint});
      }else if(command.action==='reviewAdopt'){
        await this.request('creative/apply',{sessionId,action:'acceptVersion',document:command.document,
          version:command.version,review:true});
      }else{
        await this.request('task/'+command.action,{sessionId,taskId:command.taskId,
          expectedFingerprint:command.fingerprint,...(command.action==='select'?{optionId:command.optionId}:{})});
      }
    }finally{this.starting=false;}
  }
  async approve(requestId:string,decision:Approval['choices'][number]){
    const pending=this.pending.get(requestId);
    if(!pending||!('choices' in pending)||!pending.choices.includes(decision))throw Error('审批已回答或已过期');
    this.pending.delete(requestId);
    await this.request('approval/resolve',{approvalId:requestId,result:{decision}});
  }
  async answer(requestId:string,answers:Record<string,string>){
    const pending=this.pending.get(requestId);
    if(!pending||!('questions' in pending))throw Error('问题已回答或已过期');
    const ids=pending.questions.map(q=>q.id);
    if(Object.keys(answers).length!==ids.length||ids.some(id=>typeof answers[id]!=='string'||answers[id].length>10_000))throw Error('问题回答不完整或无效');
    this.pending.delete(requestId);
    await this.request('approval/resolve',{approvalId:requestId,result:{answers:Object.fromEntries(ids.map(id=>[id,{answers:[answers[id]]}]))}});
  }
  async refreshBook(){
    this.bookRefresh??=this.loadBook().finally(()=>{this.bookRefresh=undefined;});
    await this.bookRefresh;
  }
  private async loadBook(){this.emit({type:'book.updated',book:BookSchema.parse(await this.request('book/dashboard',{}))});}
  async readLibrary(query:LibraryQuery){return LibraryPageSchema.parse(await this.request('library/list',query));}
  async readEntry(topic:LibraryTopic,key:string){return LibraryDetailSchema.parse(await this.request('library/read',{topic,key}));}
  async prepareWriting(topic:LibraryTopic,key:string,action:WritingAction,fingerprint:string){
    const result=z.object({prompt:z.string().min(1).max(30_000),fingerprint:z.literal(fingerprint),task:z.unknown().optional()})
      .parse(await this.request('library/actionContext',{topic,key,action,fingerprint}));
    this.preparedTask=result.task?{prompt:result.prompt,task:result.task}:undefined;
    return result.prompt;
  }
  private message(message:RpcMessage){
    if(message.method!=='event')return;
    if(this.recovering){this.deferred.push(message);return;}
    const record=JournalRecord.parse(message.params);
    if(record.sessionId!==this.sessionId)return;
    if(record.cursor<=(this.cursors.get(record.sessionId)??0))return;
    this.cursors.set(record.sessionId,record.cursor);
    if(record.method!=='client/event')return;
    const event=BackendEventSchema.parse(record.params);
    if('sessionId' in event&&event.sessionId!==record.sessionId)throw Error('事件会话不匹配');
    if(event.type==='run.started')this.active.add(event.runId);
    if(event.type==='run.finished'){
      this.active.delete(event.runId);
      for(const [id,pending] of this.pending)if(pending.runId===event.runId)this.pending.delete(id);
      void this.refreshBook().catch(()=>this.emit({type:'error',message:'作品概况读取失败，请刷新后核对'}));
    }
    if(event.type==='approval.requested')this.pending.set(event.approval.id,event.approval);
    if(event.type==='userInput.requested')this.pending.set(event.request.id,event.request);
    if(event.type==='approval.resolved'||event.type==='approval.expired'||event.type==='userInput.resolved')this.pending.delete(event.requestId);
    this.emit(event);
  }
  async dispose(){await this.rpc?.dispose();}
}
