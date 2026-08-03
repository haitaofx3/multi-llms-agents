# 多 Agent 圆桌协作系统 — 代码逻辑文档

> 本文档系统梳理当前项目的全部代码逻辑，按"模块职责 → 关键函数 → 数据流"组织，便于理解与二次开发。

---

## 1. 项目概述

一个支持多个 LLM Agent 围绕"圆桌"进行**辩论式多轮协作讨论**的 Web 应用。核心特征：

- 每个 Agent 拥有**独立的基础模型**（DeepSeek / Qwen / 自定义）、**记忆系统**（短期 / 长期 / 情境 + 遗忘）、**MCP 工具集**（知识检索 / 联网搜索 / 记忆回忆）。
- 首个被创建的 Agent 为**主 Agent（辩论主持人）**，其余为协作 Agent。
- 讨论以"圆桌辩论"形式进行：第 1 轮主持人分派调研/立论任务，之后轮次主持人抛出质疑、推动交锋，协作者辩护/反驳；达到最大轮次后由主持人综合总结。
- 提供游戏化圆桌 UI（席位按数量等分）、实时发言流、暂停/继续/结束、Markdown 结果导出。

---

## 2. 目录结构与职责

```
mini_test/
├── app/                 核心应用包
│   ├── config.py        环境变量与配置（Settings 单例、PROJECT_ROOT）
│   ├── llm.py           LLM 统一接入（OpenAI 兼容 + 原生 function calling + 流式）
│   ├── embeddings.py    文本向量化（sentence-transformers，失败降级哈希）
│   ├── vector_store.py  共享向量知识库（Qdrant，失败降级内存库）
│   ├── memory.py        每个 Agent 的记忆系统（三类记忆 + 转化 + 遗忘）
│   ├── tools.py         每个 Agent 的 MCP 工具注册表 + 内置工具
│   ├── agent.py         Agent 定义（角色/性格/模型/记忆/MCP/上下文构建）
│   ├── orchestrator.py  圆桌编排器（辩论主循环、任务分派/回报、停止判定）
│   ├── main.py          FastAPI 应用（REST + WebSocket + 静态服务）
│   └── static/          前端 UI
│       ├── index.html   页面骨架（配置区 + 讨论区）
│       ├── style.css    圆桌/席位/发言样式
│       └── app.js       前端逻辑（配置、席位等分、WebSocket 事件）
├── start.py             启动入口（uvicorn）
├── requirements.txt     依赖清单
├── tests/               测试
│   └── test_e2e.py      端到端集成测试（需先启动服务器）
├── docs/                文档
│   └── CODE_LOGIC.md    本文档
├── data/                运行时数据（自动生成，不入库）
│   ├── discussions/     自动保存的 Markdown 结果
│   └── memories/        各 Agent 记忆持久化（json）
└── README.md            使用说明
```

---

## 3. 运行方式

```bash
conda activate agent_env
python start.py
# 浏览器打开 http://localhost:8000
```

启动后 `start.py` 通过 uvicorn 加载 `app.main` 的 FastAPI 应用，监听 `0.0.0.0:8000`，并自动挂载 `/static` 与 `/ws` 路由。

---

## 4. 配置层 — `config.py`

**职责**：从项目根目录 `.env` 读取配置。针对本仓库 `.env` 存在行首空格、值带引号等不规范写法，采用**手写解析器** `_parse_dotenv()`（不依赖 python-dotenv），逻辑：

1. 逐行 `strip()`，跳过空行与 `#` 注释；
2. 按首个 `=` 切分 key/value，key 再 `strip()`；
3. 若 value 首尾为成对的 `'` 或 `"`，去掉引号；
4. 结果存入 `_raw` 字典。

随后通过 `_get(name, default)` / `_int(name, default)` 构建 `Settings` 类，暴露关键字段：

