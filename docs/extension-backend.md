# LiteraryGiant extension 后端 v1

本批交付后端、协议和测试。VS Code 界面留到下一批；不迁移旧资料，不自动生成书籍内容。

## 启动与边界

在安装了 LG 的 Python 环境启动：

```sh
python -m lg_cli.backend_stdio --workspace /absolute/path/to/book
```

可加 `--environment NAME`，与 CLI 使用相同模型配置。每个进程绑定一本已初始化的书；
stdout 只输出 JSONL 协议，stderr 用于启动错误。前端在 extension host 中管理该子进程，
Webview 通过 VS Code 的消息通道与 extension host 通信，不直接访问模型凭证。
本版本不开放 TCP 端口，也没有加入遥测或额外网络服务。后端依赖 POSIX 进程组、文件锁和 Unix socket；Windows 首版应在 WSL／Remote 环境运行后端。

客户端请求是 JSON-RPC 2.0 对象；逐行发送，必须有唯一的 `id`、`method` 和对象 `params`。
先 `initialize`，等待响应后再调用其他方法。响应回显 id；通知使用 `method: "event"`。
审批等待与运行在后台进行，读循环始终可接收取消和审批回答。

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","id":2,"method":"session/create","params":{}}
```

第一条响应返回 `protocolVersion: "lg.backend.v1"` 和能力列表。
第二条返回 `sessionId`、权限模式与已有 `thread_id`。后续用返回的 sessionId，不拼造路径。
`protocol/schema` 返回所有方法的参数 JSON Schema；代码来源是 `backend_protocol.py`。
同版离线副本在 `docs/backend-request-schema.json`。

## 方法

| 方法 | 参数 | 结果或用途 |
| --- | --- | --- |
| `session/list` | 无 | 会话 ID 与标题 |
| `session/create` | 无 | 创建并独占打开会话 |
| `session/open` | sessionId | 打开已有会话，取得写锁 |
| `session/read` | sessionId | CLI 共用的消息记录 |
| `session/rename` | sessionId, title | 重命名 |
| `session/close` | sessionId | 取消所属任务并释放会话 |
| `permissions/set` | sessionId, mode, creativeMode? | 空闲时修改两类权限 |
| `run/start` | sessionId, text, outputSchema? | 立即返回 runId；事件通知完成 |
| `run/steer` | sessionId, text | 给当前主 Agent 追加指令 |
| `run/cancel` | sessionId | 停止该会话当前执行树 |
| `workflow/start` | sessionId, chapter, category | 执行指定类别的章节分析 |
| `creative/apply` | sessionId, action, 对应参数 | 审批后执行创作采用 |
| `approval/resolve` | approvalId, result | 回答当前连接的待审批请求 |
| `events/read` | sessionId, after?, limit? | 按游标补读，默认 200、最多 1000 条 |
| `reference/contracts` | 无 | 五类生成契约及 methods |
| `reference/validate` | category, card | 仅验证格式，不声称证据成立 |
| `protocol/schema` | 无 | 方法参数契约 |

`creative/apply.action` 当前支持 `promoteFact`（factId）和 `acceptVersion`（document、version）。
版本采用沿用 ProjectStore 的监督检查，不绕过冲突、过期审查和候选状态限制；缺少必要审查会失败，
需先按已有 CLI 流程完成候选审查。首版不暴露任意 shell 执行接口。

每个 session 同时只运行一个客户端发起的任务；新请求应使用 steer 或先取消。
已经完成的工作流不会因重试接口而重复执行。CLI 与 extension 可先后接续同一会话，
同时打开写入会明确报占用错误。关闭当前客户端会话后，另一客户端才能接手。
直接非交互 shell 命令沿用原有 CLI 语义，不是本协议的客户端。

## 状态与事件

通知格式：

```json
{"jsonrpc":"2.0","method":"event","params":{"cursor":17,"sessionId":"...","method":"run/completed","params":{"runId":"...","status":"completed","result":{}}}}
```

主要事件为 `run/started`、`run/completed`、`engine/event`、`task/updated`、`workflow/output`、
`review/event`、`approval/requested`、`approval/resolved`、`approval/cancelled`。
`run/completed.status` 是 completed / failed / cancelled；客户端异常退出后发现未完成记录，
下一次独占打开会补记 interrupted。失败事件包含 error。

`engine/event` 保留当前固定 Codex 的原始 method/params；前端应容忍未知事件。
主 Agent、子 Agent 根据 threadId 区分，子 Agent 输出不会混入主对话的 assistant 记录。
产品协议方法由 LG 保持稳定，原始内核事件属于有版本依赖的透传部分。

事件落盘到 `.literarygiant/client-events.sqlite3`，使用单调游标；前端保存最后处理的游标，
重连后循环 `events/read` 补齐。通知与补读可能覆盖同一事件，按 cursor 去重。
资料、历史和事件可能含用户作品，存储权限为本地私有；不发送到外部日志服务。

`run/start.outputSchema` 传给 Codex `turn/start.outputSchema`，完成后 LG 再解析并校验；
失败不会作为合格结构化结果返回。只接受本地 `$ref`，避免 Schema 校验触发外部检索。
自然语言流仍可实时展示，但不得把尚未验证的片段当作最终结构化结果。

## 会话恢复与取消

`.literarygiant/conversations/<id>.jsonl` 继续保存 CLI 消息；同目录 `.runtime.json`
保存真实 Codex thread ID 和权限。新会话使用持久化 thread，重启使用 `thread/resume`。
旧会话第一次进入此路径时使用历史上下文创建 thread，不回写旧消息。
找不到已记录 thread 时明确失败，不静默创建一个失忆的新 thread。
认证配置切换到不同 CODEX_HOME 时也可能无法找到 thread，需返回原配置或明确新建会话。

取消先发送 `turn/interrupt`，随后清理该会话 app-server 进程组及工作流进程组，
覆盖所属子 Agent；正在等待的审批失效。已保存的章节、分析、版本和运行文件保留。
取消不是回滚，也不是精确地从任意执行指令处继续。下次对话恢复 thread；工作流恢复仍沿用其检查点能力。
强制终止发生在未完成写入中时，依赖既有原子文件写入与数据库事务保护。

## 权限与审批

操作权限 `mode`：

- `ask`：内核使用 on-request / user，LG 写入经客户端确认。
- `auto_review`：内核使用 on-request / auto_review，由固定 Codex 的自动审查机制处理内核请求。
  LG MCP 写入由产品层只读审查器检查具体参数与内容，返回允许或拒绝理由。
- `full_access`：内核 danger-full-access / never，LG 产品写入自动允许，并记录决定。

创作权限 `creativeMode` 独立设置，默认 ask：

- `ask`：展示具体候选内容或设定，客户端确认后执行。
- `auto_review`：单独的只读 Agent 检查确切预览，结构化返回 decision/reason；不开放写入和委派工具。
- `full_access`：自动允许创作采用，但仍执行版本、证据和监督校验。

CLI 使用 `/permissions` 设置同一份会话权限。审批期间目标变化会拒绝执行，要求重新审查。
人工和自动拒绝均不执行待批准操作。自动审查不可用、输出无效或被取消也不执行。

`approval/requested` 包含 approvalId、owner、kind、details。
普通审批回复 `{"decision":"accept"}` / decline / cancel，只对当前请求生效；
不支持隐式永久规则修改。用户问题的 result 为
`{"answers":{"question-id":{"answers":["回答"]}}}`。
过期、重复或未知 approvalId 均报错。断开后不复用待审批 ID。
未知内核请求不自动批准；MCP elicitation 当前拒绝，后续按具体表单扩展。

## Codex app-server 对接总结

固定内核代码：`core/codex/codex-rs/app-server-protocol/src/protocol/v2/`。
LG 在产品 Python 层封装，没有修改 vendored core。

1. 启动固定运行时 `app-server --listen stdio://`。
2. `initialize` 后发送 `initialized`；持续读取 JSONL。
3. `thread/start` / `thread/resume` 管理真实会话。
4. `turn/start` 发起轮次；`turn/steer` 携带 expectedTurnId 追加输入。
5. 处理 item/turn/thread 事件；审批是带 id 的服务端请求，必须回送同一 id。
6. `turn/interrupt` 请求中断，等待终态；LG 另负责其拥有的进程树和工作流生命周期。

未来 extension 连接 LG 后端，让它统一管理书籍、资料和创作审批；不再解析 TUI 输出。
上游可通过 `codex app-server generate-ts` 和 `generate-json-schema` 导出对应版本的类型。
升级时必须从固定运行时重新导出并跑协议与恢复测试，不能直接假设最新网页与本地版本一致。

官方资料：[Codex App Server](https://learn.chatgpt.com/docs/app-server)。
