import {useState} from 'react';
import {z} from 'zod';
import {TaskOptionsSchema,type TaskOptions,type AuthorTaskRequest,type ClientMessage} from '../shared/protocol';

type Send=(message:ClientMessage)=>void;
const taskState=z.object({runId:z.string().optional(),status:z.string(),reason:z.string().nullable().optional(),
  fingerprint:z.string().optional(),document:z.string().optional(),sourceStale:z.boolean().optional(),
  nextAction:z.string().optional(),inputPolicyChanged:z.boolean().optional(),
  clarification:z.object({question:z.string().min(1),options:TaskOptionsSchema}).optional(),
  candidate:z.object({document:z.string(),versionNumber:z.number().int().positive()}).optional(),
  decision:z.object({options:z.array(z.object({id:z.string(),title:z.string(),summary:z.string()}))}).optional()});
const receipt=z.object({operationId:z.string(),status:z.enum(['committed','not_committed','unknown']),
  toolOutcome:z.string(),derivedFiles:z.string()});
const validatedNarrative=z.object({schema:z.literal('lg.narrative-execution.v2'),
  status:z.literal('completed'),resultReleased:z.literal(true),
  evidenceValidation:z.object({contract:z.literal('visible_record_exact_quote.v1'),status:z.literal('validated')}),
  analysis:z.object({inference:z.string().trim().min(1).max(4000),
    evidence:z.array(z.object({record_id:z.number().int().positive(),quote:z.string().min(1).max(2000)
      .refine(value=>Boolean(value.trim()))}).strict()).min(1).max(20),
    uncertainty:z.string().trim().min(1).max(2000)}).strict()});
const statuses:Record<string,string>={waiting_author:'等待你选择方向',awaiting_review:'候选已保存，等待审读与采用',
  adopted:'此候选已采用',stopped:'任务已停止',failed:'执行失败，请核对保存结果',interrupted:'执行中断，请先检查产物',
  unsupported:'当前执行器不支持这项保证',insufficient_context:'有效认知资料不足',stale:'来源已变化，需要重新预览',
  completed:'已完成候选分析',ready:'等待开始'};
const reasons:Record<string,string>={source_changed:'基础版本已变化',author_cancelled:'作者取消了任务',
  input_char_budget_exceeded:'输入超过本任务字符预算',input_budget_exceeded:'输入超过本任务字符预算',
  model_call_budget_exhausted:'模型请求次数已用完',no_textual_progress:'没有产生文字修改',
  strict_mode_requires_native_responses:'此有限推演需要 Native Responses 执行器',
  no_visible_confirmed_records:'当前位置没有可用的已确认认知记录',registered_view_truncated:'当前认知记录超出读取上限',
  source_needs_review:'认知记录的来源需要复核',preview_changed:'预览已经变化'};
reasons.invalid_evidence='输出引用未通过校验，模型原文未发布。';
reasons.model_execution_failed='模型请求或输出结构未通过检查，结果未发布。';
const describe=(reason:string)=>reasons[reason]??reason;

export function buildClarifiedPolishRequest(options:TaskOptions,answer:string):Extract<AuthorTaskRequest,{action:'start'}>{
  const original=TaskOptionsSchema.parse(options);
  if(original.strategy!=='polish')throw Error('只有指定选区润色可以补充本次要求。');
  if(!answer.trim())throw Error('请先说明具体希望改哪一点。');
  const request=original.request+'\n\n作者补充：\n'+answer.trim();
  if(request.length>4000)throw Error('原要求与补充合计不能超过 4000 字符，请缩短补充内容。');
  return {action:'start',options:TaskOptionsSchema.parse({...original,request})};
}

