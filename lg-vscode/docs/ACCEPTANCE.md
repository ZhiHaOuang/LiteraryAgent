# 验收记录与复现

以下旧直连与真实模型结果是历史证据，不作为新连接路径的验收。当前 Extension 使用 LiteraryBackend；旧 Core 兼容回归改用底层 RpcTransport，付费创作脚本也已迁移，其后续真实模型运行须另行明确授权。

记录更新：2026-10-12。经过 LitIsLand 验证的源码已应用到正式仓库。已验证功能、交付检查与已知限制如下。以下明确区分不同验证范围；不以 Mock 成功替代真实后端或 VS Code 宿主验收。


## 0.2.0 当前作品首页与文学资料（2026-10-12）

本批先对齐 CLI 数据和操作逻辑。升级前代码已备份到 Agent `449ca4344a0d2922ed052f2e8b1d5f25b385adac`，根仓库 `f6f40c721f64024f76dd25cdba5d756a67afa744`，远程提交已核对。

- 默认首页、返回导航、人物/关系/章节/规划/设定/事件/时间线/伏笔/审阅/事实/待确认/偏好/用量入口已实现。资料读取不调用模型，生成由作者发起，候选与采用分别确认。
- 沙箱 29 项扩展回归通过；新增验证 1000 次快速切换只读取首个和最后一个请求，以及过期预览与销毁保护。Python/实际 CLI 10 项定向检查通过，保留原稿、候选、采用、来源和母章节监督规则。
- 固定二进制集成通过，独立作品 `lg-real-extension-0WDltj` 位于 LitIsLand P0 tmp；7 次审批，拒绝未写入，资料候选归档、采用、旧指纹拒绝、取消和重启恢复均通过。模型输出为本地 fixture。
- 已安装沙箱 VSIX 的 Mock 宿主通过，约 28 秒：最新重建包另验证未发送草稿往返保留，包括首页返回不重发任务、人物资料、生成候选、拒绝采用和单独确认采用，另保留原有文件/历史交互检查。
- 实际 VS Code 宿主连接固定核心、真实 LG MCP 与本地模型 fixture，通过 CLI 自动同步和人物更新采用，约 25 秒。独立作品 `/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/lg-library-host-pat9im6c/novel`；读取及 CLI 同步阶段没有模型请求文件，生成后 v3 候选在拒绝采用时保留，明确批准后才采用。最终扩展详情与实际 CLI 返回正文逐字相同。证据 `.test-output/vscode/library-real.json` 和 `real-*.png` 位于 LitIsLand 扩展副本。

- 0.2.0 桌面 F5 实际检查通过，约 21 秒；新首页命令、人物来源、返回导航、待审批重载和原有交互均通过。独立 Electron profile，Mock，无模型账号。

新资料测试不使用个人作品或真实模型账号，不把 fixture 文本称为付费模型创作。原 0.1.0 的真实模型短正文记录保留在下文。正式同步后已运行 10 项 Python/实际 CLI 检查、完整类型检查及 29 项扩展回归，全部通过。实际正式 CLI 作品证据为 `/opt/conda/envs/LitIsLand/audits/lg-vscode-p0/tmp/lg-library-cli-mg8jwtbh`。最终包、源码摘要和安装比对记录见扩展目录 `.test-output/upgrade-0.2.json`。所有重任务共用串行锁；Node 堆默认 256 MiB，Electron 桌面测试 512 MiB，启动预算不等于总 RSS 硬上限。

复现真实宿主资料检查：先在 LitIsLand 扩展目录运行 `python tests/setup-library-fixture.py --root <LitIsLand审计目录> --source <隔离Python源码根目录>`，保留输出中的新作品、fixture 启动器和 Python/source 路径。安装本版 VSIX 后，运行 `tests/run-vscode-smoke.mjs`，显式设置 `LG_TEST_VSCODE_REAL_LIBRARY=1`、`LG_TEST_VSCODE_WORKSPACE`、`LG_TEST_FIXTURE_PYTHON`、`LG_TEST_PYTHON`、`LG_TEST_SOURCE_PYTHONPATH` 和 `LG_TEST_RUNTIME_MANIFEST`，以及下文既有浏览器/字体/动态库环境。每次创建新作品；不盲目重跑已修改的作品。测试启动器只写隔离 server-data-dir 的 Machine 设置。

