import {LiteraryBackend,type LiteraryBackendSettings} from './LiteraryBackend';

/** Preserve the Codex setting as an executor choice within the product service. */
export function createBackend(settings:LiteraryBackendSettings,selection='literary'):LiteraryBackend {
  if(selection!=='literary'&&selection!=='codex')throw Error('不支持的 LG 后端设置');
  return new LiteraryBackend({...settings,...(selection==='codex'?{runtimeEngine:'codex' as const}:{})});
}