function PolishClarification({question,options,send,disabled}:{question:string;options:TaskOptions;send:Send;disabled:boolean}){
  const [answer,setAnswer]=useState(''),[error,setError]=useState('');
  return <form aria-label="补充局部润色要求" onSubmit={event=>{
    event.preventDefault();if(disabled)return;
    try{const request=buildClarifiedPolishRequest(options,answer);setError('');send({type:'boundedTask',request});}
    catch(cause){setError(cause instanceof Error?cause.message:'补充要求无效，请检查后再试。');}
  }}>
    <p>{question}</p>
    <details><summary>查看原要求和准确选区</summary>
      <p><strong>原要求</strong></p><pre>{options.request}</pre>
      <p><strong>准确选区</strong></p><pre>{options.passage}</pre></details>
    <label>具体希望改哪一点<textarea required value={answer} maxLength={4000} disabled={disabled}
      onChange={event=>{setAnswer(event.target.value);setError('');}} placeholder="例如：对话保持原样，只调整动作之间的停顿。"/></label>
    <p className="muted">原任务已停止，已用请求不会重置。补充后是独立的新任务，批准后最多再使用 1 次模型请求；保留原选区、原要求和输入预算上限，正文仍需独立采用。</p>
    <p className="muted">新任务预算：最多 {options.maxModelCalls??1} 次模型请求，宿主输入最多 {options.maxInputChars??24000} 字符；这不是费用总额上限。</p>
    {error&&<p role="alert">{error}</p>}
    <button type="submit" disabled={disabled||!answer.trim()}>预览新任务并申请执行</button>
  </form>;
}

export function TaskResult({structured,send,disabled}:{structured:unknown;send:Send;disabled:boolean}){
  if(!structured||typeof structured!=='object')return null;
  const data=structured as Record<string,unknown>;
  const checked=taskState.safeParse(data.literaryTask);
  const call=(request:AuthorTaskRequest)=>send({type:'boundedTask',request});
  if(checked.success){
    const t=checked.data;
    return <section className="bounded-result" aria-label="文学任务状态">
      <p><strong>{statuses[t.status]??t.status}</strong>{t.reason?' · '+describe(t.reason):''}</p>
      {t.sourceStale&&<p role="status">基础资料已经变化，请复核后创建新任务。</p>}
      {t.status==='stopped'&&t.reason==='no_textual_progress'&&t.nextAction==='clarify_request'&&
        !t.sourceStale&&!t.inputPolicyChanged&&t.clarification?.options.strategy==='polish'&&
        <PolishClarification key={t.runId+':'+t.fingerprint} question={t.clarification.question}
          options={t.clarification.options} send={send} disabled={disabled}/>}
      {t.status==='waiting_author'&&t.decision&&t.runId&&t.fingerprint&&<>
        <p>这里只保存了备选。选择需要独立创作批准，选择后再继续写作。</p>
        {t.decision.options.map(option=><div key={option.id}><strong>{option.title}</strong><p>{option.summary}</p>
          <button disabled={disabled||t.sourceStale} onClick={()=>call({action:'select',taskId:t.runId!,
            optionId:option.id,fingerprint:t.fingerprint!})}>选择此方向</button></div>)}
        <button disabled={disabled||t.sourceStale} onClick={()=>call({action:'advance',taskId:t.runId!,fingerprint:t.fingerprint!})}>已选择，继续写作</button>
      </>}
      {t.candidate&&<p>已保存候选 v{t.candidate.versionNumber}；生成任务没有自动采用正文。</p>}
      {t.status==='awaiting_review'&&t.candidate&&<button disabled={disabled||t.sourceStale}
        onClick={()=>call({action:'reviewAdopt',document:t.candidate!.document,version:t.candidate!.versionNumber})}>审读并申请采用</button>}
      {t.runId&&<button disabled={disabled} onClick={()=>call({action:'read',taskId:t.runId!})}>刷新此任务状态</button>}
    </section>;
  }
  const committed=receipt.safeParse(data.operationReceipt);
  if(committed.success){
    const r=committed.data;
    return <section aria-label="正式提交回执"><p>{r.status==='committed'?'数据库修改已提交。':
      r.status==='not_committed'?'数据库修改未提交。':'提交结果尚不能确定，请核对回执与版本后再操作。'}</p>
      <p className="muted">回执不证明章节文件和索引同步成功。取消不会撤销已经提交的修改。</p>
      <button disabled={disabled} onClick={()=>call({action:'receipt',operationId:r.operationId})}>查询最新提交状态</button></section>;
  }
  if(data.narrativeResult&&typeof data.narrativeResult==='object'){
    const n=data.narrativeResult as {schema?:string;status?:string;text?:string;reasons?:string[]};
    const verified=validatedNarrative.safeParse(n);
    const legacy=n.schema===undefined||n.schema==='lg.narrative-execution.v1';
    return <section aria-label="有限认知推演结果"><strong>{statuses[n.status??'']??n.status}</strong>
      <p>仅使用已登记认知进行候选分析，不修改正文、正式事实或剧情。覆盖不完整。</p>
      {n.reasons?.map((reason,i)=><p key={i}>{describe(reason)}</p>)}
      {verified.success?<>
        <p>已核对引用的记录与原文。引用有效不代表推理正确，请由作者判断。</p>
        <h4>推断</h4><p>{verified.data.analysis.inference}</p>
        <h4>登记证据</h4>{verified.data.analysis.evidence.map((item,i)=><blockquote key={i}>
          <p>记录 {item.record_id}</p><p>{item.quote}</p></blockquote>)}
        <h4>不确定性</h4><p>{verified.data.analysis.uncertainty}</p>
      </>:legacy&&n.text?<><p>旧版输出：未校验证据引用，请人工核对。</p><p>{n.text}</p></>:
        n.status==='completed'?<p>结果结构未通过检查，未展示推断；请检查运行记录。</p>:null}</section>;
  }
  if(data.sourceRevalidation&&typeof data.sourceRevalidation==='object'){
    const s=data.sourceRevalidation as {reviewCandidates?:{document:string;version:number;title:string}[]};
    return <section><p>这里只列出已知直接关联。原有作者确认保留；请逐项复核候选和来源，未登记的语义影响仍未知。</p>
      {s.reviewCandidates?.map(c=><button key={c.document+':'+c.version} disabled={disabled}
        onClick={()=>call({action:'reviewAdopt',document:c.document,version:c.version})}>审读并申请采用 · {c.title} v{c.version}</button>)}</section>;
  }
  return null;
}

