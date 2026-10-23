# 2026-10-24 冗余收敛后续批次

基线 `7477dd0b051fbbf4fe25b6abe29ec741ed5765f3`，继续使用
`codex/polish-compliance-20261022`。先在新 LitIsLand 固定源码副本验证，再应用实际项目；
每批独立提交，主线不合并。本机详细证据位于
`~/.codex/reports/LiteraryGiant-redundancy-followup-20261024/`。

## 批次一：默认产品路径

`lg-cli/lg_cli/paths.py:product_root` 原先检查 pin／lg-agent 目录存在与否，两个分支都返回相同路径。
现直接返回 `Path(__file__).resolve().parents[2]`，减少无作用的文件系统探测；
`LITERARYGIANT_HOME → LG_HOME → 默认路径` 的优先级、expanduser／resolve、
core_codex_root 的显式覆盖及 data_file 接口不变。改动为 1 行加入、4 行移除，无文件删除。

隔离与实际源码分别执行以下现有测试，均 **31 通过、0 跳过**：

```bash
PYTHONPATH=lg-cli:. TMPDIR=/private/tmp python -m unittest tests.test_config tests.test_cli tests.test_doctor tests.test_native tests.test_runtime_manifest tests.test_workflow_builtin_contract -v
```

另以原始函数作对照检查默认、LG_HOME、双变量优先、空值回退、波浪号展开和显式 Core 路径六组配置，
三个路径 API 结果均相同；真实读取六类 bundled catalog，并验证 DefinitionRegistry 的 Skill／Agent 回退。
同一检查也在离线构建的 wheel 解包目录通过，141 个包文件与验证源码／资源一致，包内 CLI 帮助通过。
没有新增第三方依赖或永久的实现镜像测试，也不宣称覆盖异常文件系统的全部行为。

精确结果、源码和产物摘要见 [验证记录](validation-20261024-redundancy-followup.json)。

## 批次二：无消费者的内部 UI 导出

TypeScript 符号／别名解析覆盖 tsconfig 的 64 个源码文件，并追踪正式入口与打包边界：
`webview/index.tsx → InputForm / ToolCallContainer / CodeBlock`；没有 UI 动态模块发现。
`package.json` 为 private 且无 UI exports/types 发布，VSIX 排除 webview 源码，实际消费 Webview IIFE；
esbuild 元数据确认无导出、无动态 import。这些闲置模块导出不构成产品发布 API。

| 文件（相对 lg-vscode/webview/ui/components） | 移除 | 保留 |
| --- | --- | --- |
| layout/InputForm.tsx | getEditModeIcon、其类型及专属图标导入 | InputForm、EditModeInfo、全部活跃 Props |
| icons/EditIcons.tsx | EditPencil、AutoEdit、PlanMode、OpenDiff、Undo、Redo、ReplaceAll、Copy、Paste、SelectAll 十个 Icon | CodeBrackets、HideContext、SlashCommand、Link 四个 Icon |
| icons/NavigationIcons.tsx | ChevronDown、Chevron、Plus、PlusSmall、Close、CloseSmall、Search、Refresh 八个 Icon 及专属助手 | ArrowUpIcon |
| toolcalls/shared/LayoutComponents.tsx | ToolCallCard、ToolCallRow、StatusIndicator、LocationsList，及专属类型／助手／FileLink 导入 | ToolCallContainer、CodeBlock、活跃容器 Props |

合计移除 23 个未使用运行值导出、6 个专属类型、2 个私有助手和2处专属导入；
四份源码净减638行、15492字节。Source provenance 的 import-files.json 是历史上游清单，保留原摘要。
FileLink.tsx 暂保留，未以空文件规避删除授权；本批也未手工清理共享 CSS。

隔离与实际项目各59项单元、严格类型检查、构建通过；实际项目另3项真实Backend／Native集成通过、
1项固定Core条件跳过。14组 InputForm／工具容器 SSR（发送、停止、等待、选区、上下文、菜单与5类状态）
在基线、隔离、实际源码生成完全相同HTML。没有桌面点击或视觉验收，不据此证明所有交互组合。

| 生产文件 | 原样基线字节 | 四文件清理后字节 |
| --- | ---: | ---: |
| extension.cjs | 130517 | 130517 |
| webview.js | 473955 | 473925 |
| webview.css | 50948 | 50031 |

extension.cjs逐字节不变；死JS本来大部分已被构建剔除，因此运行产物收益有限。
CSS减少16条Tailwind utility：完整有序规则比较确认其余规则未变；保留源码字面量、SSR类和19处动态class
表达式核验未依赖退出的utility。实际产物与隔离产物逐字节一致。初版SSR材料／比较器问题保留于私有日志，
修正验证材料后通过，未为其修改产品行为。

复核使用Node20及既有锁定依赖，在lg-vscode目录运行 `npm run typecheck`、`npm test`、`npm run build`；
真实Backend集成命令与上一批[工程清理记录](engineering-trim-20261024.md)相同。
符号、渲染、CSS、动态类和逐文件摘要在本机证据目录的 `ui-audit/` 下。

### 待作者批准的单文件删除

唯一候选：`lg-vscode/webview/ui/components/layout/FileLink.tsx`，180行／4933字节，
SHA-256 `07b79314ebb5de839237fc22a84bf1e09cdfacd47c25825aa538750c1c5421de`。
其唯一调用者是上述已退出的LocationsList。正式打开文件仍由index.tsx和MarkdownRenderer的现有消息路径处理。

新建不包含该文件的隔离目录，59项单元／类型／构建通过；两个JS与四文件清理版本一致。
额外退出的17条CSS规则／980字节仅服务于旧文件或其注释，保留类与规则核验通过。
这是删除前验证，不代表已经批准或已经删除。具体清单在本机 `file-deletion-manifest.json`，
确认前正式文件保留。授权范围不包括任何共享CSS、工具能力或其它文件。

## 批次三：历史策略声明查清后保留

| 声明 | 实际用途 | 执行边界 |
| --- | --- | --- |
| lg-tools/tool_policy.json | catalog.py公共读取、sync_runtime_resources.py同步，打包进wheel | 没有当前执行消费者；allow／deny不是宿主权限开关 |
| lg-context/context_spec.json | 同上；含旧stub和future_adapter描述 | roots及预算字段没有驱动当前上下文构造 |
| lg-agent/task_modes.json | main._main先初始化，再由print_modes／load_task_modes供CLI展示 | system_prompt、allowed_tools、can_*、requires_*等字段不授权任务 |
| lg-prompts/prompts.md | 模式标签的参考文本；参与compare_polish_sources源码冻结 | 不入wheel，未发现运行时读入最终模型指令 |

对前三份JSON核对checkout、实际bundled回退及源／包字节一致；正式 `python -m lg_cli modes --json`
退出0、stderr为空，返回完整13个模式及原字段；13个模式的引用都在14个参考标题中，无失效标签。
初始声明来自65d613c020，fe0d338ff9加入包资源回退；本轮没有证据表明公共读取已被迁移或退役。

因此保留这四类声明和两份bundled副本，不删除字段、不修改payload、不补另一套策略加载器。
两旧JSON单份合计1961字节；此批成果是明确维护边界，不能算代码体积减少。
实际权限、上下文和模型输入落点已写入[架构说明](architecture.md#兼容声明与实际执行规则2026-10-24)。

本批只改审计与架构文档。没有以绕过初始化的内部helper调用替代正式CLI验证，
没有新增模型请求、第三方依赖、Rust裁剪或文学质量结论。
