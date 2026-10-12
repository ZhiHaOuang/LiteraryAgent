import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { SnapshotSchema, STAGES, type Snapshot, type ClientMessage, type InputRequest, type Book } from '../shared/protocol';
import { InputForm } from './ui/components/layout/InputForm';
import { AssistantMessage } from './ui/components/messages/Assistant/AssistantMessage';
import { UserMessage } from './ui/components/messages/UserMessage';
import { ToolCallContainer, CodeBlock } from './ui/components/toolcalls/shared/LayoutComponents';
import { CopyButton } from './ui/components/toolcalls/shared/copyUtils';
import { PlatformProvider } from './ui/context/PlatformContext';
import './ui/styles/variables.css';
import './ui/styles/components.css';
import './ui/styles/timeline.css';
import './styles.css';
import {HomePanel,LibraryPanel} from './WorkspacePanels';
import type {LibraryTopic} from '../shared/protocol';

type View='home'|'chat'|'book'|'library';
declare function acquireVsCodeApi(): { postMessage(message: ClientMessage): void; getState(): { draft?: string;view?:View } | undefined; setState(state: { draft: string;view:View }): void };
const vscode = acquireVsCodeApi();
const send = (message: ClientMessage) => vscode.postMessage(message);
const labels: Record<string, string> = { running: '运行中', waiting: '等待确认', cancelling: '正在取消', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '连接中断', pending: '等待审批', responding: '正在回复', resolved: '已处理', expired: '已失效' };

