# LiteraryGiant VS Code

LG 写作工作台 0.2.0。作品首页、文学资料、对话、候选版本与作者审批。

默认离线 MockBackend，不需要模型账号或运行时。真实模式直接使用固定 app-server 的 JSONL RPC，通过 LG Python 启动层保留文学工具、凭据隔离和作品存储。Linux/LitIsLand 已完成固定后端、真实短正文、桌面 F5 与隔离 VSIX 交互验证，具体证据及已知限制见 [验收说明](docs/ACCEPTANCE.md)。


## 先按 CLI 逻辑使用

启动后进入当前作品首页。从任意页面点击「返回首页」，或执行「LG：返回作品首页」。切换页面保留对话、待审批项和未发送草稿，不停止或重发任务。

首页提供人物、人物关系、章节与版本、全书/分卷规划、世界与设定、剧情事件、时间线、故事线/伏笔、审阅报告、已确认事实、待确认中心、写作偏好与本书模型用量。数据复用 CLI 首页和资料浏览函数，不建立第二套作品库。列表支持搜索和分页，详情按需读取；来源、规划/候选/已采用状态沿用 CLI。概况仅展示有限摘要，完整信息进入资料库查看。

CLI 保存作品数据库、资料索引或审阅结果后，扩展会合并变更通知并刷新已保存资料。创作任务运行期间显示资料待同步，任务结束后更新；不会因刷新而启动生成或自动采用。任意 Markdown 文件的修改不会自动变成已确认设定，资料归档和版本规则仍以 CLI 为准。

需要生成更新时，打开人物、设定或其他支持更新的资料，点击「生成更新」。任务进入对话，请核对工具审批中的准确内容。保存的是候选，已有活动版本继续保留；在「待确认中心」预览后，点击「确认采用」，再批准这一次采用。正文候选继续受既有五类审阅、冲突和过期检查约束。

时间线、偏好及用量目前以 CLI 已存数据浏览为主；不新增自动推断或自动修改。详情最多显示 6 万字符，超过时明确提示且禁用直接采用；版本区显示最近 50 条，完整历史仍可通过 CLI 查询。当前 CLI 原有部分浏览查询保留 1000 条边界，本版没有宣称消除此限制。

配套 LG Python 源码必须包含 `workspace_library.py` 和新的 Studio 工具。仅显示 Python 包版本 0.3.0 不足以判断配套是否完整；旧 Python 在读取新资料接口时会显示版本/资料读取错误。真实模式的模型配置仍通过 CLI 管理。

新增 CLI 入口使用同一组工具：`studio library`、`entry`、`action-context` 为只读；`reference`、`adopt-reference`、`accept-proposal` 的写入需要 `--confirm`。与现有 Studio 操作一样，参数从 `--input <JSON文件>` 读取。

## 开发

Node 20.18.1 / npm 10.8.2 已验证。打开本目录：

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run typecheck
npm test
npm run build
```

按 F5 启动扩展开发宿主，点击活动栏中的 LiteraryGiant 书本图标，或执行「LG：打开写作工作台」。LG 使用独立侧栏，避免被资源管理器压缩。默认使用仓库内的隔离演示文件夹。修改代码时运行 `npm run watch`，然后在开发宿主执行 Reload Window。无需反复安装 VSIX。

普通离线对话会展示模拟流式输出和工具审批；`/error`、`/input`、`/slow`、`/disconnect` 分别演示错误、用户问题、取消与断线。断线后通过 Reload Window 重启后端，不自动重发任务。

## 浏览器交互验证

浏览器测试使用开发依赖中的 Chromium。LitIsLand 的可复现运行参数见 [验收说明](docs/ACCEPTANCE.md)，不要求为产品安装浏览器。

此项验证真实 Webview bundle 与模拟宿主消息，不等于 VS Code 桌面安装测试。

## 自用打包

```bash
npm run build
npm run package
```

使用 VS Code 的「从 VSIX 安装」选择生成文件。不会自动发布。生产依赖已打入 `dist`，随包附带构建时生成的 `THIRD_PARTY_NOTICES.md`。

## 受限内存环境

此前容器的 cgroup 限额为 2 GiB，2026-10-12 已确认资源限制扩大；运行前仍须读取实际 cgroup 限额，不能把 `free` 的宿主机总量当作可用预算。测试默认逐文件串行。Linux 检查可通过以下脚本运行：

```bash
python scripts/run-guarded.py --lock /opt/conda/envs/LitIsLand/audits/lg-vscode-p1/check.lock -- npm test
```

脚本使用单任务锁、256 MiB Node 堆上限，并在启动前检查预算与 192 MiB 保留空间。执行时余量低于保留空间会终止本次检查。内存不足时先停止闲置测试进程；不要与浏览器、VS Code 测试服务或其他编译任务并行启动。监测不能保证处理突发的外部内存占用。

一次性构建已改为依次编译宿主和 Webview。沙箱验证可在 `GOMAXPROCS=2`、96 MiB Node 堆下完成构建；这不代表类型检查、依赖安装或实际 VS Code 验收也能使用同样预算。开发监听模式保留两个监听器，应单独安排内存余量。

## 真实 LG 后端

在运行扩展宿主的机器上安装包含 `lg_cli.app_server` 的配套 LG Python 包，并通过 LG 自身的配置流程设置模型。打开一个独立 LG 作品目录，将以下设置写入 VS Code 用户设置；路径须替换为该机器上真实的绝对路径：

```json
{
  "literarygiant.backend": "codex",
  "literarygiant.pythonPath": "/absolute/path/to/python",
  "literarygiant.runtimeManifest": "/absolute/path/to/runtime.json"
}
```

更改后执行 Reload Window。扩展强制核验 `codex-cli 0.154.0` 的配套 manifest、commit 与二进制校验和，不回退到 PATH，不自动登录或更换账号。密钥由 Python 配置层读取，不能填入 Webview。

只信任你认可的作品目录。发送任务及批准写入前须保存或关闭未保存的编辑器；模型输出先作为候选，采用需要经过现有 LG 审阅校验和单次明确审批。断线后先恢复历史并检查作品实际状态，再决定是否继续。

架构和接口边界见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)，运行时及依赖升级步骤见 [UPGRADE.md](docs/UPGRADE.md)。目前验证范围是 Linux/LitIsLand；不宣称 Windows 原生 Python/MCP 生命周期已通过验证。
