# 润色无进展澄清与动作／台词约束

本批从产品 `bc9756b06738714de25e317b21e720c4f9ae4398` 继续，在 `codex/polish-compliance-20261022` 开发。根据作者实际选择，保留轻量润色，并把无进展后的下一步改为明确澄清；不合并主线，不把旧实验或工程通过当作v3文学质量验收。

## 发生了什么变化

| 情况 | 之前 | 现在 |
| --- | --- | --- |
| 模型原样返回选区 | 停止，只提示没有文字修改 | 仍然停止；当前有效任务附具体澄清问题和新任务草稿 |
| 作者补充希望怎样改 | 手动重新组织原要求、选区及预算 | CLI／Extension保留原要求、准确选区和输入上限，再追加作者补充 |
| 再次生成 | 只能重新开始一个任务 | 仍是独立新任务，重新预览、批准和版本绑定；旧任务不重放、不重置计数 |
| 动作和台词约束 | 事实、事件顺序和说话者的一般约束 | 进一步明确动作是否发生、结果状态、说话者的条件／承诺不转为旁白或未说出口的想法 |

没有新增自动审读、自动重试、模型切换、语义黑名单或通用工具裁剪；没有文件删除。现有四字段Schema、候选／采用分离、来源和版本保护继续使用。

## 实际入口

Extension的文学任务结果卡在当前有效的无进展任务上显示“具体希望改哪一点”。展开可核对原要求和准确选区，填写后点击“预览新任务并申请执行”，再独立批准。新任务批准后最多增加1次模型请求，结果仍需单独采用。低于默认值的原输入预算也保留；超过原字符预算会正常停止，澄清不提高预算。

CLI先读取任务，然后只生成新任务选项：

```bash
lg -C /absolute/path/to/book task show TASK_ID --json
lg -C /absolute/path/to/book task clarify TASK_ID --guidance '对话保持原样，只调整动作之间的停顿。' --json
```

将第二条命令输出另存为新的 `clarified-task.json`，再独立预览和执行：

```bash
lg -C /absolute/path/to/book task preview --input clarified-task.json --json
lg -C /absolute/path/to/book task start --input clarified-task.json --confirm --json
```

`clarify`只走共享Backend的`task/read`并构造选项，不创建运行或调用模型。空补充、合并后超过4000字符、润色多次调用或超范围预算会被拒绝。过期来源、已变化的Runtime／策略、未知执行结果以及非润色任务没有这个草稿入口。重新选区创建新任务仍使用原有流程。

## 调用链与约束

`literary_tasks._BoundedAdapter.run`发现返回选区不变后仍停止；`_offer_clarification`通过`_check`核对原基础版本、Runtime身份和策略指纹。`read`返回的澄清草稿是派生结果，不改旧任务文件。`advance`对停止任务仍只读返回。Python的`clarified_options`和Extension的`buildClarifiedPolishRequest`保留原选项，以相同分隔符追加补充；两端独立验证输入。请求改变会改变预览指纹，旧审批不能授权新任务。

Extension继续经`WebviewController → LiteraryBackend.authorTask → task/preview → task/start → backend_tasks`。CLI继续经`TerminalBackendClient`进入同一Backend。后端重新准备任务、获取独立批准，并在执行前核验指纹；审批期间改稿会阻断执行。没有增加旁路RPC或文学状态存储。

`polish-stage.txt`从1692增加到2028个英文字符，增加336字符／UTF-8字节，仍然单调用。Profile升级到`r5-polish.v3`以让旧检查点明确识别策略变化；不修改旧候选或重跑旧任务。token／费用变化未测量，不称为降本成果。

## 验证与完成边界

先在固定源码导出的LitIsLand隔离副本验证，再将核对过的最小差异应用到实际项目。测试包括真实Backend、Native执行器、SQLite和本地合成HTTP；Responses／Anthropic均核对最终请求，旧运行不重放、新批准前无请求、原稿不变、预算保持与审批期间改稿受保护。精确数量、命令和摘要见 [验证记录](validation-20261024-polish-clarification.json)。

扩大验证首次出现4个路径断言失败；同4项在未改基线的默认Mac临时目录也失败。探针确认`/var`解析为`/private/var`造成未解析根路径比较失败，按项目已有做法设置`TMPDIR=/private/tmp`后基线4项通过。原日志保留，没有因此修改产品或测试断言。

本批没有新的付费模型调用。合成响应只证明契约、权限和状态链路，不能证明动作／台词语义改善或作者总工作量下降。没有桌面实际点击验收，没有安装VSIX或部署服务器。源码和构建交付不等于已安装Extension更新；第二批文学效果与主线合并门槛仍保持。

回退可对本批提交执行常规Git revert并重新构建。旧运行记录、候选、作品数据库及作者批准不回滚或重放。v3任务在回退后的旧策略下应按现有策略变化门禁处理，不能静默续跑。
