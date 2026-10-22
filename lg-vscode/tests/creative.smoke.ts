/** Opt-in paid-model smoke. Never part of npm test; all writes stay in LG_SMOKE_WORKSPACE. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium, expect } from '@playwright/test';
import chromiumBinary from '@sparticuz/chromium';
import { LiteraryBackend } from '../src/backend/LiteraryBackend';
import {z} from 'zod';
import { SessionStore } from '../src/host/SessionStore';
import { LatestSnapshotSender } from '../src/host/LatestSnapshotSender';
import { ClientMessageSchema } from '../shared/protocol';
const record=(value:unknown):Record<string,unknown>=>{const parsed=z.record(z.unknown()).safeParse(value);return parsed.success?parsed.data:{};};
import type {Item} from '../shared/protocol';

function draftVersion(items:Item[]):number {
  const item=[...items].reverse().find(i=>i.kind==='tool'&&i.title.endsWith('studio_draft')&&i.state==='completed');
  assert.ok(item,'Missing successful candidate result');
  const result=record(item.structured).result;
  const version=record(record(Array.isArray(result)?JSON.parse(item.text):result).version).version_number;
  assert.ok(typeof version==='number'&&Number.isInteger(version)&&version>0,'Missing candidate version number');
  return version;
}

async function main(){
const workspace=process.env.LG_SMOKE_WORKSPACE,python=process.env.LG_TEST_PYTHON,manifest=process.env.LG_TEST_RUNTIME_MANIFEST;
if(!workspace||!python||!manifest)throw Error('Explicit smoke workspace, Python and runtime manifest are required');
if(!workspace.includes('/LitIsLand/'))throw Error('Creative smoke is restricted to LitIsLand');
const evidence=resolve('.test-output/creative');await mkdir(evidence,{recursive:true});
const from=process.env.LG_SMOKE_FROM;
const operations:{tool:string;arguments:Record<string,unknown>}[]=from?JSON.parse(await readFile(evidence+'/approved-operations.json','utf8')):[];
const results:unknown[]=from?JSON.parse(await readFile(evidence+'/results.json','utf8')):[];
let candidateVersion:number|undefined;
if(from&&(from.startsWith('review')||from==='accept'||from==='overview'))candidateVersion=draftVersion(JSON.parse(await readFile(evidence+'/revision.json','utf8')));
const backend=new LiteraryBackend({workspace,python,manifest,runtimeEngine:'codex',env:{...process.env,LITERARYGIANT_REGISTRY_HOME:workspace+'/registry'}});
const store=new SessionStore('codex');backend.subscribe(e=>{store.apply(e);if(e.type==='error')console.log('Backend notice:',e.message);});
const server=createServer(async(req,res)=>{
  try{
    if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end('<!doctype html><html><head><meta charset="UTF-8"><link rel="stylesheet" href="/fonts/400.css"><link rel="stylesheet" href="/webview.css"><style>:root{--vscode-font-family:"Noto Sans SC",sans-serif}h1{font-family:var(--vscode-font-family)}</style></head><body><div id="root"></div><script src="/webview.js"></script></body></html>');}
    else if(req.url==='/webview.js'||req.url==='/webview.css'){res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript':'text/css');res.end(await readFile('dist'+req.url));}
    else if(req.url==='/fonts/400.css'||/^\/fonts\/files\/[a-z0-9-]+\.woff2?$/.test(req.url??'')){res.setHeader('Content-Type',req.url!.endsWith('.css')?'text/css':'font/woff2');res.end(await readFile('node_modules/@fontsource/noto-sans-sc'+req.url!.slice(6)));}
    else{res.statusCode=404;res.end();}
  }catch{res.statusCode=500;res.end();}
});
await new Promise<void>(r=>server.listen(0,'127.0.0.1',r));
const address=server.address();assert.ok(address&&typeof address!=='string');
let browser:Awaited<ReturnType<typeof chromium.launch>>|undefined;
let sender:LatestSnapshotSender|undefined,stopPublishing=()=>{};
try{
  const explicitBrowser=process.env.LG_TEST_BROWSER;
  browser=await chromium.launch({headless:true,executablePath:explicitBrowser??await chromiumBinary.executablePath(),args:explicitBrowser?['--no-sandbox','--disable-dev-shm-usage','--disable-gpu']:chromiumBinary.args});
  const page=await browser.newPage({viewport:{width:560,height:980}});
  let initialization:Promise<void>|undefined,queue=Promise.resolve();
  sender=new LatestSnapshotSender(s=>page.evaluate(snapshot=>window.postMessage({type:'snapshot',snapshot},'*'),s));
  stopPublishing=store.subscribe(s=>sender?.offer(s));
  await page.exposeFunction('lgHost',(raw:unknown)=>{
    queue=queue.then(async()=>{
      const message=ClientMessageSchema.parse(raw);
      if(message.type==='ready'){
        initialization??=(async()=>{await backend.connect();await backend.create();store.state.sessions=await backend.list();})();
        await initialization;store.publish();
      }else if(message.type==='run')await backend.run(store.state.sessionId,message.text);
      else if(message.type==='approve')await backend.approve(message.requestId,message.decision);
      else if(message.type==='answer')await backend.answer(message.requestId,message.answers);
      else if(message.type==='refreshBook')await backend.refreshBook();
      else if(message.type==='cancel')await backend.cancel(store.state.sessionId);
    }).catch(e=>{store.error(e instanceof Error?e.message:'Host error');});return queue;
  });
  await page.addInitScript('window.acquireVsCodeApi=()=>({postMessage:m=>window.lgHost(m),getState:()=>({}),setState:()=>{}})');
  await page.goto(`http://127.0.0.1:${address.port}`);
  await page.getByRole('button',{name:'创作对话',exact:true}).click();
  await expect(page.getByText('本地 Agent · 已连接')).toBeVisible({timeout:60000});
  const stages:[string,string,string][]=[
    ['create','这是独立测试作品，仅调用 studio_status 检查，必要时 studio_initialize 初始化当前作品。不要生成正文或修改别的作品。简短回复。','studio_initialize'],
    ['position','先 reference_search 搜索“原创试写参考简报 浮城 许栎”，只使用当前作品资料。然后 studio_status，studio_save 保存 stage=position。定位原创机械悬疑与人物成长，目标 targetWords=1000000。无需再询问方向，我确认这个测试范围。只写一两句定位，不生成正文。','studio_save'],
    ['plan','继续测试：studio_status 后，用 studio_save 保存 stage=plan。给出四卷长篇规划，每卷一句，updates.volumes 为四个字符串，tasks 为下一步人物世界观。不生成正文。','studio_save'],
    ['world','继续测试：studio_status 后，studio_save 保存 stage=world。人物只有维修师许栎和值守员岑雨；地点衡湾浮城；主题是资源配额与救援选择。updates.characters、relationships、settings、foreshadowing 各写一两条，明确它们是规划。不要复制任何已有作品人物、场景或文字。','studio_save'],
    ['scene','继续测试：studio_status 后，studio_save 保存 stage=scene。第一章“停电之夜”：钟塔停摆，检修廊内救援，先隔离漏电再打开闸门，代价是放弃唯一返乡车票，结尾发现配额记录被篡改。目标正文150—300字。只保存简短场景规划，不生成正文。','studio_save'],
    ['draft','根据已存场景，用 studio_draft 保存150—300字的原创第一章候选。slug=first，title=停电之夜，sequence=1，expected_version_id=0。人物许栎和岑雨，新场景，动作因果清楚。这只是候选，不能采用。随后更新一句全书简介。','studio_draft'],
    ['revision','读取 first 当前版本，进行一次小修订：删除电流烧毁口袋车票的情节。明确许栎留下隔离电源并救人，因而错过末班返乡船，单次船票过期；他的代价来自主动选择留下，不是电流烧票。避免全城已断电却不交代残余带电设备的矛盾。用 studio_draft 保存150—300字修订候选，不采用；用 studio_save stage=review 保存一句修订理由。','studio_draft'],
    ...['CharacterArc','EventsLibrary','PayoffAngst','EmotionRhythm','Worldview'].map(category=>[
      'review-'+category,`当前为独立测试作品，规划与第一章已有候选。读取 first 的 document_history，选择最新候选；调用 studio_review_context，category=${category}，只审阅这一类。按返回的 schema 和 source_spans 填写一条简短有依据的 entry，准确填写 instance_card。用 studio_review 保存 reports={"${category}":报告对象}，source_fingerprint 必须来自刚读取的上下文。不采用，不允许空报告或假造证据。若发现实质冲突请停止并说明。`,'studio_review'] as [string,string,string]),
    ['accept','我明确确认采用 first 最新的、已通过来源校验审阅的候选版本。读取 document_history 与 read_document 核实 version_number 和 expected_version_id，然后调用 studio_accept；不要重复采用、不要绕过审阅校验。','studio_accept'],
    ['overview','更新全书一句话简介，随后 studio_status、studio_save stage=review 更新 tasks 为“等待作者进一步试读”，pending=[]。不要生成更多正文。最后读取 book_dashboard，简要报告真实采用字数。','update_book_overview'],
  ];
  const startStage=from?stages.findIndex(s=>s[0]===from||s[0].startsWith(from+'-')):0;
  assert.ok(startStage>=0,'Unknown resume stage');
  for(const [stage,prompt,expectedTool]of stages.slice(startStage)){
    const previous=store.state.run?.id;const startIndex=store.state.items.length;
    const needsCandidate=stage.startsWith('review-')||stage==='accept';
    if(needsCandidate)assert.ok(candidateVersion,'Review/adoption requires the saved revision version');
    const exactPrompt=needsCandidate?prompt+` 本次指定候选为 first 的 version_number=${candidateVersion}，必须审阅或采用这个版本。active_version_id 仅用于乐观并发校验，禁止用当前激活旧稿替代指定候选。`:prompt;
    await page.getByRole('textbox',{name:'Message input'}).fill(exactPrompt);
    await page.getByRole('button',{name:'Send message',exact:true}).click({noWaitAfter:true});
    console.log('Stage started:',stage);
    const started=Date.now();const answered=new Set<string>();
    while(Date.now()-started<240000){
      if(store.state.connection==='disconnected')throw Error('Backend disconnected');
      for(const approval of store.state.approvals.filter(a=>a.state==='pending'&&!answered.has(a.id))){
        const raw=approval.details.slice(approval.details.indexOf('{'));
        const operation=JSON.parse(raw) as {tool:string;arguments:Record<string,unknown>};
        assert.ok(['studio_initialize','studio_save','studio_draft','studio_review','studio_accept','update_book_overview'].includes(operation.tool),'Unexpected mutation: '+operation.tool);
        if('slug'in operation.arguments)assert.equal(operation.arguments.slug,'first');
        if(operation.tool==='studio_draft')assert.ok(String(operation.arguments.content).length<=700,'Candidate exceeds smoke budget');
        if(operation.tool==='studio_accept')assert.equal(stage,'accept','Early adoption is forbidden');
        if(operation.tool==='studio_review'||operation.tool==='studio_accept')assert.equal(operation.arguments.version_number,candidateVersion,'Wrong candidate version');
        operations.push(operation);answered.add(approval.id);
        await writeFile(evidence+'/approved-operations.json',JSON.stringify(operations,null,2));
        await page.locator('.approval').filter({has:page.locator('button:enabled').filter({hasText:'批准本次'})}).getByRole('button',{name:'批准本次',exact:true}).first().click({noWaitAfter:true});
        console.log('Reviewed test operation:',operation.tool);
      }
      if(store.state.run?.id!==previous&&store.state.run&&!store.busy)break;
      await new Promise(r=>setTimeout(r,150));
    }
    const items=store.state.items.slice(startIndex);
    await writeFile(evidence+'/'+stage+'.json',JSON.stringify(items,null,2));
    assert.equal(store.state.run?.state,'completed','Stage did not complete: '+stage+' '+store.state.error);
    assert.ok(items.some(i=>i.kind==='tool'&&i.title.endsWith(expectedTool)&&i.state==='completed'),'Expected successful operation: '+expectedTool);
    if(stage==='draft'||stage==='revision')candidateVersion=draftVersion(items);
    results.push({stage,status:store.state.run.state,tools:items.filter(i=>i.kind==='tool').map(i=>({name:i.title,state:i.state}))});
    await writeFile(evidence+'/results.json',JSON.stringify(results,null,2));
    console.log('Stage passed:',stage);
  }
  await backend.refreshBook();assert.ok(store.state.book.acceptedWords>0);assert.equal(store.state.book.targetWords,1000000);
  await page.getByRole('button',{name:'作品概况',exact:true}).click();await page.evaluate(()=>document.fonts.ready);
  await page.screenshot({path:evidence+'/overview.png',fullPage:true});
  await writeFile(evidence+'/final-snapshot.json',JSON.stringify(store.snapshot(),null,2));
  console.log('Creative smoke passed:',JSON.stringify({workspace,acceptedWords:store.state.book.acceptedWords,steps:results.length}));
}finally{
  stopPublishing();sender?.dispose();
  await writeFile(evidence+'/last-snapshot.json',JSON.stringify(store.snapshot(),null,2));
  await backend.dispose();await browser?.close();await new Promise<void>(r=>server.close(()=>r()));
}
}
void main().catch(error=>{console.error(error instanceof Error?error.message:'Creative smoke failed');process.exitCode=1;});
