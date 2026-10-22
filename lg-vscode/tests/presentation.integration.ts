/** Actual service presentation boundary; no model or credentials. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {resolve} from 'node:path';
import {ItemSchema} from '../shared/protocol';
test('service normalization preserves candidate diffs and hides private or failed payloads',()=>{
 const python=process.env.LG_TEST_PYTHON;assert.ok(python,'Explicit test Python is required');
 const code=String.raw`
import json
from lg_cli.codex_events import normalize_item
base={'id':'candidate','type':'mcpToolCall','server':'lg_writing','tool':'studio_draft'}
diff={'path':'ReferenceLibrary/drafts/one.md','before':'旧稿','after':'候选','applied':False}
payload={'artifact':diff['path'],'diff':diff}
values=[
 {'id':'private','type':'reasoning','text':'PRIVATE_REASONING'},
 {**base,'result':{'content':[{'type':'text','text':json.dumps(payload)}]}},
 {**base,'result':{'structuredContent':{'profileVersion':2},'content':[{'type':'text','text':'Saved'}]}},
 {**base,'result':{'content':[{'type':'text','text':'plain text'}]}},
 {**base,'result':{'structuredContent':{'artifact':7,'diff':{**diff,'applied':'false'}}}},
 {**base,'result':{'isError':True,'content':[{'type':'text','text':'PRIVATE_PROVIDER_BODY'}]}},
 {'id':'unknown','type':'newWireType','secret':'PRIVATE_UNKNOWN'}]
print(json.dumps([normalize_item(v,'run',True) for v in values]))
`;
 const values=JSON.parse(execFileSync(python,['-c',code],{encoding:'utf8',env:{...process.env,PYTHONPATH:resolve('../lg-cli')}}));
 assert.equal(values[0],null);for(const value of values.slice(1))ItemSchema.parse(value);
 assert.deepEqual(values[1].diff,{path:'ReferenceLibrary/drafts/one.md',before:'旧稿',after:'候选',applied:false});
 assert.deepEqual(values[1].files,['ReferenceLibrary/drafts/one.md']);
 assert.deepEqual(values[2].structured.result,{profileVersion:2});
 assert.equal(values[3].text,'plain text');assert.equal(values[3].diff,undefined);
 assert.equal(values[4].diff,undefined);assert.equal(values[4].files,undefined);
 assert.equal(values[5].state,'failed');assert.ok(!JSON.stringify(values).includes('PRIVATE_'));
});
