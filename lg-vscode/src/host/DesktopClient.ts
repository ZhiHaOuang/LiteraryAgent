import { execFile } from 'node:child_process';
import { isAbsolute } from 'node:path';
import { z } from 'zod';

const Profile = z.object({name:z.string(),provider:z.string(),protocol:z.string(),model:z.string(),base_url:z.string(),auth_mode:z.enum(['api_key','chatgpt'])});
export const InventorySchema=z.object({workspace:z.string(),shelf:z.string(),environment:z.string(),active:z.string(),model:z.string(),provider:z.string(),runtimeManifest:z.string(),credentialsPath:z.string(),
  books:z.array(z.object({name:z.string(),project_id:z.string(),workspace:z.string()})),profiles:z.array(Profile),
  providers:z.array(z.object({id:z.string(),name:z.string(),protocol:z.string(),base_url:z.string(),model:z.string()}))});
export type Inventory=z.infer<typeof InventorySchema>;
export type ControlRequest={action:'inventory'}|{action:'remember';workspace:string}|{action:'use';name:string}|{action:'model';name:string;model:string}|
  {action:'add';name:string;provider:string;model:string;base_url:string;key_env:string;replace:boolean};
const errors:Record<string,string>={KEY_ENV_MISSING:'Python 环境中未找到该密钥变量。请设置环境变量后重新启动 IDE，或使用 CLI 模型管理。',INVALID_KEY_ENV:'请输入有效的环境变量名，不要输入密钥。',NOT_A_BOOK:'请选择已经初始化的 LG 作品。',CONFIG_INVALID:'LG 配置无效，请核对 CLI 配置、模型与接口地址。',INVALID_REQUEST:'管理请求无效，未执行。'};

export class DesktopClient {
  constructor(readonly python:string,readonly workspace:string,readonly environment:string=''){}
  async call(request:ControlRequest):Promise<Inventory>{
    if(!isAbsolute(this.python))throw Error('请通过“LG：配置运行环境”指定安装 LG 的 Python 绝对路径。');
    const args=['-m','lg_cli.desktop','--workspace',this.workspace];
    if(this.environment)args.push('--environment',this.environment);
    const raw=await new Promise<string>((resolve,reject)=>{
      const child=execFile(this.python,args,{cwd:this.workspace,timeout:20_000,maxBuffer:1024*1024,windowsHide:true},
        (error,stdout)=>error?reject(Error('LG 管理入口无法启动，请检查 Python 路径及配套 LG 包。')):resolve(stdout));
      child.stdin?.end(JSON.stringify(request));
    });
    const result=z.discriminatedUnion('ok',[z.object({ok:z.literal(true),data:InventorySchema}),z.object({ok:z.literal(false),error:z.string()})]).parse(JSON.parse(raw));
    if(!result.ok)throw Error(errors[result.error]??'LG 配置操作失败，未切换到其他账号。');
    return result.data;
  }
}
