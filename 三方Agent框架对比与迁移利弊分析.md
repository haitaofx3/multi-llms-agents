# 多 Agent 圆桌辩论系统 — 三方框架对比与迁移利弊分析

> 分析日期：2026-09-05
> 三方框架生态演进极快，本文为 2026-09 时间点快照，正式选型前请以各框架官方仓库 / 版本发布页为准。

## 一、背景与目的

本项目（`mini_test`）是一个**多 Agent 圆桌辩论 / 协作系统**，其 Agent 编排层（`muti_llm_agent/agent.py`、`orchestrator.py`、`manager.py`、`fairness.py` 等）为**完全自研**，不依赖 LangGraph / AutoGen / CrewAI 等任何三方 Agent 框架，仅使用 `openai` SDK 作为 LLM 接入与 `fastapi` 作为 Web 层。

本文回答三个问题：

1. 当前自研框架与 2026 年主流三方框架的**结构差异**是什么？
2. 迁移到三方框架的**收益**是什么？
3. 迁移的**代价与风险**是什么，哪些层值得迁、哪些层必须自研？

结论先行：**本项目迁移整体框架的收益 < 代价**——系统的真正价值内核（跨模型公平性治理、纯内容裁判、按轮上下文控制）恰好是三方法框架不提供、且会被其抽象层侵蚀的部分；但**编排壳层**确有借鉴空间，文中 §8 给出分层结论与可选路线。

---

## 二、现状盘点：自研 Agent 框架的边界

### 2.1 模块清单与职责

| 模块 | 行数 | 职责 | 能否被三方框架替代 |
| --- | --- | --- | --- |
| `muti_llm_agent/agent.py` | 244 | Agent 定义：角色/性格/模型绑定/记忆/MCP 注册表 + 上下文构建 | 部分（Agent 对象可替代，**上下文拼装必须自控**） |
| `muti_llm_agent/orchestrator.py` | 2278 | **编排核心**：主循环、任务回合/辩论回合分离、主持人指令、定向反驳、收敛检测、裁判、汇总 | 大部分（编排壳可换，领域逻辑必须保留） |
| `muti_llm_agent/manager.py` | 176 | `DebateManager` 多会话注册表 + 元数据落盘 + 中断恢复 | 可替代（三方框架的 checkpoint / 会话持久化） |
| `muti_llm_agent/fairness.py` | 402 | **FairnessGovernor L1–L6**：字符上限、思考占比、输入/轮次对称、采样对齐、长度中性评审 | **不可替代**（领域内核） |
| `muti_llm_agent/memory.py` | 349 | 三类记忆（短期/长期/情境）+ 转化/遗忘 + 混合检索 | 可替代（但需自配检索公式） |
| `muti_llm_agent/rag.py` | — | 事实核查 + 证据按立场分桶 | 不可替代（检索结果分组为本项目定制） |
| `muti_llm_agent/tools.py` | 163 | 每个 Agent 的 MCP 工具注册表 + 内置工具 | 可替代（三方框架普遍原生支持 MCP） |
| `muti_llm_agent/llm.py` | — | 统一 LLM 接入（OpenAI 兼容、function calling、流式） | 可替代（框架的 Provider 抽象） |
| `muti_llm_agent/main.py` | 405 | FastAPI REST + WebSocket + 静态服务 | **不受影响**（Web/传输层与框架正交） |
| `muti_llm_agent/schemas.py` / `protocol.py` / `history.py` / `file_parser.py` / `logger.py` | — | 请求校验 / 事件协议 / 历史索引 / 文件解析 / 结构化日志 | **不受影响** |

### 2.2 自研框架最核心的两个抽象

**A. 双状态机（`orchestrator.py`）**

```python
self.status = "idle"          # idle/running/paused/done/stopped（UI 状态）
self.phase  = "idle"          # idle/preparing/debating/judging/finished（架构状态机）
```

讨论由 `run_discussion()`（orchestrator.py:1162）驱动的显式循环推进：`preparing → debating（逐轮）→ judging → finished`，异常熔断回 `idle`。所有推进节点向 `event_bus` 发布 `status` 事件驱动前端。

**B. 公平性治理（`muti_llm_agent/fairness.py`，`FairnessGovernor`）**

L1 输出上限：`ceiling_for(stance, round_no, ...)` 按「对手近 N 轮有效输出均值 × 比例（默认 1.5）」与硬上限（默认 3000 字符）取小，超限事后截断；L2 有效回答比例（剥离 `<think>`）；L3 输入对称性；L4 轮次对称性；L5 采样对齐（统一 temperature / top_p）；L6 长度中性评审。其设计前提是：**Token 数不是跨模型公平度量，字符 + 相对比例才是**。