## 0.1.0 历史验收记录

## 已取得的证据（0.1.0）

| 范围 | 结果 |
| --- | --- |
| TypeScript 类型检查及构建 | 当前沙箱及正式源码完整类型检查通过；正式目录离线 `npm ci --ignore-scripts --no-audit --no-fund` 安装 395 包后，类型检查、构建和打包均退出 0 |
| 低内存串行构建 | 曾以 `GOMAXPROCS=2`、96 MiB Node 堆、128 MiB 启动预算顺序构建宿主与 Webview，退出 0；构建脚本及历史分页改动均已同步正式目录。启动预算不是进程实际内存硬上限 |
| 扩展单元及本地 JSONL 子进程测试 | 当前沙箱及正式源码 27 项通过、0 项跳过；包含过期审批、迟到回复、文本 JSON MCP 产物、只读 diff 数据、错误脱敏、会话终态、真实文件系统边界、分页历史、取消后的部分回复恢复及慢接收端快照合并 |
| 固定二进制历史恢复 | 沙箱运行真实 app-server、LG 启动器和 MCP，通过批准、拒绝、概况读取、取消及进程重启恢复；模型为本地 fixture。作品：`/opt/conda/envs/LitIsLand/audits/lg-vscode-p0/tmp/lg-real-extension-Senh5v`（2026-10-12 用正式 Python 源码复测，6.8 秒、退出 0） |
| 未保存文件保护 | 检查同时比较原始与真实路径；通过工作区外符号链接打开的脏作品文件、符号链接工作区中的真实路径、未命名或无法解析的脏文件均阻止发送/批准写入。4 项真实文件系统测试在沙箱和正式目录分别通过；实际 VS Code Web 已验证编辑后拦截、撤销后恢复，磁盘内容保持不变 |
| 会话终态保护回归 | 沙箱和正式目录的 `tests/state.test.ts` 各 6 项通过；完成、取消、断线后的迟到更新不能退回运行中，恢复的等待项仍能正常推进 |
| 分批审阅采用门槛 | 沙箱和正式 Python 源码的 `tests.test_studio` 各 2 项通过；逐类提交五份本地测试报告，前四份均不能采用，五份齐全后才允许采用。不是付费模型审阅成功的证据 |
| JSON 参数校验与有限修复 | 工具参数在完整上游响应确认后才提交核心。单个函数调用可在解析错误位置增删至多两个 JSON 分隔符；仅接受保留全部键值、满足原 schema、且在该搜索范围内结果唯一的修复。不请求模型重试；截断、多调用错误及不能唯一修复的参数均不执行。正式源码桥接与 Studio 共 32 项通过、0 跳过，含固定核心验证：可修复调用仅生成一个候选版本，不可修复调用执行零次 |
| Python 产品回归 | 正式源码运行 34 项：31 项通过、3 项跳过；随后显式设置固定二进制补跑这 3 项，全部通过 |
| 实际 CLI 入口 | 正式源码 `studio dashboard --json` 成功读取真实创作测试作品，目标 1000000、已采用字数 138、章节状态 accepted，与 Webview 最终概况一致 |
| CLI 独立完整流程 | 新增 `tests.test_studio_cli`，沙箱和正式源码各通过 1 项进程级全流程测试。正式运行 36 条实际 CLI 命令，覆盖创建、四类规划、原创短正文、修订、逐类审阅、采用与概况更新，以及未确认、缺少审阅和重复采用的拒绝；正文与报告为本地测试数据，不调用模型 |
| 真实创作 | 独立作品已完成规划、世界人物、场景、短正文及修订；第 3 版已通过五类审阅并采用，采用字数 138，目标 1000000。正文及人物场景为独立原创测试资料 |
| 真实审阅与采用 | 真实模型、固定 app-server、LG MCP 和渲染 Webview 完成五类审阅、采用、概况更新。采用阶段两次错误参数请求被拒绝，只有一次采用成功。此脚本模拟宿主消息入口，不等于已在桌面 VS Code 运行同一付费模型流程 |
| 已安装 VSIX 的实际 VS Code | 含新快照发送器的正式源码重新构建、打包、安装后，在隔离 VS Code Web 通过：信任与激活、独立侧栏高度、批准/拒绝、取消、会话切换恢复、只读 diff、文件打开、未保存编辑拦截、撤销编辑后文件内容不变、概况显示。2026-10-12 最新正式包安装后，新增问题回答检查也通过，退出 0，约 21 秒。Mock 无模型账号；该结果不等于在桌面 VS Code 完成真实模型创作；F5 另有下文证据 |
| 独立构建与打包 | 最新独立副本离线 `npm ci`、类型检查、27 项测试、构建、打包全部退出 0；包含新图标。未复制 Qwen 树、CLI、模型配置、runtime 或原 node_modules |

