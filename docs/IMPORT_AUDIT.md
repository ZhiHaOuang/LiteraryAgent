# VS Code 界面迁移审计（P0）

后续实施记录（2026-10-12）：以下正文保留 P0 当时的审计结论。后续已实现独立扩展与 LG Python 启动/审批/studio 入口，固定二进制集成、真实模型短篇创作闭环、桌面 F5、隔离 VSIX 宿主和独立构建均已取得实际证据。当前验证范围及限制见 [`lg-vscode/docs/ACCEPTANCE.md`](../lg-vscode/docs/ACCEPTANCE.md)；交付包检查记录见扩展目录 `.test-output/package-audit.json`。

审计日期：2026-09-28。范围：备份当前代码、核验现有后端、比较并固定 Qwen 源码快照、验证可复用 UI 包构建。用户要求 P0 后先讨论，不进入 P1–P5 实现。

## 1. 结论与实施前提

1. 当前 LG 的文学定制主要在 Python 产品层，不在 vendored Codex 核心。直接启动现有 `codex app-server` 并不自动获得 LG 指令、写作工具、领域权限或会话管理。
2. 用户确认扩展仍须直接连接 app-server；不能擅自改为连接已有 `lg_cli.backend_stdio`。需要讨论并完成共享化后，才可宣称新的入口保留全部定制。
3. 推荐一次性提取 Qwen `v0.12.6` 的实际 WebUI 组件和部分 VS Code 展示基础设施。此快照的 UI 包已在隔离目录独立完成类型检查和构建；完整原扩展及迁移后的扩展尚未构建。
4. 已存在可用的固定 Codex runtime，虽然用户此前不确定其位置；清单明确标记它为 `upstream-unpatched`。不得把它当作定制 app-server 成品，也不得静默换成 PATH 中的 Codex。
5. 本批只新增本审计文档。未迁移 UI、未删除文件、未改后端逻辑、未发布扩展。

## 2. 工程、版本和 GitHub 备份

| 项目 | 实际位置/标识 | 已验证状态 |
| --- | --- | --- |
| 根仓库 | `/root/private_data/LiteraryGiant`，`main` | 备份提交 `7fd6f9b56f16aed250168cfb38cac3b128caa997` 已推送 |
| Agent 子仓库 | `/root/private_data/LiteraryGiant/LiteraryAgent`，`literary-agent` | 当前 84 个变更文件备份为 `2f689dca92ff5edf271c8a45bf1a289c8909d130`，已推送 |
| LG 包 | `lg-cli/pyproject.toml` | `literarygiant-cli 0.3.0`，入口 `literary` / `lg` / `literarygiant` |
| Codex 源码 | `core/codex` | `CODEX_CORE_COMMIT = 6b9826e3aa83b1a5947db50f4332cb9c65f1b340` |
| LitIsLand | `/opt/conda/envs/LitIsLand` | Python 解释器与 runtime 目录实际存在 |
| 当前 runtime | `/opt/conda/envs/LitIsLand/lg-runtime/rust-v0.154.0/codex` | 实测 `codex-cli 0.154.0` |
| runtime 清单 | 同目录 `runtime.json` | `kind: upstream-unpatched`，commit 与源码 pin 一致 |
| 既有扩展 | 当前产品代码未发现 VS Code 扩展工程 | 已有 Python 扩展后端；不能等同于已有 VS Code UI |

