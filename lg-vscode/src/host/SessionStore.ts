import { randomUUID } from 'node:crypto';
import { emptyBook, type BackendEvent, type Snapshot, type Item } from '../../shared/protocol';

export class SessionStore {
  readonly state: Snapshot;
  private listeners = new Set<(snapshot: Snapshot) => void>();
  private finals = new Set<string>();
  constructor(mode: Snapshot['mode']) {
    this.state = { version: 1, epoch: randomUUID(), sequence: 0, mode, connection: 'connecting', sessions: [], sessionId: '', run: null,
      items: [], approvals: [], inputs: [], book: emptyBook(), error: null, attachment: null };
    if (mode === 'codex') this.state.book.source = 'backend';
  }
  snapshot(): Snapshot { return structuredClone(this.state); }
  subscribe(listener: (s: Snapshot) => void) { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; }
  publish() { this.state.sequence++; for (const fn of this.listeners) fn(this.snapshot()); }
  error(message: string) { this.state.error = message; this.publish(); }
  get busy() { return !!this.state.run && ['running', 'waiting', 'cancelling'].includes(this.state.run.state); }
  private upsert(item: Item) {
    const index = this.state.items.findIndex(existing => existing.id === item.id && existing.runId === item.runId);
    if (index < 0) this.state.items.push(item); else this.state.items[index] = item;
  }
  apply(event: BackendEvent) {
    if ('sessionId' in event && event.type !== 'session.snapshot' && event.sessionId !== this.state.sessionId) return;
    const s = this.state;
    switch (event.type) {
      case 'backend.status':
        s.connection = event.status;
        if (event.error) s.error = event.error;
        if (event.status === 'disconnected') {
          if (this.busy && s.run) s.run.state = 'interrupted';
          this.expire();
          for (const item of s.items) if (['running', 'waiting'].includes(item.state)) {
            item.state = 'failed';
            this.finals.add(item.runId + ':' + item.id);
          }
        }
        break;
      case 'session.snapshot':
        s.sessionId = event.sessionId; s.items = structuredClone(event.items); s.run = null; s.approvals = []; s.inputs = []; s.error = null;
        this.finals.clear();
        for (const item of s.items) if (item.state !== 'running' && item.state !== 'waiting') this.finals.add(item.runId + ':' + item.id);
        break;
      case 'run.started':
        s.run = { id: event.runId, state: 'running' }; s.error = null;
        this.upsert({ id: 'user:' + event.runId, runId: event.runId, kind: 'user', title: '作者', text: event.text, state: 'completed' });
        break;
      case 'assistant.delta': {
        if (this.finals.has(event.runId + ':' + event.itemId)) break;
        const old = s.items.find(item => item.id === event.itemId && item.runId === event.runId);
        this.upsert({ id: event.itemId, runId: event.runId, kind: 'assistant', title: 'LG', text: (old?.text ?? '') + event.delta, state: 'running' });
        break;
      }
      case 'item.updated':
        if (this.finals.has(event.item.runId + ':' + event.item.id) && ['running', 'waiting'].includes(event.item.state)) break;
        if (event.item.state !== 'running' && event.item.state !== 'waiting') this.finals.add(event.item.runId + ':' + event.item.id);
        this.upsert(structuredClone(event.item)); break;
      case 'approval.requested':
        if (!s.approvals.some(a => a.id === event.approval.id)) s.approvals.push(structuredClone(event.approval));
        if (s.run?.state === 'running') s.run.state = 'waiting'; break;
      case 'approval.resolved':
        for (const a of s.approvals) if (a.id === event.requestId) a.state = 'resolved';
        this.clearWaiting(); break;
      case 'approval.expired':
        for (const a of s.approvals) if (a.id === event.requestId && ['pending', 'responding'].includes(a.state)) a.state = 'expired';
        this.clearWaiting(); break;
      case 'userInput.requested':
        if (!s.inputs.some(a => a.id === event.request.id)) s.inputs.push(structuredClone(event.request));
        if (s.run?.state === 'running') s.run.state = 'waiting'; break;
      case 'userInput.resolved': s.inputs = s.inputs.filter(a => a.id !== event.requestId); this.clearWaiting(); break;
      case 'run.finished':
        if (s.run?.id === event.runId) { s.run.state = event.status; this.expire(); }
        if (event.error) s.error = event.error;
        for (const item of s.items) if (item.runId === event.runId && ['running', 'waiting'].includes(item.state)) {
          item.state = event.status === 'interrupted' ? 'failed' : event.status;
          this.finals.add(item.runId + ':' + item.id);
        }
        break;
      case 'book.updated': s.book = structuredClone(event.book); break;
      case 'error': s.error = event.message; break;
    }
    this.publish();
  }
  private clearWaiting() {
    if (this.state.run?.state === 'waiting' && !this.state.inputs.length && !this.state.approvals.some(a => ['pending', 'responding'].includes(a.state))) this.state.run.state = 'running';
  }
  private expire() {
    for (const a of this.state.approvals) if (['pending', 'responding'].includes(a.state)) a.state = 'expired';
    this.state.inputs = [];
  }
}