| 类别 | 字段 |
| --- | --- |
| LLM 默认 | `LLM_MODEL_ID`、`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_TIMEOUT` |
| DeepSeek | `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL_ID` |
| 搜索 | `TAVILY_API_KEY`、`SERPAPI_API_KEY` |
| Qdrant | `QDRANT_URL`、`QDRANT_API_KEY`、`QDRANT_COLLECTION`、`QDRANT_VECTOR_SIZE` 等 |
| 嵌入 | `EMBED_MODEL_TYPE`、`EMBED_MODEL_NAME`、`EMBED_API_KEY` |
| Neo4j | `NEO4J_*`（保留，当前未在核心链路使用） |

> `PROJECT_ROOT = Path(__file__).resolve().parent.parent`（`app/` 的上一级）供其余模块定位项目根目录与资源目录。

---

## 5. LLM 接入层 — `llm.py`

**职责**：屏蔽不同厂商差异，统一为 OpenAI 兼容协议调用，并支持**原生 function calling**。

### 5.1 供应商预设 `PROVIDER_PRESETS`
- `qwen` / `default`：使用 `.env` 的 `LLM_*`（`https://dashscope.aliyuncs.com/compatible-mode/v1`）。
- `deepseek`：`https://api.deepseek.com`，模型 `deepseek-chat`。

### 5.2 `resolve_model_config(provider, base_url, api_key, model_id)`
合并"预设 + 用户覆盖"：用户传入非空值优先生效，否则回退到对应预设。返回 `{"base_url","api_key","model_id"}`。

### 5.3 `chat(messages, model_config, tools, tool_choice, temperature)`
- 用 `OpenAI(base_url, api_key, timeout)` 创建客户端（api_key 为空时给 `"EMPTY"` 避免 SDK 报错）。
- 若 `tools` 非空则附带 `tools` 与 `tool_choice="auto"`。
- 返回字典：
  ```python
  {
    "content": str,            # 文本回复（已用 _strip_think 去除 <think>...</think>）
    "tool_calls": [{"id","name","arguments"}],   # 结构化工具调用
    "assistant_message": {...} # 可直接回传 API 的 assistant 消息（含 tool_calls），维持多轮工具调用
  }
  ```
- `_strip_think()` 用正则移除 Qwen3 等模型的思考片段，保证下游文本干净。
- `tool_calls` 的 `arguments` 已 `json.loads`；解析失败则退为 `{}`。

### 5.4 流式输出 `chat_stream(messages, model_config, tools, ...)`
- 生成器，基于 `stream=True` 逐 chunk 消费，`yield` 三种事件：
  - `{"type":"content","delta":str}`：文本增量（已剥离 think 片段）；
  - `{"type":"tool_call_args","index":int,"delta":str}`：工具参数增量（按 index 累积）；
  - `{"type":"final","result":{...}}`：流结束，携带完整结果（结构同 `chat()`）。
- `ThinkFilter` 类处理**跨 chunk 边界**的 `<think>`/`</think>` 标签剥离：维护短缓冲，
  当尾部是 `<think>` 前缀时暂存等待后续 chunk，保证流式结果与非流式 `_strip_think` 一致。
- 流式结束时若 `tool_calls` 非空，仍按非流式同规则组装 `assistant_message` 回传 API，
  维持多轮工具调用。

---

## 6. 嵌入与向量库

### 6.1 `embeddings.py`
- `_load_model()` **懒加载** `sentence-transformers` 模型（全局单例），维度取自模型本身。
- 加载失败（无网络/无依赖）时降级 `_fallback_embed()`：以 `hash(text)` 为随机种子生成**确定性**单位向量（维度取 `QDRANT_VECTOR_SIZE`），保证同一文本向量一致、项目离线可用。
- 对外接口：`embed(texts)`、`embed_query(text)`、`dim()`。

### 6.2 `vector_store.py` — 共享向量库
- 所有 Agent **共用同一个** `shared_vector_store` 单例，满足"访问同一向量数据库"。
- `SharedVectorStore` 构造时 `_try_qdrant()`：
  - 成功 → 后端 `qdrant`（`_QdrantStore`，自动建集合，余弦距离）；
  - 失败（本机未跑 Qdrant）→ 降级 `in-memory`（`_InMemoryStore`，numpy 余弦检索）。
