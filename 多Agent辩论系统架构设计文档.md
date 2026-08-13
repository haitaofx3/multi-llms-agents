# 多Agent辩论系统架构设计文档

## 一、系统概述

### 1.1 项目背景与目标

本项目旨在构建一个多Agent辩论系统，通过模拟真实辩论场景，考察并提升Agent在复杂语境下的**知识性扩展能力**与**逻辑推理能力**。系统核心目标包括：

- **逻辑验证**：通过对抗性辩论，暴露Agent逻辑链条中的漏洞。
- **知识增强**：利用RAG机制，验证Agent在缺乏内部知识时的外部检索与整合能力。
- **可控性**：实现辩论流程的自动化控制、成本预算管理及结果的可量化评估。

### 1.2 系统整体架构描述

系统采用**中心化调度+异步通信**的星型架构：

- **Supervisor Agent** 作为核心控制平面，负责任务编排、状态机流转及最终结果聚合。
- **功能Agent**（Pro/Con/Judge/RAG）作为执行平面，通过消息队列与Supervisor及共享黑板交互。
- **共享黑板** 作为全局状态存储，记录辩论历史、当前论点及元数据，确保信息一致性。
- **外部服务层** 包含LLM API、向量数据库及监控告警系统，通过适配器模式接入。

### 1.3 设计原则

- **最小依赖**：核心逻辑不依赖特定LLM厂商，支持模型热切换。
- **异步解耦**：所有Agent间通信基于消息队列，避免同步调用导致的级联阻塞。
- **预算控制**：引入Token预算熔断机制，单场辩论设置硬性成本上限。
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
3. 严格遵循Token预算，若剩余预算<10%，强制结束辩论。
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

- **职责**：评估单轮表现及全场胜负，提供多维度评分与改进建议。
- **能力边界**：不参与辩论，仅做评估；需保持绝对中立。
- **输入**：完整辩论历史、ArgumentSubmit、Rebuttal。
- **输出**：Judgment。
- **System Prompt 示例**：

```
你是辩论裁判。请基于以下维度评分（1-10）：
1. 逻辑自洽性：是否存在自相矛盾。
2. 证据效力：RAG引用是否准确、相关。
3. 反驳有效性：是否击中对方要害。
4. 表达清晰度：结构是否清晰。
输出JSON格式，包含scores, winner, critique字段。
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
| budget_token | int | ✅ | 本任务剩余Token预算 |
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
| scores | object | ✅ | {logic: 8, evidence: 7, rebuttal: 9, clarity: 8} |
| winner | enum | ✅ | PRO / CON / DRAW |
| critique | string | ✅ | 详细评语 |
| convergence_flag | bool | ✅ | 是否触发收敛终止 |

### 3.5 状态同步消息 (StatusSync)

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| current_round | int | ✅ | 当前轮次 |
| agent_states | object | ✅ | {pro: "IDLE", con: "THINKING", judge: "WAITING"} |
| remaining_budget | int | ✅ | 全局剩余Token |
| elapsed_time_ms | int | ✅ | 已耗时毫秒数 |

---

## 四、辩论轮次控制逻辑

### 4.1 辩论生命周期状态机

```
[*] --> Idle
Idle --> Preparing: Supervisor接收任务
Preparing --> Debating: RAG初始化完成 & 双方Ready
Debating --> Debating: 轮次结束 & 未收敛 & 预算充足
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
- **预算熔断**：remaining_budget < MIN_TOKEN_THRESHOLD (默认1000)。
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

### 6.3 Token预算控制

- **预算分配**：Supervisor持有全局Budget，每次分发扣除预估Token（基于历史平均）。
- **动态调整**：若当前轮实际消耗>预估120%，下一轮自动缩减max_tokens参数或强制摘要。
- **硬截断**：LLM API调用前检查remaining_budget，不足则直接返回BudgetExceeded，不调用API。

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
