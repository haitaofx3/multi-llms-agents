# 多Agent辩论系统架构设计文档

## 一、系统概述

### 1.1 项目背景与目标

本项目旨在构建一个多Agent辩论系统，通过模拟真实辩论场景，考察并提升Agent在复杂语境下的**知识性扩展能力**与**逻辑推理能力**。系统核心目标包括：

- **逻辑验证**：通过对抗性辩论，暴露Agent逻辑链条中的漏洞。
- **知识增强**：利用RAG机制，验证Agent在缺乏内部知识时的外部检索与整合能力。
- **可控性**：实现辩论流程的自动化控制、跨模型公平性治理及结果的可量化评估。

### 1.2 系统整体架构描述

系统采用**中心化调度+异步通信**的星型架构：

- **Supervisor Agent** 作为核心控制平面，负责任务编排、状态机流转及最终结果聚合。
- **功能Agent**（Pro/Con/Judge/RAG）作为执行平面，通过消息队列与Supervisor及共享黑板交互。
- **共享黑板** 作为全局状态存储，记录辩论历史、当前论点及元数据，确保信息一致性。
- **外部服务层** 包含LLM API、向量数据库及监控告警系统，通过适配器模式接入。

### 1.3 设计原则

- **最小依赖**：核心逻辑不依赖特定LLM厂商，支持模型热切换。
- **异步解耦**：所有Agent间通信基于消息队列，避免同步调用导致的级联阻塞。
- **公平性治理**：引入多层公平性机制（FairnessGovernor L1–L6），以「字符数 + 相对比例」为跨模型公平的度量单位，替代旧的全局 Token 预算硬约束。
- **可观测性**：全链路Trace ID贯穿，支持单步回溯与人工介入。

---

## 二、Agent角色定义

### 2.1 Supervisor Agent（总调度中心）

- **职责**：接收宏观辩论目标，拆解为结构化任务，监控Agent健康状态，触发收敛检测，汇总最终报告。
- **能力边界**：不进行具体论点生成，仅做逻辑编排与状态管理。
- **输入**：用户自然语言指令、系统配置参数。
- **输出**：TaskDispatch、StatusSync、最终辩论报告。
- **System Prompt 示例**：

```
你是辩论系统的总调度器。你的任务不是辩论，而是管理辩论流程。
1. 解析用户意图，生成辩论主题与约束条件。
2. 监控当前轮次状态，若检测到收敛或超时，立即触发Judging流程。
3. 严格遵循公平性治理（FairnessGovernor L1–L6），不干预辩手发言长度与节奏。
4. 输出必须为JSON格式，包含action, target_agent, payload字段。
```

### 2.2 Pro-Agent（正方辩手）

- **职责**：构建正方论点，引用RAG提供的证据，针对反方论点进行逻辑反驳。
- **能力边界**：仅维护正方立场，禁止中立或倒戈。
- **输入**：TaskDispatch、Rebuttal（反方）、RAG检索结果。
- **输出**：ArgumentSubmit。
- **System Prompt 示例**：

```
你是正方辩手。你的核心立场是：{topic}。
1. 立论需包含：核心观点、逻辑推导、事实依据（来自RAG）。
2. 反驳需指出对方逻辑谬误（如：滑坡谬误、稻草人），而非单纯否认。
3. 若RAG无相关证据，明确标注"证据缺失"，严禁编造。
4. 回复格式严格遵循ArgumentSubmit协议。
```

### 2.3 Con-Agent（反方辩手）

- **职责**：构建反方论点，解构正方逻辑，提供替代性解释或反证。
- **能力边界**：仅维护反方立场，禁止人身攻击或无关发散。
- **输入/输出**：同Pro-Agent，立场相反。
- **System Prompt 示例**：

```
你是反方辩手。你的核心立场是：{opposing_topic}。
1. 必须直接回应正方上一轮的核心论据，不可顾左右而言他。
2. 优先使用逻辑归谬法，其次使用事实反证。
3. 保持冷静客观，禁止情绪化表达。
```

### 2.4 Judge Agent（裁判Agent）

