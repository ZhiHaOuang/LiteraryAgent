import { realpath } from 'node:fs/promises';
import path from 'node:path';

export function containsPath(root: string, file: string): boolean {
  const relative = path.relative(root, file);
  return relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative);
}

export async function checkedWorkspacePath(root: string, file: string): Promise<string> {
  if (!file || file.includes('\0') || /^[a-z]+:\/\//i.test(file)) throw Error('不支持的文件路径');
  const resolved = await realpath(path.resolve(root, file));
  if (!containsPath(await realpath(root), resolved)) throw Error('文件不在当前作品目录内');
  return resolved;
}

interface EditableDocument {
  readonly isDirty: boolean;
  readonly isUntitled: boolean;
  readonly uri: { readonly scheme: string; readonly fsPath: string };
}

export async function ensureCleanWorkspace(root: string, documents: readonly EditableDocument[], trusted: boolean): Promise<void> {
  if (!trusted) throw Error('请先信任当前工作区。');
  const canonicalRoot = await realpath(root);
  for (const document of documents) {
    if (!document.isDirty) continue;
    let affected = document.isUntitled || containsPath(root, document.uri.fsPath)
      || containsPath(canonicalRoot, document.uri.fsPath);
    if (!affected && document.uri.scheme === 'file') {
      try {
        affected = containsPath(canonicalRoot, await realpath(document.uri.fsPath));
      } catch {
        // An unresolved dirty file might alias this book; do not authorize a write.
        affected = true;
      }
    }
    if (affected) throw Error('请先保存或关闭未保存的编辑内容，再启动任务或批准写入。');
  }
}
