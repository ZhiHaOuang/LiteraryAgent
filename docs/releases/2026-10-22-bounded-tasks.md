# 2026-10-22 有限文学任务交付

这是包版本 **0.3.0** 上的开发交付，版本名 `lg-r5-bounded-20261022`。Git tag 固定完整源码，不表示已经发布新 wheel／VSIX 或完成文学质量验收。父仓库通过 gitlink 固定产品提交；确切提交和发布位置由父仓库交付记录保存，避免在提交自身嵌入自己的 hash。

本批从 `6b22bc47c87aecac99593ca61a1341f1c7e0c720` 出发。正式采用前检查候选实际基础版本与作者所选方向；章节导入部分成功可恢复派生状态；受支持正式操作提供事务提交查询。新增共享 Backend 的选区润色、章节分歧停点、运行身份、有限 Native Responses 认知推演及来源复核动作。Core pin `6b9826e3aa83b1a5947db50f4332cb9c65f1b340` 未改变，Rust TUI／独立 app_server 保留。

完整设计与限制见 [实施审查快照](../review-implementation.md) 和 [逐组答复](../review-answers.md)。快照中的43文件未提交状态是提交前证据，不代表本版本未保存。

## 验证与复现

[脱敏验证记录](../validation-20261022.json) 保存源码基线、分片数量、跳过原因和依赖版本；[输入摘要](../validation-20261022-inputs.json) 覆盖本批已验证的非文档产品输入，不包括本文件及验证记录自身。旧本机报告路径只作补充，不是复现依赖。

在新 LitIsLand checkout、配置好的 Python 3.12 环境中执行；证据目录必须是作品和源码之外的新目录：

```sh
git checkout lg-r5-bounded-20261022
python -B scripts/verify_review_delivery.py --suite smoke --jobs 2 --output /path/to/new-review-evidence
python -B scripts/verify_review_delivery.py --suite full --jobs 4 --output /path/to/new-full-evidence
```

脚本不安装依赖，不继承凭据，不调用用户配置的供应商。测试全部使用临时合成作品；主测试进程只允许 localhost 模型连接，真实子 worker 使用合成供应商配置。Linux 小内存环境使用 `--jobs 1`。没有固定 Core 产物的集成会跳过，不能把跳过说成通过；需要 Core 验收时另按 [构建说明](../codex-core-updates.md) 准备并验证真实产物，不用任意开发 Codex 替代。

Extension 使用锁定 Node 20.18.1／package-lock.json，依次运行 `npm ci --ignore-scripts --no-audit --no-fund`、`npm run typecheck`、`npm test`、`npm run build`。实际连接专项为 `LG_TEST_PYTHON=/path/to/python node --import tsx --test tests/literary-task-backend.integration.ts`，在 `lg-vscode` 目录执行。打包前必须先构建；本批不把构建通过等同于用户桌面已更新。

## 完成边界与回退

完整 Python 1070项中1027执行通过、43因固定Core／独立app_server产物未配置跳过；实际项目重点回归108项、Extension61项＋1项真实连接、类型／构建通过。专项与完整分片不相加。新可复现脚本的运行记录单列于验证JSON。

无付费模型、人工文学质量或长期连续创作结论。严格认知仅限新 Native Responses 请求；普通对话历史未隔离。收据仅证明领域数据库效果；全局token／费用预算、跨文件事务、独立Kernel／Runtime分发未完成。

回退先停止写作任务，核对差异后选择代码版本。保留正文版本、收据、导入恢复、任务、方向和认知记录；不以旧数据库覆盖新状态。不要使用 `git clean` 或自动删除作品。新批次必须从固定Git版本开展，并单独记录授权、验证及回退。

## 后续有限精简交付

开发分支 `codex/simplify-polish-20261022`，交付标签 `lg-polish-simplify-20261022`；仍为0.3.0源码交付，不表示桌面安装已更新。

- `1e3bd9b7b3`：CLI／worker共用私有stdin参数规则，保留两处边界。
- `d1e768cd1c`：R5 polish专用输入／输出、策略变化恢复检查，以及固定源码同模型对照工具。
- 本文所在的后续证据提交记录路径清单、实际回归与全部8次请求；父仓库固定最终完整产品提交。

新增整文件删除清单为空，Core树和三个前端生产构建文件保持不变。实际项目Python181项中178通过、3项固定Core条件跳过；Extension57项单元、1项真实Backend／Native连接、类型检查与构建通过；wheel141份包输入逐字节核对。详见 [验证](../validation-20261022-simplify.json)。

Air两题前后均产生候选并保持选区外文字，输入和费用下降；轻量P1有明确文风约束反例，作者质量与返修待评。Vision前后均0／2，仍未验收受控R5；本轮8次目录价估算0.039986元，保守预留1.176516元，现金／资源抵扣未知。见 [实验](../evaluations/zhipu-first-round-20261022.md) 与 [逐请求JSON](../evaluations/polish-simplification-20261022.json)。

回退先审查现有差异，按需要依次 revert `d1e768cd1c`、`1e3bd9b7b3`。两项可独立选择；保留正文、数据库、已采用版本、审批、收据与本机凭据。旧polish候选在新版本仍可读；策略资源变化会停止继续，已经采用的状态仍显示adopted，不自动重跑旧请求。
