import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
async function files(dir:string):Promise<string[]> { const entries=await readdir(dir,{withFileTypes:true});return (await Promise.all(entries.map(e=>e.isDirectory()?files(path.join(dir,e.name)):Promise.resolve([path.join(dir,e.name)])))).flat(); }
test('browser source has no backend transport, Node or Qwen/ACP imports',async()=>{
  for(const file of await files('webview')){
    if(!/\.[tj]sx?$/.test(file))continue;
    const source=await readFile(file,'utf8');
    assert.doesNotMatch(source,/(?:from|import)\s*['"][^'"]*(?:backend\/|node:|@qwen-code|@agentclientprotocol|child_process)[^'"]*['"]/,file);
    assert.doesNotMatch(source,/['"](?:thread\/start|turn\/start|turn\/interrupt)['"]/,file);
  }
});
