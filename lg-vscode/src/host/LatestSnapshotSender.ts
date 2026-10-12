import type { Snapshot } from '../../shared/protocol';

/** Full snapshots supersede earlier snapshots while the renderer is catching up. */
export class LatestSnapshotSender {
  private pending?: Snapshot;
  private sending = false;
  private disposed = false;
  constructor(private send: (snapshot: Snapshot) => unknown | PromiseLike<unknown>) {}
  offer(snapshot: Snapshot) {
    if (this.disposed) return;
    this.pending = snapshot;
    if (!this.sending) void this.flush();
  }
  private async flush() {
    this.sending = true;
    try {
      while (!this.disposed && this.pending) {
        const snapshot = this.pending;
        this.pending = undefined;
        try { await this.send(snapshot); }
        catch { /* A closed renderer cannot receive; newer snapshots remain eligible. */ }
      }
    } finally { this.sending = false; }
  }
  dispose() { this.disposed = true; this.pending = undefined; }
}
