# 接手审查逐组答复与任务追踪

本文件保留提交前的审查快照；当前 Git 交付与可复现命令见 [2026-10-22版本记录](releases/2026-10-22-bounded-tasks.md)。其中43文件未提交等状态不作为提交后的实时状态。

日期：2026-10-22。与 [实施结论、架构图及验证表](review-implementation.md) 配套阅读。本轮覆盖作者消息中可见的十二组问题和六个案例；原 sandbox 附件的 82 条逐题原文未取得，因此不虚构与其编号一一对应的完成声明。修复前完整审查及反例保存在本机证据目录，不能把其中修复前的状态当作当前状态。

证据层级分别写作：**源码**、**离线实跑**、**真实执行器＋本地合成 HTTP 模型**。本轮没有付费模型、人工文学盲评或长期连续创作证据。所有作品均为临时合成作品。文件链接是产品仓库中的实际位置；精确函数行号、执行日志、源码摘要和实际项目复验见交付证据索引。

## 1. 代码、安装与实际运行

父仓库基线 `a9a84393d99041ddd95979d78dd00f893607dfcc`，产品基线 `6b22bc47c87aecac99593ca61a1341f1c7e0c720`，Codex 固定 `6b9826e3aa83b1a5947db50f4332cb9c65f1b340`。本轮新增改动未提交，不存在一个“包含全部本轮改动的新 commit”；须同时使用交付文件摘要。父仓库 gitlink 仍固定原产品提交，Git 克隆者在提交／更新 gitlink 前不会得到本轮修改。

`lg` 的 console script 指向 [main.main](../lg-cli/lg_cli/main.py)；本机已安装环境的解释器为 `/Users/zh/.local/share/python-envs/LitIsLand/bin/python`。执行时是否加载正确源码，不能只看工作目录或版本号。新增 [runtime_identity.runtime_identity](../lg-cli/lg_cli/runtime_identity.py) 返回 `sys.executable`、实际包路径、包内 Python／JSON／TXT 文件摘要、包／distribution 版本和 Runtime 选择。本轮 wheel 另外解包导入验证，排除了 editable 源码替代该包的假阳性。

Extension 由 [DesktopHost](../lg-vscode/src/host/DesktopHost.ts) 解析本机 Python／manifest／作品路径，经 [LiteraryBackend.launch](../lg-vscode/src/backend/LiteraryBackend.ts) 启动 `python -m lg_cli backend`。源码和验证产物已核对，不代表用户所有窗口的旧 Extension 进程已重载或旧 VSIX 已替换；未枚举或读取用户其他进程配置。新增身份查询能辅助发现包路径不一致，但磁盘摘要不能证明常驻进程的内存字节码。

配置加载在 [config.load_config](../lg-cli/lg_cli/config.py)，实际优先级以该函数及 CLI 参数为准；不复制或公开凭据。Codex 选择仍经 [runtime_manifest](../lg-cli/lg_cli/runtime_manifest.py) 和 [core_adapter](../lg-cli/lg_cli/core_adapter.py)，没有恢复 PATH／Node／Cargo 自动回退。`--verify-manifest` 是文件 hash／pin 校验，不会执行二进制、登录或请求模型，也不是可复现构建证明。

2026-10-18 的证据记录在父仓库 `docs/validation-20261018.json`，属于当时源码／Linux Runtime。当前候选基线、同步恢复、receipt、任务／推演、UI 和 Mac 所有权等均已变化，不能沿用旧结果证明本轮。当前四分片是独立的新运行；43 项缺固定 Core 产物的条件跳过明确保留。

## 2. Backend 统一到了哪里

交互 CLI： [TerminalBackendClient](../lg-cli/lg_cli/terminal_backend.py) → [Backend.run](../lg-cli/lg_cli/backend.py) → [intent_router](../lg-cli/lg_cli/intent_router.py)／[task_scope](../lg-cli/lg_cli/task_scope.py) → [runtime_factory](../lg-cli/lg_cli/agent_runtime.py)。Extension 的生产工厂 [createBackend](../lg-vscode/src/backend/createBackend.ts) 同样选 `LiteraryBackend`；两种正式配置不再意味着两套领域 Backend。

