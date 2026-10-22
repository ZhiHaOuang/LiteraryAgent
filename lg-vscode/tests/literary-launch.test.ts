import {test} from 'node:test';
import assert from 'node:assert/strict';
import {LiteraryBackend,type LiteraryBackendSettings} from '../src/backend/LiteraryBackend';
import {createBackend} from '../src/backend/createBackend';

class LaunchBackend extends LiteraryBackend{
  spec(){return this.launch();}
}
const settings:LiteraryBackendSettings={python:'/opt/lg/bin/python',workspace:'/books/one',manifest:'',environment:'sandbox'};
test('LG startup delegates omitted manifest to product configuration',()=>{
  assert.deepEqual(new LaunchBackend(settings).spec().args,
    ['-m','lg_cli.backend_stdio','--workspace','/books/one','--environment','sandbox']);
  assert.deepEqual(new LaunchBackend({...settings,manifest:'/opt/lg/runtime.json'}).spec().args,
    ['-m','lg_cli.backend_stdio','--workspace','/books/one','--runtime-manifest','/opt/lg/runtime.json','--environment','sandbox']);
});
test('LG startup rejects ambiguous paths before launching a child',()=>{
  assert.throws(()=>new LaunchBackend({...settings,python:'python'}).spec(),/Python/);
  assert.throws(()=>new LaunchBackend({...settings,manifest:'runtime.json'}).spec(),/manifest/);
});
test('both desktop selections use LG service and codex explicitly selects its executor',()=>{
  for(const selection of ['literary','codex']){
    const backend=createBackend(settings,selection);
    assert.ok(backend instanceof LiteraryBackend);
    const spec=LaunchBackend.prototype.spec.call(backend);
    assert.equal(spec.args[1],'lg_cli.backend_stdio');
    assert.equal(spec.jsonRpc,true);
    assert.equal(spec.args.includes('--runtime-engine'),selection==='codex');
    if(selection==='codex')assert.deepEqual(spec.args.slice(-2),['--runtime-engine','codex']);
  }
  assert.throws(()=>createBackend(settings,'unknown'));
});
for(const runtime of ['native','missing']){
  test('explicit Codex selection rejects an incompatible runtime handshake: '+runtime,async()=>{
    class WrongRuntime extends LiteraryBackend{
      protected override launch(){
        const script=`require('node:readline').createInterface({input:process.stdin}).on('line',line=>{
          const request=JSON.parse(line);
          console.log(JSON.stringify({jsonrpc:'2.0',id:request.id,result:{protocolVersion:'lg.backend.v1',
            workspace:process.cwd(),capabilities:['bookLibrary','clientEvents'],
            ...(process.argv[1]==='missing'?{}:{runtime:process.argv[1]})}}));
        });`;
        return {command:process.execPath,args:['-e',script,runtime],cwd:process.cwd(),jsonRpc:true};
      }
    }
    const backend=new WrongRuntime({...settings,workspace:process.cwd(),runtimeEngine:'codex'});
    try{await assert.rejects(backend.connect(),/指定执行器/);}finally{await backend.dispose();}
  });
}