- **职责**：评估单轮表现及全场胜负，提供多维度评分与改进建议。**按每位辩手分别评分**（`agent_scores`，四维：logic / evidence / rebuttal / clarity）。系统同时实测各辩手的**模型质量分**（流式发言 TTFT 与生成速率，非 LLM、确定性计算），但质量分**仅作展示，不参与胜负**——胜方由内容四维均分决定（**纯内容判定**）。
- **评分范围**：只接收**辩手发言**（`_build_judge_debate_text` 剔除主持人开场 / 任务分派 / 追问 / 总结，仅保留 `is_main=False` 的 speech）；模型质量分也只对两位辩手（`collaborators`）的流式发言统计。
- **综合评分与胜方**：全局综合得分 `scores` 由**后端确定性计算**（按各辩手四维分数逐维取均值，不再请求 LLM 输出，避免"综合得分照抄某辩手分数"）；`composite_scores = 内容均分(agent_scores 四维均值) × JUDGE_CONTENT_WEIGHT + 模型质量分 × JUDGE_PERFORMANCE_WEIGHT`（默认 0.8 / 0.2，经 `.env` 可调）**仅作展示**；胜方取**内容均分最高**辩手所在方（PRO/CON），内容并列最高取 DRAW。`agent_scores` 缺失（如裁判调用失败回退）时 composite 退化为模型质量分、胜方为 DRAW。
- **能力边界**：不参与辩论，仅做评估；需保持绝对中立。
- **输入**：完整辩论历史（仅辩手发言）、ArgumentSubmit、Rebuttal。
- **输出**：Judgment。
- **System Prompt 示例**：

```
你是独立辩论裁判，不参与辩论，也不接收主持人的任务分派信息。
请对【每位辩手】分别按以下维度评分（1-10）：
1. 逻辑自洽性：是否存在自相矛盾。
2. 证据效力：RAG引用是否准确、相关。
3. 反驳有效性：是否击中对方要害。
4. 表达清晰度：结构是否清晰。
输出JSON格式，包含agent_scores（键=辩手名）, winner, critique, convergence_flag字段。
（全局 scores 由后端确定性计算，无需 LLM 输出；示例中的分数仅为格式展示，禁止照抄）
```

### 2.5 RAG Agent（知识检索Agent）

- **职责**：解析辩论中的事实性断言，检索外部知识库，返回带置信度的证据片段。
- **能力边界**：仅做检索与摘要，不做观点判断。
- **输入**：ArgumentSubmit中的claims字段。
- **输出**：结构化证据列表（Source, Content, Confidence, URL）。
- **System Prompt 示例**：

```
你是知识检索助手。
1. 从输入中提取关键事实性断言。
2. 调用检索工具，返回Top-3相关片段。
3. 若检索结果置信度<0.6，标记为"低可信度"。
4. 严禁修改原文内容，仅可做摘要。
```

---

## 三、消息协议格式

所有Agent间通信采用JSON格式，Schema定义如下：

### 3.1 任务分发消息 (TaskDispatch)

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| task_id | string | ✅ | UUID，全局唯一 |
| topic | string | ✅ | 辩论主题 |
| stance | enum | ✅ | PRO / CON |
| round | int | ✅ | 当前轮次，从1开始 |
| budget_token | int | ✅ | 本任务剩余Token预算（协议保留字段，仅审计/遥测，不再做任何约束） |
| timestamp | int | ✅ | Unix毫秒时间戳 |

### 3.2 论点提交消息 (ArgumentSubmit)

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| arg_id | string | ✅ | UUID |
| agent_id | string | ✅ | 发送方Agent标识 |
| content | string | ✅ | 论点正文（Markdown） |
| evidence_refs | array | ❌ | RAG证据ID列表 |
| confidence | float | ✅ | 0.0-1.0，自我评估置信度 |
| round | int | ✅ | 所属轮次 |
| token_cost | int | ✅ | 实际消耗Token数 |

### 3.3 反驳消息 (Rebuttal)

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| target_arg_id | string | ✅ | 被反驳论点ID |
| rebuttal_content | string | ✅ | 反驳内容 |
| logic_flaws | array | ❌ | 标注的逻辑谬误类型，如["StrawMan", "AdHominem"] |
| counter_evidence | array | ❌ | 反证证据ID |