独立构建证据：`.test-output/standalone.json`。最新验证副本为 `/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/lg-standalone-39atf2e8`，VSIX SHA-256 为 `1eff713b3cb94a2370b3c4c11ad9ecdbfc77c008e22228a5b96982f7281c9215`。此摘要对应当次验证包，不代表最终交付包或后续重建 zip 的时间戳保持一致。

内存记录：早期 2 GiB 容器中，128 MiB Node 堆不足以完成类型检查；那些失败不计为通过。2026-10-12 当前容器上限为 266287972352 字节，复核占用约 2.04 GiB，`oom` 与 `oom_kill` 均为 0。当前正式目录已独立安装依赖，完整类型检查、27 项测试和实际 VS Code Web 验收通过，取代此前借用沙箱依赖的局部验证。重任务仍通过 `scripts/run-guarded.py` 串行执行，默认 Node 堆 256 MiB；脚本检查容器余量，不能当作浏览器或所有子进程的内存硬限制。

CLI 完整流程的正式源码证据保留在 `/opt/conda/envs/LitIsLand/audits/lg-vscode-p0/tmp/lg-studio-cli-ray_8sgf`：`novel/` 为独立作品，`commands.json` 记录操作、是否明确确认和实际退出码，`input-*.json` 为各次输入，`dashboard.json` 为最终概况。复现时会新建独立目录并打印路径，不修改这份作品或现有作品。在 `LiteraryAgent` 根目录执行：

```bash
env TMPDIR=/opt/conda/envs/LitIsLand/audits/lg-vscode-p0/tmp PYTHONPATH=lg-cli /opt/conda/envs/LitIsLand/bin/python lg-vscode/scripts/run-guarded.py --lock /opt/conda/envs/LitIsLand/audits/lg-vscode-p1/check.lock --budget-mib 160 -- /opt/conda/envs/LitIsLand/bin/python -m unittest -v tests.test_studio_cli
```

## 无账号检查

