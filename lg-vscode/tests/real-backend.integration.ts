import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { CodexBackend, type BackendSettings } from '../src/backend/CodexBackend';
import { SessionStore } from '../src/host/SessionStore';
import type { BackendEvent } from '../shared/protocol';
import type { RpcMessage } from '../src/backend/RpcTransport';

class FixtureBackend extends CodexBackend {
  constructor(private fixture: BackendSettings) { super(fixture); }
  protected override launch() {
    return { command: this.fixture.python, args: [resolve('tests/fixtures/app_server_fixture.py'), '--workspace', this.fixture.workspace,
      '--runtime-manifest', this.fixture.manifest], cwd: this.fixture.workspace, env: this.fixture.env };
  }
}
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate: () => boolean, ms = 45_000) {
  const start = Date.now(); while (!predicate()) { if (Date.now() - start > ms) throw Error('Timed out waiting for backend event'); await sleep(25); }
}
test('real pinned app-server: LG MCP approval, denial, overview, cancel, history across process restart', { timeout: 180_000,
  skip: !process.env.LG_TEST_RUNTIME_MANIFEST || !process.env.LG_TEST_PYTHON }, async t => {
  const workspace = await mkdtemp(join(tmpdir(), 'lg-real-extension-'));
  const settings: BackendSettings = { workspace, python: process.env.LG_TEST_PYTHON!, manifest: process.env.LG_TEST_RUNTIME_MANIFEST!,
    env: { ...process.env, LITERARYGIANT_REGISTRY_HOME: join(workspace, 'registry') } };
  let backend = new FixtureBackend(settings);
  const store = new SessionStore('codex'), events: BackendEvent[] = [];
  backend.subscribe(e => { events.push(e); store.apply(e); });
  t.after(async () => {
    t.diagnostic(JSON.stringify({workspace, approvals:events.filter(e=>e.type==='approval.requested').length,
      terminalStates:events.filter(e=>e.type==='run.finished').map(e=>e.status)}));
    await backend.dispose();
  });
  await backend.connect(); const session = await backend.create();
  async function turn(marker: string, decision?: 'accept' | 'decline') {
    await backend.run(session.id, marker);
    if (decision) {
      await until(() => store.state.approvals.some(a => a.state === 'pending') || (!!store.state.run && !store.busy));
      const approval = store.state.approvals.find(a => a.state === 'pending')!;
      assert.ok(approval,'Expected LG tool approval before turn completion');
      await backend.approve(approval.id, decision);
      await assert.rejects(backend.approve(approval.id, decision), /过期|已回答/);
    }
    await until(() => !!store.state.run && !store.busy);
    assert.equal(store.state.run?.state, 'completed', JSON.stringify(events.filter(e=>e.type==='error'||e.type==='backend.status'||e.type==='run.finished')));
  }
  await turn('CASE:init', 'accept');
  await turn('CASE:plan', 'accept');
  await turn('CASE:denied', 'decline');
  await assert.rejects(readFile(join(workspace,'ReferenceLibrary/bible/book-overview.json')));
  await turn('CASE:accepted', 'accept');
  await backend.refreshBook();
  assert.equal(store.state.book.source,'backend');
  assert.equal(store.state.book.targetWords,1_000_000);
  assert.equal(store.state.book.positioning,'原创机械悬疑长篇');
  assert.match(store.state.book.overview,/浮城/);
  await turn('CASE:refdeny','decline');
  assert.equal((await backend.readLibrary({topic:'characters',query:'不得保存',offset:0})).entries.length,0);
  await turn('CASE:reference','accept');
  const people=await backend.readLibrary({topic:'characters',query:'林砚',offset:0});
  assert.equal(people.entries.length,1);
  const person=await backend.readEntry('characters',people.entries[0].key);
  assert.match(person.body,/隔离电源/);assert.match(person.body,/第 1 版.*候选资料/);
  assert.ok(store.state.items.some(i=>i.files?.some(p=>p.startsWith('ReferenceLibrary/bible/characters/'))));
  const beforeRun=store.state.run?.id;
  assert.match(await backend.prepareWriting('characters',person.key,'generate',person.fingerprint),/studio_reference/);
  assert.equal(store.state.run?.id,beforeRun,'Reading/preparing must not start a model turn');
  const pending=await backend.readLibrary({topic:'pending',query:'林砚',offset:0});
  assert.equal(pending.entries[0].status,'candidate');
  await turn('CASE:adoptref','accept');
  assert.equal((await backend.readLibrary({topic:'pending',query:'林砚',offset:0})).total,0);
  await assert.rejects(backend.prepareWriting('characters',person.key,'generate',person.fingerprint),/已变化/);
  await turn('CASE:edit', 'accept');
  assert.ok(store.state.items.some(i => i.diff?.before === '作者已采纳的原稿。'
    && i.diff.after === 'Agent 提出的改稿候选。' && !i.diff.applied));
  const chapters = await backend.readLibrary({topic:'chapters',query:'原稿',offset:0});
  const manuscript = await backend.readEntry('chapters',chapters.entries[0].key);
  assert.match(manuscript.body,/作者已采纳的原稿。/);
  assert.ok((await backend.readLibrary({topic:'pending',query:'原稿',offset:0})).total > 0);
  await turn('CASE:restore', 'accept');
  const preserved = await backend.readEntry('chapters',chapters.entries[0].key);
  assert.match(preserved.body,/作者已采纳的原稿。/);
  await turn('CASE:text');
  assert.ok(events.some(e=>e.type==='assistant.delta'));
  await backend.run(session.id,'CASE:slow'); await backend.cancel(session.id);
  await until(()=>!store.busy); assert.equal(store.state.run?.state,'cancelled');
  const history = await backend.read(session.id); assert.ok(history.some(i=>i.text.includes('CASE:accepted')));
  await backend.dispose();
  backend = new FixtureBackend(settings);backend.subscribe(e=>store.apply(e));
  await backend.connect();
  assert.ok((await backend.list()).some(s=>s.id===session.id));
  await backend.resume(session.id);
  assert.ok(store.state.items.some(i=>i.text.includes('CASE:accepted')));
  assert.equal(store.state.run,null);
  console.log('Real-runtime evidence workspace:',workspace);
});