### 3.4 裁决消息 (Judgment)

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| scores | object | ✅ | {logic: 8, evidence: 7, rebuttal: 9, clarity: 8}（全局总体分，**后端按各辩手四维分数逐维取均值确定性计算**，不再依赖 LLM 输出） |
| agent_scores | object | ❌ | 按辩手分别评分：{辩手名: {logic, evidence, rebuttal, clarity}}，键与辩手名一致 |
| model_performance | object | ❌ | 系统实测模型质量：{辩手名: {avg_ttft_ms, avg_gen_rate, performance_score}} |
| composite_scores | object | ❌ | 综合评分（**仅展示，不参与胜负**）：内容均分 × 0.8 + 模型质量分 × 0.2，{辩手名: 分值} |
| winner | enum | ✅ | PRO / CON / DRAW（**纯内容判定**：由内容四维均分最高辩手所在方推导；内容并列最高取 DRAW） |
| critique | string | ✅ | 详细评语 |
| convergence_flag | bool | ✅ | 是否触发收敛终止 |

> **综合评分权重**：`composite = mean(agent_scores 四维) × JUDGE_CONTENT_WEIGHT(默认0.8) + performance_score × JUDGE_PERFORMANCE_WEIGHT(默认0.2)`，**仅作展示**；`performance_score` 由流式实测 TTFT（≤1s→10 / ≤5s→8 / ≤15s→6 / ≤30s→4 / 否则2）与生成速率（≥80字/s→10 / ≥40→8 / ≥20→6 / ≥10→4 / 否则2）取均值，无实测数据时取中性 5.0。**胜方不依赖 composite**，只由 `agent_scores` 四维均分决定。

### 3.5 状态同步消息 (StatusSync)

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| current_round | int | ✅ | 当前轮次 |
| agent_states | object | ✅ | {pro: "IDLE", con: "THINKING", judge: "WAITING"} |
| spent_tokens | int | ✅ | 累计 Token 用量（输入+输出，**只读遥测**，不做任何约束） |
| output_tokens | int | ✅ | 累计输出 Token（实时，只读遥测） |
| fairness | object | ✅ | 公平性治理报告（FairnessGovernor L1–L6 各层状态） |
| elapsed_time_ms | int | ✅ | 已耗时毫秒数 |

---

## 四、辩论轮次控制逻辑

### 4.1 辩论生命周期状态机

```
[*] --> Idle
Idle --> Preparing: Supervisor接收任务
Preparing --> Debating: RAG初始化完成 & 双方Ready
Debating --> Debating: 轮次结束 & 未收敛
Debating --> Judging: 触发终止条件
Judging --> Finished: 裁决完成
Finished --> [*]
Debating --> Idle: 异常熔断
```

### 4.2 单轮辩论流程

1. **Supervisor** 广播 TaskDispatch(round=N)。
2. **Pro-Agent** 与 **Con-Agent** **并行**生成 ArgumentSubmit（立论/反驳）。
3. **Supervisor** 收集双方论点，写入黑板，广播给 **RAG Agent** 进行事实核查（可选，异步）。
4. **Supervisor** 将对方论点及RAG结果分发给对应辩手，触发 **Rebuttal** 生成。
5. **Supervisor** 收集反驳，执行**收敛检测**。
6. 若未终止，进入下一轮；否则触发 **Judge Agent**。

### 4.3 轮次终止条件

- **硬限制**：current_round >= MAX_ROUNDS (默认5)。
- **收敛检测**：连续2轮 similarity_score > 0.85 且无新证据引入。
- **人工终止**：通过API手动触发 force_stop。

### 4.4 收敛检测算法

采用**语义+结构**双重检测：

1. **语义相似度**：使用Embedding模型计算当前轮论点与上一轮论点的余弦相似度。
2. **观点重复检测**：提取论点中的Subject-Predicate-Object三元组，计算集合Jaccard相似度。
3. **新信息增量**：若RAG未返回新证据且语义相似度>0.8，判定为无效循环。

### 4.5 超时与重试策略

