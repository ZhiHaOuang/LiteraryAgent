# 首页与终端细节优化

这一批修复首页同步扫描、空回复框、执行过程无法展开、鼠标拖选受阻，以及首页资料直接暴露内部结构的问题。

## 操作

- 首页顶部固定显示原有史莱姆动画、书名、称呼、当前模型。窄屏使用同一动画姿态的缩小版本，面板独立滚动。
- F1 进入浏览模式；方向键选择面板，Enter 选择条目，再 Enter 查看详情。Esc 逐层返回输入。
- 对话里 F1 选择“思考摘要”或“执行记录”，Enter 或点击展开、收起；Ctrl+O 查看完整记录。
- 默认释放鼠标捕获，可直接拖选文字，用 macOS 的 Cmd+C 复制。浏览模式需要点击时才捕获鼠标；进入详情或 Esc 退出浏览即恢复拖选。
- 这里只展示公开的思考摘要，不展示模型的私有推理链。

VS Code 默认将 F1 绑定到命令面板。在 Mac 的“首选项：打开键盘快捷方式(JSON)”中，将 [vscode-keybindings.json](vscode-keybindings.json) 的对象加入现有数组。此设置仅在终端聚焦时将 F1 发给 CLI，保留 Cmd+Shift+P 的命令面板功能。参考：[官方默认快捷键](https://code.visualstudio.com/docs/reference/default-keybindings)、[终端快捷键与 sendSequence](https://code.visualstudio.com/docs/terminal/advanced)。Mac 功能键处于媒体键模式时需使用 Fn+F1。

## 数据与缓存

首页先读取本地数据或带时间的画面缓存，随后核对完整资料。无缓存首帧使用轻量渲染器，复用同一套面板内容与原有史莱姆姿态，不等待工作流、完整终端绘图库和历史日志解析。缓存位于 `$XDG_CACHE_HOME/literarygiant/home`（默认 `~/.cache/literarygiant/home`），按书籍绝对路径、身份和终端尺寸隔离，文件权限为 0600，不进入 Git。

章节数量与最新章节读取数据库；已写表示章节或场景中存在正文，审核状态在章节详情里保留。索引缓存校验数据库、WAL、分析资料和生成结果的来源文件；空 WAL 不代表资料变更，读取连接显式关闭。画面缓存也校验书籍数据库与设置。历史用量缓存校验日志和清单的时间、大小以及统计切分时间。旧快照明确显示更新时间，未完成的首次历史统计显示读取中，不伪装成零。

人物按明确姓名和出场标注合并，英文内部键只有在正文明确提供姓名时才映射。章节分析中的概念条目留在补充详情。所有来源保留，书籍资料没有被修改。明确写作“全书唯一…视角”的人物标为“视角人物”，不擅自升级为主角。

完整规划仅来自明确的大纲；结构化内容转换为中文字段，规则数组中的内部分类键不进入概览。章节顺序图仅表示规划顺序，不暗示因果关系。用量区突出总量和模型名；不同提供方的未知历史模型分别保留，统计范围在详情解释。

## 验证记录

测试在 LitIsLand 和隔离副本中运行；实际工作书籍通过只读 SQLite 备份复制后检查，不改动原正文、版本或资料库。实际书籍的首页人物由 71 条章节记录整理为 12 项人物／群体资料，概念与群像分析仍保留在详情。来源未变化时，快照检索实测约 23–188 ms；首次全量核对约 1.6 秒，在后台完成。当前工作书籍已用只读数据库连接预热个人缓存，并核对来源校验值不变。

启动性能用 `scripts/benchmark_home.py` 从子进程启动前开始计时，以真实 PTY 中整帧首页写出为首屏时间，另外记录输入就绪时间。不能把首屏显示时间与所有历史资料重新计算完成时间混为一谈。

实际安装的 `literary` 入口，使用当前 12 章书籍的隔离副本，在 64×30 PTY 中连续启动 10 次，首屏为 115.4–287.5 ms，中位数 146.0 ms；包含首次无缓存启动。输入就绪单独测得 738.6–1418.0 ms。计时终点是服务器 PTY 收到首页首帧，不包含 Mac 到服务器的网络传输和客户端绘制延迟。

实际 CLI 的 PTY 验证通过：启动首页、F1 面板导航、条目与详情、Esc 逐层返回、鼠标捕获释放、隐藏对话 ID、重命名持久化、返回首页、偏好修改、切换书籍、重启恢复最近书籍和正常退出。原有 Codex 内核集成测试也纳入回归；没有付费模型调用。

最终项目回归：297 项测试，293 项通过，4 项跳过。`git diff --check` 通过；本批没有修改 vendored Codex 核心。窄／中／宽终端的实际渲染、中文单元格宽度、人物与大纲来源、空消息、折叠入口、短操作提示、错误首行、空 WAL 缓存失效和章节审核详情均有检查或回归测试。

验证命令：

```bash
env PYTHONPATH=lg-cli LG_TEST_RUNTIME_MANIFEST=/opt/conda/envs/LitIsLand/lg-runtime/rust-v0.154.0/runtime.json /opt/conda/envs/LitIsLand/bin/python -m unittest discover -s tests -t . -q
env PYTHONPATH=lg-cli /opt/conda/envs/LitIsLand/bin/python scripts/benchmark_home.py --command literary --runs 10
```

Mac 的 VS Code 客户端未在此服务器上直接运行；鼠标释放通过真实终端控制序列验证，F1 的客户端按键路由需要上面的配置。

## 本批文件清单

- 启动与缓存：`lg-cli/lg_cli/main.py`、`startup.py`、`first_home.py`、`home_cache.py`、`usage_ledger.py`。
- 首页与来源展示：`lg-cli/lg_cli/book_home.py`、`book_presentation.py`、`story_index.py`。
- 交互与动画：`lg-cli/lg_cli/terminal_input.py`、`terminal_view.py`、`live_view.py`、`slime_animation.py`。
- 测试：`tests/test_book_home.py`、`tests/test_home_polish.py`。
- 验证脚本：`scripts/benchmark_home.py`、`scripts/preview_book_home.py`。
- 文档：`docs/terminal-ui.md`、`docs/home-polish-review.md`、`docs/vscode-keybindings.json`。
