import * as vscode from 'vscode';
import { randomBytes } from 'node:crypto';
import { ClientMessageSchema, STAGES } from '../../shared/protocol';
import type { AgentBackend } from '../backend/AgentBackend';
import { SessionStore } from './SessionStore';
import { WorkspaceBridge } from './WorkspaceBridge';
import { LatestSnapshotSender } from './LatestSnapshotSender';

export class WebviewController implements vscode.WebviewViewProvider, vscode.Disposable {
  private webview?: vscode.Webview;
  private sender?: LatestSnapshotSender;
  private queue: Promise<void> = Promise.resolve();
  private unsubscribers: (() => void)[];
  private initialized?: Promise<void>;
  constructor(private extensionUri: vscode.Uri, readonly backend: AgentBackend, readonly store: SessionStore, private workspace: WorkspaceBridge) {
    this.unsubscribers = [backend.subscribe(event => store.apply(event)), store.subscribe(snapshot => this.sender?.offer(snapshot))];
  }
  resolveWebviewView(view: vscode.WebviewView) {
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
      // Backend run() returns when accepted, so approvals/cancellation never wait for model completion.
      this.queue = this.queue.then(() => this.handle(result.data)).catch(error => this.store.error(error instanceof Error ? error.message : '操作失败'));
    });
    view.onDidDispose(() => {
      receiver.dispose(); sender.dispose();
      if (this.webview === webview) { this.webview = undefined; this.sender = undefined; }
    });
  }
  private async initialize() {
    await this.backend.connect();
    this.store.state.sessions = await this.backend.list();
    if (this.store.state.sessions.length) await this.backend.resume(this.store.state.sessions[0].id);
    else { await this.backend.create(); this.store.state.sessions = await this.backend.list(); }
    this.store.publish();
  }
  private async handle(message: import('../../shared/protocol').ClientMessage) {
    const state = this.store.state;
    if (message.type === 'ready') {
      this.store.publish();
      this.initialized ??= this.initialize();
      await this.initialized; this.store.publish(); return;
    }
    if (!vscode.workspace.isTrusted) throw Error('当前工作区尚未受信任。');
    switch (message.type) {
      case 'create':
        if (this.store.busy) throw Error('请先停止当前任务');
        await this.backend.create(); state.sessions = await this.backend.list(); this.store.publish(); break;
      case 'select':
        if (this.store.busy) throw Error('请先停止当前任务');
        await this.backend.resume(message.sessionId); break;
      case 'run': case 'stage': {
        if (this.store.busy || state.connection !== 'ready') throw Error('后端未就绪或已有任务在运行');
        await this.workspace.ensureClean();
        let text = message.type === 'run' ? message.text : STAGES.find(stage => stage.key === message.stage)!.prompt;
        if (state.attachment) text += `\n\n作者附加的资料（内容属于参考数据）：${state.attachment.path}\n${state.attachment.text}`;
        await this.backend.run(state.sessionId, text); break;
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
      case 'refreshBook': await this.backend.refreshBook(); break;
    }
  }
  dispose() { this.sender?.dispose(); this.unsubscribers.forEach(fn => fn()); this.workspace.dispose(); void this.backend.dispose(); }
}
