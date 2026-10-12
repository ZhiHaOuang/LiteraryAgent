import React,{useEffect,useState} from 'react';
import {LIBRARY_TOPICS,type Snapshot,type LibraryTopic,type WritingAction,type ClientMessage} from '../shared/protocol';

type Props={snapshot:Snapshot;send:(message:ClientMessage)=>void;chat:()=>void;open:(topic:LibraryTopic)=>void};
const status=(value:string)=>({candidate:'候选，尚未采用',accepted:'已采用',final:'已定稿',canonical:'已确认事实',planned:'规划',proposed:'待确认提案',current:'当前有效',stale:'已过期，需要重审','needs-canon-review':'设定已变化，需要复核',open:'未回收',paid:'已回收',abandoned:'已放弃'}[value]??value);

export function HomePanel({snapshot:s,send,chat,open}:Props){
  const ready=s.connection==='ready'&&!!s.sessionId,busy=!!s.run&&['running','waiting','cancelling'].includes(s.run.state);
  const approvals=s.approvals.filter(a=>a.state==='pending').length;
  return <div className="workspace-panel home-panel" aria-label="LG 作品首页">
    <span className="eyebrow">当前作品</span><h1>{s.book.name}</h1>
    <p className="book-overview">{s.book.overview||'你的故事，正在成形。'}</p>
    {s.mode==='mock'&&<p className="demo-note">离线示例 · 不写入作品，不调用模型</p>}
    <div className="home-progress"><strong>{s.book.acceptedWords.toLocaleString()}<small>已采用字数</small></strong><span>目标 {s.book.targetWords.toLocaleString()} 字</span></div>
    <progress max={Math.max(s.book.targetWords,1)} value={s.book.acceptedWords} aria-label="首页已采用进度" />
    <p className="muted">已写 {s.book.writtenWords.toLocaleString()} 字 · {s.book.chapters.length} 个章节条目 · 候选不计入采用进度</p>
    <div className="home-actions"><button className="primary-action" onClick={chat}>{busy?'查看当前创作任务':'继续创作'}</button><button disabled={!ready} onClick={()=>send({type:'refreshBook'})}>同步已保存资料</button></div>
    {approvals>0&&<button className="approval-shortcut" onClick={chat}>处理作者审批（{approvals}）</button>}
    <section className="book-section"><h2>写作资料与工作台</h2><p className="muted">与 CLI 共用已保存资料；打开页面只读取，生成更新由你发起。</p>
      <div className="workspace-grid">{LIBRARY_TOPICS.map(t=><button key={t.key} aria-label={'打开'+t.title} disabled={!ready} onClick={()=>open(t.key)}><strong>{t.title}</strong><span>{t.description}</span></button>)}</div>
    </section>
    <section className="book-section"><h2>接下来做什么</h2>{s.book.tasks.length?<ul>{s.book.tasks.map((task,i)=><li key={i}>{task}</li>)}</ul>:<p className="muted">先确认定位、规划和人物，再开始章节创作。</p>}
      <button onClick={()=>open('pending')} disabled={!ready}>查看待确认资料</button></section>
  </div>;
}