- 接口：`add(texts, metas)`、`search(query, top_k)`、`reset()`。
- 文本按"段落"切分后批量嵌入；`search` 返回命中的 `payload`（含 `text`、`source`）。

---

## 7. 记忆系统 — `memory.py`

每个 Agent 持有一个 `MemorySystem`（持久化到 `data/memories/<agent_id>/memory.json`），记忆分为三类：

| 类别 | 字段 | 含义 |
| --- | --- | --- |
| 短期 `short_term` | 滚动窗口 | 近期对话/事件 |
| 长期 `long_term` | 沉淀事实 | 重要结论/决策 |
| 情境 `contextual` | 知识摘要 | 对讨论的"摘要式"理解 |

每条记忆结构：`{id, text, ts, importance, access}`。

### 7.1 写入
- `add_short(text, importance=0.5)`：写入短期；超出 `short_capacity`（默认24）时调用 `_promote_or_drop_short()`。
- `add_long(text, importance=0.8)` / `add_context(...)`：写入长期/情境，并 `_trim()` 控制容量。

### 7.2 转化（互相转化）
- `_promote_or_drop_short()`：短期溢出时，最不重要者移除，重要者（`≥promote_importance*0.6`）提升为长期。
- `promote_short_to_long(min_importance=0.75)`：**短期 → 长期** 的显式提升。
- `summarize_short_to_context(summarizer=None)`：**短期 → 情境** 的压缩（默认简单拼接，可选 LLM 摘要），压缩后清空短期缓冲。
- 长期 ⇄ 情境：检索时合并长期事实与情境摘要，互相补充。

### 7.3 遗忘体系（每 Agent 可独立配置 `forget_config`）
- `_effective_importance(item) = importance × (1 - decay_per_day)^age_days × (1 + 0.05×(access-1))`
  —— 基于**时间衰减 + 访问频次**的"有效重要性"。
- `forget()`：有效重要性低于 `min_importance`（默认 0.08）或超出类别容量者移除。
- `_trim(cat, cap)`：按有效重要性保留前 `cap` 条（容量保护）。

### 7.4 检索与快照
- `query(query, top_k)`：合并 `[情境]` + `[长期]` + 最近 `[短期]`，按关键词匹配 + 有效重要性排序（轻量，无需再 embedding）。
- `context_text()`：给 LLM 注入记忆上下文。
- `snapshot()`：返回三类记忆条数，用于 UI/导出展示。

---

## 8. MCP 工具层 — `tools.py`

每个 Agent 拥有自己的 `MCPRegistry`（满足"每个 Agent 建立自己 MCP 对象，用于搜索对应工具"）。

### 8.1 数据结构
- `Tool(name, description, parameters, run_fn)`：`schema()` 输出 OpenAI function 定义；`run()` 安全执行（异常返回错误串）。
- `MCPRegistry`：
  - `register(tool)` / `schemas()`（导出工具定义列表）
  - `search(query, top_k)`：按描述**关键词检索"该用哪个工具"**（满足"搜索对应工具"）
  - `call(name, arguments)`：执行工具
  - `names()`：已注册工具名

### 8.2 内置工具（`build_default_tools`）
| 工具 | 说明 | 实现 |
| --- | --- | --- |
| `knowledge_search` | 检索共享向量库 | `_knowledge_search` → `shared_vector_store.search` |
| `web_search` | 联网搜索（SerpApi） | `_web_search`；未配置 key 或失败均友好提示 |
| `memory_recall` | 回忆本 Agent 记忆 | `_memory_recall` → `memory.context_text` |

> 任务分派/回报不在此注册，而是由 `orchestrator` 通过专用 schema 拦截处理。

---

## 9. Agent 定义 — `agent.py`

`Agent` 封装单体的全部状态：

```python
Agent(agent_id, name, role, personality, model_config, is_main, forget_config)
```
- `role` / `personality` 缺省 → "机器人"角色（满足"未填则按机器人带入"）。
- `memory = MemorySystem(...)`，`mcp = build_default_tools(name, memory)`。
- `history`：最近对话（不含 system），上限 `max_history=24`，用于跨轮次人设延续。