> 结论：本项目的"自研框架"并非为了造轮子而造轮子——编排壳（状态机、循环、会话）是三方法框架擅长的；公平性治理、纯内容裁判、定向反驳、按轮压缩等是**辩论这一具体场景的领域内核**。

---

## 三、2026 年三方框架格局（时间点快照）

**重大生态变化（2026-04）**：Microsoft 将 AutoGen 置为 maintenance mode（不再开发新功能），并把 AutoGen 与 Semantic Kernel 合并为 **Microsoft Agent Framework (MAF) 1.0 GA**；原 AutoGen 核心成员社区分支 **AG2** 保持活跃。官方推荐新项目直接使用 MAF。

主流候选框架定位：

| 框架 | 一句话定位 | 维护状态 |
| --- | --- | --- |
| **LangGraph** | 状态机 / 有向图编排，强流程控制 | 活跃，生态最大 |
| **CrewAI** | 角色分工 / 团队协作 | 活跃 |
| **Microsoft Agent Framework (MAF)** | 企业级多 Agent 编排（合并 AutoGen + SK） | 微软官方，活跃 |
| **AG2**（原 AutoGen 社区分支） | 对话式多 Agent 协商 | 活跃（社区驱动） |
| **Google ADK** | 原子 Agent + 标准化互联（A2A） | 活跃（Google 生态） |

趋势：① 框架生态从"百花齐放"走向整合收敛；② "MCP 管工具、A2A 管协作"成为共识，MAF / ADK 原生支持 A2A；③ 工程化（可观测、状态持久化、成本控制、失败重试）成为标配。

---

## 四、横向能力对比

| 维度 | 本项目（自研） | LangGraph | CrewAI | MAF | AG2 | Google ADK |
| --- | --- | --- | --- | --- | --- | --- |
| 编排模型 | 显式循环 + 双状态机 | 有向图 + 状态机 | 角色 + 任务流 | 图编排 + 对话模式 | 对话轮次 + 事件 | 原子 Agent + 工具 |
| 状态管理 | 自研（内存 + 会话 JSON 落盘） | 内建 checkpoint / 持久化 | 任务级状态 | 内建 checkpoint + Cosmos | 会话级 | 内建会话 + 事件 |
| 多 Agent 协作 | 主持-辩论 / 任务池认领 | 需自行构图 | 顺序/层级流程 | Supervisor/群聊/工作流 | GroupChat 群聊协商 | 消息路由 |
| 人工介入（HITL） | 无（pause/resume 为手动控制） | 原生支持 | 需自定义 | 原生支持审批工作流 | 需自定义 | 原生 |
| MCP | 自研注册表 + 内置工具 | 经 LangChain 集成 | 社区插件 | **原生** | 社区插件 | 原生 |
| A2A | 无 | 讨论中 | 讨论中 | **原生** | 讨论中 | **原生** |
| 多语言 SDK | Python（单一） | Python / JS | Python | Python / .NET | Python / .NET | Python/Go/Java/TS |
| 学习曲线 | —（已在项目中） | 陡（图概念） | 平（角色直觉） | 中 | 中（对话范式） | 中 |
| 生产就绪度 | 中（个人学习项目） | 高 | 中 | 高（企业 telemetry/合规） | 中低 | 中（v1.0 刚稳） |
| Web/传输层 | FastAPI REST + WS 自建 | 无（自带 LangGraph Platform 可选） | 无 | 有 | 无 | 无 |
| 可观测性 | 自研 JSON 日志 + trace_id | LangSmith / 生态 | 自建 | 企业 telemetry | 自建 | Cloud 生态 |

### 4.1 与本项目形态最接近的三方抽象

- **AG2 GroupChat**：多 Agent 来回发言、可设主持人角色，与本项目"圆桌 + 主持人"最像——但 AG2 是社区驱动、生产就绪度中低。
- **LangGraph StateGraph**：本项目的 phase 状态机（preparing/debating/judging/finished）本质就是一张显式状态图，可一比一映射为 LangGraph 的节点/边。
- **MAF Supervisor / GroupChat**：提供企业级能力，但 Azure/微软生态绑定较重，本项目无此诉求。

---

## 五、逐项能力映射：哪些代码会被谁替代

