import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdir,mkdtemp,readFile,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {_electron as electron,type Page,type Frame} from 'playwright';
import {expect} from '@playwright/test';

async function command(page:Page,title:string){
  const input=page.locator('.quick-input-widget input');
  const option=page.locator('.quick-input-list .monaco-list-row').filter({hasText:title}).first();
  await expect(async()=>{
    await page.bringToFront();
    await page.keyboard.press('Escape');await page.keyboard.press('F1');
    await expect(input).toBeVisible({timeout:1000});
    await input.fill('>'+title);
    await expect(option).toBeVisible({timeout:1000});
  }).toPass({timeout:30000});
  await input.press('Enter');
  await expect(input).toBeHidden();
}

async function trust(page:Page){
  await expect(page.locator('.monaco-workbench')).toBeVisible({timeout:60000});
  const button=page.getByRole('button',{name:/Yes, I trust the authors/});
  if(await button.isVisible())await button.click();
  await command(page,'Manage Workspace Trust');
  const trusted=page.getByText('You trust this folder',{exact:true});
  await expect(async()=>{
    const approve=page.getByRole('button',{name:'Trust',exact:true});
    if(await approve.isVisible())await approve.click();
    await expect(trusted).toBeVisible({timeout:1000});
  }).toPass({timeout:15000});
  await page.keyboard.press('Control+w');
  await expect(trusted).toHaveCount(0);
}