既有文学命令： [cli_backend](../lg-cli/lg_cli/cli_backend.py) → `Backend.workflow` → owned worker → [WorkflowRunner](../lg-cli/lg_cli/workflow_runner.py)／[story_cli](../lg-cli/lg_cli/story_cli.py)。Studio 经 [backend_studio](../lg-cli/lg_cli/backend_studio.py)，分析经 [backend_analysis](../lg-cli/lg_cli/backend_analysis.py)。它们共享会话事件、进程生命周期和执行器选择，但上下文准备、阶段恢复与领域服务并非同一个实现。共享启动不能替代逐路径核验。

新增文学任务／推演： [task_cli](../lg-cli/lg_cli/task_cli.py) 或 Extension `authorTask` → [Backend.dispatch](../lg-cli/lg_cli/backend.py) → [backend_tasks](../lg-cli/lg_cli/backend_tasks.py)。宿主决定有限策略和精确参数，模型只能返回候选结构；不能把计划作为执行授权。正式选择／采用仍回到既有 Studio／creative 流程。

明确旁路：`lg code` 经 `main._run_task` 和 Codex stage，CLI 与 adapter 都有作品目录门禁；它不经 Backend。`lg native` 是 [native.launch_native](../lg-cli/lg_cli/native.py) 的 Rust TUI，独立 [app_server.main](../lg-cli/lg_cli/app_server.py) 保留 Core stdio。兼容入口注入文学 MCP 和工具开关／elicitation；使用文学工具时仍有 MCP／领域版本和来源检查。但没有主 Backend 的完整任务／客户端协议和同等 effective-config 回读保证。可信本地作者编辑和直接 Python／SQLite 操作也是另一信任边界。

## 3. Native 执行路径、故障与剩余耦合

[NativeRuntime](../lg-cli/lg_cli/native_runtime.py) 自己拥有模型—工具循环：启动时校验 API 配置与 checkpoint；新线程接宿主历史，恢复线程读保存 messages；构造 instructions、messages、允许的工具 schema；[NativeModel](../lg-cli/lg_cli/native_model.py) 序列化为 Responses／Anthropic 并读 SSE；宿主校验完成状态、工具名、唯一 call ID 和 JSON 参数；保存 pending 后用 [NativeTools](../lg-cli/lg_cli/native_tools.py) 调用真实 MCP；保存结果后回填下一轮；最后保存响应和检查点。每轮最多 16 个工具调用，整轮最多 16 次模型迭代，另有配置超时／输出上限。

未知 pending 不自动重放；非法工具／参数不执行；模型／传输失败清洗公开错误；检查点保存失败阻止继续，不猜测结果。工具子进程异常的及时传播仍有部分依赖外层超时。本轮 Native stage 和 Backend 本地 HTTP 旅程无需启动 Codex；持续循环还由既有真实 MCP＋合成 generate 测试覆盖，两者证据不可混称。

源码依赖仍在：[execution_adapter](../lg-cli/lg_cli/execution_adapter.py) 导入 `CoreExecutionResult`／`core_adapter`，`AgentRuntime` 也沿用若干 Core 类型。执行路径独立成立，独立发布包、全类型解耦与认证／配置完全去 Codex 化尚未完成。Native 不需要 Rust 二进制来发这些请求，但仍需要 LG Python 包及其依赖。

| 能力 | Codex | Native | 当前限制 |
| --- | --- | --- | --- |
| 流式／工具 | Core 事件→LG；MCP／host tool | 自有 SSE／事件；同一 MCP | 各供应商错误和事件不保证完全等价 |
| steer | 真实 turn 绑定 | 模型返回后合并，工具阶段拒绝 | 不承诺任意时刻同等追加指导 |
| cancel | interrupt 与子执行收敛 | asyncio／MCP；同步新任务 worker 等待实际收敛 | 取消不等于回滚或从未发送请求 |
| resume | Core thread＋LG 绑定 | 自有 checkpoint | 未知副作用供查询，不自动重做 |
| 协作 | 保留 Core 通用机制；LG 有界审读 | LG 只读专家 | 不承诺通用子 Agent 对等 |
| 预算 | 阶段超时／输出、专家局部预算 | 同类预算＋轮次上限 | 无统一父子总 token／费用预算 |
| 作者权力 | MCP／Backend／领域门禁 | 同一产品规则 | 不外推到所有兼容 launcher |

