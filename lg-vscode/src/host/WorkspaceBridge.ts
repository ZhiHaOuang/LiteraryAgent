import * as vscode from 'vscode';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import type { Item } from '../../shared/protocol';
import { checkedWorkspacePath, ensureCleanWorkspace } from './workspacePaths';

export class WorkspaceBridge implements vscode.Disposable {
  private content = new Map<string, string>();
  private registration = vscode.workspace.registerTextDocumentContentProvider('lg-snapshot', { provideTextDocumentContent: uri => this.content.get(uri.toString()) ?? '' });
  private closeListener = vscode.workspace.onDidCloseTextDocument(doc => { if (doc.uri.scheme === 'lg-snapshot') this.content.delete(doc.uri.toString()); });
  constructor(readonly root: string) {}
  async ensureClean() { await ensureCleanWorkspace(this.root, vscode.workspace.textDocuments, vscode.workspace.isTrusted); }
  async checked(file: string) { return checkedWorkspacePath(this.root, file); }
  async open(file: string) { await vscode.window.showTextDocument(vscode.Uri.file(await this.checked(file)), { preview: true }); }
  async diff(item: Item) {
    if (!item.diff) throw Error('没有可靠的修改前后内容，无法显示精确差异。');
    const key = randomUUID();
    const left = vscode.Uri.from({ scheme: 'lg-snapshot', path: '/' + key + '/before/' + path.basename(item.diff.path) });
    const right = vscode.Uri.from({ scheme: 'lg-snapshot', path: '/' + key + '/after/' + path.basename(item.diff.path) });
    this.content.set(left.toString(), item.diff.before); this.content.set(right.toString(), item.diff.after);
    await vscode.commands.executeCommand('vscode.diff', left, right, `${path.basename(item.diff.path)} · ${item.diff.applied ? '已应用' : '候选对比'}`, { preview: true });
  }
  async attach() {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.document.uri.scheme !== 'file') throw Error('请先打开要附加的作品文件。');
    const file = await this.checked(editor.document.uri.fsPath);
    const text = editor.selection.isEmpty ? editor.document.getText() : editor.document.getText(editor.selection);
    if (text.length > 40_000) throw Error('附件超过 4 万字符，请选取较小片段。');
    return { path: path.relative(this.root, file), text };
  }
  dispose() { this.registration.dispose(); this.closeListener.dispose(); this.content.clear(); }
}
