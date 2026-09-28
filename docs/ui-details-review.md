# 本批终端细节优化审查

## 已实现

- 首页和聊天页使用同一个固定的大圆角框，原版史莱姆居中；窄窗口使用原版紧凑投影。
- 常用命令和分类内命令均左对齐名称、右对齐说明，按终端字符宽度截短。
- `/browse` 进入方向键浏览，Enter 展开、Esc 逐层返回；退出后恢复终端原生拖选。思考摘要、执行记录、代码、表格和协作任务都有入口，底栏仅保留一处导航提示。
- `/run list`、`/run show` 使用本地只读列表／详情，不调用 Agent；其他斜杠命令仍按显式 CLI 路由。管理命令不进入用户聊天框，创作命令保留独立请求框。
- `/profile` 修改用户名／称呼（同一字段）、默认模型、回复详略及个人／本书写作偏好；`/preferences` 保持兼容。
- 书籍综述由正文生成／改写的同一次模型输出提供，保存到 `ReferenceLibrary/bible/book-overview.json`。规划中间阶段可不更新，最终正文必须提供有效综述。首页仅读取；旧资料回退到大纲中的故事前提，无内容时显示“尚未设置简介”。候选稿不因综述更新而成为已采用正文或 Canonical 事实。
- Loading 使用 Thinking Orbs 原版 working/20 轨迹，转换为橙色 8×8 点阵（4 列 × 2 行），跟随最新回复；趣味词不代表实际任务。上游版本 de85557ca220332586d070d8788c0e1d6e877a0d，MIT 许可随包保存。
- 首屏立即保存可失效的画面缓存，后台完整索引完成后再更新；修复默认模型下缓存读写环境标识不一致，命中缓存时延后不需要的导入。

## 验证范围

使用 LitIsLand 隔离副本验证后逐文件应用，真实书籍资料没有用于写入实验。实际 `literary` 的 PTY 检查涵盖首页、/browse、层级详情、鼠标捕获释放、本地运行列表、历史对话、改名、/profile 保存、切书、重启恢复和窗口尺寸变化。

界面记录检查 40、64、100、160 列。Thinking Orbs 几何与原版 JavaScript 在三个时间点的输出对照，最大偏差约 1.8e-15。点阵受终端字体及分辨率限制，不宣称与浏览器像素一致。未在用户的 macOS VS Code 客户端上直接操作鼠标复制。

性能数据将记录为首屏显示和输入就绪两项，冷启动与缓存命中分开列出，不将局部测试结果作为所有环境的保证。

## 本批变更文件

- `lg-cli/lg_cli/book_home.py`
- `lg-cli/lg_cli/book_overview.py`
- `lg-cli/lg_cli/first_home.py`
- `lg-cli/lg_cli/home_cache.py`
- `lg-cli/lg_cli/live_agent.py`
- `lg-cli/lg_cli/main.py`
- `lg-cli/lg_cli/native.py`
- `lg-cli/lg_cli/preferences.py`
- `lg-cli/lg_cli/prompt_builder.py`
- `lg-cli/lg_cli/resources/native-writing.txt`
- `lg-cli/lg_cli/resources/revision.schema.json`
- `lg-cli/lg_cli/resources/scene-draft.schema.json`
- `lg-cli/lg_cli/resources/stage-output.schema.json`
- `lg-cli/lg_cli/resources/thinking-orbs-LICENSE.txt`
- `lg-cli/lg_cli/run_browser.py`
- `lg-cli/lg_cli/slash_commands.py`
- `lg-cli/lg_cli/startup.py`
- `lg-cli/lg_cli/story_workflow.py`
- `lg-cli/lg_cli/terminal_input.py`
- `lg-cli/lg_cli/terminal_view.py`
- `lg-cli/lg_cli/thinking_orb.py`
- `lg-cli/lg_cli/workflow_runner.py`
- `lg-cli/lg_cli/working_animation.py`
- `lg-cli/lg_cli/writing_mcp.py`
- `tests/fixtures/thinking-orbs-working.json`
- `tests/helpers.py`
- `tests/test_native.py`
- `tests/test_story_workflow.py`
- `tests/test_terminal_view.py`
- `tests/test_ui_details.py`
- `tests/test_ui_redesign.py`
- `tests/test_writing_mcp.py`
- `scripts/benchmark_home.py`
- `scripts/preview_book_home.py`
- `docs/terminal-ui.md`
- `docs/ui-details-review.md`（本审查记录）

## 实际入口性能记录

在当前 12 章书籍的测试副本上运行实际 `literary`（64×30），使用独立测试环境标识：

| 路径 | 无缓存首次首屏 | 后续缓存首屏 | 输入就绪范围 |
| --- | --- | --- | --- |
| 显式选择书籍，10 次 | 860.0 ms | 42.7–101.9 ms | 691.9–2562.5 ms |
| 恢复上次书籍，5 次 | 305.0 ms | 74.0–182.9 ms | 741.2–1588.1 ms |

缓存启动达到了 300 ms 目标；**无缓存冷启动尚不能保证 300 ms**。不要将缓存首屏时间误报为完整交互就绪时间。诊断中首屏代码的 CPU 时间约 68–72 ms，实际经过时间有明显波动；这不足以单独证明所有冷启动延迟的来源。

修复前一组实际入口结果为首屏 115.4–565.6 ms；首次缓存立即保存、默认环境身份匹配、延后导入后记录如上。保留失败和较慢样本，未用隔离副本更快的数据替代实际入口数据。

## 最终检查结果

- 实际项目完整测试：311 项，307 通过、4 跳过（启用 pinned core 的本地假模型集成测试）。
- 实际 literary PTY 流程检查通过；不调用付费模型。
- `git diff --check` 通过；`git status --short -- core` 无变更。
- 35 个实现／测试／文档文件在隔离副本与项目中逐字节一致，另附本审查记录，共 36 个本批变更文件。
- 最终实际屏幕记录：`/tmp/lg-details-fle8b_4m/actual-previews/`。
- 本批覆盖前的文件备份：`/tmp/lg-details-fle8b_4m/project-before-apply/`。

重启 literary 后载入本批代码。