function Values({ title, values }: { title: string; values: string[] }) {
  return <section className="book-section"><h3>{title}</h3>{values.length ? <ul>{values.map((value, i) => <li key={i}>{value}</li>)}</ul> : <p className="muted">尚未记录</p>}</section>;
}
function BookPanel({ book, approvals }: { book: Book; approvals: string[] }) {
  return <div className="book-panel" aria-label="LG 作品概况">
    <div className="section-heading"><div><span className="eyebrow">作品档案</span><h2>{book.name}</h2></div><button onClick={() => send({ type: 'refreshBook' })}>刷新</button></div>
    <p className="book-overview">{book.overview || '尚未保存全书简介。'}</p>
    {book.source === 'mock' && <p className="demo-note">离线示例数据 · 不代表真实作品</p>}
    <section className="book-section"><h3>创作定位</h3><p>{book.positioning || '尚未记录'}</p></section>
    <section className="book-section"><h3>长篇目标与进度</h3><div className="metrics"><strong>{book.writtenWords.toLocaleString()}<small>已写字数</small></strong><strong>{book.targetWords.toLocaleString()}<small>计划字数</small></strong></div>
      <progress max={Math.max(book.targetWords, 1)} value={book.acceptedWords} aria-label="已采用正文进度" /><p className="muted">已采用 {book.acceptedWords.toLocaleString()} 字 · 候选稿不计入采用进度</p></section>
    <Values title="分卷规划" values={book.volumes} />
    <Values title="章节" values={book.chapters.map(c => `${c.title} · ${c.state} · ${c.words} 字`)} />
    <Values title="人物" values={book.characters} /><Values title="人物关系" values={book.relationships} />
    <Values title="设定" values={book.settings} /><Values title="伏笔" values={book.foreshadowing} />
    <Values title="当前任务" values={book.tasks} /><Values title="待审批事项" values={[...book.pending, ...approvals]} />
  </div>;
}
function Questions({ request }: { request: InputRequest }) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  return <form className="approval" onSubmit={event => { event.preventDefault(); send({ type: 'answer', requestId: request.id, answers }); }}>
    <h3>需要你的意见</h3>{request.questions.map((q,index) => <div key={q.id}>
      <label htmlFor={`answer-${request.id}-${index}`}>{q.label}</label>
      {q.options.length > 0 && <div className="suggestions">{q.options.map(o => <button type="button" key={o} onClick={() => setAnswers({ ...answers, [q.id]: o })}>{o}</button>)}</div>}
      <input id={`answer-${request.id}-${index}`} required value={answers[q.id] ?? ''} onChange={e => setAnswers({ ...answers, [q.id]: e.target.value })} /></div>)}
    <button type="submit">发送回答</button>
  </form>;
}
function App() {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const latest = useRef<{ epoch: string; sequence: number } | undefined>(undefined);
  const [draft, setDraft] = useState(vscode.getState()?.draft ?? '');
  const input = useRef<HTMLDivElement>(null);
  const [composing, setComposing] = useState(false);
  const [tab, setTab] = useState<View>(vscode.getState()?.view??'home');
  const [menu, setMenu] = useState(false);
  const scroll = useRef<HTMLDivElement>(null);
  const sent = useRef(false);
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if(event.data?.type==='navigate'&&event.data.page==='home'){setTab('home');return;}
      if (event.data?.type !== 'snapshot') return;
      const parsed = SnapshotSchema.safeParse(event.data.snapshot);
      if (!parsed.success) return;
      const s = parsed.data;
      if (latest.current?.epoch === s.epoch && s.sequence <= latest.current.sequence) return;
      latest.current = { epoch: s.epoch, sequence: s.sequence };
      if (sent.current && s.run?.state === 'running') { setDraft(''); if (input.current) input.current.textContent = ''; sent.current = false; }
      if (s.error) sent.current = false;
      setSnapshot(s);
    };
    window.addEventListener('message', receive); send({ type: 'ready' });
    if (input.current) input.current.textContent = draft;
    return () => window.removeEventListener('message', receive);
  }, []);
  useEffect(() => { vscode.setState({ draft,view:tab }); }, [draft,tab]);
  useEffect(()=>{if(tab==='chat'&&input.current)input.current.textContent=draft;},[tab]);
  useEffect(() => {
    const el = scroll.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 180) el.scrollTop = el.scrollHeight;
  }, [snapshot?.sequence]);
  const busy = !!snapshot?.run && ['running', 'waiting', 'cancelling'].includes(snapshot.run.state);
  const ready = snapshot?.connection === 'ready'&&!!snapshot.sessionId;
  const open=(topic:LibraryTopic)=>{setTab('library');send({type:'library',query:{topic,query:'',offset:0}});};
  const submit = (e: React.FormEvent) => {
    e.preventDefault(); if (!ready || busy || !draft.trim() || sent.current) return;
    sent.current = true; send({ type: 'run', text: draft });
  };
  return <PlatformProvider value={{ platform: 'vscode', postMessage: () => {}, onMessage: () => () => {},
    copyToClipboard: async text => send({ type: 'copy', text }), openFile: path => send({ type: 'open', path }), features: { canCopy: true } }}>
    <main className="workbench">
      <header className="topbar"><div className="brand"><span className="brand-mark">LG</span><div><strong>LiteraryGiant</strong><small>把故事写下去</small></div></div><span className="connection">{snapshot?.mode === 'mock' ? '离线演示' : '本地 Agent'} · {ready ? '已连接' : snapshot?.connection === 'disconnected' ? '已断开' : '连接中'}</span></header>
      <nav className="tabs"><button className={tab==='home'?'selected':''} onClick={()=>setTab('home')}>{tab==='home'?'作品首页':'返回首页'}</button><button className={tab === 'chat' ? 'selected' : ''} onClick={() => setTab('chat')}>创作对话</button><button className={tab === 'book' ? 'selected' : ''} onClick={() => setTab('book')}>作品概况</button><button className={tab==='library'?'selected':''} onClick={()=>open(snapshot?.library.query.topic??'characters')}>资料库</button></nav>
      {snapshot?.error && <div className="error" role="alert">{snapshot.error}</div>}
      {tab==='home'?snapshot?<HomePanel snapshot={snapshot} send={send} chat={()=>setTab('chat')} open={open}/>:<p className="muted">正在读取作品首页…</p>:tab==='library'&&snapshot?<LibraryPanel snapshot={snapshot} send={send} chat={()=>setTab('chat')} open={open}/>:tab === 'book' && snapshot ? <BookPanel book={snapshot.book} approvals={snapshot.approvals.filter(a => a.state === 'pending').map(a => a.title)} /> : <>
        <div className="session-bar"><select aria-label="创作会话" value={snapshot?.sessionId ?? ''} disabled={busy || !ready} onChange={e => send({ type: 'select', sessionId: e.target.value })}>{snapshot?.sessions.map(session => <option key={session.id} value={session.id}>{session.title}</option>)}</select><button disabled={busy || !ready} onClick={() => send({ type: 'create' })}>新对话</button></div>
        <div className="transcript" ref={scroll} aria-label="对话记录" aria-live="polite">
          {!snapshot?.items.length && <div className="welcome"><span className="eyebrow">从一个念头开始</span><h1>你的故事，正在成形。</h1><p>和 LG 确定方向、整理世界，再写下第一章。</p><div className="stage-grid">{STAGES.map(stage => <button key={stage.key} disabled={!ready || busy} onClick={() => send({ type: 'stage', stage: stage.key })}>{stage.title}<span>↗</span></button>)}</div></div>}
          {snapshot?.items.map(item => <article key={item.runId + ':' + item.id} className="group item" data-item-id={item.id}>
            {item.kind === 'user' ? <UserMessage content={item.text} timestamp={0} /> : item.kind === 'assistant' ? <><AssistantMessage content={item.text} status={item.state === 'failed' ? 'error' : 'default'} /><CopyButton text={item.text} /></> : <ToolCallContainer label={item.title} status={item.state === 'completed' ? 'success' : item.state === 'failed' ? 'error' : item.state === 'running' ? 'loading' : 'warning'} labelSuffix={labels[item.state]}>
              <details><summary>{item.text.slice(0, 90) || '查看执行内容'}</summary><CodeBlock>{item.text}</CodeBlock>{item.structured !== undefined && <CodeBlock>{JSON.stringify(item.structured, null, 2)}</CodeBlock>}</details>
              {item.files?.map(path => <button key={path} onClick={() => send({ type: 'open', path })}>打开产物 · {path.split('/').pop()}</button>)}
              {item.diff && <button onClick={() => send({ type: 'diff', itemId: item.id })}>查看只读差异 · {item.diff.applied ? '已应用' : '候选对比'}</button>}
            </ToolCallContainer>}
          </article>)}
          {snapshot?.approvals.map(a => <section className="approval" key={a.id}><span className="eyebrow">{labels[a.state]}</span><h3>{a.title}</h3><pre>{a.details}</pre>{a.choices.map(decision => <button key={decision} disabled={a.state !== 'pending' || !ready || snapshot.run?.state === 'cancelling'} onClick={() => send({ type: 'approve', requestId: a.id, decision })}>{decision === 'accept' ? '批准本次' : decision === 'decline' ? '拒绝' : '取消'}</button>)}</section>)}
          {snapshot?.inputs.map(request => <Questions key={request.id} request={request} />)}
          {snapshot?.run && <p className="run-state" role="status">{labels[snapshot.run.state]}</p>}
        </div>
        <div className="composer-host">
          {snapshot?.attachment && <div className="attachment">已附加：{snapshot.attachment.path}<button onClick={() => send({ type: 'detach' })}>移除</button></div>}
          <InputForm inputText={draft} inputFieldRef={input as React.RefObject<HTMLDivElement>} isStreaming={busy} isWaitingForResponse={false}
            isComposing={composing} editModeInfo={{ label: '逐次审批', title: '写入前由作者确认', icon: null }} thinkingEnabled={false}
            activeFileName={snapshot?.attachment?.path ?? null} activeSelection={null} skipAutoActiveContext={false} contextUsage={null}
            onInputChange={setDraft} onCompositionStart={() => setComposing(true)} onCompositionEnd={() => setComposing(false)} onKeyDown={() => {}}
            onSubmit={submit} onCancel={() => send({ type: 'cancel' })} onToggleEditMode={() => {}} onToggleThinking={() => {}}
            onToggleSkipAutoActiveContext={() => send({ type: 'detach' })} onShowCommandMenu={() => setMenu(!menu)} onAttachContext={() => send({ type: 'attach' })}
            completionIsOpen={menu} completionItems={STAGES.map(stage => ({ id: stage.key, label: stage.title, description: stage.prompt, value: stage.prompt, type: 'command' as const }))}
            onCompletionSelect={item => { setDraft(item.value ?? item.label); if (input.current) input.current.textContent = item.value ?? item.label; setMenu(false); }}
            onCompletionClose={() => setMenu(false)} placeholder={ready ? '写下你的想法，或选择一个创作步骤…' : '等待后端连接…'} />
        </div>
      </>}
    </main>
  </PlatformProvider>;
}

createRoot(document.getElementById('root')!).render(<App />);
