# 2026-10-24 工程冗余盘点与有限清理

基线 `8e926aa5a1d3b963c2eef6def4503dcf7b24ece9`，分支 `codex/polish-compliance-20261022`。
先在 LitIsLand 的固定源码副本验证，再按 SHA-256 核对应用到实际项目。
本批没有整文件删除、依赖升级、Rust 改动或付费模型请求；不合并主线、不部署服务器或桌面扩展。
此前已获批的 115 个旧连接器文件删除不计入本批成果。

## 批次一：退出使用的私有实现与绑定

| 位置 | 依据与处理 | 保留的实际路径 |
| --- | --- | --- |
| `lg-cli/lg_cli/terminal_input.py` | `_progress_text` 仅有旧测试调用，布局从未绑定；移除方法及 FRAME_INTERVAL、prepare_frames、compact_content 三个无用导入 | `HistoryControl → _conversation → loading_text → render_conversation`；choice_rows 的真实调用和重导出保留 |
| `lg-cli/lg_cli/terminal_view.py` | 移除未使用的 Padding 导入 | Rich 排版与所有渲染函数保留 |
| `lg-vscode/src/host/DesktopHost.ts` | inventory 私有成员仅赋值，从未读取；移除成员和赋值 | 启动、切换作品、模型管理仍实际请求 inventory，局部 data 和配置监视保留 |
| `lg-vscode/src/backend/LiteraryBackend.ts` | 移除无用 BackendEvent 类型导入 | 共享 Backend 的事件校验与协议不变 |
| `lg-vscode/webview/{LiteraryTasksPanel,WorkspacePanels}.tsx` | 移除无用 React 默认导入，保留 hooks | 已有自动 JSX runtime 与组件行为不变 |
| `lg-vscode/webview/ui/components/layout/InputForm.tsx` | 仅移除无用 onToggleEditMode 参数解构 | Props 与调用方契约保留；没有新增或禁用按钮行为 |
| `lg-vscode/tests/legacy-history.integration.ts` | fixture 参数不再保存为未读取的私有成员 | super 参数、launch 覆盖、旧会话导入及接续测试保留 |

`lg-vscode/tsconfig.json` 把 noUnusedLocals／noUnusedParameters 纳入常规 `npm run typecheck`。
同一基线严格检查原有六处错误，清理后为零；它不检测所有未使用的公开导出或动态加载代码。

原动画测试改为读取 `editor.messages.content.text()`，覆盖 UI 的实际绑定。增加空闲／本地命令隐藏动画、
TERM=dumb 静态回退检查；动画变化不增加 transcript 或 blocks。未更改版本、来源、作者批准和通用工具规则。

## 验证与可量化收益

隔离及实际目录分别通过 Python 124 项，2 项固定 Core 条件跳过；Extension 59 项单元、3 项真实
Backend／Native 本地合成模型集成通过，1 项 Core 兼容集成跳过。集成覆盖旧历史导入接续及有界任务／澄清。
严格类型检查、前端构建和 CLI 帮助入口通过。不是全量 Python 重跑，也不是桌面点击验收。

用已有环境离线构建 wheel，无依赖安装；解包后的 141 个包文件与隔离源码／资源一致。
从解包目录导入实际 terminal_input／terminal_view 并运行 CLI 帮助通过，旧私有方法确已不再入包。
wheel 标识、源码摘要和逐项结果见 [验证记录](validation-20261024-engineering-trim.json)。

| 前端产物 | 基线字节 | 清理后字节 |
| --- | ---: | ---: |
| extension.cjs | 130544 | 130517 |
| webview.js | 473978 | 473955 |
| webview.css | 50948 | 50948 |

两个 JS 合计仅减少 50 字节，CSS 逐字节相同；收益主要是消除无用维护点和补正测试覆盖，不宣称明显的包体积、
执行速度、token 或文学质量改善。安装包版本仍为 0.3.0，Git 版本与产物摘要才标识本批。

可在既有 LitIsLand 环境复核：

```bash
PYTHONPATH=lg-cli:. TMPDIR=/private/tmp python -m unittest tests.test_terminal_view tests.test_terminal_input tests.test_ui_redesign tests.test_terminal_runtime tests.test_terminal_backend tests.test_cli_backend tests.test_workflow_control tests.test_auth_session tests.test_ui_details tests.test_dashboard tests.test_slime_animation tests.test_literary_tasks tests.test_polish_profile tests.test_workflow_builtin_contract -v
```

在 `lg-vscode/`，使用 Node 20 与已安装的锁定依赖：

```bash
npm run typecheck
npm test
npm run build
LG_TEST_PYTHON=/absolute/path/to/LitIsLand/bin/python PYTHONPATH=../lg-cli:.. TMPDIR=/private/tmp node node_modules/tsx/dist/cli.mjs --test --test-concurrency=1 tests/legacy-history.integration.ts tests/literary-task-backend.integration.ts
```

本机私有证据目录：`~/.codex/reports/LiteraryGiant-engineering-trim-20261024/`。
包括原样基线构建、两地验证日志、应用前后源码摘要、补丁和 wheel。实际 CLI／连接集成明确使用当前源码路径；
不证明已安装扩展、服务器或旧进程已加载该版本。

## 仍保留的候选与必要边界

| 候选 | 当前证据 | 下一步定位 |
| --- | --- | --- |
| `lg-cli/lg_cli/paths.py:product_root` 的目录存在判断 | 两条分支均返回 candidate，冗余很小 | 可在路径模块维护时收敛，本批不扩大路径解析变更 |
| 部分移植 UI 图标／布局导出 | 未发现正式导入，但公开导出和复用边界未完整核验 | 补调用／打包证据，再决定具体删除范围 |
| `core_adapter.run_model_turn_stub` | 无仓内调用，外部 Python 兼容用途未知 | 保留，不能仅凭 rg 无结果删除 |
| `lg-context/context_spec.json`、`lg-tools/tool_policy.json`、`lg-prompts/prompts.md` | 未发现实际策略执行消费者；前两者仍同步入包并由公共 catalog API 支持 | 不认定为强制规则；先核验兼容、文档和分发用途 |
| checkout catalog、Skill／subagent 目录、`lg_cli/resources` | DefinitionRegistry 支持目录覆盖；wheel 依赖 bundled catalog 回退 | 必要的不同安装路径，不能按内容重复直接删除 |
| 现有 Python／npm 依赖 | 本轮找到运行、构建或测试用途 | 未形成可直接删除的依赖清单 |
| Rust Core | 当前 Mac 原始 locked 构建证据仍不足 | 不删除；TUI、独立 app_server、通用工具继续保留 |

模型前后、作者等待前后、正式采用事务中的重复版本／来源检查处于不同时间边界，不属于无用重复。
上述盘点是有限产品层审计，不是全仓“零冗余”证明。

每批可按相应 Git 提交单独 revert；先检查后续变更，不连带回退作者作品或主线之外的提交。
