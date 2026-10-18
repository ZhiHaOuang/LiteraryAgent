# LiteraryGiant VS Code

LG 写作工作台 0.3.0。作品首页、文学资料、对话、候选版本与作者审批。

正式版通过 Literary Backend 与 CLI 共用会话、作者授权、模型配置和作品存储，执行器由 LG 配置选择。显式 codex 设置也通过同一服务使用固定 Codex Runtime。Mock 仅用于明确启用的开发或 LitIsLand 测试，正式安装不提供离线演示入口。既有 Linux/LitIsLand 桌面与 VSIX 验收记录见 [验收说明](docs/ACCEPTANCE.md)，后续连接层验证见 [Backend 文档](../docs/extension-backend.md)。


## 先按 CLI 逻辑使用

启动后进入当前作品首页。从任意页面点击「返回首页」，或执行「LG：返回作品首页」。切换页面保留对话、待审批项和未发送草稿，不停止或重发任务。

首页提供人物、人物关系、章节与版本、全书/分卷规划、世界与设定、剧情事件、时间线、故事线/伏笔、审阅报告、已确认事实、待确认中心、写作偏好与本书模型用量。数据复用 CLI 首页和资料浏览函数，不建立第二套作品库。列表支持搜索和分页，详情按需读取；来源、规划/候选/已采用状态沿用 CLI。概况仅展示有限摘要，完整信息进入资料库查看。

CLI 保存作品数据库、资料索引或审阅结果后，扩展会合并变更通知并刷新已保存资料。创作任务运行期间显示资料待同步，任务结束后更新；不会因刷新而启动生成或自动采用。任意 Markdown 文件的修改不会自动变成已确认设定，资料归档和版本规则仍以 CLI 为准。

需要生成更新时，打开人物、设定或其他支持更新的资料，点击「生成更新」。任务进入对话，请核对工具审批中的准确内容。保存的是候选，已有活动版本继续保留；在「待确认中心」预览后，点击「确认采用」，再批准这一次采用。正文候选继续受既有五类审阅、冲突和过期检查约束。

时间线、偏好及用量目前以 CLI 已存数据浏览为主；不新增自动推断或自动修改。详情最多显示 6 万字符，超过时明确提示且禁用直接采用；版本区显示最近 50 条，完整历史仍可通过 CLI 查询。当前 CLI 原有部分浏览查询保留 1000 条边界，本版没有宣称消除此限制。

配套 LG Python 源码必须包含 `workspace_library.py`、Studio 工具、`desktop.py` 管理入口和 `backend_stdio.py`。仅显示 Python 包版本 0.3.0 不足以判断配套完整；显式 codex 选择需要服务支持 `--runtime-engine` 并在握手中返回相同执行器。版本不匹配时明确报错。

新增 CLI 入口使用同一组工具：`studio library`、`entry`、`action-context` 为只读；`reference`、`adopt-reference`、`accept-proposal` 的写入需要 `--confirm`。与现有 Studio 操作一样，参数从 `--input <JSON文件>` 读取。

## 真实作品与共享模型管理

顶部「选择作品」直接读取 CLI 书架；切换使用原有数据库、章节与参考资料，不复制或重新初始化作品。当前目录为已初始化作品时优先使用该作品，否则沿用 CLI 最近位置或选择当前书架中的第一本书。也可打开其他书架或作品。`literarygiant.bookPath` 可固定作品路径；选书后会同步更新该设置和 CLI 最近位置。

「模型管理」显示 CLI 当前配置空间中的账号与模型。选择已保存配置、修改模型 ID、新增 API 配置，均复用 CLI 的凭证存储及服务商/协议/地址校验；保存后重连，下一次 CLI 加载配置也使用这一选择。新增配置的密钥通过环境变量名交给 Python 读取，不在界面输入密钥。需要隐藏输入密钥、更新账号或显式登录 ChatGPT 订阅时，选择「在 CLI 中管理配置…」。不自动登录或回退服务商。

CLI 修改共享模型配置后，扩展在空闲时重连；运行中的任务使用启动时的配置，完成后再更新。任务执行或等待审批时禁止切换作品和模型；切换前检查作品内未保存文件。原会话保存在同一作品的 LG 内核目录，重连只恢复历史，不重发任务。未发送的界面草稿会保留，切换作品后请核对目标再发送。

「运行环境」设置配套 Python 和固定 runtime.json 的绝对路径。「重新连接」重载配置并恢复当前作品会话。模型管理即使在连接失败时仍可使用；没有配置时提示配置原因，正式版不会退回演示数据。`literarygiant.environment` 沿用 CLI 的配置空间，留空使用 CLI 默认规则。

## 开发

Node 20.18.1 / npm 10.8.2 已验证。打开本目录：

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run typecheck
npm test
npm run build
```

按 F5 启动扩展开发宿主，点击活动栏中的 LiteraryGiant 书本图标，或执行「LG：打开写作工作台」。LG 使用独立侧栏，避免被资源管理器压缩。开发启动目录可在 `.vscode/launch.json` 中选择已初始化的作品目录；正式配置与 CLI 保持一致。修改代码时运行 `npm run watch`，然后在开发宿主执行 Reload Window。无需反复安装 VSIX。

显式开发测试可使用 `literarygiant.backend=mock`（仅开发宿主及隔离测试有效）。模拟对话会展示流式输出和工具审批；`/error`、`/input`、`/slow`、`/disconnect` 分别演示错误、用户问题、取消与断线。断线后通过 Reload Window 重启后端，不自动重发任务。

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

在运行扩展宿主的机器上安装包含 `lg_cli.backend_stdio` 和 `lg_cli.desktop` 的配套 LG Python 包。打开作品、书架或包含 `Projects` 书架的仓库，将以下设置写入 VS Code 用户设置；路径须替换为该机器上真实的绝对路径：

```json
{
  "literarygiant.backend": "codex",
  "literarygiant.pythonPath": "/absolute/path/to/python",
  "literarygiant.runtimeManifest": "/absolute/path/to/runtime.json"
}
```

设置后点击「重新连接」。扩展强制核验 `codex-cli 0.154.0` 的配套 manifest、commit 与二进制校验和，不回退到 PATH，不自动登录或更换账号。密钥由 Python 配置层读取，不能填入 Webview。

`literarygiant.backend=codex` 指定 Codex 执行器，`literary` 沿用 LG 执行器配置；两者均通过统一 Backend。manifest 可留空沿用 LG 配置。旧对话选择后导入 LG 会话并以默认作者权限接续，原始文件保留；不直接恢复原 Core thread，不重放旧工具或审批。导入界面不展示完整旧工具载荷与附件，也不会中断仍在其他客户端运行的旧任务。

只信任你认可的作品目录。发送任务及批准写入前须保存或关闭未保存的编辑器；模型输出先作为候选，采用需要经过现有 LG 审阅校验和单次明确审批。断线后先恢复历史并检查作品实际状态，再决定是否继续。

架构和接口边界见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)，运行时及依赖升级步骤见 [UPGRADE.md](docs/UPGRADE.md)。目前验证范围是 Linux/LitIsLand；不宣称 Windows 原生 Python/MCP 生命周期已通过验证。
