import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdir,readFile} from 'node:fs/promises';
import {chromium,expect,type Frame} from '@playwright/test';
import chromiumBinary from '@sparticuz/chromium';

test('installed VSIX in actual VS Code: activation, approvals, cancel, history, readonly diff and file protection',{timeout:180000,skip:!process.env.LG_VSCODE_URL},async()=>{
  const originalFile=await readFile('fixtures/workspace/README.md','utf8');
  const executablePath=process.env.LG_TEST_BROWSER||await chromiumBinary.executablePath();
  const browser=await chromium.launch({headless:true,executablePath,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--no-proxy-server','--host-resolver-rules=MAP *.localhost 127.0.0.1']});
  const page=await browser.newPage({viewport:{width:1400,height:1000}});
  await page.context().grantPermissions(['local-network-access']);
  page.on('crash',()=>console.log('VS Code browser page crashed'));
  page.on('close',()=>console.log('VS Code browser page closed'));
  let localResourceError:string|undefined;
  page.on('requestfailed',request=>{const host=new URL(request.url()).hostname;if(host.endsWith('.localhost')) {
    localResourceError='Local webview resource failed: '+request.failure()?.errorText;
    console.log(localResourceError);
  }});
  await page.context().route(url=>!['127.0.0.1','localhost'].includes(url.hostname)&&!url.hostname.endsWith('.localhost'),route=>route.abort());
  if(process.env.LG_TEST_WEBVIEW_ENDPOINT) {
    const origin=new URL(process.env.LG_VSCODE_URL!).origin;
    await page.route(url=>url.origin===origin&&url.pathname==='/',async route=>{
      const response=await route.fetch(),html=await response.text();
      const pattern=/(<meta id="vscode-workbench-web-configuration" data-settings=")([^"]+)(")/;
      assert.match(html,pattern,'Missing VS Code web configuration');
      const body=html.replace(pattern,(_all,before,encoded,after)=>{
        const config=JSON.parse(encoded.replaceAll('&quot;','"').replaceAll('&amp;','&'));
        config.webviewEndpoint=process.env.LG_TEST_WEBVIEW_ENDPOINT;
        return before+JSON.stringify(config).replaceAll('&','&amp;').replaceAll('"','&quot;')+after;
      });
      const headers=response.headers(),csp=headers['content-security-policy'];
      assert.ok(csp?.includes("frame-src 'self' https://*.vscode-cdn.net"),'Unexpected VS Code frame policy');
      headers['content-security-policy']=csp.replace('https://*.vscode-cdn.net',process.env.LG_TEST_WEBVIEW_ENDPOINT!.replace('{{uuid}}','*').replace(/\/$/,''));
      await route.fulfill({response,body,headers});
    });
  }
  await mkdir('.test-output/vscode',{recursive:true});
  const screenshot=async(name:string)=>{await page.screenshot({path:'.test-output/vscode/'+name,timeout:5000}).catch(()=>console.log('Diagnostic screenshot unavailable:',name));};
  try{
    await page.goto(process.env.LG_VSCODE_URL!,{waitUntil:'domcontentloaded'});
    await expect(page.locator('.monaco-workbench')).toBeVisible({timeout:60000});
    console.log('VS Code page loaded');
    await screenshot('loaded.png');
    const trust=page.getByRole('button',{name:/Yes, I trust the authors/});
    try{await trust.waitFor({state:'visible',timeout:10000});await trust.click();}catch{}
    const command=async(title:string)=>{
      const input=page.locator('.quick-input-widget input');
      await expect(async()=>{
        if(!await input.isVisible()) {await page.keyboard.press('Escape');await page.keyboard.press('F1');}
        await expect(input).toBeVisible({timeout:1000});
      }).toPass({timeout:15000});
      await input.fill('>'+title);
      const option=page.locator('.quick-input-list .monaco-list-row').filter({hasText:title}).first();
      await expect(option).toBeVisible({timeout:60000});await option.click();
      await expect(input).toBeHidden();
    };
    if(await page.getByText('Restricted Mode',{exact:true}).first().isVisible()) {
      await command('Manage Workspace Trust');
      await page.getByRole('button',{name:'Trust',exact:true}).click();
      await expect(page.getByText('Restricted Mode',{exact:true})).toHaveCount(0,{timeout:30000});
      await page.keyboard.press('Control+w');
      await expect(page.getByText('You trust this folder',{exact:true})).toHaveCount(0);
    }
    await command('LG：打开写作工作台');
    console.log('LG open command dispatched');
    await screenshot('open-command.png');
    let frame:Frame|undefined;
    const start=Date.now();
    while(!frame&&Date.now()-start<60000){
      if(localResourceError)throw Error(localResourceError);
      for(const f of page.frames())if(await f.getByText('你的故事，正在成形。').count())frame=f;
      if(!frame)await new Promise(r=>setTimeout(r,250));
    }
    assert.ok(frame,'LG webview did not activate');
    await expect(frame.getByText('离线演示 · 已连接')).toBeVisible();
    const bounds=await frame.locator('.workbench').boundingBox();
    assert.ok(bounds&&bounds.height>=500,'LG requires a usable full-height workspace');
    await frame.getByRole('textbox',{name:'Message input'}).fill('测试场景');
    await frame.getByRole('button',{name:'Send message',exact:true}).click({noWaitAfter:true});
    await expect(frame.getByRole('button',{name:'批准本次'})).toBeVisible({timeout:30000});
    await screenshot('approval.png');
    await frame.getByRole('button',{name:'批准本次'}).click({noWaitAfter:true});
    await expect(frame.getByRole('status')).toHaveText('已完成');
    await frame.getByRole('button',{name:/查看只读差异/}).click();
    await expect(page.getByRole('tab',{name:/mock-scene.md/})).toBeVisible();
    await screenshot('readonly-diff.png');
    await frame.getByRole('button',{name:/打开产物/}).click();
    await expect(page.getByRole('tab',{name:/README.md/})).toBeVisible();
    await page.keyboard.press('Control+End');await page.keyboard.type('\nLG UNSAVED TEST');
    await frame.getByRole('textbox',{name:'Message input'}).fill('未保存时必须拦截');
    await frame.getByRole('button',{name:'Send message',exact:true}).click({noWaitAfter:true});
    await expect(frame.getByRole('alert')).toContainText('请先保存或关闭未保存');
    await screenshot('dirty-file-blocked.png');
    await command('File: Revert File');
    const revert=page.getByRole('button',{name:/^Revert$/});if(await revert.isVisible())await revert.click();
    await frame.getByRole('button',{name:'作品概况',exact:true}).click();
    await expect(frame.getByRole('heading',{name:'创作定位'})).toBeVisible();
    await screenshot('book-overview.png');
    assert.equal(await readFile('fixtures/workspace/README.md','utf8'),originalFile);
    await frame.getByRole('button',{name:'创作对话',exact:true}).click();
    await frame.getByRole('textbox',{name:'Message input'}).fill('拒绝这次测试写入');
    await frame.getByRole('button',{name:'Send message',exact:true}).click({noWaitAfter:true});
    await frame.getByRole('button',{name:'拒绝',exact:true,disabled:false}).click();
    await expect(frame.getByRole('status')).toHaveText('已完成');
    await expect(frame.locator('.item').last()).toContainText('作者已拒绝；未写入。');
    await frame.getByRole('textbox',{name:'Message input'}).fill('/slow');
    await frame.getByRole('button',{name:'Send message',exact:true}).click({noWaitAfter:true});
    await frame.getByRole('button',{name:'Stop generation',exact:true}).click();
    await expect(frame.getByRole('status')).toHaveText('已取消');
    const previousSession=await frame.getByRole('combobox',{name:'创作会话'}).inputValue();
    const previousCount=await frame.locator('.item').count();
    await frame.getByRole('button',{name:'新对话',exact:true}).click();
    await expect(frame.getByText('你的故事，正在成形。')).toBeVisible();
    await frame.getByRole('combobox',{name:'创作会话'}).selectOption(previousSession);
    await expect(frame.locator('.item')).toHaveCount(previousCount);
    await expect(frame.locator('.approval button:enabled')).toHaveCount(0);
    await expect(frame.getByRole('button',{name:'Stop generation',exact:true})).toHaveCount(0);
    await screenshot('history-restored.png');
    await frame.getByRole('textbox',{name:'Message input'}).fill('/input');
    await frame.getByRole('button',{name:'Send message',exact:true}).click({noWaitAfter:true});
    await frame.getByRole('button',{name:'人物困境',exact:true}).click();
    await expect(frame.getByLabel('希望第一章侧重什么？',{exact:true})).toHaveValue('人物困境');
    await frame.getByRole('button',{name:'发送回答',exact:true}).click();
    await expect(frame.getByRole('status')).toHaveText('已完成');
    await screenshot('question-answered.png');
  }catch(error){
    console.log('VS Code failure:',error instanceof Error?error.message:'Unknown test failure');
    throw error;
  }finally{
    if(!page.isClosed()){
      console.log('Frame origins:',page.frames().map(f=>{try{return new URL(f.url()).origin;}catch{return 'unresolved';}}));
      await page.screenshot({path:'.test-output/vscode/last-page.png',timeout:5000}).catch(()=>{});
      console.log('VS Code visible text:',await page.locator('body').innerText({timeout:5000}).then(t=>t.slice(-1000)).catch(()=>'(unavailable)'));
    }
    await browser.close();
  }
});