- **单步超时**：Agent响应超时设为60s，超时后Supervisor标记该Agent为UNHEALTHY，自动降级至备用小模型或跳过该步（记录日志）。
- **重试机制**：JSON解析失败或协议校验失败，最多重试2次，间隔指数退避（1s, 3s）。

---

## 五、通信架构设计

### 5.1 异步消息队列

- **选型**：Redis Streams（轻量级）或 RabbitMQ（企业级）。
- **Topic设计**：
  - debate.dispatch.{task_id}：任务分发
  - debate.argument.{task_id}：论点提交
  - debate.status.{task_id}：状态心跳
- **ACK机制**：采用手动ACK，确保消息不丢失；处理失败进入死信队列（DLQ）。

### 5.2 共享黑板 (Blackboard)

- **存储**：Redis Hash + PostgreSQL（持久化）。
- **Key结构**：bb:{task_id}:round:{n}:args。
- **内容**：存储该轮所有ArgumentSubmit与Rebuttal的JSON序列化数据。
- **访问控制**：仅Supervisor与Judge有全局读权限，辩手仅可读己方历史及对方最新一轮。

### 5.3 心跳与故障恢复

- **心跳**：Agent每10s发送StatusSync，Supervisor维护last_seen时间戳。
- **故障判定**：30s无心跳视为宕机。
- **恢复**：Supervisor从黑板读取最后一致状态，重新分发TaskDispatch(round=N, retry=true)，Agent需支持幂等处理。

### 5.4 消息压缩与批量

- **压缩**：单条消息>4KB时，启用Gzip压缩，Header标记Content-Encoding: gzip。
- **批量**：RAG检索结果若包含>5条证据，合并为单条EvidenceBatch消息，减少队列压力。

---

## 六、成本与性能优化

### 6.1 模型分层策略

| 角色 | 推荐模型 | 理由 |
|------|---------|------|
| Supervisor | Qwen2.5-7B / GPT-4o-mini | 逻辑简单，高频调用，需低延迟 |
| Pro/Con Agent | Qwen2.5-72B / Claude-3.5-Sonnet | 核心生成能力，需强逻辑与表达 |
| Judge Agent | GPT-4o / Claude-3-Opus | 需极高推理与公正性，低频调用 |
| RAG Agent | BGE-M3 + Qwen2.5-7B | 检索与摘要，小模型足矣 |

### 6.2 并行与串行

- **立论阶段**：Pro与Con **完全并行**，无依赖。
- **反驳阶段**：依赖对方立论，必须**串行**，但RAG核查可与反驳生成**并行**（异步注入）。
- **裁决阶段**：等待所有轮次结束后**单次调用**，避免每轮裁决浪费Token。

### 6.3 公平性治理（替代 Token 预算控制）

Token 数不是跨模型公平的度量单位（不同模型的量化方式与 embedding 向量维度不同），全局 `total_token` 预算机制已移除，`max_tokens` 不再下发给 API。改为 `app/fairness.py` 的 **FairnessGovernor** 六层治理，以「字符数 + 相对比例」作为跨模型公平的度量单位：

- **L1 输出上限**：相对（参照对手近 N 轮有效输出均值 × `FAIR_OUTPUT_RATIO`，下限 `FAIR_FLOOR_CHAR_LIMIT`）× 比例与硬性字符安全上限（`FAIR_HARD_CHAR_CEILING` 默认 3000）取 min；超限后**事后截断**（保留头尾，不阻止生成，防止截断竞态）。
- **L2 有效回答比例**：剥离 `<think>` 思考片段后统计有效回答占比，思考占比超 `FAIR_THINK_RATIO_MAX` 记 warning。
- **L3 输入对称性**：PRO/CON 反驳时可见的对手历史轮数相等（或差 ≤ `FAIR_MAX_SEEN_DIFF`，落后方下一回合紧跟补齐）。
- **L4 轮次对称性**：每个模型每轮至多一次发言。
- **L5 采样参数对齐**：辩论发言统一 `temperature` / `top_p`（`FAIR_TEMPERATURE` / `FAIR_TOP_P`）；裁判/摘要等辅助 LLM 走同一套对齐入口（`FAIR_JUDGE_TEMPERATURE` / `FAIR_JUDGE_TOP_P`）。
- **L6 长度中性评审**：裁判提示词明确「不以长度论优劣，只看有效论证密度」。

