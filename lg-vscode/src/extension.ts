import * as vscode from 'vscode';
import { DesktopHost } from './host/DesktopHost';

export function activate(context: vscode.ExtensionContext) {
  const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!root) { void vscode.window.showErrorMessage('请打开一个 LG 作品文件夹。'); return; }
  if (!vscode.workspace.isTrusted) { void vscode.window.showErrorMessage('请先信任当前作品目录，再启动 LG。'); return; }
  const controller = new DesktopHost(context,root);
  context.subscriptions.push(controller,
    vscode.window.registerWebviewViewProvider('literarygiant.chat', controller),
    vscode.commands.registerCommand('literarygiant.open', () => vscode.commands.executeCommand('literarygiant.chat.focus')),
    vscode.commands.registerCommand('literarygiant.home', async () => {await vscode.commands.executeCommand('literarygiant.chat.focus');controller.home();}),
    ...(['selectBook','models','reconnect','setup'] as const).map(action=>vscode.commands.registerCommand(`literarygiant.${action}`,()=>controller.perform(action))));
}