### 9.1 `system_prompt(...)` —— 提示词构建
按固定顺序拼接，注入动态上下文：
1. 身份（名称/角色/性格）；
2. **角色与性格约束**：必须严格符合人设，不得声明自己是 AI，除非用户修改；
3. **讨论形式**：圆桌辩论说明；
4. **记忆**（`memory.context_text`）；
5. **共享上下文 / 任务接收方变量**（`shared_context` 的回填状态）；
6. **当前任务状态**（`_task_status_text`）；
7. **模式指令**：
   - 主 Agent：主持辩论、第1轮分派、之后轮次追问、最后总结；仅用 `delegate_task` 分派，不亲自执行；
   - 协作 Agent（task 模式）：只通过 `report_result` 返回**最终结果 + 是否完成**，严禁返回中间过程；
   - 协作 Agent（discussion 模式）：针对主持人质疑进行**辩护/反驳**，可调用 `knowledge_search` 检索论据。

---

## 10. 编排器 — `orchestrator.py`（核心）

### 10.1 全局结构
- `DELEGATE_SCHEMA` / `REPORT_SCHEMA`：任务分派/回报的 function-calling 定义。
- `EventBus`：基于 `asyncio.Queue` 的发布/订阅，把事件实时推给 WebSocket。
- `Orchestrator` 单例 `orchestrator`，关键状态：
  - `agents / main / collaborators`、`shared_context`（变量名→{by,value,task_id}）、`tasks`、`round / max_rounds`、`topic`、`status`（idle/running/paused/done/stopped）、`transcript`（发言记录）、`_pause_event`（异步暂停控制）。

### 10.2 配置 `configure()`
校验 1–6 个 Agent，逐个 `resolve_model_config` 构造 `Agent`；**首个为 `main`**，其余进 `collaborators`；重置轮次/任务/上下文/历史，状态置 `idle`。

### 10.3 控制 `pause() / resume() / end()`
基于 `asyncio.Event`：暂停时 `clear()`，循环每轮 `await _pause_event.wait()`；`end()` 置 `stopped` 并 `set()`，使循环在下一轮检测后退出。

### 10.4 单 Agent 回合 `_agent_turn(agent, user_msg, mode, allow_delegate, stream)`
封装"工具调用循环"，`stream=True` 时对**文本发言**进行流式推送：
1. 构建 `messages = [system] + history + [user]`；
2. 工具集 = `mcp.schemas()`，主 Agent 追加 `DELEGATE_SCHEMA`（除非 `allow_delegate=False`），协作任务模式追加 `REPORT_SCHEMA`；
3. `_consume_stream()` 在后台线程运行 `chat_stream`，通过 `asyncio.Queue` + `loop.call_soon_threadsafe`
   把增量桥接回事件循环（不阻塞事件循环、不卡暂停/结束控制）；
4. 收到首个文本增量即推送 `speech_start`（建气泡 + 席位高亮），随后逐个 `speech_delta`；
5. 循环（上限5次）：
   - 无 `tool_calls` → 退出；
   - 主 Agent 调 `delegate_task` 或协作 Agent 调 `report_result` → **special 分支**，收集后退出；
   - 普通工具 → `mcp.call` 执行并把 `tool` 消息回传，继续循环；
   - 流式异常时**回退非流式 `chat`**，保证对话不中断；
6. 回合结束若发生过流式发言，补发 `speech_end`（携带完整文本，前端以之收尾）并写入 transcript；
7. 把 user/assistant 文本写入 `history`，返回 `{content, delegate, report, streamed}`。
   - `streamed=True` 时调用方（`run_discussion`）**不再重复发送完整 `speech` 事件**，
     仅处理 `last_main_message` / 记忆等后续逻辑。

### 10.5 主持人指令 `_main_directive(round, is_last, had_any_task, pending_exists)`
- **第 1 轮**：分派调研/立论任务（`delegate_task`），或先陈述开场立场。
- **第 2…N 轮**：基于上一轮发言与已回填结果，**抛出疑问、要求论据、指出分歧**，可追加追问任务；强制"至少提出一个有针对性质疑"。
- 若存在未完成任务，附加"请先审视进展"提示。

