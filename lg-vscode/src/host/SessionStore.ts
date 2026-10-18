import { randomUUID } from 'node:crypto';
import { emptyBook, emptyLibrary, type BackendEvent, type Snapshot, type Item } from '../../shared/protocol';

export class SessionStore {
  readonly state: Snapshot;
  private listeners = new Set<(snapshot: Snapshot) => void>();
  private finals = new Set<string>();
  private active = new Map<string,NonNullable<Snapshot['run']>>();
  constructor(mode: Snapshot['mode']) {
    this.state = { version: 1, epoch: randomUUID(), sequence: 0, mode, connection: 'connecting', sessions: [], sessionId: '', run: null,
      items: [], approvals: [], inputs: [], book: emptyBook(), error: null, attachment: null, library:emptyLibrary() };
    if (mode === 'codex') this.state.book.source = 'backend';
  }
  snapshot(): Snapshot { return structuredClone(this.state); }
  subscribe(listener: (s: Snapshot) => void) { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; }
  publish() { this.state.sequence++; for (const fn of this.listeners) fn(this.snapshot()); }
  error(message: string) { this.state.error = message; this.publish(); }
  get busy() { return this.active.size>0; }
  private selectRun(){
    const current=[...this.active.values()].at(-1);
    if(current)this.state.run=current;
  }
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
          this.active.clear();
          this.expire();
          for (const item of s.items) if (['running', 'waiting'].includes(item.state)) {
            item.state = 'failed';
            this.finals.add(item.runId + ':' + item.id);
          }
        }
        break;
      case 'session.snapshot':
        s.sessionId = event.sessionId; s.items = structuredClone(event.items); s.run = null; s.approvals = []; s.inputs = []; s.error = null;
        this.active.clear();
        for(const run of event.activeRuns??[])this.active.set(run.id,structuredClone(run));
        this.selectRun();
        this.finals.clear();
        for (const item of s.items) if (item.state !== 'running' && item.state !== 'waiting') this.finals.add(item.runId + ':' + item.id);
        break;
      case 'run.started':
        s.run = { id: event.runId, state: 'running' }; s.error = null;
        this.active.set(event.runId,s.run);
        this.upsert({id:(event.source==='retry'?'retry:':'user:')+event.runId,runId:event.runId,
          kind:event.source==='retry'?'notice':'user',title:event.source==='retry'?'重试':'作者',text:event.text,state:'completed'});
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
        if(this.active.get(event.approval.runId)?.state==='running')this.active.get(event.approval.runId)!.state='waiting';
        this.selectRun();break;
      case 'approval.resolved':
        for (const a of s.approvals) if (a.id === event.requestId) a.state = 'resolved';
        this.clearWaiting(); break;
      case 'approval.expired':
        for (const a of s.approvals) if (a.id === event.requestId && ['pending', 'responding'].includes(a.state)) a.state = 'expired';
        this.clearWaiting(); break;
      case 'userInput.requested':
        if (!s.inputs.some(a => a.id === event.request.id)) s.inputs.push(structuredClone(event.request));
        if(this.active.get(event.request.runId)?.state==='running')this.active.get(event.request.runId)!.state='waiting';
        this.selectRun();break;
      case 'userInput.resolved': s.inputs = s.inputs.filter(a => a.id !== event.requestId); this.clearWaiting(); break;
      case 'run.finished':
        this.active.delete(event.runId);
        if(s.run?.id===event.runId)s.run.state=event.status;
        this.expire(event.runId);this.selectRun();
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
    for(const run of this.active.values()){
      if(run.state==='waiting'&&!this.state.inputs.some(q=>q.runId===run.id)&&
        !this.state.approvals.some(a=>a.runId===run.id&&['pending','responding'].includes(a.state)))run.state='running';
    }
    this.selectRun();
  }
  private expire(runId?:string) {
    for(const a of this.state.approvals)if((!runId||a.runId===runId)&&['pending','responding'].includes(a.state))a.state='expired';
    this.state.inputs=runId?this.state.inputs.filter(q=>q.runId!==runId):[];
  }
}