| 本项目代码 | 迁移后去向 | 说明 |
| --- | --- | --- |
| `orchestrator.run_discussion()` 主循环 | LangGraph 图 / MAF workflow | 逐轮辩论循环可表达为图的循环边 + 条件边（收敛即跳转 judging） |
| `orchestrator` 的 phase 状态机 | LangGraph 节点 + 路由 / MAF 状态 | 直接受益于 checkpoint 与时间旅行调试 |
| `manager.py` 会话管理 | 框架 checkpoint / 持久化 | 免费获得崩溃恢复、断点续跑 |
| `pause() / resume() / end()` | 框架状态控制 / HITL 拦截点 | 三方框架控制能力更强 |
| `agent.py`（Agent 对象） | 框架 Agent 抽象 | 保留角色/性格等 metadata，去掉自建上下文管线 |
| `memory.py` | 框架记忆组件 | 需把「语义 50% + 关键词 30% + 重要度 20%」公式在框架存储上重现 |
| `tools.py` | 原生 MCP 支持 | 直接用标准 MCP 生态，省维护 |
| `llm.py` | 框架 Provider 抽象 | 免自维护 OpenAI 兼容层（但本项目多供应商本就同一协议，收益小） |
| `main.py` / `schemas.py` / `history.py` / `file_parser.py` / `logger.py` / 前端 | **不动** | Web/传输/工具链与框架正交，无需改动 |
| `fairness.py`、`rag.py` 立场分桶、定向反驳、按轮强压缩 | **无法迁移，须按框架语义重写或作为图节点注入** | 见 §6.2 风险分析 |

---

## 六、迁移的收益

### 6.1 基建省力

- **状态持久化 / 断点续跑免费获得**：自研 `manager.py` 需手工维护会话元数据与中断恢复；LangGraph / MAF 的 checkpoint 原生提供，还附带时间旅行调试（回放任意一步的输入输出）——排查"哪一轮喂错了上下文"会大幅变快。
- **编排代码大幅缩水**：`orchestrator.py` 2278 行中主循环、轮次控制、事件发布等"壳"代码可被框架图/工作流替代，只留下领域节点。
- **HITL / 重试 / 幂等成为标配**：本项目若想做"审核后放行"需自研；三方框架直接声明式支持。

### 6.2 工具与生态

- **MCP 生态直接可用**：接入联网搜索、代码执行、office 等现成工具，比自研 `tools.py` 快一个量级。
- **可观测性成熟**：LangSmith / langfuse 提供 trace、token、延迟可视化与自动评测（eval）面板，强于自研 JSON 日志；对 prompt 注入敏感的多轮辩论价值尤其大。
- **版本跟随社区**：新模型协议、function calling 兼容、结构化输出由框架维护，不必自研 `llm.py`。

### 6.3 表达复杂流程

- 若未来要加"多阶段评审""子辩论并行""人机回环"，图式编排比硬编码循环更易改、更可审计；本项目主循环每次改规则都伴随状态机边界风险。

---

## 七、迁移的代价与风险

### 7.1 领域内核无法平移（最关键）

系统的真实价值不在"能跑多 Agent"，而在**怎么判得公平、怎么辩得深入**：

1. **FairnessGovernor L1–L6（`muti_llm_agent/fairness.py`）**：跨模型公平治理是本项目区别于任何三方框架的地方。它在**字符级**约束每个发言的构造与事后截断，并强制对称的输入可见性 / 轮次 / 采样参数——这些必须落到**每一轮 prompt 的实际拼装**上。
2. **纯内容裁判**：winner 由内容四维均分决定、模型质量分仅展示不参与胜负——这是领域规则，框架不会给你。
3. **对手定向反驳 + 按轮强压缩**：每轮"谁看到对手的什么（原文 / 摘要 / 论据句）"完全由 `orchestrator.py` 的私有逻辑控制。

风险本质：**框架抽象层会插入自己的消息格式、历史管理与默认 prompt 注入**，与本项目"精确控制每轮上下文"的需求冲突。迁移这类内核不是"换个库"而是"在框架语义下重写全部领域逻辑"，工作量接近重构而非替换。

### 7.2 抽象层泄漏与版本漂移

- 框架升级会引入**不可控行为漂移**（默认 prompt、消息裁剪策略变化），而本项目对输入上下文高度敏感——一次框架小版本升级可能悄悄改变辩论公平性。
- 三方框架普遍版本节奏快（LangGraph / MAF 都处于快速迭代期），需常驻维护预算。

### 7.3 测试体系重写

`tests/test_ci_fast.py`、`tests/test_api_flow.py`（`httpx.ASGITransport` + LLM 调用点打桩）依赖自研调用点的**确定性**。迁框架后打桩点改变、默认注入变多，确定性测试的重写成本可观；公平性类测试尤其依赖"我能精确构造输入"。

