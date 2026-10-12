import { randomUUID } from 'node:crypto';
import { EventBackend, type AgentBackend } from './AgentBackend';
import { emptyBook, type Approval, type Item, type Session } from '../../shared/protocol';

export class MockBackend extends EventBackend implements AgentBackend {
  private sessions = new Map<string, { session: Session; items: Item[] }>();
  private active?: { sessionId: string; runId: string; controller: AbortController };
  private pending = new Map<string, (decision: string) => void>();
  private connected = false;
  constructor(private delay = 55) { super(); }
  async connect() { this.connected = true; this.emit({ type: 'backend.status', status: 'ready' }); await this.refreshBook(); }
  async list() { return [...this.sessions.values()].map(s => s.session); }
  async create() {
    const session = { id: randomUUID(), title: '创作会话 ' + (this.sessions.size + 1) };
    this.sessions.set(session.id, { session, items: [] }); await this.resume(session.id); return session;
  }
  async read(id: string) { const session = this.sessions.get(id); if (!session) throw Error('会话不存在'); return structuredClone(session.items); }
  async resume(id: string) { if (this.active) throw Error('请先停止当前任务'); this.emit({ type: 'session.snapshot', sessionId: id, items: await this.read(id) }); }
  private item(sessionId: string, item: Item) {
    const items = this.sessions.get(sessionId)!.items;
    const i = items.findIndex(old => old.id === item.id && old.runId === item.runId);
    if (i < 0) items.push(item); else items[i] = item;
    this.emit({ type: 'item.updated', sessionId, item });
  }
  async run(sessionId: string, text: string) {
    if (!this.connected) throw Error('后端已断开');
    await this.read(sessionId);
    if (this.active) throw Error('已有任务在运行');
    const runId = randomUUID(), controller = new AbortController();
    this.active = { sessionId, runId, controller };
    this.emit({ type: 'run.started', sessionId, runId, text });
    this.item(sessionId, { id: 'user:' + runId, runId, kind: 'user', title: '作者', text, state: 'completed' });
    void this.perform(sessionId, runId, text, controller).catch(error => this.emit({ type: 'error', message: String(error) }));
  }
  private async perform(sessionId: string, runId: string, text: string, controller: AbortController) {
    let status: 'completed' | 'failed' | 'cancelled' = 'completed';
    let error: string | undefined;
    let accumulated = '', replyCompleted = false;
    try {
      if (text.includes('/disconnect')) { this.connected = false; this.emit({ type: 'backend.status', status: 'disconnected', error: '模拟连接中断；任务没有重发。' }); return; }
      const response = '这是离线演示。LG 会保留作品规划、候选稿与正式正文的区别。\n\n' + (text.includes('/slow') ? '正在逐步整理场景……'.repeat(20) : '第一章可以从修理棚里一场意外停电开始。人物必须先作出一个有代价的选择。');
      for (const delta of response.match(/.{1,6}|\n/g) ?? []) {
        await new Promise(resolve => setTimeout(resolve, this.delay));
        if (controller.signal.aborted) throw Error('cancelled');
        accumulated += delta;
        this.emit({ type: 'assistant.delta', sessionId, runId, itemId: 'reply:' + runId, delta });
      }
      this.item(sessionId, { id: 'reply:' + runId, runId, kind: 'assistant', title: 'LG', text: accumulated, state: 'completed' });
      replyCompleted = true;
      if (text.includes('/error')) throw Error('模拟工具执行失败；没有写入作品。');
      const toolId = 'tool:' + runId;
      this.item(sessionId, { id: toolId, runId, kind: 'tool', title: '保存场景候选', text: '准备保存候选，不改变正式正文。', state: 'waiting' });
      const requestId = randomUUID();
      const decision = new Promise<string>(resolve => this.pending.set(requestId, resolve));
      if (text.includes('/input')) this.emit({ type: 'userInput.requested', sessionId, request: { id: requestId, runId, questions: [{ id: 'direction', label: '希望第一章侧重什么？', options: ['人物困境', '世界悬念'] }] } });
      else this.emit({ type: 'approval.requested', sessionId, approval: { id: requestId, runId, itemId: toolId, title: '保存场景候选（模拟）', details: '只演示审批，不写入磁盘。', choices: ['accept', 'decline'], state: 'pending' } });
      const result = await decision;
      this.pending.delete(requestId);
      if (controller.signal.aborted) throw Error('cancelled');
      this.emit({ type: 'approval.resolved', sessionId, requestId });
      this.emit({ type: 'userInput.resolved', sessionId, requestId });
      this.item(sessionId, { id: toolId, runId, kind: 'tool', title: '保存场景候选', text: result === 'accept' ? '已演示批准；未写入真实作品。' : '作者已拒绝；未写入。', state: result === 'accept' ? 'completed' : 'cancelled', structured: { simulated: true, decision: result },
        ...(result === 'accept' ? { files: ['README.md'], diff: { path: 'mock-scene.md', before: '灯熄了。', after: '灯熄了。他先切断电源，再推开闸门。', applied: false } } : {}) });
    } catch (e) {
      status = controller.signal.aborted ? 'cancelled' : 'failed'; error = status === 'failed' ? String(e) : undefined;
      if (accumulated && !replyCompleted) this.item(sessionId, {
        id: 'reply:' + runId, runId, kind: 'assistant', title: 'LG', text: accumulated, state: status,
      });
    }
    finally {
      for (const resolve of this.pending.values()) resolve('cancel');
      this.pending.clear(); this.active = undefined;
      for (const item of this.sessions.get(sessionId)?.items ?? []) {
        if (item.runId === runId && ['running', 'waiting'].includes(item.state)) this.item(sessionId, { ...item, state: status });
      }
      if (this.connected) this.emit({ type: 'run.finished', sessionId, runId, status, error });
    }
  }
  async cancel(sessionId: string) {
    if (this.active?.sessionId !== sessionId) throw Error('没有正在运行的任务');
    this.active.controller.abort(); for (const resolve of this.pending.values()) resolve('cancel');
  }
  async approve(id: string, decision: Approval['choices'][number]) {
    const resolve = this.pending.get(id); if (!resolve) throw Error('审批已过期或已经回答'); this.pending.delete(id); resolve(decision);
  }
  async answer(id: string, answers: Record<string, string>) {
    if (!answers.direction?.trim()) throw Error('请回答问题'); await this.approve(id, 'accept');
  }
  async refreshBook() {
    this.emit({ type: 'book.updated', book: { ...emptyBook(), name: '未命名长篇 · 离线示例', overview: '一名维修学徒在封闭城邦中追查失控的供能网络。',
      positioning: '原创科幻 · 人物成长 · 长篇规划', chapters: [{ id: 'chapter-1', title: '第一章：停电之后', state: '规划中', words: 0 }],
      volumes: ['第一卷：失衡的城市'], characters: ['林砚：维修学徒'], relationships: ['林砚与师傅：信任受到考验'],
      settings: ['城邦依赖统一供能网络（提案）'], foreshadowing: ['修理单上的陌生印记（规划）'], tasks: ['确认首章场景'], pending: ['尚无正式采用的正文'] } });
  }
  async dispose() { this.connected = false; this.active?.controller.abort(); for (const resolve of this.pending.values()) resolve('cancel'); this.pending.clear(); this.emit({ type: 'backend.status', status: 'disconnected' }); }
}
