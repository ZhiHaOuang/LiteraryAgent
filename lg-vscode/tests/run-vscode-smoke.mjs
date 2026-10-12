import {spawn} from 'node:child_process';
import {resolve} from 'node:path';
import {once} from 'node:events';
import {createServer} from 'node:http';
import {readFile,mkdir,writeFile} from 'node:fs/promises';

const root=process.env.LG_VSCODE_TEST_ROOT;
if(!root||!root.includes('/LitIsLand/'))throw Error('Set LG_VSCODE_TEST_ROOT to the isolated LitIsLand VS Code test directory');
const workspace=resolve(process.env.LG_TEST_VSCODE_WORKSPACE||'fixtures/workspace');
if(!workspace.startsWith(resolve(root)+'/'))throw Error('Test workspace must remain in the LitIsLand audit directory');
const settings={'telemetry.telemetryLevel':'off','update.mode':'none','extensions.autoUpdate':false,'literarygiant.backend':'mock'};
if(process.env.LG_TEST_VSCODE_REAL_LIBRARY){
  const python=process.env.LG_TEST_FIXTURE_PYTHON,manifest=process.env.LG_TEST_RUNTIME_MANIFEST;
  if(!python?.startsWith(resolve(root)+'/')||!manifest?.includes('/LitIsLand/'))throw Error('Set explicit isolated fixture launcher and pinned manifest');
  Object.assign(settings,{'literarygiant.backend':'codex','literarygiant.pythonPath':python,'literarygiant.runtimeManifest':manifest});
}
// This pinned server overrides --user-data-dir with <server-data-dir>/data.
await mkdir(resolve(root,'vscode-web-data/data/Machine'),{recursive:true});
await writeFile(resolve(root,'vscode-web-data/data/Machine/settings.json'),JSON.stringify(settings));
const assets=createServer(async(req,res)=>{
  const name=new URL(req.url,'http://localhost').pathname.slice(1);
  if(!['index.html','fake.html','service-worker.js'].includes(name)){res.writeHead(404);res.end();return;}
  try {
    const data=await readFile(resolve(root,'vscode-web/vscode-server-linux-x64-web/out/vs/workbench/contrib/webview/browser/pre',name));
    res.writeHead(200,{'Content-Type':name.endsWith('.js')?'application/javascript':'text/html',
      'Access-Control-Allow-Origin':'*','Cache-Control':'no-store'});res.end(data);
  }catch {res.writeHead(500);res.end();}
});
await new Promise((resolveListen,reject)=>{assets.once('error',reject);assets.listen(0,'127.0.0.1',resolveListen);});
const webviewEndpoint=`http://{{uuid}}.localhost:${assets.address().port}/`;
const server=spawn(resolve(root,'vscode-web/vscode-server-linux-x64-web/bin/code-server'),[
  '--host','127.0.0.1','--port','0','--without-connection-token','--accept-server-license-terms','--telemetry-level','off',
  '--server-data-dir',resolve(root,'vscode-web-data'),'--user-data-dir',resolve(root,'vscode-user'),
  '--extensions-dir',resolve(root,'vscode-extensions'),
],{stdio:['ignore','pipe','pipe'],detached:true});
server.stderr.resume();
let test;
try {
  const port=await new Promise((resolvePort,reject)=>{
    const timer=setTimeout(()=>reject(Error('VS Code server startup timeout')),30000);
    let output='';
    server.on('error',error=>{clearTimeout(timer);reject(error);});
    server.on('exit',()=>{clearTimeout(timer);reject(Error('VS Code server exited before test'));});
    server.stdout.on('data',chunk=>{
      output=(output+chunk.toString()).slice(-8192);
      const match=output.match(/Server bound to 127\.0\.0\.1:(\d+)/);
      if(match){clearTimeout(timer);resolvePort(match[1]);}
    });
  });
  console.log('Isolated VS Code ready; starting installed-VSIX test');
  test=spawn(process.execPath,['--import','tsx','--test','tests/vscode.e2e.ts'],{
    stdio:'inherit',env:{...process.env,LG_TEST_WEBVIEW_ENDPOINT:webviewEndpoint,LG_VSCODE_URL:`http://127.0.0.1:${port}/?folder=${encodeURIComponent(workspace)}`},
  });
  const [code]=await once(test,'exit');process.exitCode=code??1;
} finally {
  test?.kill('SIGTERM');
  try{process.kill(-server.pid,'SIGTERM');}catch{}
  await new Promise(resolveClose=>assets.close(resolveClose));
}
