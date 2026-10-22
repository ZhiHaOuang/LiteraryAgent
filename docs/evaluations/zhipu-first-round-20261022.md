# 智谱首轮同模型对照 — 2026-10-22

作者已批准人民币5元总预算，确认两个型号为 `glm-4.5-air`、`glm-4.6v`，自报赠送资源包剩余12,000,000与5,769,930 token。本轮只用这两个型号，不扩大费用或自动改用其他模型；资源包是否适用、扣除倍率和现金实际扣费需账单核对，不能由普通模型响应推出。

## 一次密钥输入与配置

从产品仓库根目录，使用已有LitIsLand Python执行：

```sh
PYTHONPATH=lg-cli:. python -B scripts/configure_zhipu.py --dialog
```

Mac弹出隐藏输入窗口；其他平台省略`--dialog`使用终端隐藏输入。只输入一次Key，复用已有LG凭据库保存两个provider／protocol／endpoint／model绑定的profile，权限0600、目录0700。默认激活`zhipu-air`，LG配置空间的Runtime设为Native；不改正式作品、机器VS Code设置、Core或服务器凭据。不自动覆盖已有同名profile或已有不同Runtime配置。

实际短请求发现：Air通过现有Anthropic兼容路径及结构化工具；4.6V在该路径返回正常用量，但输出没有通过JSON契约，因此显式改为智谱Responses接口。Responses真实响应返回`model=glm-4.6v`和正确JSON。此选择已经写入配置脚本，不是运行失败后的自动降级。

| profile | 模型 | 协议及基址 |
| --- | --- | --- |
| `zhipu-air`，默认 | `glm-4.5-air` | Anthropic，`https://open.bigmodel.cn/api/anthropic` |
| `zhipu-vision` | `glm-4.6v` | Responses，`https://open.bigmodel.cn/api/v1` |

可用 `literary auth use zhipu-vision` 或 `literary auth use zhipu-air` 显式切换。`auth status`仅检查配置，不证明端点可用。人物／读者有限推演原本仅支持Native Responses；Air配置不能因此宣称支持该入口。已有持续对话和R5阶段能力按现有协议边界使用。[智谱兼容接口](https://docs.bigmodel.cn/cn/guide/develop/claude/introduction)、[Responses基址](https://docs.bigmodel.cn/cn/guide/develop/responses/introduction)

## 实验口径与预算

每个型号独立比较4个合成任务，每题LG／简化提示各一次，共16次生成；两种型号的结果不混成同模型对照。P1对话润色、P2有限视角叙述润色、C1有代价的机械救援、C2尚未揭露的联络人身份。章节方向已由固定请求给定，`plot_disagreement=false`，不伪造作者选择来跳过分歧流程。

LG组直接运行 `literary_tasks.start → StoryWorkflowService／章节阶段 → NativeStageExecutor → NativeModel`。简化基线也经过同一Native阶段执行器与对应型号的协议；它是简单直接提示，不能冒充完整通用Coding Agent。润色基线返回完整章节、依提示保持选区外文本；LG返回结构化替换段并由宿主拼接。该差异、Schema开销与额外输出长度须一起报告，不把全部收益归因于文学提示词。

两组均不显式设置temperature，实际供应商默认值未知；同一型号内参数与协议相同。Air使用桥接器原有的禁用思考设置；4.6V使用Responses默认。没有为了横向比较两个模型而强行宣称其推理参数一致。每次最大输出4096 token，包括供应商计入该上限的推理开销；完整wire payload最多64,000字节。

预算器在每次发送前落盘意图，并按公开128K上下文、保守输入131,072 token及输出4096 token预留最高档目录价。Air预留约0.1901元／次，4.6V约0.2867元／次；16次最坏预留约3.815元，另对已发生3次探测保留0.74元余量，总计低于5元。实际用量回来后释放未使用预留；不拿字符当token，也不靠资源包免费假设通过预算门禁。

现金扣费仍以平台账单为准；本地门禁依赖供应商公开上下文、价格与输出上限的契约，不是银行侧硬封顶。用量缺失、传输不完整、返回型号不符或不足以预留下一次时停止，不自动重试。已存在的输出目录拒绝恢复／重跑，避免响应丢失后重复消费。所有已知失败与探测都保留，不伪装成零费用成功。

[官方目录价格](https://docs.bigmodel.cn/cn/guide/start/pricing)：本次Air低于32K输入时，输入0.8元／百万、输出达到0.2K档为6元／百万（更短输出为2元）；4.6V低于32K输入为1／3元。记录中的原价估算不计算缓存折扣和资源包抵扣，实际账单字段保持未知。

## 执行与记录

先在专属LitIsLand副本运行离线测试。实际执行明确带`--run`，默认只生成计划、零调用。使用新的仓库外目录，不能位于正式作品目录中：

```sh
PYTHONPATH=lg-cli:. python -B scripts/compare_literary_models.py --output /path/to/new-experiment
PYTHONPATH=lg-cli:. python -B scripts/compare_literary_models.py --output /path/to/another-new-experiment --run --budget-cny 5
```

两条示例独立执行；不要把计划目录直接拿来重跑。实验创建新的合成数据库和作品容器，逐次核对active正文未变，不正式采用，不读取Library或真实作品。保存完整可观察模型输入、最终候选、R5阶段／候选／来源记录、请求前意图、usage、耗时与价格估算。供应商私有推理和encrypted content不进入记录；只记录相应token数量，不把模型内部思考当设计依据。

`blind-review.md`是各模型内部打乱A/B的候选材料，`author-review.csv`保留人工评分、改动字数和耗时空栏，`blind-key.json`单独保存映射。自动字面检查只标选区外逐字保持和指定词串，不证明语义一致或文学质量。作者未审读之前，人工质量、返修工作量和最终认可均记为待评，不填0或由Agent冒充作者。

首轮工程验证包括真实localhost HTTP跨越R5、Native和费用记录；不是只mock模型结果。真实调用的最终结果、失败、提交身份与证据目录在完成后补充，不把本设计文档当作质量实验已经完成。