备份链接：[Agent 提交](https://github.com/ZhiHaOuang/LiteraryAgent/commit/2f689dca92ff5edf271c8a45bf1a289c8909d130)、[根仓库提交](https://github.com/ZhiHaOuang/LiteraryGiant/commit/7fd6f9b56f16aed250168cfb38cac3b128caa997)。两次 `git ls-remote` 已确认远程分支指向对应提交。没有 force push。备份保存原有修改，不代表其全部功能已经验收。

runtime 实测 SHA-256：

```text
3188814c35471432d4123203e0eb38e5bddc60226e3d7ddf0e59e649ea140022
```

`bash scripts/update_codex_core.sh --verify` 成功：提交中的 `core/codex` 树与固定上游 commit 树相同，且该前缀无未提交修改。此结果反驳“文学定制已写入这个核心”的假设。

## 3. 后端入口、领域逻辑与共享化范围

以下路径均相对于 `LiteraryAgent`：

| 文件/模块 | 实际职责 | 直连 app-server 时的处理要求 |
| --- | --- | --- |
| `lg-cli/lg_cli/main.py` | CLI 和终端交互入口 | 保持 CLI 行为；不把终端逻辑搬到 React |
| `lg-cli/lg_cli/backend_stdio.py` | 现成 JSONL / JSON-RPC 产品后端入口 | 已能运行，但用户未选择此协议作为扩展主连接 |
| `lg-cli/lg_cli/backend.py` | 会话、事件游标、工作流、操作审批、创作采用审批 | 这些产品能力不是原始 app-server 自动提供的，需要共享或重新暴露 |
| `lg-cli/lg_cli/live_agent.py` | 启动 app-server、握手、创建/恢复 thread、流式输出、steer、取消 | 提取生命周期与启动配置责任；扩展不得复制领域逻辑 |
| `lg-cli/lg_cli/native.py:writing_args` | LG MCP 工具清单、指令文件、工具限制、关闭遥测/更新等配置 | 应由 LG 后端安装/启动层拥有，不能仅在 UI 中拼接 |
| `lg-cli/lg_cli/resources/native-writing.txt` | 写作行为指令 | app-server 入口必须获得同一份指令 |
| `lg-cli/lg_cli/story_index.py`、`book_overview.py` | 叙事结构指令及当前书籍上下文 | 共享现有实现与上下文限制 |
| `lg-cli/lg_cli/writing_mcp.py` | 读正文、写章节、修改正文、版本差异、设定提案等领域工具 | 保留真实工具及数据约束，避免在扩展中重写 |
| `lg-cli/lg_cli/workflow_control.py` | 控制连接、工作流停止与重试 | 需要新的 app-server 集成方式；不能丢弃既有控制能力 |
| `lg-cli/lg_cli/project_store.py` | 作品数据库、版本和创作状态 | 继续单一写入责任，不让扩展额外应用补丁 |
| `lg-cli/lg_cli/session_runtime.py` | CLI/扩展会话锁、thread ID、操作和创作权限 | 原始 thread 历史不能替代完整产品会话状态 |
| `lg-cli/lg_cli/provider_transport.py`、`anthropic_bridge.py` | 既有模型协议适配与凭据隔离 | 保留后端责任，不把真实凭据送给 Webview |
| `lg-cli/lg_cli/core_adapter.py` | runtime 发现、清单与哈希校验、exec 适配 | 新入口强制使用明确的已验证 runtime，禁用 PATH 回退 |

实际主链路：`Backend / CLI → LiveAgent → provider_bridge + writing_args + 指令/书籍上下文 → 固定 app-server → LG writing MCP → ProjectStore`。

`LiveAgent._start` 使用 `initialize` / `initialized`，在 `thread/start` 或 `thread/resume` 中传入 `baseInstructions`、工作目录和权限。领域工具通过 `writing_args` 配置的 MCP 子进程提供。裸启动 app-server 没有这些注入。

### 必须在实现前解决的差异

- `discover_core_commands` 在有 manifest 时校验 commit、版本和二进制哈希；没有 manifest 时会尝试环境命令、源码产物、Node wrapper 和 PATH 中的 `codex`。扩展不能直接继承后者。
- `writing_mcp.py` 的 `approved` 包装器仅在提供 `control_socket` 时请求产品审批；缺少该连接时会直接进入工具函数。仅复制工具配置、遗漏控制连接，会改变现有人工审批语义。
- `Backend.open` 实际安装了操作审批处理器，`Backend.creative` 另行处理创作采用与预览失效。两类审批不能合并为一个泛化的“允许工具”按钮。
- `SessionRuntime` 使用 POSIX `fcntl`，工作流控制使用 Unix socket。当前验证是 Linux 环境，不构成 Windows 桌面原生可用的证明。
- `LiveAgent._drain_stderr` 当前丢弃诊断输出。新的进程层需要脱敏后提供可见错误。
- `LiveAgent` 现有事件没有完整的界面状态契约；`backend.py` 仍透传 `engine/event`。新 Webview 不能直接依赖这些 Codex 原始事件。

### 构建方法与定制产物现状

Python 产品包构建入口为 `lg-cli/pyproject.toml`；现有 README 的安装方法是 `python -m pip install ./lg-cli`。本批使用 LitIsLand 解释器运行隔离源码，不重新安装或覆盖现有 CLI。

`scripts/build_native_ui.py` 从固定源码导出隔离构建树、应用 `native-ui/patches/0001-lg-tui.patch` 和 `0002-lg-entry.patch`，然后构建 `codex-tui`，生成 `native-runtime.json`。补丁涉及 TUI 文案、slash 命令与 TUI main，不等于定制 app-server。此构建脚本本批只读审计，未运行 Rust 构建。

### 供下一批讨论的方案（尚未实施）

推荐维持“扩展直接发送 app-server RPC”，同时让 LG 拥有专门的后端启动入口、共享启动配置和领域 MCP 实现。LG 指令、模型适配、权限和作品存储继续由后端提供；app-server 负责模型循环与工具调度。需要专门验证领域审批如何进入 app-server 请求链，不能靠扩展自动放行，也不能假装现成接口已经支持所需全部语义。

若用户要求领域能力全部编入一个 Rust 二进制、不能保留 Python/MCP 组件，则需迁移当前 Python 产品能力，工作量和 CLI 回归范围显著扩大。现有仓库约定要求避免向 vendored core 添加产品逻辑；确需核心补丁时要最小化并记录在 `core/patches`。该选择待讨论，不以方便实现为由擅自降低目标。

## 4. 实际协议能力和生成结果

对上述绝对路径 runtime 执行并成功退出：

```bash
/opt/conda/envs/LitIsLand/lg-runtime/rust-v0.154.0/codex --version
/opt/conda/envs/LitIsLand/lg-runtime/rust-v0.154.0/codex app-server --help
/opt/conda/envs/LitIsLand/lg-runtime/rust-v0.154.0/codex app-server generate-ts --out /opt/conda/envs/LitIsLand/audits/lg-vscode-p0/protocol-ts
/opt/conda/envs/LitIsLand/lg-runtime/rust-v0.154.0/codex app-server generate-json-schema --out /opt/conda/envs/LitIsLand/audits/lg-vscode-p0/protocol-schema
```

`app-server --help` 明确支持 stdio、`generate-ts`、`generate-json-schema`。本次使用默认生成参数，未声称覆盖所有实验性类型。当前产品在握手中启用了 `experimentalApi`，未来新入口必须核对实际使用字段并按需生成相应实验性类型。

生成的 `ClientRequest.ts` 包含 `thread/start`、`thread/list`、`thread/read`、`thread/resume`、`turn/start`、`turn/steer`、`turn/interrupt`。`ServerRequest.ts` 包含命令/文件/权限审批、用户输入、MCP elicitation 与 dynamic tool call。存在类型不代表这些交互已在新扩展实现或运行验证。

| 产物 | 文件数 | 目录内容 SHA-256 |
| --- | --- | --- |
| `protocol-ts` | 711 | `d655ffc1162656214ef4651ac4e2099656dc85861beb6fb0ad59fc69830a8fb4` |
| `protocol-schema` | 305 | `a02e63d92869a0d1cacefd833448bdc6363e2b9715448e7dc213f564b17cd8ae` |

摘要算法：按相对路径排序所有文件，依次向 SHA-256 输入 `relativePath UTF-8 + NUL + fileBytes + NUL`。它标识本次生成内容，不是兼容保证。生成物仅在沙箱，未来必须从实际选择的 LG 构建重新生成，不能用当前上游 runtime 冒充定制构建。

[OpenAI 官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)仅作接口背景参考；本次版本结论来自本地二进制、生成物与固定源码。

## 5. Qwen 快照比较与选择

官方来源：`https://github.com/QwenLM/qwen-code.git`。

| 候选 | 固定 commit | 实际依赖与选择依据 |
| --- | --- | --- |
| 拉取时的 main，包版本 `0.24.6` | `a765229c0affd0642973412a3480b992935e2eff` | `EmbeddedApp.tsx` 直接导入 `DaemonClient` 与 daemon transcript 类型，使用 `@qwen-code/web-shell`；扩展依赖 ACP bridge、SDK、core、CLI，整仓要求 Node 22+。不选作本批迁移起点 |
| **推荐 `v0.12.6`** | **`ac30c98a266a6335f8ebc7a9011f266cf6b63f13`** | 组件集中于 `packages/webui`，运行时外部导入扫描仅见 React/markdown-it；聊天、输入、工具与审批组件实际存在，UI 包已独立构建成功。扩展壳仍需解耦 |

沙箱路径：

```text
/opt/conda/envs/LitIsLand/audits/lg-vscode-p0/
  qwen-code/       # main 对照快照
  qwen-v0.12.6/    # detached worktree，推荐快照
  ui-build-probe/  # 原样 UI 源码与配置，单独的实验包清单/锁文件
  agent-snapshot/ # 已备份 LG 产品代码与测试的隔离副本
  protocol-ts/
  protocol-schema/
  tmp/
```

固定来源链接：[v0.12.6 源码树](https://github.com/QwenLM/qwen-code/tree/ac30c98a266a6335f8ebc7a9011f266cf6b63f13)。没有导入整仓到产品，也没有添加 Qwen 子模块。

### 保留、替换与排除清单

以下 Qwen 路径均相对于固定快照，属于下一阶段计划，本批没有执行删除：

| 实际源路径 | 保留/替换边界 |
| --- | --- |
| `packages/webui/src/components/layout/{InputForm,ChatHeader,SessionSelector,CompletionMenu}.tsx` | 保留输入、布局、选择器等交互实现；替换 Qwen 登录、模型/权限语义及文案 |
| `packages/webui/src/components/messages/MarkdownRenderer/`、`Assistant/`、`UserMessage.tsx`、`Waiting/` | 保留 Markdown、消息、等待/中断展示及样式；补验证链接、资源和不可信内容处理 |
| `packages/webui/src/components/toolcalls/` 和 `shared/` | 保留卡片、复制、代码块、折叠、状态展示；改用自有 item/run/request 标识，不依赖 ACP 工具状态 |
| `packages/webui/src/components/PermissionDrawer.tsx`、`messages/AskUserQuestionDialog.tsx` | 保留展示交互；决定选项由真实后端能力提供，新增过期与重复回复保护 |
| `packages/webui/src/styles/`、`components/icons/`、`tailwind.preset.cjs` | 提取必需样式与通用图标；排除品牌资源和无关示例 |
| `packages/webui/src/context/PlatformContext.tsx` | 保留宿主回调思路，缩减为本项目文件/只读 diff/复制接口；不保留 login 功能 |
| `packages/webui/src/types/`、`adapters/ACPAdapter.ts`、`JSONLAdapter.ts` | 不作为自有协议直接改名沿用；重新定义界面事件/快照/状态校验 |
| `packages/vscode-ide-companion/src/webview/App.tsx` 及 hooks | 保留合适布局和编辑器上下文交互；替换 ACP 模型、审批状态、Qwen core tokenLimits 与旧会话状态 |
| `.../webview/providers/{PanelManager,WebViewContent,ChatWebviewViewProvider}.ts` | 复用生命周期与资源 URI 处理的适用部分；重建消息验证、CSP、命名和快照恢复 |
| `.../diff-manager.ts`、`services/readonlyFileSystemProvider.ts` | 只提取只读内容展示/编辑器定位；移除 `ide/diffAccepted` 和接受/拒绝修改语义 |
| `.../services/acpFileHandler.ts` | 不复用写文件、applyEdit、自动保存链；扩展不能重复执行后端修改 |
| `.../services/{acpConnection,qwenAgentManager,qwenConnectionHandler,qwenSessionManager,qwenSessionReader,qwenSessionUpdateHandler}.ts` | 不导入产品连接与会话实现；替换为自有后端适配和 SessionStore |
| `.../ide-server.ts`、`webview/handlers/AuthMessageHandler.ts` | 排除 Qwen IDE HTTP 服务和登录绑定 |
| `packages/vscode-ide-companion/scripts/prepackage.js`、`copy-bundled-cli.js` | 替换；原 prepackage 会构建根仓库并安装/捆绑 Qwen CLI，不能用于新产品 |
| 两包 `package.json`、扩展命令/配置/状态键 | 建立独立包、锁文件、版本和命名；不得继续读写 `.qwen` 配置 |

### 直接与间接依赖

- WebUI：React、React DOM、markdown-it；后者的实体、链接和参数处理依赖通过实验锁文件记录。构建使用 Vite、React 插件、TypeScript、vite-plugin-dts、PostCSS、Tailwind、Autoprefixer。
- 历史扩展：`@qwen-code/webui`、ACP SDK、MCP SDK、express/cors、zod、semver 等。源码还越过 package.json 直接导入 Qwen core 的 IDE schemas、路径工具、文件发现和 tokenLimits，因此只检查包清单不足以证明独立。
- 当前版额外依赖 `@qwen-code/web-shell` 和 daemon SDK；UI 状态耦合更深。
- 新产品保留必要的普通 npm 依赖，不保留 Qwen SDK、core、CLI、daemon、ACP 或整仓 workspace 依赖。
- `MarkdownRenderer` 已禁用原始 HTML，但仍有渲染后 DOM 处理和 `dangerouslySetInnerHTML`，后续须对恶意链接/路径/图片做专项测试，不能仅凭 `html: false` 宣称完整安全。
- diff 的双参调用会从磁盘读取“旧内容”；对已经应用的变更，这不一定是本轮修改前基线。新实现必须使用可靠的前后内容或显示基线缺失。
- Webview 现有 CSP 使用宿主资源源与 inline style；新工程需结合实际打包方式控制脚本、样式和资源访问。不得无条件加载外部图片或账户资源。

## 6. UI 独立构建实验

环境：Linux，Node `v20.18.1`，npm `10.8.2`。Node/npm 为现有系统工具，安装依赖与构建输出均位于 LitIsLand 审计目录；未修改其全局安装。没有 pnpm。发现 VS Code remote CLI，不代表本机具备可操作的桌面 Extension Development Host。

实验从推荐快照原样复制 `packages/webui` 的源码、CSS、tsconfig、Vite/PostCSS/Tailwind 配置。仅实验包清单改为私有的 `lg-p0-webui-build-probe`，移除不参与本次库构建的 Storybook 工具依赖；没有改 UI 源码。14 个直接依赖版本取自快照根 `package-lock.json`：

```text
react/react-dom 19.2.4; markdown-it 14.1.0
@types/markdown-it 14.1.2; @types/react 19.2.10; @types/react-dom 19.2.3
@types/node 20.19.1; @vitejs/plugin-react 4.7.0
autoprefixer 10.4.22; postcss 8.5.6; tailwindcss 3.4.18
typescript 5.8.2; vite 5.4.21; vite-plugin-dts 4.5.4
```

新增实验锁文件固定实际解析到的间接依赖，SHA-256 为 `d4db447641c79b6961ca397327a304f05885adcbaf5a0d75d6e790c53aa47300`。这不是声称完全复现原 monorepo 的所有间接依赖。

在 `ui-build-probe` 中实际执行：

```bash
npm install --ignore-scripts --no-audit --no-fund
npm run typecheck
npm run build
```

全部退出 0。安装 217 个包；构建转换 151 个模块，生成 `dist/index.js`、`index.cjs`、`index.umd.js`、`index.d.ts`、CSS 和 source maps。ESM 为约 356.27 kB，CSS 约 79.04 kB；类型声明工具使用其自带 TypeScript 5.9.3。未运行 Qwen 原 prepackage、CLI、登录或 daemon。已有实验目录可用 `npm ci --ignore-scripts --no-audit --no-fund` 重新安装后复现。

此证据证明推荐 UI 包可以在没有整仓 workspace 包的环境构建，不证明完整扩展、迁移后 UI 或 F5 已通过。尚未进行依赖安全审计，不能把历史锁版本直接当作未来产品的长期依赖策略。

## 7. 后端测试与领域回归证据

隔离副本从已备份 Agent 的 `git archive HEAD` 导出产品目录和测试；临时工作区置于 LitIsLand 的 `tmp`。未使用真实书籍作写入测试，未使用真实模型凭据。测试调用已有 app-server 和本地模拟 provider；不是模拟 app-server 替代全部集成。

在 `agent-snapshot` 中实际执行：

```bash
env TMPDIR=/opt/conda/envs/LitIsLand/audits/lg-vscode-p0/tmp PYTHONPATH=lg-cli LG_TEST_RUNTIME_MANIFEST=/opt/conda/envs/LitIsLand/lg-runtime/rust-v0.154.0/runtime.json /opt/conda/envs/LitIsLand/bin/python -m unittest -v tests.test_live_agent tests.test_backend tests.test_writing_mcp tests.test_live_runtime.LiveRuntimeTests.test_creative_reviewer_has_read_tools_only tests.test_live_runtime.LiveRuntimeTests.test_persistent_thread_resumes_in_new_app_server_process tests.test_live_runtime.LiveRuntimeTests.test_real_core_streaming_and_steering
```

结果：**17 tests，29.880 秒，OK**。其中部分单测使用 fake agent/RPC；三项指定 LiveRuntime 测试使用真实固定二进制和本地模拟模型。覆盖现有审批拒绝/接受与取消、事件游标/会话互斥、创作预览失效、结构化输出校验、MCP 拒绝写入与版本历史、真实恢复和 steer。不能用这组定向测试替代目标中的全部传输、界面和文件保护验收。

另执行：

```bash
env TMPDIR=/opt/conda/envs/LitIsLand/audits/lg-vscode-p0/tmp PYTHONPATH=lg-cli LG_TEST_RUNTIME_MANIFEST=/opt/conda/envs/LitIsLand/lg-runtime/rust-v0.154.0/runtime.json /opt/conda/envs/LitIsLand/bin/python -m unittest -v tests.test_live_runtime.LiveRuntimeTests.test_main_agent_controls_explicit_workflow_over_real_mcp
```

结果：**1 test，16.996 秒，OK**。真实 app-server 调用 LG 独有的工作流控制 MCP，停止旧沙箱任务、以新指令重试；测试断言第二次尝试完成且输出为 `STEER_FROM_MAIN`。这证明现有 Python 启动链能让 app-server 执行 LG 特有行为，不能证明未来裸直连入口已经具备该行为，也不是付费真实模型测试。

回归验收候选：

1. LG 工作流停止/带新指令重试，保留上述真实 app-server + MCP 测试。
2. 修改章节前请求审批；拒绝后数据库与正文不变；批准后保留版本历史和过期版本检查。已有 `tests/test_writing_mcp.py`、`tests/test_backend.py` 提供基础，未来应从新扩展入口端到端验证。

在实际项目 `LiteraryAgent` 目录另运行 `env PYTHONPATH=lg-cli /opt/conda/envs/LitIsLand/bin/python -m lg_cli.backend_stdio --help`，成功显示真实入口参数；未启动模型任务。

## 8. 许可与来源范围

用户明确表示拥有移除源码及随包声明的单独授权。此事实按用户说明记录；授权原文、适用 commit、权利人和第三方覆盖范围尚未核验。本批没有删除许可证、NOTICE 或文件头。

实际快照并非单一声明来源：`packages/webui/package.json` 标记 MIT，而其多个源文件标记 Qwen Team / Apache-2.0；扩展 `esbuild.js` 等文件存在 Google LLC / Apache-2.0 文件头，npm 依赖另有各自许可证。不得假设一份 Qwen 授权天然覆盖所有第三方代码。下一批需要明确单独授权的覆盖范围，再形成具体删除/保留清单；本审计的来源映射用于工程核验，不在本批删除。

## 9. P0 产物与待讨论项

已完成：GitHub 备份与远程核验、真实源码/版本/运行时定位、共享化差异定位、实际协议生成、Qwen 双快照比较、推荐 UI 包独立构建、18 项定向后端测试、审计文档。

未运行或未交付：定制 app-server 构建、真实付费模型调用、扩展直连领域端到端测试、原 Qwen 完整扩展构建、迁移后 UI/MockBackend、桌面 F5、未保存文件保护、VSIX 安装、第二个真实后端版本、最终产品依赖/安全/许可审计。P1–P5 均未宣称完成。

下一批前需确认：

1. “直接连接 app-server”是否允许 LG app-server 后端继续调用现有 Python 领域 MCP 与模型适配组件？推荐允许并共享后端逻辑；若不允许，需要更大范围的 Rust 迁移。
2. 是否以“章节修改审批与版本历史”为首要领域验收，工作流控制作为回归项？用户尚未给出明确的具体业务验收示例。
3. 单独授权是否涵盖 Google 来源文件、WebUI 源文件声明和随包第三方依赖？只需说明范围，私密协议不应提交到代码仓库。
4. 首个实际安装目标是当前 Linux Remote VS Code，还是另一台桌面系统？这会影响 Python/MCP/runtime 打包和生命周期验证；不能把 remote CLI 存在当成桌面验证成功。

在上述讨论前保持现有工程行为。下一阶段应在 LitIsLand 先完成最小可运行验证，再将必要改动应用到产品；任何删除另列具体清单并遵守用户的删除前确认规则。