### 7.4 学习与规模目标冲突

- 目录位于 `program_Agent` 教学语义下，**自研编排本身是学习目标**——理解主循环、状态机、记忆转化正是 Agent 原理的教材。
- 本项目是个人级规模，尚无"可观测性、checkpoint、合规"的真实生产诉求；这些正是三方框架的最大卖点。

### 7.5 其他

- 依赖体积与供应商锁定（尤其 MAF/Azure、ADK/GCP 生态）；LangGraph Platform 为收费托管，会诱使脱离自托管。
- AutoGen 官方已停更，若团队既有经验是 AutoGen，选型上需绕道 MAF 或 AG2，存在不确定性。

---

## 八、分层结论：哪些值得迁、哪些必须自研

**建议：整体保持自研，不迁移到三方框架。** 若只允许保留一层不可替换的东西，那就是 `muti_llm_agent/fairness.py` + `orchestrator.py` 中的领域回合逻辑——它们既是差异化内核，也是迁移成本最高、框架收益最低的部分。

按"迁移成本 × 框架收益"分层：

| 层 | 现状 | 结论 |
| --- | --- | --- |
| 传输/Web 层（`main.py`、`schemas.py`、前端） | 自研 FastAPI | **维持**，与框架正交，无需动 |
| 编排壳（主循环、状态机、会话） | 自研 | **可试点**替换为 LangGraph（收益最大处） |
| 工具层（`tools.py`） | 自研 | **可渐进**改为标准 MCP，不必为此迁框架 |
| LLM 接入（`llm.py`） | 自研 | 维持（多供应商同一 OpenAI 协议，框架收益小） |
| 记忆系统（`memory.py`） | 自研 | 可选，但须自配混合检索公式 |
| **公平性 / 裁判 / 定向反驳 / 按轮压缩** | 自研 | **必须自研**，勿迁移 |
| RAG 立场分桶 | 自研 | 必须自研（检索结果分组是领域定制） |

### 8.1 若未来要试水，推荐路线

**路线 A（低风险试点）**：只把 `run_discussion()` 的 phase 状态机用 LangGraph 表达，Agent 节点内部仍调用现有的 `_agent_turn` / `_rag_check_round` / `_judge_round` 领域函数。收益可见（checkpoint / 调试 / 流程可视化），风险可控（领域逻辑原样保留）。

**路线 B（中等）**：工具层换为标准 MCP，`tools.py` 收敛为薄适配。

**路线 C（激进，不推荐）**：整体迁 LangGraph / MAF。等价于重写公平性治理并在框架抽象层上重新校准，投入产出比最差。

---

## 九、分场景决策速查

| 你的目的 | 建议 |
| --- | --- |
| 学习 Agent 编排原理（本项目现状） | **保持自研**，主循环/状态机本身就是教材 |
| 快速做多角色内容型 Demo | CrewAI（角色直觉、上手最快） |
| 生产级流程控制 + 可审计 | LangGraph（checkpoint、HITL、LangSmith） |
| 微软 / Azure 企业生态 | MAF（AutoGen 后继，合并 Semantic Kernel） |
| 社区驱动、对话式多 Agent 协商 | AG2（注意非微软官方） |
| Google Cloud / 多语言 / A2A 互联 | Google ADK |
| 想要辩论公平性 / 裁判这类定制治理 | **留在自研框架**，三方框架不提供 |

---

## 十、一句话总结

**"三方框架给的是工程化（编排、持久化、可观测、生态），本项目的护城河是领域化（公平治理、纯内容裁判、上下文精确控制）。"** 迁移工程化层收益有限且会把领域化层拖进框架语义重写的泥潭——当前阶段维持自研、只做 MCP 与编排壳的渐进借鉴，是性价比最高的路线。

---

## 附：本文依据的本地文件

- `README.md`（功能特性 1–39、目录结构）
- `docs/CODE_LOGIC.md`（模块职责 → 关键函数 → 数据流）
- `多Agent辩论系统架构设计文档.md`（状态机 §4、FairnessGovernor、裁判机制）
- `muti_llm_agent/orchestrator.py`（双状态机、`run_discussion`、各回合函数）
- `muti_llm_agent/fairness.py`（`FairnessGovernor` L1–L6 参数与 API）
- `muti_llm_agent/manager.py` / `agent.py` / `memory.py` / `tools.py` / `main.py`（模块边界）
- 三方框架现状：2026-09 Web 检索（MAF 1.0 GA、AutoGen maintenance mode、AG2、LangGraph / CrewAI / ADK 定位）
