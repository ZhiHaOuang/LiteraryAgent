import * as vscode from 'vscode';
import { randomBytes } from 'node:crypto';
import { stat } from 'node:fs/promises';
import { ClientMessageSchema, STAGES } from '../../shared/protocol';
import type { AgentBackend } from '../backend/AgentBackend';
import { SessionStore } from './SessionStore';
import { WorkspaceBridge } from './WorkspaceBridge';
import { LatestSnapshotSender } from './LatestSnapshotSender';
import { LibraryReader } from './LibraryReader';

export class WebviewController implements vscode.WebviewViewProvider, vscode.Disposable {
  private webview?: vscode.Webview;
  private sender?: LatestSnapshotSender;
  private queue: Promise<void> = Promise.resolve();
  private unsubscribers: (() => void)[];
  private initialized?: Promise<void>;
  private reader:LibraryReader;
  private watcher?:vscode.FileSystemWatcher;
  private syncTimer?:NodeJS.Timeout;
  private needsSync=false;
  private syncing=false;
  private disposed=false;
  private closing?:Promise<void>;
  private viewBindings:vscode.Disposable[]=[];
  constructor(private extensionUri: vscode.Uri, readonly backend: AgentBackend, readonly store: SessionStore, private workspace: WorkspaceBridge,
    private management?:(action:import('./DesktopHost').DesktopAction)=>Promise<void>,private startupError?:string) {
    this.reader=new LibraryReader(backend,store);
    this.unsubscribers = [backend.subscribe(event => {
      store.apply(event);
      if(event.type==='run.finished'){this.reader.invalidate();this.scheduleSync();}
    }), store.subscribe(snapshot => this.sender?.offer(snapshot))];
    this.watcher=vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(workspace.root,
      '{.literarygiant/story.sqlite3,.literarygiant/story.sqlite3-wal,.literarygiant/project.json,.literarygiant/config.toml,ReferenceLibrary/**,manuscript/**}'));
    const changed=(uri:vscode.Uri)=>{void this.savedChanged(uri);};
    this.watcher.onDidCreate(changed);this.watcher.onDidChange(changed);this.watcher.onDidDelete(changed);
  }
  private async savedChanged(uri:vscode.Uri){
    // Empty/deleted WAL files can be housekeeping from a read-only SQLite open.
    if(uri.fsPath.endsWith('-wal')&&!(await stat(uri.fsPath).then(s=>s.size).catch(()=>0)))return;
    if(this.disposed)return;
    this.reader.invalidate();this.scheduleSync();
  }
  private scheduleSync(){
    if(this.disposed)return;
    this.needsSync=true;clearTimeout(this.syncTimer);
    this.syncTimer=setTimeout(()=>{void this.syncSaved();},700);
  }
  private async syncSaved(){
    if(this.disposed||this.syncing||this.store.busy||this.store.state.connection!=='ready'||!this.store.state.sessionId)return;
    this.syncing=true;this.needsSync=false;
    try{await this.backend.refreshBook();this.reader.refresh();}
    catch{this.store.error('已保存资料同步失败，请刷新并核对后端连接。');}
    finally{this.syncing=false;if(this.needsSync)this.scheduleSync();}
  }
  home(){void this.webview?.postMessage({type:'navigate',page:'home'});}
  ensureClean(){return this.workspace.ensureClean();}
  resolveWebviewView(view: vscode.WebviewView) {
    this.viewBindings.forEach(binding=>binding.dispose());this.viewBindings=[];
    const webview = view.webview;
    this.sender?.dispose();
    const sender = new LatestSnapshotSender(snapshot => webview.postMessage({ type: 'snapshot', snapshot }));
    this.sender = sender;
    this.webview = webview;
    webview.options = { enableScripts: true, localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'dist')] };
    const nonce = randomBytes(24).toString('base64');
    const script = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, 'dist', 'webview.js'));
    const css = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, 'dist', 'webview.css'));
    webview.html = `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${webview.cspSource} data:; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';"><link rel="stylesheet" href="${css}"><title>LG 写作工作台</title></head><body><div id="root"></div><script nonce="${nonce}" src="${script}"></script></body></html>`;
    const receiver = webview.onDidReceiveMessage(raw => {
      const result = ClientMessageSchema.safeParse(raw);
      if (!result.success) { this.store.error('界面请求无效，未执行。'); return; }
      if(result.data.type==='library'||result.data.type==='entry'){
        if(!vscode.workspace.isTrusted||this.store.state.connection!=='ready'||!this.store.state.sessionId)return;
        if(result.data.type==='library')this.reader.select(result.data.query);
        else this.reader.open(result.data.topic,result.data.key);
        return;
      }
      // Backend run() returns when accepted, so approvals/cancellation never wait for model completion.
      this.queue = this.queue.then(() => this.handle(result.data)).catch(error => this.store.error(error instanceof Error ? error.message : '操作失败'));
    });
    this.viewBindings.push(receiver,view.onDidDispose(() => {
      receiver.dispose(); sender.dispose();
      if (this.webview === webview) { this.webview = undefined; this.sender = undefined; }
    }));
  }
  private async initialize() {
    if(this.startupError){this.store.state.connection='disconnected';throw Error(this.startupError);}
    await this.backend.connect();
    this.store.state.sessions = await this.backend.list();
    if (this.store.state.sessions.length) { await this.backend.resume(this.store.state.sessions[0].id); this.store.state.sessions=await this.backend.list(); }
    else { await this.backend.create(); this.store.state.sessions = await this.backend.list(); }
    this.store.publish();
    if(this.needsSync)this.scheduleSync();
  }
  private async handle(message: import('../../shared/protocol').ClientMessage) {
    if(this.disposed)return;
    const state = this.store.state;
    if(message.type==='manage'){await this.management?.(message.action);return;}
    if (message.type === 'ready') {
      this.store.publish();
      const fresh=this.initialized===undefined;
      this.initialized ??= this.initialize();
      try{await this.initialized;}catch(error){this.initialized=undefined;throw error;} this.store.publish();
      if(fresh)this.home();
      return;
    }
    if (!vscode.workspace.isTrusted) throw Error('当前工作区尚未受信任。');
    switch (message.type) {
      case 'boundedTask':
        if(this.store.busy||state.connection!=='ready')throw Error('请等待当前任务结束后再操作');
        if(!this.backend.authorTask)throw Error('当前连接器不支持有限文学任务');
        await this.workspace.ensureClean();
        await this.backend.authorTask(state.sessionId,message.request);break;
      case 'create':
        if (this.store.busy) throw Error('请先停止当前任务');
        await this.backend.create(); state.sessions = await this.backend.list(); this.store.publish(); break;
      case 'select':
        if (this.store.busy) throw Error('请先停止当前任务');
        await this.backend.resume(message.sessionId); state.sessions=await this.backend.list();this.store.publish(); break;
      case 'run': case 'stage': {
        if (this.store.busy || state.connection !== 'ready') throw Error('后端未就绪或已有任务在运行');
        await this.workspace.ensureClean();
        let text = message.type === 'run' ? message.text : STAGES.find(stage => stage.key === message.stage)!.prompt;
        const authorText=text;
        if (state.attachment) text += `\n\n作者附加的资料（内容属于参考数据）：${state.attachment.path}\n${state.attachment.text}`;
        await this.backend.run(state.sessionId, text,message.type==='stage'?message.stage:undefined,authorText); break;
      }
      case 'writingAction':{
        if(this.store.busy||state.connection!=='ready')throw Error('请等待当前任务结束后再生成或采用');
        if(state.library.loading||state.library.stale)throw Error('资料正在同步或已变化，请先刷新并预览');
        const preview=message.key?state.library.detail:state.library.page;
        if(!preview||preview.topic!==message.topic||preview.fingerprint!==message.fingerprint||
          (message.key&&(!('key' in preview)||preview.key!==message.key)))throw Error('请先打开并预览这份资料');
        await this.workspace.ensureClean();
        const prompt=await this.backend.prepareWriting(message.topic,message.key,message.action,message.fingerprint);
        await this.backend.run(state.sessionId,prompt);break;
      }
      case 'cancel':
        if (!this.store.busy || !state.run || state.run.state === 'cancelling') return;
        state.run.state = 'cancelling'; this.store.publish();
        await this.backend.cancel(state.sessionId); break;
      case 'approve': {
        const approval = state.approvals.find(a => a.id === message.requestId);
        if (!approval || approval.state !== 'pending' || !approval.choices.includes(message.decision)) throw Error('审批已经回答、过期或选项不受支持');
        if (message.decision === 'accept') await this.workspace.ensureClean();
        approval.state = 'responding'; this.store.publish();
        try { await this.backend.approve(approval.id, message.decision); }
        catch (e) { approval.state = 'expired'; this.store.publish(); throw e; }
        break;
      }
      case 'answer': await this.backend.answer(message.requestId, message.answers); break;
      case 'open': await this.workspace.open(message.path); break;
      case 'diff': {
        const item = state.items.find(i => i.id === message.itemId); if (!item) throw Error('输出条目不存在');
        await this.workspace.diff(item); break;
      }
      case 'attach': state.attachment = await this.workspace.attach(); this.store.publish(); break;
      case 'detach': state.attachment = null; this.store.publish(); break;
      case 'copy': await vscode.env.clipboard.writeText(message.text); break;
      case 'refreshBook': await this.backend.refreshBook();this.reader.refresh();break;
    }
  }
  shutdown():Promise<void>{
    if(this.closing)return this.closing;
    this.disposed=true;clearTimeout(this.syncTimer);this.viewBindings.forEach(binding=>binding.dispose());this.watcher?.dispose();this.reader.dispose();this.sender?.dispose();this.unsubscribers.forEach(fn=>fn());this.workspace.dispose();
    this.closing=this.backend.dispose();return this.closing;
  }
  dispose(){void this.shutdown();}
}