token 用量（累计 `spent_tokens` / 输出 `output_tokens`）仅作**只读遥测**实时展示，不做任何约束。

### 6.4 缓存策略

- **RAG缓存**：相同Query的Embedding结果缓存24h，Key=rag:hash(query)。
- **论点去重**：Supervisor维护seen_hashes，若新论点Hash已存在，直接丢弃并提示Agent"观点重复"。

---

## 七、安全与可观测性

### 7.1 错误隔离

- **沙箱执行**：RAG Agent的代码执行（如有）在Docker容器内运行，禁止网络外联。
- **Prompt注入防护**：所有用户输入及Agent间传递内容，在注入System Prompt前进行转义与边界标记（如<user_content>...</user_content>）。

### 7.2 完整辩论日志

- **结构化日志**：每条消息写入debate_logs表，包含trace_id, agent_id, role, input_tokens, output_tokens, latency_ms, raw_payload。
- **链路追踪**：集成OpenTelemetry，支持从task_id一键查看全链路Span。

### 7.3 审计与回溯

- **快照机制**：每轮结束，将黑板状态序列化为JSON快照，存入对象存储（S3/OSS）。
- **回放功能**：支持加载任意历史快照，从该轮次重新开始辩论（用于调试或人工接管）。

### 7.4 人工审核接口

- **暂停点**：Supervisor在Judging前可选配置human_in_loop=true，暂停等待API确认。
- **干预API**：POST /api/debate/{id}/intervene，支持注入SystemMessage或强制修改当前轮次结果。

---

## 八、开发部署建议

### 8.1 技术栈推荐

- **语言**：Python 3.11+ (AsyncIO)
- **框架**：LangGraph (状态机管理) / AutoGen (可选，但建议自研核心调度以控成本)
- **消息队列**：Redis 7+ (Streams)
- **向量库**：Milvus / Qdrant
- **数据库**：PostgreSQL 16 (元数据) + Redis (状态)
- **监控**：Prometheus + Grafana + LangSmith (LLM专用)

### 8.2 关键依赖

- litellm：统一LLM API接口，支持Fallback与成本追踪。
- tenacity：指数退避重试。
- pydantic：消息协议校验。
- sentence-transformers：本地收敛检测Embedding。

### 8.3 部署架构建议

- **容器化**：每个Agent独立Docker镜像，资源限制（CPU/Mem）防止单点耗尽。
- **Supervisor单点**：初期单实例即可，通过Redis分布式锁保证任务不重复分发；后期可水平扩展，按task_id哈希分片。
- **GPU资源**：若本地部署RAG/Embedding，需独立GPU节点；LLM生成建议调用云端API以规避硬件维护成本。
- **冷启动**：预热RAG索引与Embedding模型，避免首场辩论延迟过高。

---

## 九、前端体验设计与历史记忆化（实现落地）

本节记录当前系统在**前端体验**与**历史记忆**上的落地设计，作为对既有架构的体验层补充。

### 9.1 UI 设计语言（简约大气，避免"AI 感"）

界面整体采用**低饱和深色 + 暖金点缀**的克制设计，替代霓虹 / 渐变科技感 / emoji 等"AI 味"表达：

- **色板**：背景 `#0f1115`、卡片 `#161920 / #1c2029`、主文字 `#e8eaee`、强调石板蓝 `#8a94b8`、暖金 `#d9a441`（主操作 / 主持人）、正方鼠尾草绿 `#7aa784`、反方灰玫瑰 `#c08a8a`、中立石板蓝 `#7c93b8`。
- **排版**：无衬线中文栈 + 克制的字重 / 字距 / 大写小标标题，圆角 `12px`，低阴影层次。
- **交互克制**：弹窗、按钮、状态胶囊、气泡动画均为低频微动效，不干扰阅读。

### 9.2 AI 选手头像（首字母文字头像）