R5 最小两任务不依赖补齐 Native 通用委派。新阶段禁工具，采用复用已有审读；本轮 Native 真实 HTTP 完整旅程通过。当前 Mac 的固定 Codex Runtime 旅程仍为待补证，不能据适配器单测写成已实跑。

## 4. Literary Kernel 的状态与写边界

Kernel 是 LG 拥有的领域规则集合，主要在 [ProjectStore](../lg-cli/lg_cli/project_store.py)、[creative_decisions](../lg-cli/lg_cli/creative_decisions.py)、[narrative_knowledge](../lg-cli/lg_cli/narrative_knowledge.py)、[managed_documents](../lg-cli/lg_cli/managed_documents.py) 及来源／审读模块；不是已经独立发布的同名包。`story.sqlite3` 是正文与领域状态权威。

| 对象／操作 | 当前强制规则 | 覆盖例外 |
| --- | --- | --- |
| 正文候选 | 默认 candidate、不激活；Story 本轮记录实际 base | 旧无 base 元数据不能伪装为已有保护；直接可信 Store 调用须看调用者 |
| 正式采用 | 事务内 head、已知 base、状态、配置要求的审读、required_decision | 监督是否强制取决于作品策略；receipt 不替代监督 |
| 作者保存 | `edit_active_document` CAS；整章编辑核版本／文档集合 | 作者明确直接编辑可保留正式状态，是设计例外 |
| 恢复／导入 | 恢复新建 candidate；导入校验制品身份；保存已知来源／方向约束 | 不直接把历史稿设成正式正文 |
| 事实／提案 | 正式提升检查来源；保留旧确认、显式 unbound | 作者 bible 管理入口属于可信管理路径 |
| 创作方向 | options、selection、withdraw 分开；绑定精确 head／方案／来源 | 生成／比较三个方案不授权选择 |
| 认知 | proposal 与 confirmed 指针分开；holder／坐标／来源／不交叠区间 | beliefs 可为误信，不提升为世界真相 |
| 审读／来源 | 绑定精确候选、snapshot、契约；来源变化读时标 stale | 不等于全语义依赖或模型实际使用证明 |

受管理方向／认知禁止普通编辑、metadata、恢复、采用绕过专用转换。普通 Store 的可信作者／内部 API 不自行包含所有交互授权，不能宣称阻止任意本地 SQLite 修改。主写作 Runtime 的通用 Shell／patch 不暴露；本机作者可直接改文件不是模型可用的权限。

章节文件、索引、分析投影、RunStore 和 SQLite 并非全局事务。采用后导出失败仍可报告 DB 成功并 warning；本轮修补导入后索引／基线失败为 `committed-needs-sync`，恢复只修投影，不再次采用。断电、损坏磁盘及所有跨文件写入仍没有全局 exactly-once 证明。

## 5. 作者授权、批准失效与提交失联

[mcp_approval](../lg-cli/lg_cli/mcp_approval.py)／[session_runtime](../lg-cli/lg_cli/session_runtime.py) 分开 operation 与 creative。ask、auto_review、full_access 是作者显式策略，不能提升 task scope、取消版本／来源检查或把试写变成正式方向。auto_review 可能需要模型；本轮只用合成模型，不启动用户付费供应商。

批准绑定真实目标、版本、参数、选区、task 和书指纹；执行前后复核。grant 消耗不等于提交已成功。模型换工具／参数仍受 schema、工具集合、宿主 scope 和 Store 转换限制。有界审读无工具、不能继续委派；通用委派不能被当作默认创作授权。

主 Codex 写作通过 [native.writing_settings](../lg-cli/lg_cli/native.py) 与 [codex_policy](../lg-cli/lg_cli/codex_policy.py) 关闭 Shell、unified_exec、apply_patch、外部 MCP／hooks 等，并回读 effective config；Core 文件系统保持只读，文学 MCP 是明确授权写通道。Native 的允许工具表没有 Shell／patch。故保留通用机制不等于给当前写作任务暴露通用写入。兼容入口没有同等级回读证据，不能扩大保证。