### 10.6 讨论主循环 `run_discussion()`
```
while round < max_rounds:
    等待暂停/结束信号; 若 stopped 退出
    round += 1; 发布 round 事件
    # 1) 主持人回合
    directive = _main_directive(...)
    main_out = _agent_turn(main, directive, "discussion")
    发布 speech; 记录 last_main_message
    对每个 delegate → _create_task + 发布 task_assigned
    # 2) 协作 Agent 回合（按席位顺序）
    for collab in collaborators:
        pending = 该 collab 未完成任务
        if pending:                      # 任务模式
            for task: _agent_turn(collab, task_umsg, "task")
                      若有 report → _apply_report + 发布 speech/task_result
                      否则记录发言, rounds_waiting+1; 超 3 轮强制标记完成
        else:                           # 辩论模式
            umsg = 引用主持人最新发言 → 辩护/反驳
            _agent_turn(collab, umsg, "discussion") → 发布 speech
    # 3) 记忆管家 _memory_housekeeping()
    # 4) 若到达最大轮次 → break
# 循环结束后：_final_summary() → 发布 summary → status=done
```

**关键设计（修复"第1轮即停"）**：
- 已**移除**"所有任务完成即停止"的提前退出；
- 讨论**持续至 `max_rounds`**，每轮主持人继续追问，协作者持续交锋；
- 仅在达到最大轮次后，由主持人产出结构化最终总结（`_final_summary` 关闭 `allow_delegate`，强制只输出总结文本）。
- **已通过端到端测试验证**：3 Agent、max_rounds=3 的实跑中，`round` 事件集合为 `{1,2,3}`，主持人第 2–3 轮持续抛出质疑并追加追问任务，未在任务完成的第 1 轮提前结束（详见 §17）。

### 10.7 任务与变量
- `_create_task(d)`：生成任务 `{id, recipient, content, expected_outcome, var_name, completed, rounds_waiting}`，并在 `shared_context[var_name]` 登记"由主持人提供、待回填"。
- `_apply_report(task, report)`：把 `var_value` 回填到变量、标记完成，并把结果以 `importance=0.85` 写入该 Agent **长期记忆**。
- 这实现了"主 Agent 提供任务接收方变量 → 协作 Agent 回填 → 反馈给分派方"。

### 10.8 记忆管家 `_memory_housekeeping()`
每轮结束对每个 Agent：
1. `promote_short_to_long(threshold=0.7)`（短期→长期）；
2. 偶数轮 `summarize_short_to_context()`（短期→情境）；
3. `forget()`（应用遗忘策略）。

### 10.9 导出 `export_markdown()`
生成结构化 Markdown：主题/轮次/时间/参与 Agent → 发言记录（含最终总结）→ 任务结果表 → 各 Agent 记忆快照。后端在 `status=done` 时自动落盘 `data/discussions/`。

---

## 11. 后端 — `app/main.py`

FastAPI 应用，挂载 `/static`，提供 REST + WebSocket。

### 11.1 启动钩子 `@app.on_event("startup")`
订阅 `event_bus`，监听 `status=done` → 自动 `export_markdown()` 保存到 `data/discussions/`。

### 11.2 REST 接口
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 返回 `index.html` |
| GET | `/api/defaults` | 返回供应商预设、向量库后端、最大 Agent 数 |
| POST | `/api/configure` | 配置 Agent（1–6 个）、主题、最大轮次 |
| POST | `/api/ingest` | 向共享向量库注入知识（按段落切分） |
| POST | `/api/start` | 启动讨论（`asyncio.create_task(run_discussion)`） |
| POST | `/api/pause` `/api/resume` `/api/end` | 暂停/继续/结束 |
| GET | `/api/status` | 当前状态、轮次、Agent、任务、共享上下文 |
| GET | `/api/export` | 返回 Markdown 文本 |
| POST | `/api/save_markdown` | 保存 Markdown 到服务器 |

