# 新 ReferenceLibrary 资料契约

本批仅规范与共享 Library 对应的五类新分析资料。不迁移、补写或重排旧文件。
大纲、草稿、研究简报等其他类别保持现状，后续根据实际使用逐步收敛。

## 标准来源与边界

- 分类视角与 focus_fields：`Library/AbstractLibrary/library_index.json`。
- 类别实例结构：`Jormungandr/abstractmodel/instance_cards.py` 的 `INSTANCE_CARD_PROMPT_SCHEMAS`。
- 产品内可独立安装的固定副本：`lg_cli/resources/reference-contracts.json`。

对应的是 Library 的 `instance_card` 内容格式，不是把外部资料的整条实例身份复制进本书。
本书没有外部 pattern_id、bridge_index_path 等来源时不能伪造这些字段。
现有分析外壳继续保存 project_id、chapter、run_id、scope、章节版本证据与 fingerprint；
新记录增加 `contract: "lg.reference-card.v1"`，每个 entry 必须包含 instance_card。

| 类别 | 专用字段 |
| --- | --- |
| EventsLibrary | scene_context、causal_chain、turning_point、after_state |
| PayoffAngst | emotional_setup |
| CharacterArc | arc_path |
| EmotionRhythm | emotion_beats |
| Worldview | rule_application |

共同字段：portable_core、implementation_details、source_locked_details、fusion_hooks。
嵌套字段名与 Library 保持一致；列表、字符串、数字范围和枚举由类别 Schema 约束。
缺少原文依据时用空字段，不为提高完整度编造内容。

## methods 与审查

1. `reference_contracts.methods(category)` 在每个专门 Agent 调用前说明视角、排除项和证据边界。
2. `response_schema(category)` 把该类精确结构交给模型；不再允许只写一段通用 analysis 代替类别内容。
3. 程序检查 Schema、稳定 key 唯一性和 source span；原文引文与 document/version 由程序绑定。
4. 世界观应用实例的 chunk_id 使用本书已有 span ID，必须属于该条证据。
5. 程序补上 Library 同名的版本、类型、grounding 和完整度字段，再由保存入口复检。
6. 无效结果保存在运行诊断目录，最多修复一次；修复失败不发布为正式分析。

完整度仅表示结构字段的填充比例，不等于语义真实性评分。语义审查仍依赖专门 Agent、
可追溯引文和现有创作监督流程；分析不会自动变成 Canonical 设定。

旧 `lg.book-analysis.v1` 且没有新 contract 标识的文件保持原读取规则，不自动补卡片。
新文件即使在后续读取时也按新契约验证。CLI 分析与后端 workflow/start 使用同一个写入服务。
后端 reference/validate 只检查格式，结果明确返回 evidenceVerified:false。

后续更新 Library 格式时，应先更新固定契约、增加兼容测试，再决定是否显式迁移旧文件；
不在程序启动时扫描或改写用户资料库。