export function LiteraryTasksPanel({send,disabled}:{send:Send;disabled:boolean}){
  const [kind,setKind]=useState('polish'),[document,setDocument]=useState(''),[request,setRequest]=useState(''),
    [passage,setPassage]=useState(''),[disagreement,setDisagreement]=useState(true),[position,setPosition]=useState(0);
  const call=(request:AuthorTaskRequest)=>send({type:'boundedTask',request});
  return <details className="bounded-task-panel"><summary>有限写作任务与认知推演</summary>
    <form onSubmit={event=>{event.preventDefault();
      if(kind==='sources')call({action:'sources',document});
      else if(kind==='character'||kind==='reader')call({action:'narrative',options:{audience:kind,position,
        operation:kind==='character'?'character_next_action':'reader_expectation',
        ...(kind==='character'?{holder_document:document}:{})}});
      else call({action:'start',options:{strategy:kind as 'polish'|'chapter',document,request,
        ...(kind==='polish'?{passage}:{plot_disagreement:disagreement})}});
    }}>
      <label>任务<select value={kind} onChange={e=>setKind(e.target.value)}>
        <option value="polish">指定选区润色</option><option value="chapter">章节创作</option>
        <option value="character">人物行动推演</option><option value="reader">读者预期推演</option>
        <option value="sources">复核来源影响</option></select></label>
      {kind!=='reader'&&<label>{kind==='character'?'人物标识':'章节标识'}<input required value={document}
        maxLength={512} onChange={e=>setDocument(e.target.value)} placeholder="已保存资料的标识"/></label>}
      {(kind==='polish'||kind==='chapter')&&<label>本次创作要求<textarea required value={request} maxLength={4000} onChange={e=>setRequest(e.target.value)}/></label>}
      {kind==='polish'&&<label>准确选区<textarea required value={passage} maxLength={24000} onChange={e=>setPassage(e.target.value)}/></label>}
      {kind==='chapter'&&<label><input type="checkbox" checked={disagreement} onChange={e=>setDisagreement(e.target.checked)}/>先比较剧情方向，等我选择后再写正文</label>}
      {(kind==='character'||kind==='reader')&&<><label>{kind==='character'?'故事坐标':'阅读坐标'}<input type="number" step="1" required value={position} onChange={e=>setPosition(Number(e.target.value))}/></label>
        <p className="muted">只读当前位置已确认的登记认知；不带入当前对话或全局资料。当前仅 Native Responses 支持此项有限保证。</p></>}
      <button type="submit" disabled={disabled}>{kind==='sources'?'读取复核信息':'预览并申请执行'}</button>
    </form>
  </details>;
}