### 11.3 WebSocket `/ws`
- 连接即推送当前 `status`（含 agents/topic/max_rounds），便于前端初始化席位；
- 订阅 `event_bus`，把每个事件 `send_json` 给前端；
- 断开时从订阅列表移除；前端 `onclose` 每 2 秒自动重连。

---

## 12. 前端 — `static/`

### 12.1 页面结构 `index.html`
- **配置区 `#setup`**：Agent 数量（2–6）、每个 Agent 卡片（名称/角色/性格/模型供应商 + 自定义字段）、主题、最大轮次、知识注入、开始按钮。
- **讨论区 `#discussion`**：左侧圆桌（`#round-table`）含等分席位与中心信息；右侧控制（暂停/继续/结束/返回）+ 任务接收方变量面板 + 任务进度面板；下方实时发言流；结束栏（导出/下载）。

### 12.2 样式 `style.css`
深色游戏化主题：圆桌为径向渐变圆形，席位按绝对定位分布，发言时 `.speaking` 高亮放大；主 Agent 用皇冠头像与粉色描边区分。

### 12.3 逻辑 `app.js`
- `init()`：拉取 `/api/defaults`、生成卡片、绑定事件、连接 WS。
- `generateAgentCards(n)`：按数量生成配置卡片，首个标记"主 Agent"；供应商切换 `onProviderChange()` 自动填充/隐藏自定义字段。
- `startDiscussion()`：组装 `agents` 配置 → `/api/configure` → 渲染席位 → 切换到讨论区 → `/api/start`。
- `renderSeats(agents)`：**席位等分算法**——`angle = (-90 + i*360/n)`，以圆心 50% 为圆心、半径 38% 用百分比定位，实现 2→2 等分、3→3 等分……依次类推。
- `connectWS()` / `handleEvent(ev)`：按事件类型更新 UI：
  - `status`：状态徽标 + 席位渲染 + 结束/暂停按钮显隐；
  - `round`：中心轮次；
  - `speech`：发言气泡 + 席位高亮；
  - `task_assigned` / `task_result`：任务卡片 + 变量回填面板（`state.tasks` / `state.ctx`）；
  - `summary`：总结气泡；`log`：系统日志。
- `exportMarkdown()` / `downloadMarkdown()`：拉取 `/api/export` 弹出/下载 Markdown。

---

## 13. 事件协议（WebSocket JSON）

| `type` | 关键字段 | 含义 |
| --- | --- | --- |
| `status` | `status, topic, max_rounds, agents` | 状态变化/初始化 |
| `round` | `round, max` | 进入新一轮 |
| `speech` | `speaker, text, is_main, round` | 发言（非流式回退时使用） |
| `speech_start` | `speaker, round, is_main` | 流式发言开始（前端建气泡 + 席位高亮） |
| `speech_delta` | `speaker, delta` | 流式文本增量（追加到当前气泡） |
| `speech_end` | `speaker, round, text` | 流式发言结束（以完整文本收尾） |
| `task_assigned` | `from, to, content, expected, var` | 任务分派 |
| `task_result` | `from, var, value, completed, message, task_id` | 任务回报/变量回填 |
| `summary` | `speaker, text, round` | 最终总结 |
| `log` | `text` | 系统日志 |

---

## 14. 容错与降级

| 环节 | 降级行为 |
| --- | --- |
| 嵌入模型加载失败 | 确定性哈希向量（维度 384），离线可用 |
| Qdrant 不可达 | 进程内内存向量库（仍对所有 Agent 共享） |
| 联网搜索（SerpApi）失败/未配 key | 工具返回友好提示，不崩溃 |
| LLM 调用异常 | `_agent_turn` 捕获并记日志，回合降级为空内容 |
| 协作 Agent 未回报 | 发言记录 + `rounds_waiting` 计数，超 3 轮强制标记完成 |

---

## 15. 需求实现对照

