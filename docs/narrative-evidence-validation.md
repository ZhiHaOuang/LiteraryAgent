# 有限认知输出的证据校验

本批只深化现有 `narrative/preview` 与 `narrative/run`。仍仅支持全新 Native Responses 请求，固定人物行动／读者预期用途；不扩展普通会话、任意问题、历史隔离或全书依赖分析，不改变正文、事实或作者确认。

## 当前结果契约

`lg-cli/lg_cli/narrative_execution.py` 将结果版本提升为 `lg.narrative-execution.v2`。模型输出使用随 Python 包分发的 `resources/narrative-evidence.schema.json`：

```json
{
  "inference": "基于有限信息的推断。",
  "evidence": [{"record_id": 7, "quote": "该记录内容中的精确原文"}],
  "uncertainty": "当前缺少什么，结论有何限制。"
}
```

Schema 限定字段、长度和证据条数。宿主再核对：

- 推断、不确定性和引文去除空白后非空。
- 记录 ID 是整数，并属于本次实际发送的可见记录；未来记录、其他观察者记录和不存在的 ID 均无效。
- 引文是该记录 `content` 的精确子串，不做大小写、空白或 Unicode 归一化来扩大匹配。
- 生成后来源／模型身份／策略指纹仍与预览一致；输出 Schema 和证据契约版本也进入预览指纹。

成功才释放 `analysis` 与兼容显示字段 `text`，并标记：

```json
{"resultReleased":true,"evidenceValidation":{"contract":"visible_record_exact_quote.v1","status":"validated"}}
```

无效引用返回 `failed / invalid_evidence`，无 `text` 或 `analysis`；结构错误在阶段执行器被拒绝，返回 `model_execution_failed`。失败不自动重试，不把流式原文发布到 Backend 会话或 UI。取消和来源变化继续丢弃结果；quiet HTTP 仍等待既有 worker 收敛，不宣称网络已瞬间停止。

UI 分开显示推断、登记证据与不确定性。已有 v1／未标版本文本仍可读，但明确注明“旧版输出：未校验证据引用”。新版本结构不符时不回退展示原始 `text`。

**校验只证明 ID 与引文对应本次输入，不证明引文支持推断、模型没有先验知识、输出没有事实错误或文学质量合格。** 推断和不确定性仍是作者需要判断的模型文本。输入预算仍按宿主 prompt+instructions 字符数计，不是 token、费用或包含输出 Schema 的整个 HTTP 请求大小。

## 验证

本批在专属 LitIsLand 隔离副本运行，仅使用合成作品、临时 SQLite 和 localhost 合成 Responses 服务；未调用外部模型、读取真实凭据或正式作品。

| 证据 | 本轮结果 | 覆盖与边界 |
| --- | --- | --- |
| `cognition-evidence-red.log` | 1 项预期失败 | 真实 HTTP 返回不存在的记录 `999999`，旧实现错误地 `completed/resultReleased=true` |
| `cognition-evidence-green.log` | 22 项通过 | `tests.test_narrative_execution` 与 `tests.test_review_delivery`；有效引用、未来／他人／未知记录、伪造引文、空／非法结构、Schema指纹、取消与来源变化；保留原必要线索召回／禁止秘密载荷断言、正式 Backend 结果不发布与 R5 审读／采用测试 |
| `cognition-evidence-ui-tests.log` | 8 项通过，无跳过 | 文学任务相关 UI／连接器测试；已校验分区、旧版提示、拒绝结果不显示文本 |
| `cognition-evidence-real-ui.log` | 1 项通过，无跳过 | 保留真实 Extension 连接器→Python Backend→Native HTTP 的选区润色旅程 |
| `cognition-evidence-typecheck.log`、`cognition-evidence-build.log` | 退出码 0 | 当前前端类型检查与构建；不是人工桌面体验或文学质量验收 |

脱敏记录见 [本批验证JSON](validation-20261022-cognition.json)；本机原始日志在本轮隔离目录上层 `/private/tmp/LitIsLand-next-batches-20261022-j3_tm0ol/`。不把上述专项相加为全项目唯一测试总数。随后运行128模块全量：1076项中1033执行通过、43因固定Core／独立app_server产物缺失跳过、零失败；前端完整63项通过。

## 回退

优先停用有限推演入口，保留历史会话、结果和作品状态。若回退代码，应将认知模块、Schema 与展示规则作为同一批处理；不能把 v2 结果重新标成未经验证的自由文本成功，也不能将旧 v1 结果标为已校验。回退不删除用户数据、不撤销作者认知确认、不覆盖数据库。旧前端不具备新结构的完整展示能力，更新后的结果应在配套前端查看。
