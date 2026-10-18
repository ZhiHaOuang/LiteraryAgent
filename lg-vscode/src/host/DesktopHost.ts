import * as vscode from 'vscode';
import { dirname, isAbsolute, resolve } from 'node:path';
import { stat } from 'node:fs/promises';
import { createBackend } from '../backend/createBackend';
import { MockBackend } from '../backend/MockBackend';
import { DesktopClient, type Inventory } from './DesktopClient';
import { SessionStore } from './SessionStore';
import { WebviewController } from './WebviewController';
import { WorkspaceBridge } from './WorkspaceBridge';

export type DesktopAction='selectBook'|'models'|'reconnect'|'setup';
export class DesktopHost implements vscode.WebviewViewProvider,vscode.Disposable {
  private controller?:WebviewController;
  private view?:vscode.WebviewView;
  private watcher?:vscode.FileSystemWatcher;
  private timer?:NodeJS.Timeout;
  private changing=false;
  private disposed=false;
  private client?:DesktopClient;
  private inventory?:Inventory;
  constructor(private context:vscode.ExtensionContext,private root:string){}
  async resolveWebviewView(view:vscode.WebviewView){
    this.view=view;
    view.onDidDispose(()=>{if(this.view===view)this.view=undefined;});
    if(this.controller)this.controller.resolveWebviewView(view);
    else await this.restart();
  }
  home(){this.controller?.home();}
  private config(){return vscode.workspace.getConfiguration('literarygiant',vscode.Uri.file(this.root));}
  private async updateSetting(name:string,value:string){
    const inspected=this.config().inspect<string>(name);
    const target=inspected?.workspaceFolderValue!==undefined?vscode.ConfigurationTarget.WorkspaceFolder:
      inspected?.workspaceValue!==undefined?vscode.ConfigurationTarget.Workspace:vscode.ConfigurationTarget.Global;
    await this.config().update(name,value,target);
  }
  private mock(){
    const audit=process.env.LG_VSCODE_TEST_ROOT;
    const test=this.context.extensionMode!==vscode.ExtensionMode.Production||
      !!(audit?.includes('/LitIsLand/')&&resolve(this.root).startsWith(resolve(audit)+'/'));
    return test&&this.config().get<string>('backend')==='mock';
  }
  private async restart(){
    if(this.disposed)return;
    const config=this.config();
    const mock=this.mock();
    let workspace=this.root,data:Inventory|undefined,startupError:string|undefined;
    const python=config.get<string>('pythonPath','');
    let manifest=config.get<string>('runtimeManifest','');
    if(!mock){
      const selected=config.get<string>('bookPath','')||this.context.workspaceState.get<string>('lg.book','');
      workspace=selected?resolve(this.root,selected):this.root;
      this.client=new DesktopClient(python,workspace,config.get<string>('environment',''));
      try{
        try{data=await this.client.call({action:'inventory'});}
        catch(error){
          if(!selected)throw error;
          this.client=new DesktopClient(python,this.root,config.get<string>('environment',''));
          data=await this.client.call({action:'inventory'});
        }
        workspace=data.workspace||data.books[0]?.workspace||workspace;
        if(!data.workspace&&data.books.length){
          data=await this.client.call({action:'remember',workspace});
        }
        if(!data.workspace)throw Error('当前目录不是 LG 作品。请点击“选择作品”打开现有书架中的作品。');
        this.client=new DesktopClient(python,workspace,data.environment);
        data=await this.client.call({action:'inventory'});
        manifest=manifest||data.runtimeManifest;
        await this.context.workspaceState.update('lg.book',workspace);
      }catch(error){startupError=error instanceof Error?error.message:'无法读取 CLI 作品配置。';}
    }
    if(this.disposed)return;
    this.watcher?.dispose();clearTimeout(this.timer);
    await this.controller?.shutdown();
    this.inventory=data;
    const store=new SessionStore(mock?'mock':'codex');
    if(data)store.state.configuration={workspace,environment:data.environment,profile:data.active,model:data.model,provider:data.provider};
    const settings={workspace,python,manifest,environment:data?.environment||config.get<string>('environment','')};
    const backend=mock?new MockBackend():createBackend(settings,config.get<string>('backend','literary'));
    this.controller=new WebviewController(this.context.extensionUri,backend,store,new WorkspaceBridge(workspace),action=>this.perform(action),startupError);
    if(this.view)this.controller.resolveWebviewView(this.view);
    if(data){
      const directory=dirname(data.credentialsPath);
      const stamp=(file:string)=>stat(file,{bigint:true}).then(s=>`${s.mtimeNs}:${s.size}`).catch(()=> 'missing');
      const files=[data.credentialsPath,resolve(directory,'config.toml')];
      const stamps=new Map(await Promise.all(files.map(async file=>[file,await stamp(file)] as const)));
      const watcher=vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(directory,'{credentials.json,config.toml}'));
      this.watcher=watcher;
      const changed=(uri:vscode.Uri)=>{void stamp(uri.fsPath).then(next=>{
        if(this.disposed||this.watcher!==watcher||stamps.get(uri.fsPath)===next)return;
        // Credential reads repair permissions; that alone must not restart the core.
        stamps.set(uri.fsPath,next);this.scheduleReconnect();
      });};
      watcher.onDidChange(changed);watcher.onDidCreate(changed);watcher.onDidDelete(changed);
    }
  }
  private scheduleReconnect(){
    clearTimeout(this.timer);
    this.timer=setTimeout(()=>{
      if(this.disposed)return;
      if(this.changing||this.controller?.store.busy||this.controller?.store.state.connection==='connecting')this.scheduleReconnect();
      else void this.perform('reconnect').catch(error=>this.report(error));
    },1200);
  }
  private report(error:unknown){const text=error instanceof Error?error.message:'LG 管理操作失败';this.controller?.store.error(text);void vscode.window.showErrorMessage(text);}
  private async guard(){
    if(!vscode.workspace.isTrusted)throw Error('请先信任当前作品目录。');
    if(this.controller?.store.busy)throw Error('请先完成或停止当前任务，再切换作品或模型。');
    if(this.controller?.store.state.connection==='connecting')throw Error('请等待后端启动完成，再切换配置。');
    await this.controller?.ensureClean();
  }
  async perform(action:DesktopAction){
    if(this.disposed||this.changing)return;
    this.changing=true;
    try{
      await this.guard();
      if(action==='setup'){
        const python=await vscode.window.showInputBox({title:'LG Python 绝对路径',value:this.config().get<string>('pythonPath',''),validateInput:v=>isAbsolute(v)?null:'请输入绝对路径'});
        if(python===undefined)return;
        const manifest=await vscode.window.showInputBox({title:'Codex runtime.json 绝对路径（可留空，沿用 LG 配置）',value:this.config().get<string>('runtimeManifest',''),validateInput:v=>isAbsolute(v)||!v?null:'请输入绝对路径'});
        if(manifest===undefined)return;
        await this.updateSetting('pythonPath',python);
        await this.updateSetting('runtimeManifest',manifest);
        await this.restart();return;
      }
      if(action==='reconnect'){await this.restart();return;}
      const client=this.client??new DesktopClient(this.config().get<string>('pythonPath',''),this.root,this.config().get<string>('environment',''));
      const data=await client.call({action:'inventory'});
      if(action==='selectBook'){
        const items=[...data.books.map(b=>({label:b.name,description:b.workspace,workspace:b.workspace})),{label:'打开其他书架或作品…',description:'',workspace:''}];
        const picked=await vscode.window.showQuickPick(items,{title:'LG 选择作品',matchOnDescription:true});
        if(!picked)return;
        let target=picked.workspace;
        if(!target){
          const folders=await vscode.window.showOpenDialog({canSelectFiles:false,canSelectFolders:true,canSelectMany:false,openLabel:'打开 LG 书架或作品'});
          if(!folders?.[0])return;
          target=folders[0].fsPath;
          const other=new DesktopClient(client.python,target,data.environment);
          const listing=await other.call({action:'inventory'});
          if(!listing.workspace){
            const book=await vscode.window.showQuickPick(listing.books.map(b=>({label:b.name,description:b.workspace,workspace:b.workspace})),{title:'书架中的作品'});
            if(!book)return;target=book.workspace;
          }
          await this.guard();await other.call({action:'remember',workspace:target});
        }else {await this.guard();await client.call({action:'remember',workspace:target});}
        await this.context.workspaceState.update('lg.book',target);
        // Explicit book settings are also updated so they cannot silently win over the picker.
        if(this.config().get<string>('bookPath',''))await this.updateSetting('bookPath',target);
        await this.restart();return;
      }
      const choice=await vscode.window.showQuickPick([
        ...data.profiles.map(p=>({label:`${p.name===data.active?'✓ ':''}${p.name}`,description:`${p.provider} · ${p.model||'账号默认模型'}`,action:'use',name:p.name})),
        {label:'修改配置中的模型 ID…',action:'model',name:''},
        {label:'新增 API 模型配置…',action:'add',name:''},
        {label:'在 CLI 中管理配置、更新密钥或登录订阅…',action:'terminal',name:''},
      ],{title:`LG 模型管理 · 与 CLI 共享 ${data.environment}`,matchOnDescription:true});
      if(!choice)return;
      if(choice.action==='terminal'){
        const quote=(value:string)=>"'"+value.replaceAll("'","'\\''")+"'";
        const terminal=vscode.window.createTerminal({name:'LG 模型管理',cwd:client.workspace});
        terminal.show();terminal.sendText([client.python,'-m','lg_cli','--environment',data.environment,'-C',client.workspace,'auth'].map(quote).join(' '));return;
      }
      let request:import('./DesktopClient').ControlRequest;
      if(choice.action==='use')request={action:'use',name:choice.name};
      else if(choice.action==='model'){
        const selected=await vscode.window.showQuickPick(data.profiles.map(p=>({label:p.name,description:p.model,profile:p})),{title:'选择要修改的模型配置'});
        if(!selected)return;
        const model=await vscode.window.showInputBox({title:'模型 ID（保存后激活此配置）',value:selected.profile.model,validateInput:v=>v.trim()&&!/\s/.test(v)&&v.length<=200?null:'请输入不含空白的模型 ID'});
        if(model===undefined)return;
        request={action:'model',name:selected.profile.name,model};
      }else{
        const provider=await vscode.window.showQuickPick(data.providers.map(p=>({label:p.name,description:p.protocol,provider:p})),{title:'服务商或兼容接口'});
        if(!provider)return;
        const name=await vscode.window.showInputBox({title:'新配置名称',validateInput:v=>/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(v)&&!data.profiles.some(p=>p.name===v)?null:'请输入尚未使用的名称（字母、数字、点、横线或下划线）'});
        if(name===undefined)return;
        const model=await vscode.window.showInputBox({title:'模型 ID',value:provider.provider.model,validateInput:v=>v.trim()&&!/\s/.test(v)&&v.length<=200?null:'请输入有效模型 ID'});
        if(model===undefined)return;
        const url=await vscode.window.showInputBox({title:'API Base URL',value:provider.provider.base_url});
        if(url===undefined)return;
        const variable=await vscode.window.showInputBox({title:'密钥环境变量名（不要输入密钥）',prompt:'由 Python 读取 IDE 进程继承的环境变量；也可通过 CLI 管理入口输入密钥。',validateInput:v=>/^[A-Za-z_][A-Za-z0-9_]*$/.test(v)?null:'请输入环境变量名'});
        if(variable===undefined)return;
        request={action:'add',name,provider:provider.provider.id,model,base_url:url,key_env:variable,replace:false};
      }
      await this.guard();
      const previousConnection=this.controller?.store.state.connection;
      if(this.controller){this.controller.store.state.connection='connecting';this.controller.store.publish();}
      try{await client.call(request);}catch(error){if(this.controller&&previousConnection){this.controller.store.state.connection=previousConnection;this.controller.store.publish();}throw error;}
      await this.restart();
    }catch(error){this.report(error);}
    finally{this.changing=false;}
  }
  dispose(){this.disposed=true;clearTimeout(this.timer);this.watcher?.dispose();this.controller?.dispose();}
}