| # | 需求 | 实现位置 |
| --- | --- | --- |
| 1 | 读取 `.env` | `config.py` |
| 2 | 多 Agent 对话（2–6，可绑定 DeepSeek/Qwen） | `orchestrator.configure` + `llm.resolve_model_config` |
| 3 | 共享向量库 + 记忆（短期/长期/情境 + 转化 + 遗忘） | `vector_store.py` + `memory.py` |
| 4 | 主 Agent 只分派，协作者只回报结果、不反馈过程 | `DELEGATE_SCHEMA` / `REPORT_SCHEMA` + 提示词 |
| 5 | 每个 Agent 独立 MCP 工具对象 + 工具搜索 | `tools.MCPRegistry`（含 `search`） |
| 6 | 任务接收方变量（主提供、协作者回填） | `shared_context` + `_create_task`/`_apply_report` |
| 7 | 轮次未达上限则多轮辩论（主持人抛疑） | `run_discussion` 循环 + `_main_directive` |
| 8 | 角色/性格严格约束 | `agent.system_prompt` 角色约束段 |
| 9 | 圆桌游戏化 UI + 暂停/结束 + Markdown 保存 | `app/static/` + `app.main` 自动落盘 |
| 10 | 席位等分（2→2 等分…） | `app.renderSeats` 角度算法 |
| 11 | 角色/性格必填，否则按机器人 | `Agent` 默认值 |
| 12 | 最大轮次后由主持人总结收尾 | `_final_summary`（关闭 delegate） |

---

## 16. 典型调用时序（一次讨论）

```
浏览器
  └─ POST /api/configure  → orchestrator.configure()
  └─ POST /api/start      → asyncio.create_task(run_discussion())
                                ├─ 每轮: _main_directive → 主持人 _agent_turn
                                │        └─ 可能调用 delegate_task → _create_task
                                ├─ 协作 Agent _agent_turn (task/discussion)
                                │        └─ 可能调用 report_result → _apply_report
                                ├─ _memory_housekeeping()
                                └─ 达 max_rounds → _final_summary()
  WebSocket /ws
  └─ 实时推送 status/round/speech/task_*/summary 事件 → 前端渲染
  status=done → 后端自动保存 data/discussions/*.md
```

---

## 17. 端到端验证记录（E2E）

为印证上文逻辑，使用 `tests/test_e2e.py`（配置 → 注入 → 开始 → WebSocket 收事件 → 导出）对真实运行做了一次验证。被测配置：3 个 Agent（主持人=主，技术派/人文派=协作）、`max_rounds=3`、共享向量库后端为 `in-memory`。观察到的事实：

- **配置与注入**：`/api/configure` 成功创建 3 个 Agent；`/api/ingest` 写入 2 段知识，返回 `backend=in-memory`。
- **多轮辩论（修复"第1轮即停"后的实证）**：`run_discussion` 实际走满 `max_rounds=3`，`round` 事件集合为 `{1,2,3}`：
  - 第 1 轮：主持人用 `delegate_task` 分派 `task1_technical_outcome` / `task2_humanitarian_update`，协作 Agent 以 `report_result` 回填（状态 ✅）。
  - 第 2 轮：主持人引用第 1 轮结果，抛出"技术可靠 ≠ 社会可用"质疑、给出新加坡反例，并追加追问任务。
  - 第 3 轮：协作 Agent 围绕"推广节奏 / 伦理底线"继续交锋。
- **收尾**：`status=done`（round=3 / max=3），`_final_summary` 产出结构化最终总结；`/api/export` 返回约 29KB Markdown，含发言记录、任务结果表、各 Agent 记忆快照。
- **WebSocket**：实时推送 `status / round / speech / task_assigned / task_result / summary`，前端可据此渲染圆桌与任务面板。

> 该验证同时覆盖了 §14 的降级路径（本机无 Qdrant → 自动降级 `in-memory`；无 GPU/网络环境下嵌入降级为确定性哈希向量）。

---

*文档覆盖 `app/config / app/llm / app/embeddings / app/vector_store / app/memory / app/tools / app/agent / app/orchestrator / app/main / app/static` 全部模块，可作为二次开发与维护的权威参考。*