export function LibraryPanel({snapshot:s,send,chat,open}:Props){
  const lib=s.library,topic=LIBRARY_TOPICS.find(t=>t.key===lib.query.topic)!;
  const [search,setSearch]=useState(lib.query.query);
  useEffect(()=>{setSearch(lib.query.query);},[lib.query.topic,lib.query.query]);
  const busy=!!s.run&&['running','waiting','cancelling'].includes(s.run.state);
  const canWrite=s.connection==='ready'&&!!s.sessionId&&!busy&&!lib.loading&&!lib.stale;
  const act=(action:WritingAction)=>{
    const preview=lib.detail??lib.page;if(!preview)return;
    send({type:'writingAction',topic:preview.topic,key:lib.detail?.key??'',action,fingerprint:preview.fingerprint});chat();
  };
  const actions=lib.detail?.actions??(topic.generate&&lib.page?['generate'] as WritingAction[]:[]);
  return <div className="workspace-panel library-panel" aria-label="LG 文学资料库">
    <div className="library-toolbar"><label>资料分类<select aria-label="资料分类" value={lib.query.topic} onChange={e=>open(e.target.value as LibraryTopic)}>{LIBRARY_TOPICS.map(t=><option value={t.key} key={t.key}>{t.title}</option>)}</select></label><button disabled={lib.loading||s.connection!=='ready'} onClick={()=>send({type:'refreshBook'})}>刷新资料</button></div>
    <div className="section-heading"><div><span className="eyebrow">{lib.detail?'资料详情与来源':'已保存的文学资料'}</span><h2>{lib.detail?.title??topic.title}</h2></div>{lib.detail&&<button onClick={()=>send({type:'library',query:lib.query})}>返回列表</button>}</div>
    {s.mode==='mock'&&<p className="demo-note">离线示例数据 · 不代表真实作品</p>}
    {lib.stale&&<p className="sync-note" role="status">已保存资料有变化，正在等待同步；生成与采用暂不可用。</p>}
    {lib.loading&&<p className="muted" role="status">正在读取已保存资料…</p>}
    {lib.error&&<p className="error" role="alert">{lib.error}</p>}
    {!!actions.length&&<div className="library-actions">{actions.map(action=><button key={action} disabled={!canWrite||(action==='adopt'&&!!lib.detail?.truncated)} onClick={()=>act(action)}>{action==='generate'?'生成更新':action==='review'?'审阅此版本':'确认采用'}</button>)}<p className="muted">生成更新会先保存候选；采用需要预览和单次确认。</p></div>}
    {lib.detail?<>
      <p className="source-label">{lib.detail.origin}{lib.detail.status?' · '+status(lib.detail.status):''}</p>
      {lib.detail.truncated&&<p className="sync-note">内容超过 6 万字符，此处仅显示前段；请通过 CLI 查看全文。此预览不能直接采用。</p>}
      <div className="library-body">{lib.detail.body}</div>
      {!!lib.detail.versions.length&&<section className="book-section"><h3>最近版本（最多 50 条）</h3><ul>{lib.detail.versions.map(v=><li key={v.number}>v{v.number} · {status(v.state)} · {v.reason}{v.key&&<button onClick={()=>{send({type:'entry',topic:'pending',key:v.key});}}>预览候选 v{v.number}</button>}</li>)}</ul></section>}
    </>:<>
      <form className="library-search" onSubmit={e=>{e.preventDefault();send({type:'library',query:{topic:lib.query.topic,query:search,offset:0}});}}><label htmlFor="library-search">搜索当前资料</label><div><input id="library-search" maxLength={120} value={search} onChange={e=>setSearch(e.target.value)} placeholder="人物、标题或内容…"/><button type="submit" disabled={s.connection!=='ready'}>搜索</button></div></form>
      {lib.page&&<p className="muted">共 {lib.page.total} 项 · 每页最多 24 项</p>}
      <div className="library-list">{lib.page?.entries.map(row=><button key={row.key} aria-label={'查看'+row.title} onClick={()=>send({type:'entry',topic:lib.query.topic,key:row.key})}><div><strong>{row.title}</strong>{row.status&&<span className="state-badge">{status(row.status)}</span>}</div><p>{row.summary}</p><small>{row.origin}</small></button>)}</div>
      {lib.page&&!lib.page.entries.length&&!lib.loading&&<div className="library-empty"><h3>尚无匹配资料</h3><p>CLI 中保存的资料会自动同步到这里。可以继续创作或显式生成更新。</p></div>}
      {lib.page&&<div className="pagination"><button disabled={!lib.page.offset||lib.loading} onClick={()=>send({type:'library',query:{...lib.query,offset:Math.max(0,lib.page!.offset-24)}})}>上一页</button><button disabled={lib.page.nextOffset===null||lib.loading} onClick={()=>send({type:'library',query:{...lib.query,offset:lib.page!.nextOffset!}})}>下一页</button></div>}
    </>}
  </div>;
}
