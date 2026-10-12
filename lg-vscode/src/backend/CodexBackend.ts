import { execFile } from 'node:child_process';
import { isAbsolute, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { z } from 'zod';
import { BookSchema, LibraryPageSchema, LibraryDetailSchema, type LibraryQuery, type LibraryTopic, type WritingAction, type Approval, type InputRequest, type Item, type Session } from '../../shared/protocol';
import { EventBackend, type AgentBackend } from './AgentBackend';
import { RpcTransport, type RpcId, type RpcMessage, type LaunchSpec } from './RpcTransport';
import { array, record, text, normalizeItem, errorCategory } from './normalize';
import type { ThreadStartParams } from './generated/v2/ThreadStartParams';
import type { ThreadStartResponse } from './generated/v2/ThreadStartResponse';
import type { ThreadResumeResponse } from './generated/v2/ThreadResumeResponse';
import type { ThreadReadResponse } from './generated/v2/ThreadReadResponse';
import type { ThreadListResponse } from './generated/v2/ThreadListResponse';
import type { TurnStartParams } from './generated/v2/TurnStartParams';
import type { TurnStartResponse } from './generated/v2/TurnStartResponse';
import type { McpServerToolCallResponse } from './generated/v2/McpServerToolCallResponse';
import type { Thread } from './generated/v2/Thread';
import type { Turn } from './generated/v2/Turn';
import type { ThreadTurnsListResponse } from './generated/v2/ThreadTurnsListResponse';

const Description = z.object({ schema: z.literal(1), workspace: z.string(), model: z.string(),
  runtimeVersion: z.literal('codex-cli 0.154.0'), baseInstructions: z.string() });
type Description = z.infer<typeof Description>;
type PendingRequest = { rpcId: RpcId; threadId: string; runId: string; kind: 'decision' | 'elicitation' | 'input'; questionIds?: string[] };
export type BackendSettings = { python: string; manifest: string; workspace: string; env?: NodeJS.ProcessEnv };

export class CodexBackend extends EventBackend implements AgentBackend {
  private rpc?: RpcTransport;
  private description?: Description;
  private sessionId = '';
  private active?: { id: string; text: string; cancelling: boolean };
  private pending = new Map<string, PendingRequest>();
  private finished = new Set<string>();
  private cancelTimer?: NodeJS.Timeout;
  private bookRefresh?: Promise<void>;
  private turnStarted: Promise<void> = Promise.resolve();
  private markTurnStarted?: () => void;
  constructor(private settings: BackendSettings) { super(); }
  protected launch(): LaunchSpec {
    const { python, manifest, workspace, env } = this.settings;
    if (!isAbsolute(python) || !isAbsolute(manifest)) throw Error('请在用户设置中指定绝对 Python 路径与 runtime manifest');
    return { command: python, args: ['-m', 'lg_cli.app_server', '--workspace', workspace, '--runtime-manifest', manifest], cwd: workspace, env };
  }
  async connect() {
    const spec = this.launch();
    this.emit({ type: 'backend.status', status: 'connecting' });
    try {
      const raw = await new Promise<string>((resolve, reject) => execFile(spec.command, [...spec.args, '--describe'],
        { cwd: spec.cwd, env: spec.env, timeout: 45_000, maxBuffer: 1024 * 1024, windowsHide: true },
        (error, stdout) => error ? reject(Error('LG 启动校验失败；请检查运行时、Python 包和模型配置')) : resolve(stdout)));
      this.description = Description.parse(JSON.parse(raw));
      if (resolve(this.description.workspace) !== resolve(this.settings.workspace)) throw Error('LG 返回的作品目录不匹配');
      this.rpc = new RpcTransport(spec, message => { void this.message(message).catch(() => {
        this.emit({ type: 'error', message: '后端事件不符合当前兼容版本，连接已关闭' }); void this.dispose();
      }); }, reason => {
        clearTimeout(this.cancelTimer); this.pending.clear(); this.active = undefined;
        this.emit({ type: 'backend.status', status: 'disconnected', error: reason });
      });
      this.rpc.start();
      await this.rpc.request('initialize', { clientInfo: { name: 'literarygiant_vscode', title: 'LG Writing Workbench', version: '0.2.0' }, capabilities: { experimentalApi: true } });
      await this.rpc.notify('initialized', {});
      this.emit({ type: 'backend.status', status: 'ready' });
    } catch (error) {
      this.emit({ type: 'backend.status', status: 'disconnected', error: error instanceof z.ZodError ? 'LG 启动描述或运行时版本不兼容' : error instanceof Error ? error.message : 'LG 连接失败' });
      await this.rpc?.dispose(); throw error instanceof z.ZodError ? Error('LG 启动描述或运行时版本不兼容') : error;
    }
  }
  private request<T>(method: string, params: unknown): Promise<T> {
    if (!this.rpc) return Promise.reject(Error('LG 后端尚未连接'));
    return this.rpc.request<T>(method, params);
  }
  private threadParams(): ThreadStartParams {
    if (!this.description) throw Error('LG 启动校验尚未完成');
    return { cwd: this.description.workspace, model: this.description.model || null, sandbox: 'read-only',
      approvalPolicy: 'on-request', approvalsReviewer: 'user', baseInstructions: this.description.baseInstructions };
  }
  async list(): Promise<Session[]> {
    const sessions: Session[] = [];
    let cursor: string | null = null;
    const cursors = new Set<string>();
    do {
      const result: ThreadListResponse = await this.request('thread/list', { cwd: this.settings.workspace, limit: 100, cursor,
        sourceKinds: ['appServer', 'vscode'], sortKey: 'updated_at' });
      for (const t of result.data) if (resolve(t.cwd) === resolve(this.settings.workspace) && !t.parentThreadId)
        sessions.push({ id: t.id, title: t.name || t.preview.slice(0, 60) || 'LG 创作会话' });
      cursor = result.nextCursor;
      if (cursor && cursors.has(cursor)) throw Error('后端重复返回会话游标，未将不完整列表视为完整历史');
      if (cursor) cursors.add(cursor);
    } while (cursor);
    return sessions;
  }
  async create(): Promise<Session> {
    if (this.active) throw Error('请先停止当前任务');
    const result = await this.request<ThreadStartResponse>('thread/start', { ...this.threadParams(), ephemeral: false });
    this.sessionId = result.thread.id;
    this.emit({ type: 'session.snapshot', sessionId: this.sessionId, items: [] });
    await this.refreshBook();
    return { id: this.sessionId, title: 'LG 创作会话' };
  }
  async read(sessionId: string): Promise<Item[]> {
    const thread = await this.ownedThread(sessionId);
    return (await this.history(thread)).flatMap(turn => turn.items.map(i => normalizeItem(i, turn.id, turn.status !== 'inProgress')).filter((i): i is Item => !!i));
  }
  private async ownedThread(sessionId: string): Promise<Thread> {
    const result = await this.request<ThreadReadResponse>('thread/read', { threadId: sessionId, includeTurns: false });
    if (result.thread.id !== sessionId || resolve(result.thread.cwd) !== resolve(this.settings.workspace)) throw Error('会话不属于当前作品');
    return result.thread;
  }
  private async history(thread: Thread): Promise<Turn[]> {
    if (thread.historyMode !== 'paginated') {
      const result = await this.request<ThreadReadResponse>('thread/read', { threadId: thread.id, includeTurns: true });
      if (result.thread.id !== thread.id || resolve(result.thread.cwd) !== resolve(this.settings.workspace)) throw Error('会话不属于当前作品');
      return result.thread.turns;
    }
    const turns = new Map<string, Turn>(), cursors = new Set<string>();
    let cursor: string | null = null;
    do {
      const page: ThreadTurnsListResponse = await this.request('thread/turns/list', {threadId: thread.id, cursor, limit: 100, sortDirection: 'asc', itemsView: 'full'});
      for (const turn of page.data) {
        if (turn.itemsView !== 'full') throw Error('后端没有返回完整历史内容，请核对协议版本');
        turns.set(turn.id, turn);
      }
      cursor = page.nextCursor;
      if (cursor && cursors.has(cursor)) throw Error('后端重复返回历史游标，恢复已停止');
      if (cursor) cursors.add(cursor);
    } while (cursor);
    return [...turns.values()];
  }
  async resume(sessionId: string) {
    if (this.active) throw Error('请先停止当前任务');
    // Verify ownership before changing the running thread.
    await this.ownedThread(sessionId);
    const result = await this.request<ThreadResumeResponse>('thread/resume', { ...this.threadParams(), threadId: sessionId });
    if (result.thread.id !== sessionId || resolve(result.thread.cwd) !== resolve(this.settings.workspace)) throw Error('恢复的会话不属于当前作品');
    const turns = result.thread.historyMode === 'paginated' ? await this.history(result.thread) : result.thread.turns;
    this.sessionId = result.thread.id;
    const items = turns.flatMap(turn => turn.items.map(i => normalizeItem(i, turn.id, turn.status !== 'inProgress')).filter((i): i is Item => !!i));
    this.emit({ type: 'session.snapshot', sessionId: this.sessionId, items });
    // Recovered history is never re-executed. A live in-progress turn requires interruption first.
    const current = turns.find(t => t.status === 'inProgress');
    if (current) {
      this.active = { id: current.id, text: '', cancelling: true };
      this.emit({ type: 'run.started', sessionId: this.sessionId, runId: current.id, text: '恢复中的任务' });
      await this.cancel(this.sessionId);
    }
    await this.refreshBook();
  }
  async run(sessionId: string, prompt: string) {
    if (sessionId !== this.sessionId || this.active) throw Error('会话不匹配或已有任务运行');
    await this.bookRefresh;
    this.turnStarted = new Promise(resolve => { this.markTurnStarted = resolve; });
    this.active = { id: '', text: prompt, cancelling: false };
    try {
      const params: TurnStartParams = { threadId: sessionId, input: [{ type: 'text', text: prompt, text_elements: [] }] };
      const result = await this.request<TurnStartResponse>('turn/start', params);
      this.started(result.turn.id);
    } catch (error) { this.markTurnStarted?.(); this.active = undefined; throw error; }
  }
  private started(runId: string) {
    if (!this.active || this.active.id === runId || this.finished.has(runId)) return;
    this.active.id = runId;
    this.emit({ type: 'run.started', sessionId: this.sessionId, runId, text: this.active.text });
  }
  async cancel(sessionId: string) {
    if (sessionId !== this.sessionId || !this.active?.id) throw Error('没有可取消的已启动任务');
    this.active.cancelling = true;
    // turn/start can reply before the server observes the queued turn. Interrupt only
    // after its start notification, otherwise this version returns "no active turn".
    let startupTimer: NodeJS.Timeout | undefined;
    try {
      await Promise.race([this.turnStarted, new Promise<void>((_, reject) => {
        startupTimer = setTimeout(() => reject(Error('任务启动未确认，已关闭连接')), 15_000);
      })]);
    } catch (error) { await this.dispose(); throw error; }
    finally { clearTimeout(startupTimer); }
    if (!this.active) return;
    // Cancel any unanswered elicitation before interrupting, so a queued click cannot authorize it.
    for (const [id, request] of this.pending) if (request.runId === this.active.id) {
      this.pending.delete(id);
      await this.rpc!.respond(request.rpcId, request.kind === 'elicitation' ? { action: 'cancel', content: null, _meta: null } : request.kind === 'input' ? { answers: {} } : { decision: 'cancel' });
    }
    await this.request('turn/interrupt', { threadId: sessionId, turnId: this.active.id });
    if (this.active) this.cancelTimer = setTimeout(() => {
      this.emit({ type: 'error', message: '尚未收到取消完成事件；已关闭连接，恢复历史后确认结果。' }); void this.dispose();
    }, 15_000);
  }
  async approve(id: string, decision: Approval['choices'][number]) {
    const request = this.pending.get(id);
    if (!request || request.kind === 'input' || this.active?.cancelling) throw Error('审批已回答、过期或正在取消');
    this.pending.delete(id);
    await this.rpc!.respond(request.rpcId, request.kind === 'elicitation'
      ? { action: decision, content: decision === 'accept' ? { confirm: true } : null, _meta: null }
      : { decision });
    this.emit({ type: 'approval.resolved', sessionId: request.threadId, requestId: id });
  }
  async answer(id: string, answers: Record<string, string>) {
    const request = this.pending.get(id);
    if (!request || request.kind !== 'input' || this.active?.cancelling) throw Error('问题已回答或过期');
    if (Object.keys(answers).some(key => !request.questionIds?.includes(key)) || request.questionIds?.some(key => !answers[key]?.trim())) throw Error('请回答所有问题');
    this.pending.delete(id);
    await this.rpc!.respond(request.rpcId, { answers: Object.fromEntries(Object.entries(answers).map(([key, value]) => [key, { answers: [value] }])) });
    this.emit({ type: 'userInput.resolved', sessionId: request.threadId, requestId: id });
  }
  refreshBook(): Promise<void> {
    if (!this.sessionId || this.active) return Promise.resolve();
    this.bookRefresh ??= this.loadBook().finally(() => { this.bookRefresh = undefined; });
    return this.bookRefresh;
  }
  private async readTool(tool:string,args:unknown):Promise<unknown> {
    if (!this.sessionId) throw Error('请等待 LG 作品会话就绪');
    const result=await this.request<McpServerToolCallResponse>('mcpServer/tool/call',{threadId:this.sessionId,server:'lg_writing',tool,arguments:args});
    if(result.isError)throw Error('作品资料已变化或读取失败，请刷新资料并核对配套 LG Python 版本。');
    if(result.structuredContent)return result.structuredContent;
    return JSON.parse(text(result.content.map(record).find(c=>c.type==='text')?.text));
  }
  async readLibrary(query:LibraryQuery){return LibraryPageSchema.parse(await this.readTool('book_library',query));}
  async readEntry(topic:LibraryTopic,key:string){return LibraryDetailSchema.parse(await this.readTool('book_entry',{topic,key}));}
  async prepareWriting(topic:LibraryTopic,key:string,action:WritingAction,fingerprint:string){
    const result=z.object({prompt:z.string().min(1).max(30_000),fingerprint:z.literal(fingerprint)}).parse(
      await this.readTool('book_action_context',{topic,key,action,expected_fingerprint:fingerprint}));
    return result.prompt;
  }
  private async loadBook() {
    try {
      const result = await this.request<McpServerToolCallResponse>('mcpServer/tool/call', { threadId: this.sessionId, server: 'lg_writing', tool: 'book_dashboard', arguments: {} });
      if (result.isError) throw Error('LG 作品概况读取失败');
      let data = result.structuredContent;
      if (!data) {
        const part = result.content.map(record).find(c => c.type === 'text');
        data = JSON.parse(text(part?.text));
      }
      this.emit({ type: 'book.updated', book: BookSchema.parse(data) });
    } catch { this.emit({ type: 'error', message: '无法读取 LG 作品概况；请确认 Python/MCP 版本与连接状态。' }); }
  }
  protected async message(message: RpcMessage) {
    if (message.id !== undefined && message.method) { await this.serverRequest(message); return; }
    const p = record(message.params), sessionId = text(p.threadId);
    if (sessionId !== this.sessionId) return;
    if (message.method === 'serverRequest/resolved') {
      const rpcId = p.requestId;
      if (typeof rpcId !== 'string' && typeof rpcId !== 'number') throw Error('Invalid resolved request ID');
      for (const [id, request] of this.pending) if (request.threadId === sessionId && request.rpcId === rpcId) {
        this.pending.delete(id);
        this.rpc!.expire(rpcId);
        this.emit({ type: request.kind === 'input' ? 'userInput.resolved' : 'approval.expired', sessionId, requestId: id });
      }
      return;
    }
    const runId = text(p.turnId);
    if (message.method === 'turn/started') { this.markTurnStarted?.(); this.started(text(record(p.turn).id)); }
    else if (message.method === 'item/agentMessage/delta') this.emit({ type: 'assistant.delta', sessionId, runId, itemId: text(p.itemId), delta: text(p.delta) });
    else if (message.method === 'item/started' || message.method === 'item/completed') {
      const item = normalizeItem(p.item, runId, message.method === 'item/completed');
      if (item) this.emit({ type: 'item.updated', sessionId, item });
    } else if (message.method === 'turn/completed') {
      const turn = record(p.turn), id = text(turn.id);
      this.markTurnStarted?.();
      this.finished.add(id);
      if (this.active?.id === id) { this.active = undefined; clearTimeout(this.cancelTimer); }
      for (const [key, request] of this.pending) if (request.runId === id) { this.pending.delete(key); await this.rpc!.reject(request.rpcId, -32800); }
      const status = turn.status === 'interrupted' ? 'cancelled' : turn.status === 'failed' ? 'failed' : 'completed';
      this.emit({ type: 'run.finished', sessionId, runId: id, status, ...(status === 'failed' ? { error: `本次生成失败（${errorCategory(turn.error)}）；已保留历史，未自动重试。` } : {}) });
      void this.refreshBook();
    } else if (message.method === 'error') this.emit({ type: 'error', message: `app-server 报告任务错误（${errorCategory(p.error)}）；请检查模型配置与已保存结果。` });
  }
  private async serverRequest(message: RpcMessage) {
    const p = record(message.params), threadId = text(p.threadId), runId = text(p.turnId) || this.active?.id || 'read-only';
    if (threadId !== this.sessionId || this.active?.cancelling || this.finished.has(runId)) { await this.rpc!.reject(message.id!, -32800); return; }
    const id = randomUUID();
    if (message.method === 'item/tool/requestUserInput') {
      const questions = array(p.questions).map(record);
      if (!questions.length || questions.some(q => q.isSecret || !text(q.id) || !text(q.question))) { await this.rpc!.reject(message.id!); return; }
      this.pending.set(id, { rpcId: message.id!, threadId, runId, kind: 'input', questionIds: questions.map(q => text(q.id)) });
      const request: InputRequest = { id, runId, questions: questions.map(q => ({ id: text(q.id), label: text(q.question), options: array(q.options).map(o => text(record(o).label)) })) };
      this.emit({ type: 'userInput.requested', sessionId: threadId, request }); return;
    }
    const elicitation = message.method === 'mcpServer/elicitation/request';
    const command = message.method === 'item/commandExecution/requestApproval';
    const file = message.method === 'item/fileChange/requestApproval';
    if (elicitation) {
      const schema = record(p.requestedSchema), properties = record(schema.properties);
      if (p.serverName !== 'lg_writing' || p.mode !== 'form' || schema.type !== 'object' ||
        Object.keys(properties).length !== 1 || record(properties.confirm).type !== 'boolean') {
        await this.rpc!.respond(message.id!, { action: 'decline', content: null, _meta: null });
        this.emit({ type: 'error', message: '不支持此 MCP 表单，已拒绝；没有授权写入。' }); return;
      }
    } else if (!command && !file) {
      await this.rpc!.reject(message.id!);
      this.emit({ type: 'error', message: `不支持的后端请求：${message.method}；未执行。` }); return;
    }
    this.pending.set(id, { rpcId: message.id!, threadId, runId, kind: elicitation ? 'elicitation' : 'decision' });
    this.emit({ type: 'approval.requested', sessionId: threadId, approval: { id, runId, ...(text(p.itemId) ? { itemId: text(p.itemId) } : {}),
      title: elicitation ? 'LG 写作操作待审批' : command ? '命令执行待审批' : '文件修改待审批',
      details: elicitation ? text(p.message) : JSON.stringify({ command: p.command, cwd: p.cwd, reason: p.reason, grantRoot: p.grantRoot }, null, 2),
      choices: ['accept', 'decline', 'cancel'], state: 'pending' } });
  }
  async dispose() { clearTimeout(this.cancelTimer); await this.rpc?.dispose(); }
}