[operation_receipts](../lg-cli/lg_cli/operation_receipts.py) 在批准前公告操作 ID，批准与新鲜性检查后登记 attempt；受支持正式转换的效果证据在领域 DB 同一事务写入。支持七类 Studio 正式工具及 direct promoteFact／approveScene，不是所有 RPC。`lookup` 先读 attempt 再读 DB 证据，避免并发查询误报未提交。

| 情形 | 可观察结果／处理 |
| --- | --- |
| 事务已提交，响应丢失／工具后置失败 | committed；另列 toolOutcome，derivedFiles=not_attested |
| 正常退出／异常回滚且无领域提交 | not_committed；不授权重放旧 ID |
| 进程被杀、pending、未知 ID且没有提交证据 | unknown；查询／核对，不猜测未提交 |
| 同 ID 再执行或绑定参数改变 | 拒绝，不进行第二次正式转换 |
| cancel 与 commit 并发 | commit 已发生仍报告其证据；取消不倒转领域状态 |

真实 MCP 丢交付、并发取消、子进程退出前后与回滚测试均有实跑。数据库收据不是文件同步认证，也不表示任意工具多事务原子。裸测试服务器省略审批 handler 的直接执行不能证明正式授权链；本轮集成另保留真实审批路径。

## 6. 完整模型输入与认知过滤

| 输入来源 | 普通对话／既有流程 | 新 `narrative/run` |
| --- | --- | --- |
| 系统／项目指令、作品概况 | runtime_context／stage_input 注入 | 只用固定用途 instructions |
| 用户文本、引用、priorDiscussion | 进入 Backend／意图和 Runtime | 禁止自由 question／context，只收结构化字段 |
| 历史／压缩／checkpoint | 各 Runtime 保留；Core 增补本轮未知 | 全新 stage，无历史、无 checkpoint 恢复 |
| 全局事实、规划、参考、memory | author_global／author_scene_support等原范围 | 不注入 |
| 工具结果／缓存／子任务 | 各工具和策略分别约束 | tools=[]，不委派、不装缓存材料 |
| 人物／读者认知 | knowledge_view 仅过滤登记记录 | 只取 visible confirmed 的 content／stance／record_id |
| 来源正文、标题、topic／rationale | 可能经作者读路径出现 | 都不进入请求 |

[narrative_view](../lg-cli/lg_cli/narrative_view.py) 返回 `registered_records_only`、`model_input_isolation=false`：它没有把全部聊天输入变成受限视图。普通自然语言“早期人物推演”仍可能走 discussion，并携带已谈未来秘密。该旧行为没有被默默改变。

[narrative_execution](../lg-cli/lg_cli/narrative_execution.py) 显式限定 Native Responses、character_next_action／reader_expectation、作者给定的 story／reading 整数坐标。空／截断／失效来源／非法登记／超预算均拒绝；HTTP 请求同时检验必要早期线索存在、未来秘密／标题／历史／指令缺席。前后投影指纹改变或取消就不发布结果；请求已发送与结果已发布分别表示，失败传输状态可能为 null。

这是“人物推演输入不知秘密”的有限保证。作者侧写作可以知道未来规划但不提前揭示，需要另一类阅读位置／输出审读；本轮没有把它实现成同一过滤开关，也未证明模型先验或文学推断正确。产品结果明确标 provisional、partial 及 limitations；不支持的 Runtime 显示 unsupported，不静默回退。

## 7. 来源、旧章修订与恢复

[generation_sources](../lg-cli/lg_cli/generation_sources.py) 记录宿主实际准备交付的来源身份、版本、投影／内容摘要与输出摘要。Story stage 保存最终 prompt／instructions／schema；Live 读取观察由当前 lease 绑定，旧观察不借用。它比来源名称更精确，但历史和未识别输入仍为 partial／unknown，不证明模型注意力。

[source_review](../lg-cli/lg_cli/source_review.py) 重建确切候选的已捕获投影；[source_impact](../lg-cli/lg_cli/source_impact.py) 在同一 SQLite 读取快照查声明来源、捕获来源、编辑基底的已知直接关联，有限扫描／返回预算。没有登记的语义影响是未知，查询未发现不等于不存在。