- 每个席位 / 气泡头像使用**名称首字母**作为内容（`initialOf`），不引入 emoji 头像。
- 配色按**立场**映射：`HOST→av-host`（暖金系）、`PRO→av-pro`（绿系）、`CON→av-con`（玫瑰系）、`NEUTRAL→av-neutral`（蓝系）；未知立场按席位序号 `av-seat0..5` 兜底。
- 主 Agent 附加金色光环（`seat.main .avatar` 金环描边），与普通席位区分。

### 9.3 聊天气泡式发言（对话效果）

- 每次发言渲染为独立 `.bubble-row`（头像 + 气泡），带 `bubbleIn` 弹出动画，**只承载当前发言内容**，不拼接 / 不回放历史。
- 并发流式场景（第 1 轮并行立论）按 `speaker` 维护独立气泡：`speech_start` 建气泡 → `speech_delta` 增量插入（置于流式光标 `.caret` 前）→ `speech_end` 以完整文本收尾；多个 Agent 可同时滚动互不覆盖。
- 任务分派 / 回报、裁判裁决、最终总结、系统日志、错误各有专属气泡样式（虚线 / 居中整行 / 徽标式），与普通发言在视觉上分层。

### 9.4 历史讨论记忆化与检索

- **记忆化保存**：每场讨论完成（`status=done`，lifespan 监听）或手动保存 Markdown（`POST /api/save_markdown`）时，自动把「题目 + trace_id + 文件名 + 状态 + 轮次 + Token 统计 + 参与 Agent」写入 `data/discussions/index.json`（按 filename 去重 upsert，保留最近 200 条）。
- **检索接口**：`GET /api/history?q=` 按题目 / trace_id 关键词过滤；`GET /api/history/{filename}` 返回完整 Markdown（`resolve()` + `relative_to()` 路径穿越防护，仅允许 `data/discussions/` 内 `.md`）。
- **写入侧防穿越（P0-1）**：`POST /api/save_markdown` 的 `filename` 经 `_safe_discussion_filename` 清洗——只保留 `Path(fname).name`（丢弃目录 / 绝对路径 / `..`）、强制 `.md` 后缀、清洗后为空则回退 `discussion_<时间戳>.md`；与读取侧 `history.get_markdown` 的 `resolve()` + `relative_to()` 防护共同构成读写两侧的路径穿越防线。
- **前端历史面板**：顶栏"历史记录"入口 → 左侧列表（时间倒序，展示题目 / 状态 / 轮次 / 人数）+ 搜索框（300ms 防抖调检索接口）+ 右侧 Markdown 预览与一键下载。

### 9.5 本地安全（关闭远端暴露）

- `start.py` 默认绑定 `127.0.0.1:8000`，调试结束即本地关闭，**不长期挂载公网 / 局域网端口**；确需开放时以 `HOST=0.0.0.0` 环境变量显式覆盖。

### 9.6 接口安全与可靠性加固（P0 / P1 落地）

- **REST 请求体 pydantic 校验（P0-2）**：`app/schemas.py` 为 `configure / start / ingest / save_markdown` 定义 `ConfigureRequest / StartRequest / IngestRequest / SaveMarkdownRequest` 模型。只做**类型 / 形状**校验，类型非法时 FastAPI 返回 `422` 并自动生成 OpenAPI 文档；业务语义校验（Agent 数量 1–6、讨论状态冲突、空文本等）仍保留在各端点内，维持前端依赖的既有 `{ok, error}` `400` 错误契约。
- **lifespan 落盘可靠性（P1-3）**：讨论完成自动保存 Markdown 的异常不再静默吞掉（原先 `except Exception: pass`），改为捕获后写结构化日志（`lifespan.save_markdown` 事件）并幂等重试一次（写入与索引均为 upsert，重复执行安全）。
- **接口级测试覆盖（P1-4）**：`tests/test_api_flow.py` 使用 `httpx.AsyncClient` + `ASGITransport` 直接驱动 FastAPI 应用，monkeypatch `_consume_stream / _run_llm_retry / _judge_round / _final_summary` 四个 LLM 调用点实现确定性输出，**无需启动服务器、无需真实模型**即可覆盖 configure → start → done → export → save_markdown → history 全链路、路径穿越防护、空请求体默认文件名与既有 400 契约，可在 CI 中随单测一起运行。
