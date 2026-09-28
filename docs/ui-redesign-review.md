# CLI 界面优化审查

本批基线：Agent `a5559ea15`，主仓库 `52f75f7`。

## 已实现

- `/` 显示按使用次数更新的前三项和用途分类卡片；输入全局过滤。
  Enter 直接执行无参数命令，必填参数逐项填写；Tab 补全，Esc 返回。
  使用次数保存在仓库外的个人配置目录，跨书籍共用，无遥测。
- `/novel` 阅读编辑章节；`/outlines` 阅读编辑已有数据库大纲和生成的大纲文件。
  保存前确认，保留旧版本/原文副本，拒绝覆盖外部新修改。
- 普通对话使用持续的主 Agent 会话；工作中 Enter 通过 `turn/steer` 追加指令。
  多位审查 Agent 可并行，身份、步骤和状态显示在紧凑任务区，Ctrl+O 查看详情。
  子 Agent 的停止、重试和指挥由主 Agent 的协作工具完成。
- 用户消息右侧金色圆角框、正文左对齐；Agent 回复和公开阶段摘要左侧暗橙框。
  代码、表格和长日志默认缩略，Ctrl+O 查看原文；不展示完整推理链。
- 原创橙色墨圈字符动画配预设趣味英文词，贴在当前回复末行，空间不足时框内换行。
  装饰与实际任务步骤分离，工作结束消失。旧对话渲染缓存避免动画重绘全部历史。

显式 CLI 工作流保留原有后端与校验。运行期间追加消息会交给主 Agent；
主 Agent 可查看当前任务、停止或带着新要求重试。调整方向采用停止后重试，
新的作者要求传入每个模型阶段。已成功的任务不能通过该入口重复执行。
控制入口仅接受本次会话的任务标识，通过本机私有套接字通信。
书籍、会话或模型切换时重建连接，退出清理进程。

## 审查方式

重启 `literary`。建议在 64 列左右的终端检查：

1. 输入 `/`，观察常用项、分类、搜索和单次 Enter。
2. `/outlines` 打开已有大纲，E 编辑，Ctrl+S 或 Esc 离开编辑，确认保存。
3. 普通对话提出任务，工作时直接追加消息；Ctrl+O 查看日志或审查详情。

实际 prompt-toolkit 画面的 SVG 和文本预览可重新生成：

```bash
PYTHONPATH=lg-cli python scripts/preview_terminal.py --output /tmp/lg-previews
```

预览包含 40、64、80 列的对话、菜单、折叠详情状态，使用虚构书籍与临时配置，不调用模型。
本次生成结果位于 `/tmp/lg-ui-aop2MA/previews/`。

## 验证

- `LitIsLand` 隔离副本验证后，仅同步已验证文件到项目。
- 隔离副本完整测试 266 项：259 项通过，7 项按条件跳过。
- 同步后的实际项目专项回归 63 项全部通过，覆盖命令、编辑、呈现、会话隔离与工作流控制。
- 固定版本真实内核 + 本地模拟模型：流式回复中追加指令、两位审查 Agent 并行、
  主 Agent 经真实 MCP 停止命令并带新要求重试，三项均通过。
- 实际 `/usr/local/bin/literary` 的 64×30 PTY 烟雾测试：菜单、大纲读取/编辑/保存、原文保留、正常退出通过。
- `literary --help` 与 `git diff --check` 通过；未调用付费模型服务。
- 未修改 `core/codex`，未新增第三方依赖，未上传个人频次或测试书籍。

## 文件清单

- 命令与输入：`lg-cli/lg_cli/slash_commands.py`、`terminal_input.py`
- 对话与动效：`lg-cli/lg_cli/terminal_view.py`、`working_animation.py`、`live_view.py`
- 持续会话：`lg-cli/lg_cli/live_agent.py`、`main.py`、`anthropic_bridge.py`
- 工作流控制：`lg-cli/lg_cli/workflow_control.py`、`writing_mcp.py`、`native.py`、`core_adapter.py`
- 大纲编辑：`lg-cli/lg_cli/outline_editor.py`
- 预览：`scripts/preview_terminal.py`
- 文档：`docs/terminal-ui.md`、本文件
- 测试：`tests/test_command_palette.py`、`test_live_agent.py`、`test_live_runtime.py`、
  `test_ui_redesign.py`、`test_bookshelf.py`、`test_slash_commands.py`、
  `test_terminal_input.py`、`test_terminal_view.py`、`test_workflow_control.py`、`test_provider_profiles.py`