`sources/check` 接入 CLI／Extension，输出 coverage、automaticChanges=false、候选 review 动作和重新提案建议。已确认事实／方向／认知的来源过期时保留作者确认并标待复核，不自动撤销，也不把旧来源当成完全新鲜。事实与认知重新提案／替代仍由现有专属转换和创作批准处理。只有已知候选进入自动准备的 reviewCandidates 列表，不自动审读或采用。

旧 [WorkflowRunner.resume](../lg-cli/lg_cli/workflow_runner.py) 不只看阶段 ID，还校验实际 prompt、instructions、schema、book／memory／模型／Runtime／指导及输出摘要。失配的文学阶段及后继重算，code 失配不重放。新有限任务另外绑定 policy、base、精确方向、Runtime 及预算；executing 的不确定结果只供检查，不能“恢复”成第二次写入。

## 8. R5 两条最小任务

策略由明确的结构化 `strategy` 和宿主校验决定，不由模型计划授予权限。`polish` 需要 active 章内唯一的准确 passage，连重叠多匹配也拒绝；调用一次既有 Story revision；模型只输出改后片段，宿主拼接原前后文。加载片段、最多各 600 邻接字符及既有有界 Story 支持资料；不是纯“只有选区”的输入。最终完整 prompt＋instructions 再算预算，短选区不因整章长而误拒绝。文本范围外不变可确定性验证，片段内事实语义不变仍需审读／作者判断。

`chapter` 在 existing chapter 容器工作，允许空容器。无分歧直接一次正文 stage；有分歧先一次 stage 产生严格三选项，保存 direction options 后停在 waiting_author；作者通过独立选择，advance 复核后才生成一次正文 candidate。它不自动选择、不自动审读、不自动正式采用，不创建全书规划。资料不足由模型报告风险或宿主缺记录停止，不能宣称有完善的语义资料充分性判定。

任务保存 `literary-task.json`、阶段输入／输出和必要制品；简单润色不额外强制方案比较、五专家或全书 workflow。章节策略保存方案、精确选择依据、候选与生成来源。有限任务的 1／2 请求预算仅属于生成阶段，之后独立批准的审读另计；字符预算不是 token 或总费用。无文本进展、预算不足、版本变化、方向过期、配置变化停止；异常 executing 不自动重试。

实际入口是 `lg task` 和 Extension 任务面板，共用 [backend_protocol](../lg-cli/lg_cli/backend_protocol.py)／[协议 Schema](backend-request-schema.json)。本地合成 HTTP 覆盖方向等待、选择、重启、候选、五项真实 Native 审读 worker 和创作批准采用。Codex 当前机器产物旅程待补证；任意自适应策略、全书循环和文学质量不属于已完成范围。

## 9. R1—R4 与删除权限

| 批次 | 已存在／本轮进展 | 当前不能据此声称的结果 |
| --- | --- | --- |
| R1 | 固定 pin、API-only、manifest、禁止隐式发现；新增只读身份核验 | Rust OAuth 等源码已删除、签名溯源或可复现构建 |
| R2 | 会话／阶段／专家策略与 Runtime 接口已有；本轮补有限任务、receipt、Mac 恢复所有权 | Core 类型完全断开、通用委派／全预算／全部入口同等覆盖 |
| R3 | 有隔离构建脚本；历史原样 locked 失败，本地包版本修正后 Linux 开发构建成功 | 原样 pin 已通过；当前 Mac ARM64 构建／全部 release 分发已通过 |
| R4 | 历史产品 subscription_auth 已删除并验收；本轮无删除 | Rust 模块裁剪、体积下降或新增可直接删除范围 |

具体删除／保留／优化表及依赖见 [实施记录](review-implementation.md)。旧 `CodexBackend.ts` 不在生产 factory，但 [兼容测试](../lg-vscode/tests/literary-launch.test.ts)、[生命周期测试](../lg-vscode/tests/backend-lifecycle.test.ts)、history 与旧 integration／smoke 仍有引用；generated 类型被旧 adapter 导入。现在只能批准反向引用／测试迁移及 VSIX 内容补证，不能直接删目录。通用认证、MCP、隔离、恢复、可观测性不得按 coding 标签删除；已决定保留的 Rust TUI／app_server 不重列删除对象。

## 10. Oh Story 的实际融合

