import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {LibraryPanel} from '../webview/WorkspacePanels';
import {SessionStore} from '../src/host/SessionStore';
import {ClientMessageSchema,type WritingAction} from '../shared/protocol';

for(const [action,label] of [['confirm-knowledge','确认此认知'],['withdraw-knowledge','撤回认知确认']] as const){
  test(`knowledge action ${action} requires a complete connected author preview`,()=>{
    const state=new SessionStore('codex').snapshot();
    state.connection='ready';state.sessionId='session';state.library.query.topic='pending';
    state.library.detail={topic:'pending',key:'knowledge:1',title:'人物误信',body:'作者确认的认知标注',
      truncated:false,status:'confirmed',origin:'CLI',actions:[action as WritingAction],
      fingerprint:'a'.repeat(64),versions:[]};
    const render=()=>renderToStaticMarkup(createElement(LibraryPanel,{snapshot:state,
      send:()=>{},chat:()=>{},open:()=>{}}));
    const attributes=()=>{
      const button=[...render().matchAll(/<button([^>]*)>([^<]*)<\/button>/g)].find(match=>match[2]===label);
      assert.ok(button,`Missing exact ${action} label`);return button[1];
    };
    assert.doesNotMatch(attributes(),/disabled/);
    state.library.detail.truncated=true;assert.match(attributes(),/disabled/);
    state.library.detail.truncated=false;state.library.stale=true;assert.match(attributes(),/disabled/);
    state.library.stale=false;state.connection='disconnected';assert.match(attributes(),/disabled/);
    const message=ClientMessageSchema.parse({type:'writingAction',topic:'pending',key:'knowledge:1',
      action,fingerprint:'a'.repeat(64)});
    assert.ok(message.type==='writingAction');assert.equal(message.action,action);
  });
}
