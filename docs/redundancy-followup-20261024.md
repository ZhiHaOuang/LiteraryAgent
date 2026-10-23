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