在扩展目录分别执行：

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run typecheck
npm test
npm run build
```

打开扩展目录，F5 启动开发宿主。默认 `mock`，无需 Codex/Qwen 或模型账号。检查普通对话、审批与拒绝、`/error`、`/input`、`/slow` 取消及 `/disconnect`。开发配置使用 `fixtures/workspace`，不打开现有作品。

Linux 沙箱浏览器检查使用开发依赖中的 Chromium：

```bash
env TMPDIR=/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/browser-tmp AWS_EXECUTION_ENV=AWS_Lambda_nodejs20.x npx tsx --test tests/webview.e2e.ts
```

该测试是实际 bundle 加模拟宿主消息，不能替代 F5/已安装 VSIX。`tests/vscode.e2e.ts` 另接受 `LG_VSCODE_URL` 指向隔离的实际 VS Code Web 服务；需要事先将 VSIX 安装到该服务的扩展目录。不要连接个人工作区运行它：测试会制造并撤销测试文件的未保存编辑。

实际宿主测试使用 `tests/run-vscode-smoke.mjs` 启动独立服务。此环境不能访问 VS Code Webview CDN，因此脚本通过回环静态服务提供同一 VS Code 包中的 `index.html`、`fake.html`、`service-worker.js`；测试启动页仅将 `webviewEndpoint` 和外层 CSP 的 CDN frame 来源替换为本次本地域名，保留 LG 自身 CSP、工作区信任、服务工作线程和源校验。隔离浏览器授予本地网络权限、禁用代理并阻止外部页面请求，不使用关闭网页安全检查的参数。字体需要显式设置 `FONTCONFIG_PATH`。

在 LitIsLand 扩展副本中复现：

```bash
env TMPDIR=/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/browser-tmp FONTCONFIG_PATH=/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/browser-tmp/fonts LG_VSCODE_TEST_ROOT=/opt/conda/envs/LitIsLand/audits/lg-vscode-p1 LG_TEST_BROWSER=/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/playwright-browsers/chromium_headless_shell-1200/chrome-headless-shell-linux64/chrome-headless-shell LD_LIBRARY_PATH=/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/browser-deps/root/usr/lib/x86_64-linux-gnu:/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/browser-deps/root/lib/x86_64-linux-gnu python scripts/run-guarded.py --lock /opt/conda/envs/LitIsLand/audits/lg-vscode-p1/check.lock --budget-mib 2048 -- node tests/run-vscode-smoke.mjs
```

截图保存在该副本的 `.test-output/vscode/`。测试发现并修复了 Mock 取消流时丢失已显示回复的问题；修复后恢复历史条目数量及内容一致，不新增任务或审批。

## 固定核心集成

在扩展目录设置以下环境后运行独立集成测试。它使用真实二进制、LG 启动器和 MCP，但模型为本地 fixture，不消耗模型账号：

```bash
env TMPDIR=/opt/conda/envs/LitIsLand/audits/lg-vscode-p0/tmp LG_TEST_PYTHON=/opt/conda/envs/LitIsLand/bin/python LG_TEST_RUNTIME_MANIFEST=/opt/conda/envs/LitIsLand/lg-runtime/rust-v0.154.0/runtime.json PYTHONPATH=/opt/conda/envs/LitIsLand/audits/lg-vscode-p0/agent-snapshot/lg-cli:/opt/conda/envs/LitIsLand/audits/lg-vscode-p0/agent-snapshot npx tsx --test tests/real-backend.integration.ts
```

## 真实创作验收

`tests/creative.smoke.ts` 是显式启用的付费模型测试，不属于 `npm test`。必须指定 `LG_SMOKE_WORKSPACE`、`LG_TEST_PYTHON`、`LG_TEST_RUNTIME_MANIFEST`，并已有有效的 LG 模型配置；只允许 LitIsLand 路径。不要用于现有作品。

可通过 `LG_TEST_BROWSER` 使用已安装的 Chromium；在当前 Linux 沙箱同时使用上文的 `FONTCONFIG_PATH`、`LD_LIBRARY_PATH`。2026-10-12 修复前的 JSON 失败证据保存在 `.test-output/creative/diagnostic-2026-10-12.json`；后续成功证据为 `results.json`、`accept.json`、`final-snapshot.json` 和 `overview.png`。这些文件位于 LitIsLand 扩展副本的 `.test-output/creative/`，不随包发布。

本次长会话测试曾触及 Node 256 MiB 堆上限，退出 250；这不是通过记录，也不会反映在容器 `oom_kill` 中。核对磁盘后，四类报告均已保存且来源有效，恢复证据见 `recovery-2026-10-12.json`。随后加入快照发送背压：至多保留一份在途快照与一份最新待发送快照，页面关闭后释放待发送数据。类型检查、27 项扩展测试通过；从缺失的世界观审阅继续，在相同 256 MiB 堆限制下完成采用及概况更新并退出 0。尚不能由这次分段恢复推断任意长度会话都不会耗尽内存。

脚本通过真实 Webview 的按钮批准预先限定的独立作品操作，并记录 `.test-output/creative/approved-operations.json`；这是测试驱动授权，不是产品自动批准能力。正文保持短篇幅，引用资料只提供抽象机制，人物和场景独立。后续审阅/采用锁定成功保存的候选版本，来源指纹由 LG 工具校验。

完成标准：所有九个创作阶段真实保存，五类审阅与采用通过，概况显示真实已采用字数；CLI 使用隔离作品另走完整流程。上述真实模型流程以分段恢复完成；CLI 指令流程另有本地测试数据验证，正式 CLI 已读取真实模型最终作品。测试失败后先核对快照、候选历史和审批日志；只有确认上一次已终止且保存状态明确后，才选择显式恢复阶段，不能盲目重发。

## P0–P5 交付核对

| 阶段 | 实物与验证范围 |
| --- | --- |
| P0 | 产品仓库 `docs/IMPORT_AUDIT.md`保留历史审计；核心 `--verify` 通过，实际二进制 SHA-256 与 pin 一致；112 个生成类型逐字节匹配固定输出，24 个 UI 来源映射逐项核对通过 |
| P1 | 独立 Webview、自有 `shared/protocol.ts`、默认 Mock；实际桌面 F5 构建并运行流、工具、审批、错误、问题回答和取消，无模型账号 |
| P2 | `CodexBackend` 直接使用原始 RPC；正式 Python 启动器 describe 校验通过；固定二进制与 MCP 集成及上述真实模型创作证明 LG 定制链路 |
| P3 | 实际宿主检查批准/拒绝、历史、文件打开、只读 diff、未保存文件保护；桌面重载保持唯一待审批请求；固定核心重启恢复不重发任务 |
| P4 | 27 项回归、独立副本离线安装/构建/打包；不复制 Qwen 树、CLI、账号或 runtime。浏览器源码无后端 RPC、Node、Qwen/ACP 导入；打包 bundle 无原运行绑定 |
| P5 | 自用 `literarygiant-vscode-0.1.0.vsix`、README、ARCHITECTURE、UPGRADE 与本验收说明。实际安装包交互通过；包内容与构建输出逐字节核对，未上传 Marketplace |

正式项目默认 Python 安装实际指向 `LiteraryAgent/lg-cli/lg_cli`；不设置 PYTHONPATH 的启动器 `--describe` 返回 schema 1、`codex-cli 0.154.0` 和 LG 指令。真实模型作品由正式 CLI 读取，与已采用 138 字的 Webview 快照一致。

交付包清单和 SHA-256 存于扩展目录 `.test-output/package-audit.json`，不随包自嵌入。包中仅有 14 个文件：manifest、包描述、README、三份说明文档、来源映射、第三方声明、三个运行 bundle、图标和 gitignore；不含源码树、node_modules、测试作品、模型配置或凭据。仅文档更新后的重打包通过逐项 bundle 摘要与已安装验证包比对，避免将 zip 时间戳差异误判成运行代码变化。

### 已知限制

- 验证平台为 Linux/LitIsLand，VS Code 1.138.0；未证明 Windows 原生 Python/MCP 或其他核心版本兼容。固定版本不允许静默回退。
- 真实模型创作使用实际渲染 Webview 与模拟宿主消息入口，真实后端完整运行；实际 VS Code 宿主使用 Mock 验证交互。两者范围分别记录，没有宣称桌面宿主内完成整段付费模型流程。
- 真实创作曾因 Node 堆耗尽而分段恢复，已加入快照积压限制；不保证无限历史或任意长度会话的内存有界。所有重任务串行，启动预算不等于 RSS 硬上限。
- 桌面 F5 曾发生扩展宿主退出 134，根因未确认；已取得完整通过记录，失败记录仍保留，详见下节。
- 依赖固定与功能检查不等于全面第三方安全审计。仅移除了用户声明授权覆盖的 Qwen 自有声明，第三方依赖声明随包保留。

## 桌面 F5 与 Webview 生命周期（2026-10-12）

`tests/desktop.e2e.ts` 在隔离的 VS Code 1.138.0（commit `7debcd0e2acdea1c52de81bf9ee1620444407dda`）实际按 F5，触发构建并启动默认 Mock 开发宿主。最近一次退出 0，1 项通过，耗时 20.6 秒。通过待审批时重载 Webview、保持同一会话和条目数、保留唯一待审批项，再批准，以及拒绝、取消、错误、问题回答、概况和断线检查。测试修复了问题区域嵌套 label 导致建议按钮名称错误的问题，输入框现通过显式 label 关联。

成功记录与截图位于 LitIsLand 扩展副本的 `.test-output/desktop/`。桌面 Electron 每个 V8 堆设为 512 MiB，测试 Node 为 256 MiB；启动预算 3072 MiB 不等于总 RSS 硬限额。Electron 使用 headless 平台，未在用户桌面安装扩展。此前同一环境发生开发扩展宿主退出 134，未进入 LG 激活日志；提高堆限制仍曾复现，后续带有限诊断采集的运行通过且无内存失败信号。原因未确认，保留失败记录，不把单次通过解释为已修复该偶发问题。

在 LitIsLand 扩展副本执行：

```bash
env -u DISPLAY TMPDIR=/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/browser-tmp FONTCONFIG_PATH=/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/browser-tmp/fonts LG_VSCODE_TEST_ROOT=/opt/conda/envs/LitIsLand/audits/lg-vscode-p1 LG_TEST_DESKTOP_CODE=/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/VSCode-linux-x64/code LD_LIBRARY_PATH=/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/browser-deps/root/usr/lib/x86_64-linux-gnu:/opt/conda/envs/LitIsLand/audits/lg-vscode-p1/browser-deps/root/lib/x86_64-linux-gnu python scripts/run-guarded.py --lock /opt/conda/envs/LitIsLand/audits/lg-vscode-p1/check.lock --budget-mib 3072 -- node --import tsx --test tests/desktop.e2e.ts
```

## 0.3.0 真实书架与共享模型（2026-10-13）

LitIsLand 独立书架 `lg-library-host-ap62pfcg` 的已安装 VSIX 交互测试通过（43.1 秒）。使用固定真实核心与真实 LG MCP，服务商响应为 fixture，不使用真实账号、不调用真实模型。覆盖 CLI 保存后的资料自动同步、候选预览、拒绝及单独采用；新增 API 配置、修改模型 ID、选择已有配置、CLI 改模型后自动重连、跨两本书切换及返回后资料内容一致。管理操作未产生生成请求。证据：`.test-output/vscode/library-real.json`、`desktop-management.json`、`shared-model-book-management.png`。

Python 桌面入口/CLI 模型配置测试覆盖同一存储、不同配置空间隔离、缺失密钥变量、接口校验失败保留旧配置，以及 CLI 运行中的外部账号、密钥和地址变更会更新下一次请求。扩展 29 项单元测试通过，类型检查与构建通过。重型任务串行并使用 run-guarded；隔离测试进程结束后没有残留本批测试作品进程。

本批只接入既有作品并完成实现，不对真实作品做正文生成、修改或创作采用验收。之前版本的真实短正文及桌面 F5 证据保留为历史，本节不宣称重新执行了这些验收。

正式工程启动描述校验通过：《潮线之外》（`Projects/BeyondTheTide`）的标识与路径对应，模型沿用 CLI 当前配置，核心为 `codex-cli 0.154.0`。校验前后 project.json 与 story.sqlite3 摘要一致，没有生成请求；正式三个 bundle 与通过交互检查的 LitIsLand bundle 字节一致。正式 Python 39 项检查通过，追加多账号凭证隔离和 Native 的 9 项检查通过（合计 43 项不同测试）。