上游固定来源见 [oh-story-source.json](oh-story-source.json)：`2cf7be61b139f3852c82f981a7ff4eed2aaf5926`，32项文件 hash；本轮不重新下载／安装上游，不声称检查其最新状态。历史取舍见 [oh-story-integration.md](oh-story-integration.md)。

| 上游机制／启发 | LG 原有基础与当前采用 | 正式路径、验证与缺口 |
| --- | --- | --- |
| 意图分发、规划与正文分离 | 原命令／workflow → intent_router＋task_scope；本轮有限 strategy | Backend／CLI／Extension；宿主校验，有界两任务，不是任意规划执行 |
| 局部 revision | 原 Story 修订和版本库 → 精确 passage／宿主拼接／真实 base | edit 与 task；并发基线、重叠选区、HTTP 旅程；语义质量未验收 |
| tracking／状态事务 | 原 SQLite 权威库扩展版本／来源／receipt | Store／MCP／Studio；不移植第二权威库，不保证跨文件事务 |
| 重大方向作者裁决 | 原候选／批准 → options、selection、withdraw；本轮 waiting_author | task/select→Studio creative；重启后继续与采用实际跑通 |
| 人物／读者信息差 | LG 显式 holder／双轴登记；本轮新增固定用途的全新请求 | knowledge-view／narrative/run；HTTP 正反例，不宣称普通聊天隔离 |
| Hook 的触发职责 | 不安装 Hook，部分职责转宿主 scope、批准、schema、来源／review 门禁 | 是明确执行代码；session-end／压缩前后全部职责未覆盖，纯提示要求仍是提示 |
| 七类角色／Skill | 沿用 LG 原 writer、style、continuity、director 等定义 | DefinitionRegistry／原 workflow／有界审读；没有移植七个自主 Agent |
| author-memory | 沿用项目指令、配置、历史、旧 memory | 未证明独立、长期学习的作者偏好库 |

上述是机制适配，不是逐行来源归属证明。LG 很多基础早已存在；本轮 receipt、Mac owner、严格请求构造不能冒称上游已有同等实现。下一轮最多深化三项：有限策略的真实任务收益、方向分歧的作者停点、人物推演与作者写作的输入政策。LG 已有足够宿主基础，不需要继续以角色数量或安装 Hook 作为目标。

## 11. 工具、Skill、协作与 UI

工具 schema、身份／版本／来源／scope 和权限分层执行：[NativeRuntime](../lg-cli/lg_cli/native_runtime.py) 校验工具与调用，MCP wrapper 批准，Store 事务管状态。Native 使用真实 MCP，不绕过领域门禁直接执行任意 Python 函数。错误契约分为无效参数、越权／过期、未知执行及业务拒绝；不能把所有失败变成自动重试。

[definitions](../lg-cli/lg_cli/definitions.py) 加载产品／作品 Skill 与角色；[prompt_builder](../lg-cli/lg_cli/prompt_builder.py)、Story／Workflow 在实际阶段消费。agent.tools 的提示文字和 skill.tools 声明不是 Runtime 白名单。没有证据说明持续聊天自动装入全部 Skill；Extension 自身也不是另一个 Skill 执行器。新增文学能力优先接语义服务／Schema／宿主策略，再用 Skill 表达可复用指导。

[bounded_reviews](../lg-cli/lg_cli/bounded_reviews.py)／[review_policy](../lg-cli/lg_cli/review_policy.py) 生成冻结审读证据，专家无工具、无继续委派、受次数／字符／输出／deadline 约束；不继承父任务全部聊天或权限。但它可以使用为审读准备的作品证据，不能冒称人物视角隔离。Core 通用子 Agent 是另一机制，Native 通用委派尚未对齐，总预算不保证统一。

本轮在现有 [protocol.ts](../lg-vscode/shared/protocol.ts) 增加 boundedTask 消息与精确 schema，在 [LiteraryBackend](../lg-vscode/src/backend/LiteraryBackend.ts) 和 [WebviewController](../lg-vscode/src/host/WebviewController.ts) 接实际 Backend，在 [LiteraryTasksPanel](../lg-vscode/webview/LiteraryTasksPanel.tsx) 展示 waiting_author、候选、adopted／sourceStale、unsupported／partial 与查询／继续／review-adopt 动作。结果通过已有 structured item／journal 保存，不另造故事权威状态。普通审批沿用原 UI；有跨重启含义的新任务状态不能仅靠一条聊天提示。

