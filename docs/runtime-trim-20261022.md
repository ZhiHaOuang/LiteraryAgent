# Extension 旧直连器精简记录 — 2026-10-22

本批基于 `lg-narrative-evidence-20261022`，实际基线为 `a33f4a58fe6ac37ab05a5d88a8c0889bbe434ac9`。作者已明确批准固定清单内 115 个文件及对应测试／文档迁移。应用前逐项核对 SHA-256 并保存在仓库外；没有扩大删除范围。

## 实际范围

- 删除 `lg-vscode/src/backend/CodexBackend.ts`、`normalize.ts` 和 `generated/` 清单内 113 个文件，共 115 个、86,790 字节源码。
- 迁移 11 个测试／文档文件：生命周期与历史测试改走现用 `LiteraryBackend`；保留 `RpcTransport` 的底层测试和独立 Core stdio 集成；展示过滤断言通过实际 Python `codex_events.normalize_item` 验证。
- 生产代码原本已经通过 `backend/factory.ts` 使用共享 LG Backend。本次没有改变 Runtime 选择规则，也没有删 npm 依赖。

完整文件路径、删除前哈希、迁移后哈希、验证与固定 Core 标识见 [验证记录](validation-20261022-trim.json)。`literarygiant.backend=codex` 继续选择共享 Backend 内的 Codex Runtime。Rust TUI、独立 app_server、通用工具、共享测试夹具及 `core/codex` 全部保留。

## 验证含义

隔离副本合并本批与认知证据输出后：57 项单元测试通过，Native／任务／展示集成 8 项通过；1 项 Core stdio 集成因缺少已验证 Runtime 产物跳过。类型检查、build、package 成功。实际项目重复相关入口与前端检查的结果单列在 JSON 的 `actualProject`，不以隔离结果代替实际验证。

认知证据版本与“认知证据＋本批”版本的三份生产 dist 文件逐字节相同，生产依赖图也相同。VSIX 增加 743 字节，仅三个说明文档发生变化；**本批减少源码维护面，没有减少生产包体积或 Rust Runtime 体积**。VSIX 仅构建和检查，没有安装。未运行付费模型创作 smoke。

本批不修改 Python 生产代码。此前认知证据批的 128 模块回归为 1,033 项执行通过、43 项条件跳过；该证据仍绑定原批输入，不能把这里的前端测试累加成新的 Python 全量结果。

## Rust Core 的下一步门槛

在隔离副本使用官方校验过的 Rust 1.95.0，针对固定上游 `6b9826e3aa83b1a5947db50f4332cb9c65f1b340`、目标 `aarch64-apple-darwin` 实际执行：

```sh
cargo metadata --format-version 1 --locked --filter-platform aarch64-apple-darwin --no-deps --offline
cargo metadata --format-version 1 --locked --filter-platform aarch64-apple-darwin --offline
cargo metadata --format-version 1 --locked --filter-platform aarch64-apple-darwin
```

三条命令独立执行。第一条通过，只能证明清单解析；第二条因缺少 crossterm Git 缓存失败；第三条在取得依赖元数据后以 101 退出，明确报告原始锁文件需要更新、`--locked` 不允许更新。尚未查明锁文件需要更新的具体原因，未通过去掉 `--locked` 改写它来掩盖问题。

未启动编译；不宣称当前 Mac Core 构建或反向依赖验收完成。原始与结束时 Cargo.lock SHA-256 均为 `29b37678141ef79e8548a8f135161e5360cd5c2c775cf497589bcfdfe9efc1a0`，隔离上游与实际 Core 工作树均无改动。

因此下一批 Rust 工作只能先研究可复现的锁文件／构建基线，并在独立派生环境确认完整依赖图；没有依据批准直接删除 Rust 模块。不能把局部 `--no-deps` 成功当作完整依赖或编译成功。

## 复核与回退

Extension 使用 Node 20.18.1、现有 lockfile 的依赖安装，运行 `npm run typecheck`、`npm test`、`npm run build`、`npm run package`。本轮实际验证复用仓库外锁定依赖，不在作者项目安装依赖或改机器配置；命令和产物哈希保存在本机证据目录。集成测试须显式提供测试 Python，合成模型仅监听 localhost；Core 集成还须提供已验证的 `LG_TEST_RUNTIME_MANIFEST`，否则明确跳过。

回退应作为本批代码的独立 revert，恢复删除文件及配套测试／文档；不重置整个工作树，不删除会话、作品、数据库或机器配置。交付标签保留删除前版本；新旧版本的正式作品均不在本批迁移范围内。
