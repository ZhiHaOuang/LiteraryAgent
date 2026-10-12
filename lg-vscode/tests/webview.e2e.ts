import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { chromium, expect } from '@playwright/test';
import chromiumBinary from '@sparticuz/chromium';
import { MockBackend } from '../src/backend/MockBackend';
import { SessionStore } from '../src/host/SessionStore';
import { ClientMessageSchema } from '../shared/protocol';

test('rendered webview: stream, approval survives reload, deny, cancel, book overview', { timeout: 90_000 }, async (t) => {
  const server = createServer(async (req, res) => {
    try {
      if (req.url === '/') { res.setHeader('Content-Type','text/html'); res.end('<!doctype html><html><head><meta charset="UTF-8"><link rel="stylesheet" href="/fonts/400.css"><link rel="stylesheet" href="/webview.css"><style>:root{--vscode-font-family:"Noto Sans SC",sans-serif}h1{font-family:var(--vscode-font-family)}</style></head><body><div id="root"></div><script src="/webview.js"></script></body></html>'); }
      else if (req.url === '/webview.js' || req.url === '/webview.css') { res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript':'text/css');res.end(await readFile('dist'+req.url)); }
      else if (req.url === '/fonts/400.css' || /^\/fonts\/files\/[a-z0-9-]+\.woff2?$/.test(req.url ?? '')) {res.setHeader('Content-Type',req.url!.endsWith('.css')?'text/css':'font/woff2');res.end(await readFile('node_modules/@fontsource/noto-sans-sc'+req.url!.slice(6)));}
      else { res.statusCode=404;res.end(); }
    } catch {res.statusCode=500;res.end();}
  });
  await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise<void>(resolve=>server.close(()=>resolve())));
  const address=server.address();assert.ok(address&&typeof address!=='string');
  const backend=new MockBackend(1),store=new SessionStore('mock');backend.subscribe(e=>store.apply(e));
  const browser=await chromium.launch({headless:true, executablePath:await chromiumBinary.executablePath(),args:chromiumBinary.args});
  const page=await browser.newPage({viewport:{width:450,height:850},deviceScaleFactor:1});
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  let initialized=false;
  store.subscribe(s=>{ void page.evaluate(snapshot=>window.postMessage({type:'snapshot',snapshot},'*'),s).catch(()=>{}); });
  await page.exposeFunction('lgHost',async(raw:unknown)=>{
    const message=ClientMessageSchema.parse(raw);
    if(message.type==='ready'){
      if(!initialized){initialized=true;await backend.connect();await backend.create();store.state.sessions=await backend.list();}
      store.publish();
    }else if(message.type==='run')await backend.run(store.state.sessionId,message.text);
    else if(message.type==='approve')await backend.approve(message.requestId,message.decision);
    else if(message.type==='cancel') {store.state.run!.state='cancelling';store.publish();await backend.cancel(store.state.sessionId);}
    else if(message.type==='refreshBook')await backend.refreshBook();
  });
  await page.addInitScript(`window.acquireVsCodeApi = () => ({postMessage: message => window.lgHost(message), getState: () => ({}), setState: () => {}});`);
  try {
    await page.goto(`http://127.0.0.1:${address.port}`);
    await expect(page.locator('[aria-label="LG 作品首页"]')).toBeVisible();
    await page.getByRole('button',{name:'创作对话',exact:true}).click();
    const input=page.getByRole('textbox',{name:'Message input'});
    await input.fill('先规划一个原创场景');await page.getByRole('button',{name:'Send message',exact:true}).click();
    await expect(page.getByRole('button',{name:'批准本次'})).toBeVisible();
    await page.reload();await page.getByRole('button',{name:'创作对话',exact:true}).click();await expect(page.getByRole('button',{name:'批准本次'})).toBeVisible();
    await page.getByRole('button',{name:'拒绝',exact:true}).click();
    await expect(page.getByText('已处理',{exact:true})).toBeVisible();
    assert.equal(store.state.items.filter(i=>i.kind==='assistant').length,1);
    await input.fill('/slow');await page.getByRole('button',{name:'Send message',exact:true}).click();
    await expect(page.getByRole('button',{name:'Stop generation',exact:true})).toBeVisible();
    await page.getByRole('button',{name:'Stop generation',exact:true}).click();
    await expect(page.getByRole('status')).toHaveText('已取消');
    await page.getByRole('button',{name:'作品概况',exact:true}).click();
    for(const title of ['创作定位','长篇目标与进度','分卷规划','人物关系','设定','伏笔','当前任务','待审批事项'])await expect(page.getByRole('heading',{name:title,exact:true})).toBeVisible();
    await page.evaluate(()=>document.fonts.ready);await mkdir('.test-output',{recursive:true});await page.screenshot({path:'.test-output/book-overview.png',fullPage:true});
    await page.getByRole('button',{name:'创作对话',exact:true}).click();await page.screenshot({path:'.test-output/chat.png',fullPage:true});
    assert.deepEqual(errors,[]);
  }finally{await mkdir('.test-output',{recursive:true});await page.screenshot({path:'.test-output/last-page.png',fullPage:true});if(errors.length)console.log({pageErrors:errors});await backend.dispose();await browser.close();}
});