Extension 工程证据是 61 项单元、1 项真实 Python Backend＋Native HTTP 连接、typecheck／build；没有宣称重新执行历史 14 项全部 Core 集成或人工 VSCode 桌面验收。

## 12. 验证能支持什么

源码存在只支持实现位置；离线真实 SQLite／故障注入支持特定确定性行为；真实进程＋Native 本地 HTTP 支持协议、审批、状态和恢复链；这三者都不能证明付费供应商或文学质量。本轮完整 Python 128 模块分四个互不重叠分片，1070 项中 1027 执行通过、43 因固定 Core／app_server 产物未配置跳过。初次测试环境错误、发现的 Mac owner／CLI 参数问题和最终绿色日志都保留，不能只取绿色而抹掉失败原因。

关键边界未被 mock 掉：候选／提交测试用真实 Store；丢返回／取消用真实 MCP／SQLite；认知断言在完整 HTTP body；最终章节旅程用真实 Backend、NativeStageExecutor、审读 worker 与合成 HTTP，仅 worker 配置加载注入本地测试供应商。Story 单测的 FakeAdapter、既有 generate 替换、构建脚本的 fake cargo 另有清楚范围，不冒称真实 Core。

重现命令、测试分组、跳过列表、包／前端构建、真实项目入口和 SHA256 在机器可读 `validation.json` 与证据索引；运行脚本去除用户凭据环境，主测试进程使用 localhost 网络守卫，实际子 worker 注入本地合成供应商配置，并使用合成用户目录。不要直接用正式作品或用户真实供应商配置重放这些旅程。

## 六条完整案例的过程与制品

1. **指定选区润色**：`task preview` 只读 chapter head／passage → `task/start` 显示精确预览并要 operation 批准 → `literary_tasks.start` 保存 run → `_BoundedAdapter` 核最终 prompt 字符上限 → `StoryWorkflowService.revise_document` 冻结输入／schema并发一次无工具请求 → 宿主拼接选区保存 candidate／base／generation sources、输出制品 → `awaiting_review`。正文 active 不变；另行 review／creative adoption 后 Store 事务才切 active。多匹配／超限／空输出／并发版本变化／无进展均拒绝或停；有候选后归档失败不等于候选从未写入。实际测试覆盖 host 拼接、真实 HTTP 与陈旧 base 采用拒绝；不保证片段内事实语义。

2. **有分歧的章节**：`task/start` 读取现有章容器和作者 request，固定 instructions＋严格三方案 schema，一次无工具 stage → `save_decision_options` 保存 draft 方向并记录 optionsVersion／head → `waiting_author`，不生成正文。`task/select` 先校任务新鲜性，再由 Studio 预览／creative 批准精确 option；可换会话后 `task/advance`，核 policy／模型／基础／选项／selection／来源和剩余一次请求 → chapter candidate，带 required_decision。`review=True` 采用请求另外 operation／creative 批准后跑五类审读，满足现有策略才 `accept_version`。方向改动／撤回／来源陈旧或基础变化拒绝；候选保留供核对，不自动重绑。合成旅程七次 HTTP 包含二次生成和五次审读，不是“两次生成预算涵盖审读”的声明。

3. **秘密讨论后早期推演**：原会话可以保留未来秘密；新增入口不复用这个会话上下文。`narrative/preview` 只读人物／阅读坐标与 registered view，得到精确 fingerprint；缺必要记录即 insufficient_context。作者 operation 批准后 `narrative/run` 再准备同一视图 → 新 Native stage，只发固定 instructions／用途、binding、visible statements，tools=[] → 再核视图／配置 → completed 时才释放 provisional 文本。没有正文、事实、方向写入；Backend 只保存公开结果事件、Native 记录原用量。取消／视图变动不发布；quiet HTTP 保持 worker 所有权到真实收敛，不声称瞬间切断。完整请求同时含早期线索并排除秘密，非用空输入通过。

