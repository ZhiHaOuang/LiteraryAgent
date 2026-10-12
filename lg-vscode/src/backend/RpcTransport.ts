import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';

export type RpcId = string | number;
export type RpcMessage = { id?: RpcId; method?: string; params?: unknown; result?: unknown; error?: { code: number; message: string } };
export type LaunchSpec = { command: string; args: string[]; cwd: string; env?: NodeJS.ProcessEnv };

/** JSONL framing is byte based: a UTF-8 character may straddle any pipe read. */
export class JsonLines {
  private buffer = Buffer.alloc(0);
  constructor(private deliver: (message: RpcMessage) => void, private limit = 4 * 1024 * 1024) {}
  push(chunk: Buffer) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    let end: number;
    while ((end = this.buffer.indexOf(10)) !== -1) {
      if (end > this.limit) throw Error('RPC frame exceeds size limit');
      const line = this.buffer.subarray(0, end); this.buffer = this.buffer.subarray(end + 1);
      if (!line.length) continue;
      const message: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(line));
      if (!message || typeof message !== 'object' || Array.isArray(message)) throw Error('Invalid RPC frame');
      const m = message as RpcMessage;
      if (m.id !== undefined && typeof m.id !== 'string' && typeof m.id !== 'number') throw Error('Invalid RPC id');
      if (m.method !== undefined && typeof m.method !== 'string') throw Error('Invalid RPC method');
      if (!m.method && (m.id === undefined || (('result' in m) === ('error' in m)))) throw Error('Invalid RPC response');
      this.deliver(m);
    }
    if (this.buffer.length > this.limit) throw Error('RPC frame exceeds size limit');
  }
  end() { if (this.buffer.length) throw Error('Incomplete RPC frame at EOF'); }
}

export class RpcTransport {
  private process?: ChildProcessWithoutNullStreams;
  private sequence = 0;
  private pending = new Map<RpcId, { resolve: (value: unknown) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }>();
  private serverRequests = new Set<RpcId>();
  private ended = false;
  private writeQueue: Promise<void> = Promise.resolve();
  constructor(private spec: LaunchSpec, private onMessage: (message: RpcMessage) => void,
    private onClose: (reason: string) => void, private timeoutMs = 45_000) {}
  start() {
    if (this.process) throw Error('RPC process already started');
    const child = this.process = spawn(this.spec.command, this.spec.args, { cwd: this.spec.cwd,
      env: this.spec.env ?? process.env, stdio: 'pipe', windowsHide: true, detached: process.platform !== 'win32' });
    const frames = new JsonLines(message => this.receive(message));
    child.stdout.on('data', (data: Buffer) => { try { frames.push(data); } catch { this.fail('app-server 协议无效，连接已关闭'); void this.dispose(); } });
    child.stdout.on('end', () => { try { frames.end(); } catch { this.fail('app-server 输出不完整，连接已关闭'); } });
    // Drain, but never forward stderr: provider/proxy error bodies can contain secrets.
    child.stderr.resume();
    child.on('error', () => this.fail('LG app-server 无法启动，请检查 Python 与运行时设置'));
    child.on('exit', code => this.fail(`LG app-server 已退出（${code ?? 'signal'}）`));
    child.stdin.on('error', () => this.fail('app-server 输入管道已关闭'));
  }
  private receive(message: RpcMessage) {
    if (this.ended) return;
    if (message.method) {
      if (message.id !== undefined) {
        if (this.serverRequests.has(message.id)) { this.fail('app-server 重复请求编号'); void this.dispose(); return; }
        this.serverRequests.add(message.id);
      }
      this.onMessage(message); return;
    }
    const request = this.pending.get(message.id!);
    if (!request) return; // A late response must never trigger an automatic retry.
    this.pending.delete(message.id!); clearTimeout(request.timer);
    if (message.error) request.reject(Error(`app-server 请求失败（RPC ${message.error.code}）`));
    else request.resolve(message.result);
  }
  request<T = unknown>(method: string, params: unknown): Promise<T> {
    const id = ++this.sequence;
    return new Promise<T>((resolve, reject) => {
      if (this.ended || !this.process) { reject(Error('app-server 未连接')); return; }
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(Error(`${method} 请求超时；执行结果未知，未自动重试`));
        // A timed-out mutation may still be running. End this connection before any new turn.
        this.fail('app-server 请求超时，请恢复历史确认结果后再继续'); void this.dispose();
      }, this.timeoutMs);
      this.pending.set(id, { resolve: value => resolve(value as T), reject, timer });
      void this.send({ id, method, params }).catch(() => this.fail('app-server 写入失败'));
    });
  }
  notify(method: string, params: unknown) { return this.send({ method, params }); }
  expire(id: RpcId) { this.serverRequests.delete(id); }
  async respond(id: RpcId, result: unknown) {
    if (!this.serverRequests.delete(id)) throw Error('请求已回答或已过期');
    await this.send({ id, result });
  }
  async reject(id: RpcId, code = -32601) {
    if (!this.serverRequests.delete(id)) return;
    await this.send({ id, error: { code, message: 'Unsupported or expired client request' } });
  }
  private send(message: RpcMessage) {
    const bytes = JSON.stringify(message) + '\n';
    const operation = this.writeQueue.then(() => new Promise<void>((resolve, reject) => {
      if (this.ended || !this.process?.stdin.writable) { reject(Error('app-server 已断开')); return; }
      this.process.stdin.write(bytes, error => error ? reject(error) : resolve());
    }));
    this.writeQueue = operation.catch(() => {}); return operation;
  }
  private fail(reason: string) {
    if (this.ended) return;
    this.ended = true;
    for (const request of this.pending.values()) { clearTimeout(request.timer); request.reject(Error(reason)); }
    this.pending.clear(); this.serverRequests.clear(); this.onClose(reason);
  }
  async dispose() {
    this.fail('LG app-server 连接已关闭');
    const child = this.process;
    if (!child || child.exitCode !== null || child.signalCode !== null) return;
    const stop = (signal: NodeJS.Signals) => {
      try { if (process.platform !== 'win32' && child.pid) process.kill(-child.pid, signal); else child.kill(signal); } catch { /* Already exited. */ }
    };
    child.stdin.end(); stop('SIGTERM');
    await new Promise<void>(resolve => {
      const timer = setTimeout(() => { stop('SIGKILL'); resolve(); }, 3000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
    });
  }
}