test('desktop F5 builds and launches the default offline LG development host',{
  timeout:240000,skip:!process.env.LG_TEST_DESKTOP_CODE,
},async()=>{
  const source=resolve('.'),root=process.env.LG_VSCODE_TEST_ROOT;
  assert.ok(source.includes('/LitIsLand/')&&root?.includes('/LitIsLand/'));
  const profile=await mkdtemp(root+'/desktop-profile-');
  await mkdir(profile+'/User');
  await writeFile(profile+'/User/settings.json',JSON.stringify({
    'telemetry.telemetryLevel':'off','update.mode':'none','extensions.autoCheckUpdates':false,
    'extensions.autoUpdate':false,'workbench.startupEditor':'none','window.restoreWindows':'none',
  }));
  await mkdir('.test-output/desktop',{recursive:true});
  const original=await readFile('fixtures/workspace/README.md','utf8');
  const env:Record<string,string>={};
  for(const [key,value]of Object.entries(process.env))if(value!==undefined&&
    !/^(VSCODE_|ELECTRON_|NODE_OPTIONS$)/.test(key)&&!/(API_KEY|TOKEN|SECRET|PASSWORD)/.test(key))env[key]=value;
  const app=await electron.launch({executablePath:process.env.LG_TEST_DESKTOP_CODE!,timeout:60000,env,
    args:['--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--js-flags=--max-old-space-size=512',
      ...(!env.DISPLAY?['--ozone-platform=headless']:[]),
      '--user-data-dir',profile,'--extensions-dir',profile+'/extensions','--locale=en',
      '--skip-welcome','--skip-release-notes','--disable-updates',source]});
  let memoryFailure=false;
  let nativeDiagnostics='';
  app.process().stderr?.on('data',chunk=>{
    nativeDiagnostics=(nativeDiagnostics+String(chunk)).slice(-32768);
    if(/heap out of memory|reached heap limit|allocation failed/i.test(String(chunk))){
      memoryFailure=true;console.log('Desktop process reported a memory allocation failure');
    }
  });
  let development:Page|undefined;
  try{
    const page=await app.firstWindow();
    const sourceWindow=await app.browserWindow(page);
    await sourceWindow.evaluate(window=>{window.setBounds({width:1440,height:1000});window.show();window.focus();});
    await trust(page);
    await page.screenshot({path:'.test-output/desktop/source.png'});
    await page.keyboard.press('F5');
    await expect(async()=>{
      const continueTrust=page.getByRole('button',{name:/Trust Folder.*Continue/});
      if(await continueTrust.isVisible())await continueTrust.click();
      const pages=app.windows();
      for(const candidate of pages)if(candidate!==page&&!candidate.isClosed()
        &&(await candidate.title()).includes('Extension Development Host')
        &&await candidate.locator('.monaco-workbench').count())development=candidate;
      assert.ok(development,'F5 has not opened the development window');
    }).toPass({timeout:90000});
    const developmentWindow=await app.browserWindow(development!);
    await developmentWindow.evaluate(window=>{window.setBounds({width:1440,height:1000});window.show();window.focus();});
    await expect(development!.locator('.monaco-workbench')).toBeVisible();
    await command(development!,'LG：打开写作工作台');
    console.log('F5 development host opened; checking sidebar layout');
    const sidebar=development!.locator('.part.sidebar');
    await expect(async()=>{
      const bounds=await sidebar.boundingBox();assert.ok(bounds);
      if(bounds.width>=400)return;
      let dragged=false;
      for(const sash of await development!.locator('.monaco-sash.vertical').all()){
        const handle=await sash.boundingBox();
        if(!handle||handle.height<200||Math.abs(handle.x+handle.width/2-bounds.x-bounds.width)>10)continue;
        const y=handle.y+handle.height/2;
        await development!.mouse.move(handle.x+handle.width/2,y);
        await development!.mouse.down();
        await development!.mouse.move(bounds.x+480,y,{steps:12});
        await development!.mouse.up();dragged=true;break;
      }
      assert.ok(dragged,'Primary sidebar resize handle is missing');
      assert.ok((await sidebar.boundingBox())!.width>=400,'Primary sidebar did not resize');
    }).toPass({timeout:10000});
    console.log('F5 development host opened; LG sidebar expanded');
    let frame:Frame|undefined;
    await expect(async()=>{
      for(const candidate of development!.frames())if(await candidate.getByText('你的故事，正在成形。').count())frame=candidate;
      assert.ok(frame,'LG webview did not activate in the F5 host');
    }).toPass({timeout:45000});
    let view=frame!;
    await expect(view.getByText('离线演示 · 已连接')).toBeVisible();
    const send=async(text:string)=>{
      await view.getByRole('textbox',{name:'Message input'}).fill(text);
      await view.getByRole('button',{name:'Send message',exact:true}).click({noWaitAfter:true});
    };
    await send('离线 F5 流程');
    await expect(view.getByRole('button',{name:'批准本次',disabled:false})).toBeVisible();
    const pendingSession=await view.getByRole('combobox',{name:'创作会话'}).inputValue();
    const pendingItems=await view.locator('.item').count();
    await view.evaluate(()=>{document.documentElement.dataset.lgReloadProbe='before';});
    await command(development!,'Developer: Reload Webviews');
    await expect(async()=>{
      for(const candidate of development!.frames()){
        if(await candidate.getByRole('button',{name:'批准本次',disabled:false}).count()
          &&await candidate.locator('html').getAttribute('data-lg-reload-probe')===null)view=candidate;
      }
      assert.equal(await view.locator('html').getAttribute('data-lg-reload-probe'),null);
      await expect(view.getByRole('button',{name:'批准本次',disabled:false})).toBeVisible({timeout:1000});
    }).toPass({timeout:15000});
    assert.equal(await view.getByRole('combobox',{name:'创作会话'}).inputValue(),pendingSession);
    assert.equal(await view.locator('.item').count(),pendingItems);
    await expect(view.getByRole('button',{name:'批准本次',disabled:false})).toHaveCount(1);
    await view.getByRole('button',{name:'批准本次',disabled:false}).click();
    await expect(view.getByRole('status')).toHaveText('已完成');
    await send('拒绝写入');
    await view.getByRole('button',{name:'拒绝',exact:true,disabled:false}).click();
    await expect(view.getByRole('status')).toHaveText('已完成');
    await send('/slow');
    await view.getByRole('button',{name:'Stop generation',exact:true}).click();
    await expect(view.getByRole('status')).toHaveText('已取消');
    await send('/error');
    await expect(view.getByRole('alert')).toContainText('模拟工具执行失败');
    await send('/input');
    await expect(view.getByText('需要你的意见')).toBeVisible();
    await view.getByRole('button',{name:'人物困境',exact:true}).click();
    await expect(view.getByLabel('希望第一章侧重什么？',{exact:true})).toHaveValue('人物困境');
    await view.getByRole('button',{name:'发送回答',exact:true}).click();
    await expect(view.getByRole('status')).toHaveText('已完成');
    await view.getByRole('button',{name:'作品概况',exact:true}).click();
    await expect(view.getByRole('heading',{name:'创作定位'})).toBeVisible();
    await development!.screenshot({path:'.test-output/desktop/f5-overview.png'});
    await view.getByRole('button',{name:'创作对话',exact:true}).click();
    await send('/disconnect');
    await expect(view.getByRole('alert')).toContainText('模拟连接中断');
    assert.equal(await readFile('fixtures/workspace/README.md','utf8'),original);
    assert.equal(memoryFailure,false,'Desktop reported an allocation failure');
    await writeFile('.test-output/desktop/result.json',JSON.stringify({
      source,profile,f5:true,backend:'mock',checks:['webview-reload-with-pending-approval','approve','decline','cancel','error','input','overview','disconnect'],
    },null,2));
  }finally{
    for(const line of nativeDiagnostics.split('\n'))if(/Assertion failed|Check failed|FATAL ERROR|Address already in use|Starting inspector|node::.*Assertion/i.test(line))console.log(line.slice(0,1000));
    for(const [index,page]of app.windows().entries())await page.screenshot({path:`.test-output/desktop/last-${index}.png`,timeout:5000}).catch(()=>{});
    await app.close();
  }
});