4. **已采纳旧章修订**：可信作者版本化保存，或章文件 `import_chapter` 读一次完整快照 → 校 baseline 和 DB heads → 保存精确 import intent → `edit_chapter_documents` 一次领域事务 → refresh index／save baseline。后二者失败时返回 committed-needs-sync；重试精确核 DB 版本／内容／origin，只补投影，不再次保存正文；文件又变或DB又变则保留新内容／冲突停。后续 `sources/check` 在只读快照列已知直接依赖、来源状态与候选 review 动作；事实确认不自动撤销，作者重新提案／裁决。此链无模型调用，除非作者再批准审读／生成。故障注入覆盖索引、baseline、重试与文件读竞态；语义传递影响仍未知。

5. **批准后丢返回／同时取消**：Backend 在审批交付时先公告并保存 operationId → 按实际请求／预览／book fingerprint 批准 → MCP 或 direct creative 完成当前性核验 → operation_scope 记 pending → Store 在实际领域事务中记精确 DB 效果再 commit → 返回成功或传输丢失。查询 `operation/read` 根据事务证据回答 committed，toolOutcome 可为 failed／unknown；未有提交证据且 pending 为 unknown，正常回滚为 not_committed。取消只停止后续，不撤销已有 commit；同 ID 重试拒绝，客户端显示查询和核对。使用真实 MCP、响应交付屏障／cancel、进程退出和回滚测试；不是 mock 一个“已提交”返回。

6. **中断后 base 已变**：旧 workflow 恢复先读冻结 prompt／instructions／schema／memory／输出指纹，失配从相应文学阶段重算；code 不自动重放。新 task `read` 校 state hash 并只读派生 stale／interrupted；advance 在 resume lock 内再核 base／Runtime／policy／方向／预算。waiting_author 可在未变条件继续；executing 不确定、基础变动或旧批准失效则 inspect_run／new_task，不能让 checkpoint ID 自身授予写权。模型只在新批准且通过校验后调用，旧制品不改成新来源；测试覆盖模型／指导／schema／memory变化、base变更和任务恢复。尚无任意断电后的自动精确一次重做保证。

## 下一批可批准什么

当前 A—D 有限代码交付，不自动开启以下建议。

| 批次 | 问题、前置、复用与候选改动 | 入口影响、验收、停止与回退 |
| --- | --- | --- |
| N1 固定 Runtime 与分发补证 | 先取得本机可用的固定 Mac 产物及对应 Linux 身份；复用 runtime_manifest、build_codex_runtime、43项条件集成和 wheel／Extension构建。先补证，不先改 Rust | 核验实际 CLI／Extension 产物、只读配置、MCP、取消／恢复；原样 locked 构建失败就如实停在失败，不能顺势改依赖。无代码回退；保留现有 Runtime 配置，不切换凭据。**当前只建议批准补证，不能批准删除** |
| N2 两任务的逐 Runtime 契约验收 | 依赖 N1产物；复用两任务、required_decision、receipt、review策略。只针对复现的 Codex／Native状态或审批差异补适配，不补全部通用委派 | 在主 CLI／Extension 跑同一润色与分歧章夹具、陈旧base／选择／取消反例；不能兑现契约就显式不支持，不放宽权限。回退停用新任务入口，保留候选／选择／运行记录。**证据指向具体失败后才批准小修** |
| N3 直接来源复核的作者闭环 | 依赖已有 source_impact／review与创作批准；先限定一个旧章、一个候选和一个确认记录，补需要的重新提案／确认交互，不做传递语义闭包 | CLI／Extension准确显示待复核和替代关系；拒绝过期批准，不自动撤销确认／批量重写；关联未知明确保留。回退入口代码，保留所有领域历史。**可批准具体最小设计与反例，不能批准全书自动传播** |
| N4 文学收益实验 | 工程边界稳定后，作者确定模型、样本、费用与人工评审；复用合成任务结构记录预算／介入，不新增故事权威库 | 同模型、同预算、盲评与有限连续写作；质量无收益就停止扩大机制，不以工程测试数代替。配置和作品分开保存，无需改变正式数据。**当前仅研究设计，不启动付费调用或长期创作** |

当前没有一个新增 Core 或兼容目录达到“调用退出、断依赖、固定构建、分发验证、具体删除授权”全部条件。可以继续并行准备裁剪证据；不能把本轮目标完成解释成批准下一批删文件。
