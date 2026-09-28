# 书籍工作台审查

本批在上一批 CLI 界面改动基础上实施，启动入口仍为 `literary`。

## 使用

- 启动恢复上次工作的书籍并显示工作台；切换书籍后显示新书工作台。
- 首页直接输入开始交流。`/home` 返回工作台，保留当前对话。
- 鼠标滚轮、方向键和 PageUp/PageDown 滚动首页；Ctrl+O 进入面板完整资料。
- 92 列以下纵向排列，92–149 列两栏，150 列及以上三栏。
- `/preferences` 修改称呼、默认模型、回复详略和写作偏好。
- `/resume` 显示简短标题与最近更新时间；恢复后用 `/rename` 修改名称。
- `/` 的常用三项共用金色圆角框。分类内部去掉重复类别前缀，说明左边缘对齐。

## 资料与统计

首页仅读取本地资料，不发模型请求。书籍概况、人物、人物关系、剧情进展、完整规划、个人偏好和用量分别显示。
旧书已有的人物文档、明确设定、时间线，以及仍匹配当前版本的章节人物／事件分析也会读取；过期分析不混入当前进展。
主角须有明确角色标记。资料缺失显示“尚未记录”，不会从任意正文猜出关系。
图谱有节点与标注关系的连线；先后关系与因果关系分开。规划面板同时呈现既有节点和未来节点。
候选材料、草稿、规划明确标注。详情保留来源文档与版本。

新生成内容可携带结构化 `story_index`；主 Agent 写作工具、场景草稿/修订及传统工作流都支持。
索引和正文版本绑定；接受版本后自动更新 `ReferenceLibrary/indices/story.json`。
生成文件的索引校验内容哈希，采用生成稿时继承索引。手动编辑正文后旧索引失效，缺项明确提示。
关系索引与正文一同生成，无独立首页分析调用；模型生成这些字段可能增加少量输出 token。

个人偏好保存在 `$XDG_CONFIG_HOME/literarygiant/preferences.json`，默认 `~/.config/literarygiant/preferences.json`。
本书写作偏好覆盖保存在 `.literarygiant/preferences.json`；默认模型复用现有隔离模型配置。
对话名称使用独立元数据，聊天正文与内部恢复标识保留。

实际用量写入本书 `.literarygiant/usage.sqlite3`，按提供方和模型分组，包含主 Agent、子 Agent与工作流调用。
缓存输入仅计一次；中断请求、用量缺失分别标注。订阅会话按每个线程的累计用量增量记账，并通过本地内核查询实际模型；重复通知不重复计数，分叉历史不计作新请求。
旧日志仅汇总带明确 token 数的事件；没有可靠模型名称的记录单独标注。
新账本记录开始后的日志不再重复累计。历史缺失不会被显示成完整的零用量。

## 审查材料

预览脚本 `scripts/preview_book_home.py` 使用虚构书籍与本地模拟用量，导出 40、64、100、160 列的真实 prompt-toolkit 画面。

```bash
TERM=xterm-256color PYTHONPATH=lg-cli python scripts/preview_book_home.py --output /tmp/lg-home-previews
```

本次预览：`/tmp/lg-home-ys763ive/previews/`。真实终端烟雾测试覆盖启动、恢复、改名、返回首页、修改称呼与退出。

## 验证结果

- 在 `LitIsLand` 隔离副本开发验证后同步产品文件；未新增依赖。
- 实际项目完整测试 283 项：279 项通过，4 项可选条件跳过。
- 固定内核与本地模拟模型验证：并行审查、实时指挥、工作流停止/重试、主/子 Agent 用量及订阅统计路径。
- 正式 `literary` 64×30 PTY 操作：启动首页、隐藏对话 ID、改名保存、返回首页、修改称呼、切书、重启恢复上次书籍、退出均通过。
- 1005 章书籍不会受旧浏览列表上限截断；下属场景正文也计入章节正文统计。
- 索引验证涵盖旧资料、版本失效、生成稿采用、关系端点校验及目录边界。
- `git diff --check` 通过。未调用付费模型，未上传个人配置或测试书籍。

## 修改文件

- 首页与索引：`book_home.py`、`story_index.py`、`project_store.py`、`story_workflow.py`、`run_store.py`、`workflow_runner.py`及三个输出 schema。
- 输入和菜单：`main.py`、`terminal_input.py`、`terminal_view.py`、`slash_commands.py`。
- 设置和对话：`preferences.py`、`config.py`、`conversation_store.py`。
- 用量与模型入口：`usage_ledger.py`、`anthropic_bridge.py`、`provider_transport.py`、`live_agent.py`、`core_adapter.py`、`writing_mcp.py`。
- 预览、文档及相关测试。

业务代码均位于产品层，未修改内核、用户书稿或外部资料库。
