import * as vscode from 'vscode';
import { MockBackend } from './backend/MockBackend';
import { CodexBackend } from './backend/CodexBackend';
import { SessionStore } from './host/SessionStore';
import { WorkspaceBridge } from './host/WorkspaceBridge';
import { WebviewController } from './host/WebviewController';

export function activate(context: vscode.ExtensionContext) {
  const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!root) { void vscode.window.showErrorMessage('请打开一个 LG 作品文件夹。'); return; }
  const mode = vscode.workspace.getConfiguration('literarygiant').get<'mock' | 'codex'>('backend', 'mock');
  if (!vscode.workspace.isTrusted) { void vscode.window.showErrorMessage('请先信任当前作品目录，再启动 LG。'); return; }
  const config = vscode.workspace.getConfiguration('literarygiant');
  const backend = mode === 'mock' ? new MockBackend() : new CodexBackend({ workspace: root,
    python: config.get<string>('pythonPath', ''), manifest: config.get<string>('runtimeManifest', '') });
  const controller = new WebviewController(context.extensionUri, backend, new SessionStore(mode), new WorkspaceBridge(root));
  context.subscriptions.push(controller,
    vscode.window.registerWebviewViewProvider('literarygiant.chat', controller),
    vscode.commands.registerCommand('literarygiant.open', () => vscode.commands.executeCommand('literarygiant.chat.focus')));
}
