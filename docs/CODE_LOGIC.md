# 多 Agent 圆桌辩论系统 — 代码逻辑文档

> 本文档系统梳理当前项目的全部代码逻辑，按"模块职责 → 关键函数 → 数据流"组织，便于理解与二次开发。
> 内容对齐《多Agent辩论系统架构设计文档》（Supervisor 编排 + 立场辩手 + Judge 裁决 + RAG 核查 + 公平性治理 + 收敛检测 + 状态机 + 协议层）与当前实现。
> **交叉引用约定**：`架构文档 §N` 指《多Agent辩论系统架构设计文档》；**裸 `§N.M`**（如 §1.3 / §3.4 / §5.4 / §5.8，与代码注释一致）指《问题整理与根因分析.md》中对应小节；本文档自身章节用 `§N` 标题，不含 `.M` 后缀。

---

## 1. 项目概述

一个支持多个 LLM Agent 围绕"圆桌"进行**辩论式多轮协作讨论**的 Web 应用。核心特征：

- **多会话隔离**：`DebateManager`（`app/manager.py`）按 `session_id` 管理独立 `Orchestrator` 实例，不同浏览器客户端互不干扰；元数据落盘 `data/sessions/{sid}.json`，进程重启后遗留会话标记为 `interrupted` 并展示中断横幅。
- 每个 Agent 拥有**独立的基础模型**（Qwen / DeepSeek / MiMo / 自定义，默认取 `.env` 的 `LLM_*`，当前为 MiMo）、**立场**（主持人 HOST / 正方 PRO / 反方 CON / 中立 NEUTRAL）、**记忆系统**（短期 / 长期 / 情境 + 遗忘）、**MCP 工具集**（知识检索 / 联网搜索 / 记忆回忆）。
- 首个 Agent 为**辩论主持人（Supervisor 视角）**：分派任务、逐轮追问、最终汇总；辩论结束后由**裁判（Judge）**单次裁决，按内容四维（logic / evidence / rebuttal / clarity）对**每位辩手**独立评分，**胜方由内容四维均分决定（纯内容判定）**；系统实测的模型质量分（TTFT / 生成速率）与内容分合成综合评分（`composite_scores`）**仅作展示、不参与胜负**。最终报告为**结构化结论（四段式）**：核心结论 / 各方立场 / 裁判裁决 / 分歧与待办。
- 辩论流程：第 1 轮**并行立论**（`asyncio.gather`），后续轮次**串行反驳/辩护**；达到终止条件（最大轮次 / 语义收敛 / 手动结束）后进入裁决与汇总。全局 Token 预算已移除，改为 **FairnessGovernor L1–L6** 公平性治理（以字符数 + 相对比例为度量），token 用量与**费用估算（¥）**仅作只读遥测。
- 提供**简约大气**的圆桌 UI（席位按数量等分）、**聊天气泡式发言**（并发流式按 speaker 独立气泡）、**工具调用可视化**（气泡内显示"正在调用工具 X…"）、**首字母文字头像**（按立场配色）、暂停 / 继续 / 结束、Markdown 结果导出。
- **历史记忆化检索**：每场讨论完成或保存时，题目 + 元信息自动写入 `data/discussions/index.json`，前端历史面板支持关键词搜索与一键查看 / 下载。
- **可观测性**：每次讨论生成 `trace_id`，贯穿事件流、状态同步与导出的 Markdown。

---

## 2. 目录结构与职责

```
mini_test/
├── start.py              启动入口（uvicorn）
├── requirements.txt      依赖清单（含版本下限约束）
├── requirements-dev.txt  开发 / 质量工具依赖（ruff / mypy / coverage）
├── pyproject.toml        工程化配置（ruff / mypy / coverage）
├── .env                  环境变量配置（不提交到版本库）
├── app/                  核心应用包
│   ├── config.py         环境变量与配置（Settings 单例、公平性/收敛/重试参数）
│   ├── protocol.py       消息协议（TaskDispatch / ArgumentSubmit / Rebuttal / Judgment / StatusSync / EvidenceItem，pydantic 模型 + trace_id/uuid/毫秒时间戳）
│   ├── schemas.py        REST 请求体 pydantic 模型（ConfigureRequest / StartRequest / IngestRequest / SaveMarkdownRequest）
│   ├── llm.py            LLM 统一接入（OpenAI 兼容 + function calling + 流式 + usage 统计 + ThinkFilter）
│   ├── embeddings.py     文本向量化（sentence-transformers 懒加载，失败降级哈希向量，后台预热）
│   ├── vector_store.py   共享向量知识库（Qdrant 快速失败，降级内存库；懒初始化）
│   ├── memory.py         每个 Agent 的记忆系统（三类记忆 + 转化 + 遗忘）
│   ├── tools.py          每个 Agent 的 MCP 工具注册表 + 内置工具
│   ├── rag.py            独立 RAG 事实核查服务（断言提取 / 带置信度检索 / 24h 缓存）
│   ├── agent.py          Agent 定义（角色/性格/立场/模型/记忆/MCP/上下文构建）
│   ├── fairness.py       公平性治理（FairnessGovernor L1–L6：输出上限/有效比例/输入对称/轮次对称/采样对齐/长度中性）
│   ├── orchestrator.py   编排器（Supervisor：状态机、并行立论、收敛终止、Judge、任务回合/辩论回合分离、结构化结论）
│   ├── manager.py        多会话管理器（DebateManager：session 注册表 + 元数据持久化 + 中断恢复；会话统一经 get_or_create 持有）
│   ├── bgtasks.py        后台 asyncio 任务强引用集合（RUF006；main/orchestrator 统一复用 spawn()，防 GC 提前回收）
│   ├── history.py        历史讨论记忆索引（data/discussions/index.json + 检索 + Markdown 读取，防路径穿越）
│   ├── file_parser.py    上传文件文本提取（txt/md/docx/xlsx/pdf/图片 OCR，懒加载依赖 + 大小/字符上限）
│   ├── main.py           FastAPI 应用（REST + WebSocket + 静态服务，lifespan 自动落盘 + 重试 + 中断标记）
│   └── static/           前端 UI
│       ├── index.html    页面骨架（配置区 + 讨论区 + 历史面板 + 核心结论面板 + 模板区）
│       ├── style.css     设计变量 / 圆桌席位 / 聊天气泡 / 首字母头像 / 历史面板
│       └── app.js        前端逻辑（配置、席位等分、WebSocket 事件、并发流式气泡、历史检索、模板、工具可视化、结论面板）
├── scripts/
│   └── eval_baseline.py  质量基线评估脚本（五维评分 0-100 + 基线对比，P0-2）
├── tests/
│   ├── test_e2e.py       端到端集成测试（需先启动服务器；校验状态机/公平性/裁判/流式）
│   ├── test_ci_fast.py   快速单元测试（状态机/公平性/裁决，无需服务器）
│   ├── test_api_flow.py  HTTP 接口级测试（ASGITransport + LLM 打桩，无需服务器/真实模型）
│   ├── test_multi_session.py  多会话隔离测试（DebateManager / 会话元数据 / 中断恢复）
│   └── test_features_v2.py    新特性测试（费用估算 / 收敛降级 / 工具可视化 / 结构化结论 / 论据句抽取 / 任务-辩论分离）
├── docs/
│   └── CODE_LOGIC.md     本文档
├── data/                 运行时数据（自动生成，不提交到版本库）
│   ├── discussions/      自动保存的 Markdown 结果
│   └── memories/         各 Agent 记忆持久化（json）
└── README.md             使用说明
```

---

## 3. 运行方式

```bash
conda activate agent_env
python start.py
# 浏览器打开 http://localhost:8000
```

**无外网环境**：本地嵌入模型（sentence-transformers）首次加载会尝试从 HuggingFace 下载，网络不通会卡住启动。此时加离线变量强制走确定性哈希向量兜底：

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python start.py
```

启动后 `start.py` 通过 uvicorn 加载 `app.main` 的 FastAPI 应用，**默认监听 `127.0.0.1:8000`**（仅本地访问，避免远端暴露 / 长期挂载；需要开放局域网时以 `HOST=0.0.0.0` 环境变量显式覆盖），自动挂载 `/static`、REST 接口与 WebSocket `/ws`。

---

## 4. 配置层 — `config.py`

**职责**：从项目根目录 `.env` 读取配置。针对本仓库 `.env` 存在行首空格、值带引号等不规范写法，采用**手写解析器** `_parse_dotenv()`（不依赖 python-dotenv），逻辑：

1. 逐行 `strip()`，跳过空行与 `#` 注释；
2. 按首个 `=` 切分 key/value，key 再 `strip()`；
3. 若 value 首尾为成对的 `'` 或 `"`，去掉引号；
4. 结果存入 `_raw` 字典。

随后通过 `_get()` / `_int()` / `_float()` 构建 `Settings` 类，暴露关键字段：

| 类别 | 字段 |
| --- | --- |
| LLM 默认 | `LLM_MODEL_ID`、`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_TIMEOUT`（默认模型 MiMo 生成较慢，放宽到 300s） |
| Qwen / DashScope | `QWEN_API_KEY` / `QWEN_BASE_URL`（compatible-mode）/ `QWEN_MODEL_ID`（默认 `qwen3.7-plus`） |
| DeepSeek | `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL_ID`（默认 `deepseek-chat`） |
| MiMo | `MIMO_API_KEY` / `MIMO_BASE_URL`（`/v1` 前缀，SDK 自动拼接 `/chat/completions`）/ `MIMO_MODEL_ID`（默认 `mimo-v2.5`） |
| **独立裁判** | `JUDGE_NAME` / `JUDGE_PROVIDER` / `JUDGE_BASE_URL` / `JUDGE_API_KEY` / `JUDGE_MODEL_ID`；**综合评分权重** `JUDGE_CONTENT_WEIGHT`（默认 0.8）/ `JUDGE_PERFORMANCE_WEIGHT`（默认 0.2） |
| 搜索 | `TAVILY_API_KEY`、`SERPAPI_API_KEY` |
| Qdrant | `QDRANT_URL`、`QDRANT_API_KEY`、`QDRANT_COLLECTION`、`QDRANT_VECTOR_SIZE`、`QDRANT_TIMEOUT`（短超时快速失败）等 |
| 嵌入 | `EMBED_MODEL_TYPE`、`EMBED_MODEL_NAME`、`EMBED_API_KEY` |
| Neo4j | `NEO4J_*`（保留，当前未在核心链路使用） |
| **公平性治理** | `FAIR_OUTPUT_RATIO`（相对上限比例，默认 1.5）、`FAIR_HARD_CHAR_CEILING`（单次发言硬性字符上限，默认 3000）、`FAIR_FLOOR_CHAR_LIMIT`（相对基准下限，默认 400）、`FAIR_OPENING_CHAR_LIMIT`（立论上限，默认 1200）、`FAIR_REFERENCE_ROUNDS`（参照对手最近 N 轮，默认 2）、`FAIR_THINK_RATIO_MAX`（思考占比上限，默认 0.5）、`FAIR_MAX_SEEN_DIFF`（可见对手轮数最大差值，默认 1）、`FAIR_TEMPERATURE`/`FAIR_TOP_P`（辩论采样参数，默认 0.7/1.0）、`FAIR_JUDGE_TEMPERATURE`/`FAIR_JUDGE_TOP_P`（辅助采样参数，默认 0.3/1.0） |
| **收敛检测** | `CONVERGENCE_SIMILARITY`（默认 0.85）、`CONVERGENCE_ROUNDS`（默认 2）、`CONVERGENCE_PER_SPEAKER`（默认 1，按发言者逐人算相似度再取平均，§5.7）；**降级阈值** `CONVERGENCE_TEXT_SIMILARITY`（默认 0.6，嵌入不可用时走 difflib 文本相似度） |
| **费用估算（P1-6）** | `COST_INPUT_PER_1M`（默认 2.0）/ `COST_OUTPUT_PER_1M`（默认 8.0），人民币 ¥ / 百万 token，只读遥测 |
| **LLM 重试** | `LLM_RETRY_ATTEMPTS`（默认 3）、`LLM_RETRY_DELAYS`（(1.0, 3.0)） |
| **流式超时** | `STREAM_TIMEOUT`（默认 300s，对齐 `LLM_TIMEOUT`） |
| **辩论对手/输出约束** | `OPPONENT_SNIPPET_MAX_CHARS`（第 2 轮对手发言原文截断上限，默认 500 字）、`OPPONENT_SUMMARY_MAX_CHARS`（第 3 轮起对手发言 LLM 摘要上限，默认 200 字）、`OPPONENT_ARGUMENT_SNIPPET_MAX_CHARS`（摘要轮额外保留的对手**关键论据句**上限，默认 160 字，§5.4）；输出长度约束走公平性 L1 字符上限（见上） |
| **记忆压缩** | `MEMORY_SUMMARY_MAX_CHARS`（短期 → 情境的 LLM 聚合摘要上限，默认 200 字） |

> `PROJECT_ROOT = Path(__file__).resolve().parent.parent`（`app/` 的上一级）供其余模块定位项目根目录与资源目录。

**模块级辅助（§1.4 / §3.4）**：
- `resolve_provider(prefix, provider="", base_url="", api_key="", model_id="")`：**provider 规格统一合并入口**——显式字段为空时依次回退 `settings.{PREFIX}_PROVIDER/_BASE_URL/_API_KEY/_MODEL_ID`，返回过滤掉空值的 spec dict（全部为空返回空 dict，调用方据此判断「未配置」）。只做合并不解析预设，供应商预设解析仍由 `app.llm.resolve_model_config` 负责；`_build_judge` 以 `resolve_provider("JUDGE", ...)` 收敛裁判配置（§12.2）。
- `OPPONENT_STANCE = {"PRO": "CON", "CON": "PRO"}`：立场对手映射，收敛到 `config.py` 模块级常量，由 `orchestrator._find_opponent` 与 `app/fairness.py` 共享，避免重复定义漂移（§3.4）。

---

## 5. LLM 接入层 — `llm.py`

**职责**：屏蔽不同厂商差异，统一为 OpenAI 兼容协议调用，支持 function calling、流式输出、usage 统计与 think 片段剥离。

### 5.1 供应商预设 `PROVIDER_PRESETS`
每个预设对应 `.env` 中**独立的环境变量块**（模型可热切换，互不干扰）：
- `qwen`：`QWEN_*`（DashScope compatible-mode，`qwen3.7-plus`）。
- `deepseek`：`DEEPSEEK_*`（`https://api.deepseek.com`，模型 `deepseek-chat`）；**注意不能用 `LLM_API_KEY` 兜底**（那是默认模型的 key，请求 DeepSeek 必然 401 且误导性强）。
- `mimo`：`MIMO_*`（`https://api.xiaomimimo.com/v1`，模型 `mimo-v2.5`；BASE_URL 只写到 `/v1`，SDK 自动拼接 `/chat/completions`）。
- `default`：`LLM_*`（默认 MiMo `mimo-v2.5`；`_normalize_base_url` 兜底剥离尾部 `/chat/completions`）。
- 未知 provider 回退 `default`。

### 5.2 `resolve_model_config(provider, base_url, api_key, model_id)`
- `provider == "custom"`：`base_url` / `api_key` / `model_id` 任一为空抛 `ValueError`，避免用错误端点/凭据请求。
- 预置提供商：API Key 为空同样提前报错，提示去 `.env` 补充。
- 返回 `{"base_url","api_key","model_id"}`。

### 5.3 `chat(messages, model_config, tools, tool_choice, temperature)`
- 返回字典：
  ```python
  {
    "content": str,            # 文本回复（已用 _strip_think 去除 <think>...</think>）
    "tool_calls": [{"id","name","arguments"}],   # 结构化工具调用
    "assistant_message": {...} # 可直接回传 API 的 assistant 消息（含 tool_calls），维持多轮工具调用
    "usage": {...}             # prompt_tokens / completion_tokens / total_tokens（缺失时 _estimate_usage 估算）
  }
  ```
- `_usage_from_response()` 从 OpenAI 兼容响应提取 usage；流式也尽量在最后 chunk 捕获。

### 5.4 流式输出 `chat_stream(messages, model_config, tools, ...)`
- 生成器，逐 chunk 消费，`yield` 事件：
  - `{"type":"content","delta":str}`：文本增量（已剥离 think 片段）；
  - `{"type":"tool_call_args","index":int,"delta":str}`：工具参数增量（按 index 累积）；
  - `{"type":"final","result":{...}}`：流结束，携带完整结果（结构同 `chat()`）。
- `ThinkFilter` 类处理**跨 chunk 边界**的 `<think>`/`</think>` 剥离：维护短缓冲，当尾部是 `<think>` 前缀时暂存等待后续 chunk，保证流式结果与非流式 `_strip_think` 一致。

### 5.5 摘要工具 `summarize_text(model_config, text, max_chars)`
共享的 LLM 压缩工具，供**对手发言摘要**与**历史强压缩**复用：
- `asyncio.to_thread` 调用同步 `chat()`，要求输出 ≤ `max_chars` 字的浓缩摘要，返回 `(summary, usage|None)`（usage 由编排器计入**只读遥测** `spent`）。
- 调用失败 / 返回为空 / 超长时回退 `_fallback_truncate(text, max_chars)`：确定性保留**头部 + 尾部**片段（以 `…` 相连），保证绝不超过 `max_chars`、不因摘要失败抛异常。
- 短文本（≤ max_chars）原样返回，不做无谓调用。

---

## 6. 嵌入与向量库

### 6.1 `embeddings.py`
- `_load_model()` **懒加载** `sentence-transformers` 模型（全局单例，`local_files_only=True`），维度取自模型本身。
- 加载失败（无网络/无依赖）时降级 `_fallback_embed()`：以 `hash(text)` 为随机种子生成**确定性**单位向量（维度取 `QDRANT_VECTOR_SIZE`），保证同一文本向量一致、项目离线可用。
- `start_background_warmup()`：后台线程预热模型，不阻塞 ASGI 启动。
- 对外接口：`embed(texts)`、`embed_query(text)`、`dim()`。

### 6.2 `vector_store.py` — 共享向量库
- 所有 Agent **共用同一个** `shared_vector_store` 单例，满足"访问同一向量数据库"。
- `SharedVectorStore` 构造时 `_try_qdrant()`（`QDRANT_TIMEOUT=3` 短超时快速失败）：
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
- `summarize_short_to_context(summarizer=None)`：**短期 → 情境** 的压缩（`async`；支持同步/异步 `summarizer(texts)->str`，失败回退拼接），压缩后清空短期缓冲。编排器在记忆管家（§12.6 第 4 步）偶数轮传入 LLM 摘要器（`summarize_text` + `MEMORY_SUMMARY_MAX_CHARS`），摘要 usage 计入只读遥测。
- 长期 ⇄ 情境：检索时合并长期事实与情境摘要，互相补充。

### 7.3 遗忘体系（每 Agent 可独立配置 `forget_config`）
- `_effective_importance(item) = importance × (1 - decay_per_day)^age_days × (1 + 0.05×(access-1))` —— 基于**时间衰减 + 访问频次**的"有效重要性"。
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
  - `search(query, top_k)`：按描述**关键词检索"该用哪个工具"**
  - `call(name, arguments)`：执行工具
  - `names()`：已注册工具名

### 8.2 内置工具（`build_default_tools`）
| 工具 | 说明 | 实现 |
| --- | --- | --- |
| `knowledge_search` | 检索共享向量库 | `_knowledge_search` → `shared_vector_store.search` |
| `web_search` | 联网搜索（SerpApi） | `_web_search`；未配置 key 或失败均友好提示 |
| `memory_recall` | 回忆本 Agent 记忆 | `_memory_recall` → `memory.context_text` |

> 任务分派/回报不在此注册，而是由 `orchestrator` 通过专用 schema（`DELEGATE_SCHEMA` / `REPORT_SCHEMA`）拦截处理。

---

## 9. 协议层 — `protocol.py`（架构文档 §3）

用 pydantic 定义 Agent 间通信的消息模型，统一字段约定：

| 模型 | 对应消息 | 关键字段 |
| --- | --- | --- |
| `TaskDispatch` | 任务分发（Supervisor → 辩手） | `task_id`(UUID)、`topic`、`stance`、`round`、`budget_token`(协议保留字段，仅审计/遥测)、`timestamp`(Unix 毫秒) |
| `ArgumentSubmit` | 论点提交（辩手 → Supervisor） | `arg_id`、`agent_id`、`content`、`evidence_refs`、`confidence`、`round`、`token_cost` |
| `Rebuttal` | 反驳消息 | `target_arg_id`、`rebuttal_content`、`logic_flaws`、`counter_evidence` |
| `Judgment` | 裁决消息（Judge → Supervisor） | `scores`(后端逐维均值，确定性计算)、`agent_scores`(按辩手)、`model_performance`(TTFT/速率/质量分)、`composite_scores`(综合评分，仅展示)、`winner`(PRO/CON/DRAW，纯内容判定)、`critique`、`convergence_flag` |
| `StatusSync` | 状态同步（心跳/轮次广播） | `trace_id`、`phase`(idle/preparing/debating/judging/finished)、`current_round`、`agent_states`、`spent_tokens`(只读遥测)、`output_tokens`(只读遥测)、`fairness`(治理报告)、`elapsed_time_ms` |
| `EvidenceItem` | RAG 证据片段 | `evidence_id`、`source`、`content`、`confidence`、`url` |

辅助函数：`new_trace_id()`（12 位十六进制）、`new_uuid()`、`now_ms()`。

> 当前编排器在事件发布与任务管理中使用这些字段（task_id=UUID、timestamp=毫秒、token_cost），协议模型为后续"序列化落库 / 走消息队列"打基础。

---

## 10. Agent 定义 — `agent.py`

`Agent` 封装单体的全部状态：

```python
Agent(agent_id, name, role, personality, model_config, is_main, forget_config, stance)
```
- `role` / `personality` 缺省 → "机器人"角色。
- `stance`：`HOST` / `PRO` / `CON` / `NEUTRAL`，`system_prompt` 中显式声明立场（架构文档 §2.2/2.3）。
- `memory = MemorySystem(...)`，`mcp = build_default_tools(name, memory)`。
- `history`：最近对话（不含 system），上限 `max_history=24`。

### 10.1 `system_prompt(...)` —— 提示词构建
按固定顺序拼接：
1. 身份（名称/角色/性格）；
2. **角色与性格约束**：必须严格符合人设，不得声明自己是 AI；
3. **讨论形式**：辩论式多轮协作说明；
4. **立场约束**（`_stance_declaration`）：
   - PRO：必须坚定维护正方，禁止倒戈/中立/折中，反驳须指出逻辑谬误；
   - CON：必须坚定维护反方，直接回应对方核心论据，优先逻辑归谬、其次事实反证；
   - HOST：保持中立公正，引导辩论不亲自立论；
   - NEUTRAL：不作强制声明。
5. **记忆**（`memory.context_text`）；
6. **共享上下文 / 任务接收方变量**；
7. **当前任务状态**（`_task_status_text`）；
8. **模式指令**：
   - 主 Agent：主持辩论、第1轮分派、之后轮次追问、最后总结；仅用 `delegate_task` 分派，不亲自执行；
   - 协作 Agent（task 模式）：只通过 `report_result` 返回**最终结果 + 是否完成**；
   - 协作 Agent（discussion 模式）：针对他人观点辩护/反驳，可调用 `knowledge_search` 检索论据。

### 10.2 历史轮次标记与按轮强压缩（长对话管理）
`history` 的**平行列表** `history_rounds`：每条历史消息记录所属辩论轮次（`append_history(role, content, round_no)`，缺省取当前最大轮次）。

- **三处裁剪必须同步**：① 条数上限（`max_history` 滚动窗口）；② 字符硬上限（`CONTEXT_MAX_CHARS`，超限丢最旧轮次）；③ 压缩截断。三处均同时裁剪 `history` 与 `history_rounds`，否则「按轮保留」会错位。
- `compress_history(force, keep_rounds=None)`：`keep_rounds=None` 走优化 A9 原逻辑（保留最近 `CONTEXT_COMPRESS_KEEP_RECENT` 条，`CONTEXT_COMPRESS_THRESHOLD` 触发，由 `_memory_housekeeping` 每轮边界调用）；`keep_rounds=N` 委托 `_compress_keep_rounds(N)`（按轮强压缩，能力保留，不再由预算触发）。
- `_compress_keep_rounds(keep_rounds)`：**从尾部扫描最近 N 个不同轮次**的起始下标 `cut`，`history[:cut]` 全部经 `_summarize`（`summarize_text`，失败回退确定性截断）压入 `compressed_summary`（与既有摘要拼接），保留 `history[cut:]` 原文；不同轮次数 ≤ N 时无需压缩返回 `None`。
- `reset_history()` 同时清空 `history_rounds`。

---

## 11. RAG 事实核查服务 — `rag.py`（架构文档 §2.5）

独立于辩手的检索服务，`rag_service = RAGService()` 为全局单例。

### 11.1 `extract_claims(text, max_claims=5)`
按中英文句号/分号切分文本，过滤过短（<8）或过长（>200）片段，输出候选事实断言。

### 11.2 `check(text, top_k=3)`
对一段辩论文本做事实核查：
1. `extract_claims` 提取断言；
2. 逐条 `_search_cached(claim, top_k)` 检索共享向量库；
3. 过滤低于 `min_confidence` 的证据，按 `source|content[:40]` 去重（保留更高置信度）；
4. 按置信度降序返回证据列表 `[EvidenceItem]`。

### 11.3 `_search_cached`（24h 缓存，架构文档 §6.4）
- Key = `rag:abs(hash(query))`，带锁 + TTL（24h）。
- 检索命中转换为 `EvidenceItem`：`_score` 存在则作为置信度，否则 `_score_of` 给近似值。

### 11.4 `evidence_text(items)`
把证据格式化为可注入 prompt 的文本（高/低可信标记 + 来源 + 内容），并提示"不得编造未给出的数据"。

> 编排器在每轮辩论结束后异步调用 `rag_service.check` 核查本轮发言，证据注入下一轮上下文（`_evidence_for_round`）。

---

## 12. 编排器 — `orchestrator.py`（核心，Supervisor 实现）

### 12.1 全局结构
- `DELEGATE_SCHEMA` / `REPORT_SCHEMA`：任务分派/回报的 function-calling 定义。
- `STANCE_LABEL`：立场显示名；`PHASES = ("idle", "preparing", "debating", "judging", "finished")` 架构状态机（§4.1）。
- `EventBus`：基于 `asyncio.Queue` 的发布/订阅。
- `Orchestrator` 实例统一由 `DebateManager` 注册表持有（§12.15 / §1.3 解耦：**不再存在模块级 `orchestrator` 单例**，测试与入口均走 `manager.get_or_create("default")`）。关键状态：
  - `agents / main / collaborators`、`shared_context`、`tasks`、`round / max_rounds`、`topic`；
  - `status`（idle/running/paused/done/stopped，UI 状态）与 `phase`（idle/preparing/debating/judging/finished，**架构状态机**）；
  - `trace_id`、`spent / output_tokens`（token 用量，**只读遥测**，不再做约束）、`governor`（FairnessGovernor 公平性治理器，每场全新）、`convergence_streak`、`round_arguments / latest_arguments / round_evidence`（RAG 证据按立场分桶，§12.11）、`judgment`、`_pause_event`。

### 12.2 配置 `configure()`
- 校验 status 非 running；重置全部状态并生成新 `trace_id`。
- 逐个 `resolve_model_config` 构造 `Agent`；**立场**：显式指定优先，否则主位=HOST、其余按 PRO/CON 交替（i 为奇数为 PRO，偶数为 CON）。
- `_build_judge`：`resolve_provider("JUDGE", ...)` 合并前端显式字段与 `.env` 的 `JUDGE_*` 默认值（§1.4），再交 `resolve_model_config` 解析预设；完全未配置返回 `None`（`_judge_round` 回退主 Agent 模型）。裁判不进入 `agents / collaborators`，不占席位、不参与辩论、不接收任务。
- 首个为 `main`，其余进 `collaborators`。

### 12.3 控制 `pause() / resume() / end()`
基于 `asyncio.Event`：暂停 `clear()`，主循环每轮 `await _pause_event.wait()`；`end()` 置 `stopped` 并 `set()`。

### 12.4 单 Agent 回合 `_agent_turn(agent, user_msg, mode, allow_delegate, stream)`
封装"工具调用循环"（上限 5 次）：
1. 构建 `messages = [system] + history + [user]`；`system_prompt` 内部会做记忆检索（`memory.context_text`），经 `asyncio.to_thread` 卸载到线程执行，避免阻塞事件循环（§2.4）；
2. 工具集 = `mcp.schemas()`，主 Agent 追加 `DELEGATE_SCHEMA`（除非 `allow_delegate=False`），协作任务模式追加 `REPORT_SCHEMA`；
3. **流式**：`_consume_stream()` 在后台线程运行 `chat_stream`，通过 `asyncio.Queue` + `loop.call_soon_threadsafe` 把增量桥接回事件循环；整体受 `settings.STREAM_TIMEOUT` 约束；流式异常时**回退非流式重试调用**。**工具调用可视化（P1-4）**：流式期间把 `tool_call_start` / `tool_call_args` 事件转发前端（气泡显示"正在调用工具 X…"），`_consume_stream` 返回 `result["tool_starts"]`（已发过 `tool_call_start` 的工具索引集合）供调用方去重——`_agent_turn` 对未流式发出索引的工具补发 `tool_call_start`（同样先补 `speech_start` 保证纯工具回合气泡存在），已发出的不再重复发；
4. **性能实测**：流式结果携带 `metrics = {ttft_s, gen_s, chars}`（在 `_consume_stream` 的 `_drain` 内实测首 token 延迟与生成耗时），回合末尾按辩手聚合进 `agent_metrics`（calls / ttft_sum / gen_sum / chars），供裁判**模型质量评分**使用；**非流式回退不计入**；
5. **非流式**：`_run_llm_retry()` 对 429/5xx/超时做指数退避重试（`LLM_RETRY_DELAYS` = 1s、3s）；
6. 每次调用 `_apply_usage(result)` 累加 `spent` / `output_tokens`（只读遥测）；
7. 收到首个文本增量即推送 `speech_start`，随后逐个 `speech_delta`；
8. 工具调用循环：主 Agent `delegate_task` → 收集 `delegate`；协作 Agent `report_result` → 收集 `report`；普通工具 → `mcp.call` 并把 tool 消息回传，继续循环；
9. **输出字数约束（需求 5，公平性 L1）**：`mode=="discussion"` 时按 `ceiling = self.governor.ceiling_for(stance, round, 对手近N轮有效输出均值)` 得到字符上限（立论/无参照用 `opening_char_limit`），超限保留头尾截断（`truncate_output`，事件 `fairness.output_truncate`）——提示词字数要求失效时仍受事后硬截断兜底；不再把 `max_tokens` 下发给 API；
10. 回合结束若发生过流式发言，补发 `speech_end`（携带完整文本 + **最新 `output_tokens` 与 `cost_rmb` 费用估算**）并写入 transcript；
11. 把 user/assistant 写入 `history`（带 `round_no=self.round`，与 `history_rounds` 平行），返回 `{content, delegate, report, streamed, token_cost}`。

**`_consume_stream`**：后台线程 + `asyncio.Queue` 桥接，`wait_for` 整体超时（`STREAM_TIMEOUT`）；`_drain` 消费 chunk 时实测 `ttft_s`（首 token 延迟）/ `gen_s` / `chars`，随结果返回 `metrics`。
**`_run_llm_retry`**：`asyncio.to_thread(chat, ...)` 包装，指数退避重试。

### 12.5 主持人指令 `_main_directive(round, is_last, had_any_task, pending_exists)`
- **第 1 轮**：分派调研/立论任务（`delegate_task`），或先陈述开场立场。
- **第 2…N 轮**：基于上一轮发言与已回填结果，抛出疑问、要求论据、指出分歧，可追加追问任务；强制"至少提出一个有针对性的质疑"。
- 存在未完成任务时附加"请先审视进展"提示。

### 12.6 讨论主循环 `run_discussion()`
```
phase = preparing → status = running（发布 status 事件 + StatusSync）
while round < max_rounds:
    等待暂停/结束信号; 若 stopped 退出
    round += 1; phase = debating; 发布 round 事件 + StatusSync
    # 0) 公平性治理：发布 governor.report()（L1-L6 各层状态）作为日志（替代旧的预算规划）
    # 1) 主持人回合（分派 / 追问）→ _create_task + task_assigned 事件
    # 2) 协作 Agent 回合（§5.8 拆分，先任务后辩论，两者完全分离）：
    #    2a) 任务回合 _task_turn：只处理待办任务，结果回填 shared_context 并发布
    #        task_result 事件，不进入 round_arguments（不污染收敛/RAG/对手上下文）
    #    2b) 辩论回合 _debate_turn：
    #        第 1 轮 → asyncio.gather 并行立论（架构文档 §4.2/§6.2）
    #        后续轮 → 串行反驳/辩护（_prepare_opponent_context 注入对手上轮发言：
    #                  第 2 轮原文截断 / 第 3 轮起 LLM 摘要 + 关键论据句）
    # 3) RAG 事实核查（_rag_check_round，异步注入下一轮上下文）
    # 4) 记忆管家（_memory_housekeeping：提升/压缩/遗忘；调用 compress_history 按轮强压缩）
    # 5) 收敛检测（_check_convergence）→ 连续 N 轮相似且无新证据则 break
    # 6) 达到最大轮次 → break（公平性治理下无预算熔断；轮次对称性由 L4 保证）
# Judging：phase = judging；单次调用 Judge（_judge_round）
#   调用失败或无辩手可评时回退确定性 Judgment（DRAW + 说明，score 后端按内容均分计算）
# Finished：phase = finished；主持人汇总（_final_summary，一律用 LLM；_fallback_summary 作系统兜底）
#   status = done；发布 status + StatusSync
# 异常熔断：status = stopped，phase = idle
```

**并行立论**（架构文档 §6.2）：第 1 轮协作 Agent 无相互依赖，用 `asyncio.gather(..., return_exceptions=True)` 并发 `_debate_turn`；流式事件带 `speaker`，前端按 speaker 独立气泡，天然支持并发流式。

### 12.7 公平性治理 `FairnessGovernor`（`app/fairness.py`，替代旧 budget planner）

旧的全局 Token 预算系统（`app/budget.py` + 三路自适应校准 + `budget_profile.json` 档案）已整体移除：不再按预算推算每轮 `effective_max_tokens`、不再有预算熔断。`FairnessGovernor` 每场讨论全新实例（`run_discussion` 中创建），以 **L1–L6 六层公平性**替代"成本预算"约束，全程只保证模型间公平、不干预辩论进程：

- **L1 输出字数上限（需求 5）**：`ceiling_for(stance, round, opponent_recent_chars)` 计算相对上限（`对手近N轮有效输出均值 × FAIR_OUTPUT_RATIO` 与硬上限 `FAIR_HARD_CHAR_CEILING` 取 min）；立论/无参照用 `opening_char_limit`。上限仅写入提示词（`_debate_prompt` 的 `【输出要求】`），超限由 `_agent_turn` 事后 `truncate_output` 头尾截断兜底（事件 `fairness.output_truncate`）；**不再把 `max_tokens` 下发给 API**。
- **L2 有效回答占比**：剥离 `<think>` 推理后统计有效输出占比（`effective_ratio`），低于 `FAIR_THINK_RATIO_MAX` 阈值记告警。
- **L3 输入对称**：记录各方被注入的上下文规模（`context_chars`），超差 `FAIR_MAX_SEEN_DIFF` 记违约。
- **L4 轮次对称**：`record_turn` / `record_speech` 保证每模型每轮至多发言一次、发言次数均衡（`speech_counts`）。
- **L5 采样对齐**：`sampling_params()`（temperature/top_p）统一来自 `FAIR_TEMPERATURE` / `FAIR_TOP_P`；裁判侧 `judge_params()` 用 `FAIR_JUDGE_TEMPERATURE` / `FAIR_JUDGE_TOP_P`——所有模型同一套采样参数，消除随机性差异。
- **L6 长度中立裁判**：`judge_guidance()` 注入裁判 system prompt，要求"评分只依据内容质量，不受发言长短影响"。

**观测**：`governor.report()` 返回 `{ok, warnings, violations, layers:[{layer, name, status, detail}]}`，经 `_status_sync` 暴露为 `fairness` 字段；export_markdown 亦逐层打印。Token 用量（`spent` / `output_tokens`）仅由 `_apply_usage` 累加为**只读遥测**，不参与任何约束。

### 12.8 协作 Agent 回合与对手定向反驳（需求 1–5）

**`_task_turn(collab)`（任务回合，§5.8）**：
- 只处理该辩手的待办任务（无任务直接返回）：逐个 `_agent_turn(..., mode="task", stream=True)`；收到 `report` → `_apply_report` + 发布 `task_result`；未回报则 `rounds_waiting+1`，超 3 轮强制标记完成。
- **不调用 `_record_argument` / `_record_memory`**：任务输出仅回填 `shared_context` 并流式展示，不进入 `round_arguments`（避免污染收敛检测 / RAG 核查 / 对手上下文）也不进入辩论记忆（避免任务细节暴露到辩论发言）。

**`_debate_turn(collab)`（辩论回合，§5.8）**：立论/反驳发言，与任务回合完全分离；无论本轮回合是否处理过任务，辩手每轮都发言，保证严格交替。先 `await self._prepare_opponent_context(collab)` 准备对手上轮发言，再 `_debate_prompt(collab)` 构造立论/反驳 prompt（含对手引用 + 关键论据句 + 上一轮 RAG 证据注入 + 字数提示），`_agent_turn(..., mode="discussion", stream=True)`。

**对手定向（需求 2/3）**：
- `_find_opponent(collab)`：`OPPONENT_STANCE` 定义收敛于 `app.config`（§3.4，`orchestrator` 与 `fairness` 共享）。先在上一轮 `round_arguments[round-1]` 找对立场**最近发言人**（`reversed` 扫描）；上一轮无对立场发言者时回退 `latest_arguments` 全局最近对立场发言者；`NEUTRAL`/`HOST` 无固定对手返回 `None`。
- `_prepare_opponent_context(collab)`：第 1 轮不准备；**第 2 轮**取对手上轮发言原文截断到 `OPPONENT_SNIPPET_MAX_CHARS` 字；**第 3 轮起**调用 `summarize_text`（`llm.py`）压缩到 `OPPONENT_SUMMARY_MAX_CHARS` 字，按 `(对手名, 轮次)` 缓存进 `_opponent_summary_cache` 防同轮重复调用，usage 计入 `self.spent`（只读遥测），事件 `debate.opponent_summary`；第 3 轮起额外以 `_extract_argument_sentences(text, OPPONENT_ARGUMENT_SNIPPET_MAX_CHARS)` 抽取对手发言中的**关键论据句**写入 `_opponent_prep["arguments"]`（§5.4，弥补 LLM 摘要丢失具体论点的不足）；结果写入 `_opponent_prep`（每轮开始清空，`configure()` 重置）。
- `_debate_prompt(collab)`：第 1 轮基于角色/性格立论（需求 1，PRO/CON 分立场措辞）；第 2 轮起优先注入 `_opponent_prep`（标注"发言"（第 2 轮原文）/ "摘要"（第 3 轮起）），摘要轮若含 `arguments` 则追加指令「对方发言中的关键论据句（请逐条回应，不要遗漏）」（§5.4）；`NEUTRAL`/无立场对手回退旧 `last_debate_speaker` 逻辑；**全轮**追加 `【输出要求】... {limit} 字以内 ...`（需求 5），`limit = self.governor.ceiling_for(stance, round, 对手近N轮有效输出均值)`（公平性 L1 相对上限，与硬上限取 min；仅作提示约束，超限由 `_agent_turn` 事后截断兜底，不再把 `max_tokens` 下发 API）；RAG 证据按调用者立场取桶注入（`_evidence_for_round(round-1, collab.stance)`，§12.11）。

**论据句抽取 `_extract_argument_sentences(text, max_chars)`（模块级函数，§5.4）**：按 `。！？!?;\n` 切句后，只保留带**序号标记**（首先/其次/最后/第一/1.…）或**论据性关键词**（指出/表明/因为/因此/证据/数据…）的句子（标记 3 分 / 关键词各 1 分，长句同分靠前），无任何命中回退最长句；按分数降序拼接，超出 `max_chars` 整体截断。保证摘要轮辩手仍能看到对方的具体论点，逐条回应而非泛泛反驳。

### 12.9 任务与变量
- `_create_task(d)`：**校验 recipient 存在**（P15 修复，非法则忽略并记日志）；生成任务 `{id, task_id(UUID), recipient, content, expected_outcome, var_name, completed, rounds_waiting, timestamp(ms), token_cost}`，并在 `shared_context[var_name]` 登记。
- `_apply_report(task, report)`：回填变量、标记完成，结果以 `importance=0.85` 写入该 Agent **长期记忆**。

### 12.10 收敛检测 `_check_convergence()`（架构文档 §4.4）
- `round >= 2` 时计算本轮 vs 上一轮发言的相似度：嵌入可用（`is_fallback() == False`）用 `embed_query` 余弦相似度，阈值 `CONVERGENCE_SIMILARITY`（0.85）；**嵌入不可用（P0-3 降级）**时切换为 `_text_similarity()`——difflib `SequenceMatcher` 对去空白文本计算相似度，阈值 `CONVERGENCE_TEXT_SIMILARITY`（0.6），保证离线哈希向量模式仍能收敛提前终止；
- **按发言者逐人计算（§5.7）**：`CONVERGENCE_PER_SPEAKER=1` 时取连续两轮**都有发言**的共同辩手，逐人算相似度再取**平均**，避免拼接全文时单一长文本主导而误判收敛；无共同发言者（或关闭开关）时回退整轮聚合拼接对比。`embed_query` 经 `asyncio.to_thread` 在 worker 线程执行，不阻塞事件循环（§2.4）；
- 相似度 ≥ 阈值**且**本轮无新 RAG 证据 → `convergence_streak += 1`，否则清零；
- `convergence_streak >= CONVERGENCE_ROUNDS`（2）→ 触发收敛，提前进入裁决。

### 12.11 RAG 核查 `_rag_check_round()`
**按立场分桶（§5.3）**：把本轮发言按发言人立场分组，每组合并文本 → `asyncio.to_thread(rag_service.check, ...)` → 证据归入对应立场桶，结果存 `round_evidence[round] = {stance: [EvidenceItem]}`（旧数据 / 无立场发言回退 `"default"` 桶）。下一轮 `_evidence_for_round(round_no, stance)` 只取**与本人立场对应**的证据桶（无立场或空桶回退默认桶），辩手不会被对手立场的核查结果干扰。

### 12.12 Judge 裁决 `_judge_round()`（架构文档 §2.4）
- **输入范围**：`_build_judge_debate_text()` 只保留 `type=="speech" and not is_main` 的**辩手发言**（剔除主持人开场 / 任务分派 / 追问 / 总结）。
- 构建完整辩论历史 + 各方立场，调用裁判（采样参数来自 `governor.judge_params()`，公平性 L5），要求输出 JSON（**按辩手**的 `agent_scores` + `winner` + `critique` + `convergence_flag`，键必须与辩手名一致）；
- **few-shot 锚定修复**：`_example_pool` 内置 3 组互不相同的示例分数（如 `{logic:8,evidence:7,rebuttal:9,clarity:8}` / `{7,6,8,7}` / `{9,8,7,8}`），按辩手轮换生成 `agent_scores_example` 仅供格式展示，并显式要求"禁止照抄，不同辩手分数可以（也应当）不同"——根除旧版"全场照抄同一组分数"的锚定问题；
- `_extract_json` 容忍代码块/前后缀，`Judgment(**data)` 校验；
- **全局综合得分 `scores`（后端确定性计算，不再依赖 LLM 输出）**：`_compute_composite()` 按各辩手 `agent_scores` 逐维（logic/evidence/rebuttal/clarity）取均值得到 `scores`——修复"综合得分 == 某辩手得分"的照抄 bug；
- **模型质量分（系统确定性，非 LLM，仅展示）**：`_model_performance()` 把 `agent_metrics` 聚合为 `{avg_ttft_ms, avg_gen_rate, performance_score}`；`_score_ttft`（≤1s→10 / ≤5s→8 / ≤15s→6 / ≤30s→4 / 否则 2）与 `_score_rate`（≥80字/s→10 / ≥40→8 / ≥20→6 / ≥10→4 / 否则 2）取均值，无实测取中性 5.0；
- **综合评分（仅展示，不参与胜负）**：`composite_scores = 内容均分 × JUDGE_CONTENT_WEIGHT + 模型质量分 × JUDGE_PERFORMANCE_WEIGHT`（默认 0.8/0.2）；`agent_scores` 缺失（如裁判调用失败回退）时退化为模型质量分；
- **胜方 = 纯内容判定**：取 `agent_scores` 四维均分最高的辩手所在方（PRO/CON）；并列最高或两端并列取 `DRAW`。**模型质量分 / composite 不参与胜负**（修复旧版按 composite 推导胜方、被 API 延迟左右结果的问题）；
- 失败降级为 `Judgment(winner="DRAW", critique="裁判调用失败…")`，同样走 `_compute_composite`（内容缺失 → composite 退化、胜方 DRAW）。
- **公平性 L6（长度中立）**：`governor.judge_guidance()` 追加到裁判 system prompt，要求评分只依据内容质量、不受发言长短影响。

### 12.13 汇总与导出
- `_final_summary(judgment)`：主持人输出**结构化结论（P0-1 四段式）**——`_normalize_conclusion(text, judgment)` 把模型 LLM 输出按 `##` 标题切分，**裁决段始终用权威数据 `_judge_ruling_text(judgment)` 确定性重写**，核心结论 / 各方立场 / 分歧与待办优先取模型输出、缺失时确定性补齐；结果写入 `self.conclusion` 并经 `conclusion` 事件发布（`core / positions / judge_ruling / pending / winner / round`），前端核心结论面板实时展示；
- `_fallback_summary(judgment)`：**确定性汇总**（不调用 LLM，按发言记录 + 裁决拼装；含 `agent_scores` 与 `composite_scores` 综合评分输出行）——公平性治理下无预算熔断，作为系统兜底路径保留；
- `export_markdown()`：主题/轮次/时间/Trace ID/**Token 用量（只读遥测：累计 + 输出）**/**预估费用（P1-6，≈ ¥）**/**公平性治理报告（`ok` + 各层 L1–L6 状态与详情）**/Agent → 发言记录 → 裁判裁决（`scores` + `agent_scores` + 胜方）→ **「模型性能与综合评分」表**（辩手/平均TTFT/平均生成速率/模型质量分/综合评分）→ 任务结果表 → **结构化结论（四段式）** → 各 Agent 记忆快照。

### 12.14 状态同步 `_status_sync()`（架构文档 §3.5）
发布 `status_sync` 事件：`trace_id / phase / current_round / agent_states / spent_tokens / output_tokens / fairness / cost_rmb / elapsed_time_ms`。其中 `spent_tokens`（累计 token 用量）与 `output_tokens`（累计输出 token）为**只读遥测**，`cost_rmb` 为 **P1-6 费用估算（¥，`_estimate_cost_rmb`）**；`fairness` 为 `governor.report()`（L1–L6 各层状态），替代旧的 `budget_plan` 视图。

### 12.15 多会话管理 — `app/manager.py`（P1-7）

**背景（P5）**：原 `orchestrator = Orchestrator()` 模块级单例 + 全局 `EventBus` 不支持多客户端——两个浏览器同时打开会互相覆盖配置 / 状态 / 事件，一个客户端的 `pause/end` 会操作到另一客户的讨论。

**`DebateManager`（全局单例 `manager`）**：
- `get_or_create(session_id="default")`：按 `session_id` 取会话；不存在则惰性构造 `Orchestrator(session_id=...)`（惰性导入防循环依赖）。**会话实例统一由此路径持有**（§1.3 解耦：已删除模块级 `orchestrator` 单例与 `register()` 双入口，`main.py` / 测试 / 前端均走本方法）。**安全字符白名单** `_SAFE_SID = ^[A-Za-z0-9._-]+$`：非法 `session_id` 读写元数据时一律清洗回 `"default"`，防路径穿越；
- `set_done_handler(handler)`：注册"讨论完成"回调（`main.py` 用于自动落盘 Markdown），每个会话完成时经 `notify_done(orch)` 触发。

**元数据持久化（轻量快照，非发言明细）**：
- `save_meta(orch)` → `data/sessions/{session_id}.json`：`{session_id, status, phase, topic, trace_id, round, max_rounds, spent_tokens, agents:[{name, role, stance, is_main, model_id}], judge, updated_at}`；
- `load_meta(session_id)`：读取（不存在 / 损坏返回 `None`）；读写共用 `_meta_path()`（含白名单清洗）。

**中断恢复**：
- `mark_interrupted()`：**进程启动时**调用，把 `data/sessions/` 中遗留的 `status ∈ {running, paused}` 会话标记为 `interrupted`（`phase=idle` + `note="服务重启，讨论中断"`）——避免把本次运行中的会话误标记；
- `interrupted_metas()` / `interrupted_meta_for(session_id)`：供前端中断横幅展示（`GET /api/status` 对中断会话返回 `status=interrupted` + `interrupted=true`）。

### 12.16 费用估算（P1-6，人民币 ¥）

`_estimate_cost_rmb(total, output)`（模块级函数，`orchestrator.py`）：
1. `input = max(0, total - output)`（输入 token = 累计 − 输出）；
2. `费用 ≈ input/1e6 × COST_INPUT_PER_1M + output/1e6 × COST_OUTPUT_PER_1M`（默认 ¥2 / ¥8 每百万 token，`.env` 可覆盖）。

暴露位置：`speech_end`（每轮发言后）、`status_sync` 与 `/api/status`（状态面板）、`export_markdown` 运行摘要（`- 预估费用（P1-6）：≈ ¥x.xxxx`）。**只读遥测**，不参与任何约束。

---

## 13. 后端 — `app/main.py`

FastAPI 应用，挂载 `/static`，提供 REST + WebSocket。

### 13.1 启动 `lifespan`
用 `@asynccontextmanager` 注册（**替代已弃用的 `@app.on_event("startup")``**）。启动时：
1. **中断标记**：调用 `manager.mark_interrupted()` 把上一进程遗留的 `running/paused` 会话标记为 `interrupted`（前端展示中断横幅）；
2. **注册完成回调**：`manager.set_done_handler(_on_done)`，各会话讨论完成（`status=done`）→ 自动 `export_markdown()` 落盘 `data/discussions/` 并 `record_discussion()` 写入历史记忆索引。

落盘逻辑抽为 `_persist_done_markdown()`（导出 → 写入 `discussion_<时间戳>.md` → 记录历史索引，返回文件名），监听器**不静默吞异常**：`except` 后写结构化日志（`lifespan.save_markdown` 事件）并按 `(1, 2)` 幂等重试（写入与索引 upsert 均幂等，重复执行安全）；关闭时取消监听任务并退订。

### 13.2 REST 接口

> **会话路由（P1-7）**：所有写接口请求体（`ConfigureRequest` / `StartRequest` / `SessionRequest` / `IngestRequest` / `SaveMarkdownRequest`）与 `GET /api/status`、`GET /api/export`、`/ws` 均支持 `session_id` 参数（缺省回退 `"default"`），经 `_get_orch(session_id)` 绑定到对应 `Orchestrator` 实例；响应统一携带 `session_id`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 返回 `index.html` |
| GET | `/api/defaults` | 返回供应商预设（**已剥离 api_key**）、向量库后端、最大 Agent 数 |
| POST | `/api/configure` | 配置 Agent（1–6）、主题、最大轮次；**请求体 `ConfigureRequest`（含 `session_id`，pydantic 校验，非法 422）**；返回 `agents/stance/phase/trace_id/max_rounds/session_id` + **`fairness`（L1–L6 各层状态报告，替代旧可行性/预算视图）** |
| POST | `/api/ingest` | 向共享向量库注入知识（**请求体 `IngestRequest` 校验**，按段落切分） |
| POST | `/api/start` | 启动讨论（**请求体 `StartRequest` 可省略**，`_spawn_task(run_discussion)` 登记后台任务） |
| POST | `/api/pause` `/api/resume` `/api/end` | 暂停/继续/结束（**请求体 `SessionRequest` 仅需 `session_id`，可省略**） |
| GET | `/api/status` | 状态、阶段、轮次、Agent、任务、共享上下文、裁判结果、**Token 遥测（`spent_tokens` / `output_tokens`）与公平性报告（`fairness`）与费用估算（`cost_rmb`）**；会话被标记为 `interrupted` 时额外返回 `status=interrupted` + `interrupted=true` |
| GET | `/api/export` | 返回 Markdown 文本 |
| POST | `/api/save_markdown` | 保存 Markdown 到服务器（**请求体 `SaveMarkdownRequest`；`filename` 经 `_safe_discussion_filename` 清洗防路径穿越**，空请求体回退默认名；同时写入历史记忆索引） |
| GET | `/api/history?q=` | 历史讨论列表（`items` 按时间倒序；`q` 非空时按题目 / trace_id 关键词过滤） |
| GET | `/api/history/{filename}` | 查看某场历史讨论的完整 Markdown（`404` 表示记录不存在，含路径穿越防护） |

> **请求体验证双轨契约（P0-2）**：`app/schemas.py` 只做**类型 / 形状**校验（`app/schemas.py:11`），非法请求体由 FastAPI 返回 `422` 并自动生成 OpenAPI 文档；业务语义校验（Agent 数量 1–6、讨论状态冲突、空文本等）保留在各端点内，维持前端依赖的既有 `{ok, error}` `400` 契约。

> **`_safe_discussion_filename(fname)`（P0-1，`app/main.py:284`）**：`filename` 只保留 `Path(fname).name`（丢弃目录 / 绝对路径 / `..`），清洗后为空回退 `discussion_{_ts()}.md`，未以 `.md` 结尾则补后缀；与读取侧 `history.get_markdown` 的 `resolve()` + `relative_to()` 防护（§13.4）构成读写两侧的路径穿越防线。

### 13.3 WebSocket `/ws?session_id=`
- 连接即推送当前 `status`（含 agents/topic/max_rounds/phase + **`spent_tokens` / `output_tokens` / `fairness` / `cost_rmb` / `elapsed_time_ms` / `session_id`**），便于前端初始化席位与状态面板；
- 订阅 `event_bus`，把每个事件 `send_json` 给前端；
- 断开/异常时在 `finally` 中退订，避免订阅队列泄漏。

### 13.4 历史讨论记忆索引 — `app/history.py`
把每场讨论的「题目 + 元信息」持久化到 `data/discussions/index.json`（按 filename 去重 upsert，头部插入，保留最近 200 条），作为历史讨论的记忆化保存，支撑后续检索：

- `record_discussion(topic, trace_id, filename, status, phase, round_no, max_rounds, token_spent, agents)`：写入 / 更新一条索引（内部 `threading.Lock` 保证并发安全）；`token_spent` 来自只读遥测，仅作展示。
- `list_discussions(q)`：按题目 / trace_id 做不区分大小写关键词过滤，按时间倒序返回。
- `get_markdown(filename)`：读取 `data/discussions/` 下的 Markdown 内容；`resolve()` + `relative_to()` 做**路径穿越防护**，仅允许该目录内 `.md` 文件。

**写入时机**（两处）：
1. `lifespan` 监听器收到 `status=done` 自动落盘 Markdown 后；
2. `POST /api/save_markdown` 保存 Markdown 后。

**读取入口**：`GET /api/history`（列表 / 搜索）与 `GET /api/history/{filename}`（完整 Markdown），由前端历史面板调用。

---

### 13.5 文件解析与共享知识注入 — `app/file_parser.py`
为「共享知识库」提供**文件上传 → 文本提取 → 按段注入向量库**的前置处理，支持 `txt/md/csv/log`、`docx`（python-docx）、`xlsx`（openpyxl）、`pdf`（pypdf）、图片 OCR（RapidOCR）：

- `parse_file(filename, data) -> (text, meta)`：按扩展名分派到各格式提取函数；全部第三方依赖**懒加载**，缺失时抛 `ParseError` 给出一行可读提示，不阻塞其它格式。
- 保护措施：`MAX_UPLOAD_BYTES = 20MB` 超限拒绝；`MAX_TEXT_CHARS = 200_000` 超出截断并在 `meta["truncated"]` 标注；纯文本 utf-8 → gbk 回退。
- `split_paragraphs(text)`：按行切分为待索引的知识块。
- 入口：`POST /api/upload`（multipart）解析后调 `shared_vector_store.add()` 注入；前端「共享知识库」区提供文件选择 + 上传解析按钮，`/api/defaults` 暴露 `upload_exts` 支持格式表。

### 13.6 REST 请求体模型 — `app/schemas.py`（P0-2）

与 `app/protocol.py`（Agent 间通信协议）分离的 **REST 边界模型**，为四个写接口提供类型校验：

| 模型 | 对应接口 | 关键字段 |
| --- | --- | --- |
| `AgentConfig` | `/api/configure` 中单个 Agent | `name / role / personality / provider / base_url / api_key / model_id / stance / agent_id / forget_config` |
| `ConfigureRequest` | `POST /api/configure` | `session_id`（可选）、`agents: list[AgentConfig]`、`topic`、`max_rounds=8`、`judge` |
| `StartRequest` | `POST /api/start` | `session_id`（可选）、`topic / max_rounds`（全可省略，省略沿用 configure 值） |
| `SessionRequest` | `POST /api/pause / resume / end` | `session_id`（可选） |
| `IngestRequest` | `POST /api/ingest` | `session_id`（可选）、`text`、`source`（默认"用户注入"） |
| `SaveMarkdownRequest` | `POST /api/save_markdown` | `session_id`（可选）、`filename`（可省略，省略自动生成） |

**设计约定**（`app/schemas.py:8`）：只做类型 / 形状校验，非法请求体由 FastAPI 返回 `422`；业务语义校验仍保留在各端点内，维持既有 `{ok, error}` `400` 契约。`main.py` 侧用 `payload.model_dump()` 转 dict 后交给 `orchestrator`。

### 13.7 工程化配置与测试门禁（P1-4 / P1-5）

- **`pyproject.toml`**：声明 `requires-python = ">=3.10"`；`[tool.ruff]` 启用 `E4/E7/E9/F/I/UP/RUF/B/S` 规则（`line-length=110`，忽略 `BLE001/S110/RUF001/002/003` 中文标点误报；`tests/*` per-file-ignores `S101/S310`）；`[tool.mypy]` strict 模式仅覆盖 `app/protocol.py`（全量开启为后续工作）；`[tool.coverage]` 以 `app/` 为源码、`branch=true`。
- **`requirements-dev.txt`**：`-r requirements.txt` + `ruff>=0.5` / `mypy>=1.10` / `coverage>=7.0`，与运行依赖分离。
- **接口级测试 `tests/test_api_flow.py`**：`httpx.AsyncClient(transport=ASGITransport(app))` 直接驱动 FastAPI 应用，monkeypatch `_judge_round / _final_summary / _consume_stream / _run_llm_retry` 四个 LLM 调用点实现确定性输出，**无需启动服务器、无需真实模型**。7 个用例：HTTP 全链路（configure→start→done→export→save_markdown→history）、`../../evil.md` 路径穿越被清洗为 `evil.md`、空请求体 save_markdown 回退默认文件名、空 Agent 列表保持 `400` 契约、configure 返回 fairness 报告、**status 携带 Token 遥测 + 公平性字段（`spent_tokens` / `output_tokens` / `fairness` 各层状态）**。运行：`python -m unittest tests.test_api_flow -v`。
- **CI 门禁**（`.github/workflows/ci.yml`）：安装步骤含 `fastapi httpx ruff mypy`，在 fast 单测之后新增 `Lint (ruff)`、`Type check (mypy)`、`API flow tests` 三步。
- **RUF006 后台任务强引用**：后台任务集合与 `spawn()` 抽取为独立模块 `app/bgtasks.py`（§2），`main.py` / `orchestrator.py` 统一 `from app.bgtasks import spawn as _spawn_task` 登记后台任务（完成回调自动 discard），消除两份重复实现，防止任务被 GC 提前回收。

---

## 14. 前端 — `static/`

### 14.1 页面结构 `index.html`
- **顶栏 `.topbar`**：Logo + "历史记录"按钮 + 状态胶囊。
- **配置区 `#setup`**（双栏：`.setup-col` + `.side-col`）：Agent 数量（2–6）、每个 Agent 卡片（名称/角色/性格/**立场**/模型供应商 + 自定义字段）、主题、最大轮次、知识注入、开始按钮（已移除 Token 预算配置）。
- **讨论区 `#discussion`**：左侧圆桌（`#round-table`）含等分席位与中心信息（主题/轮次/任务进度）；右侧控制（暂停/继续/结束/返回）+ 运行状态（阶段/轮次/Token 遥测/Trace ID）+ **状态面板（Token 统计 + 公平性事件）** + 任务面板 + 变量面板；下方**聊天气泡**发言流；结束栏（预览 / 一键导出 Markdown）。
- **历史弹窗 `#history-modal`**：左侧搜索框 + 记录列表（题目 / 时间 / 状态 / 轮次 / 人数），右侧 Markdown 预览 + 一键导出。

### 14.2 样式 `style.css`
**设计语言：简约大气（低饱和深色 + 暖金点缀）**，明确避免霓虹 / emoji 的"AI 味"：
- 设计变量：`--bg: #0f1115`、`--surface: #161920`、`--gold: #d9a441`、`--pro: #7aa784`、`--con: #c08a8a`、`--neutral: #7c93b8` 等。
- **首字母文字头像**：`.bubble-avatar` / `.seat .avatar` 用 Agent 名称首字母 + 立场配色（`.av-host` / `.av-pro` / `.av-con` / `.av-neutral`，未知立场按席位序号 `.av-seat0..5` 兜底）；主 Agent 加金色光环（`.seat.main .avatar`）。
- **聊天气泡**：`.bubble-row`（flex 行 + `bubbleIn` 弹出动画）内含 `.bubble-avatar` + `.bubble-main`（`.bubble-meta` 名字/轮次标签 + `.bubble` 气泡）；任务/总结/裁决/日志/错误各有专属气泡风格；`.bubble-row.speaking` 高亮金边。
- **流式光标**：`.caret`（金色竖线 + `blink` 闪烁动画），流式增量时保持在文本末尾。
- 历史面板 `.history-modal` / `.history-layout` / `.h-item`。
- **状态面板（Token 统计 + 公平性事件）**：`.budget-bar` 样式保留但仅承载只读遥测（无预算总量/进度概念）；`.budget-sub` 虚线分隔「公平性事件」；`.budget-events`（max-height 96px 滚动）；`.budget-event`（12px 行）+ `.ev-tag`（10px 圆角小标签，`ev-warn` 金 / `ev-err` 红 / `ev-ok` 绿）。

### 14.3 逻辑 `app.js`
- `init()`：拉取 `/api/defaults`、生成卡片、绑定事件、连接 WS；探测服务器是否存在可继续的讨论（`/api/status`）。
- `generateAgentCards(n)`：按数量生成配置卡片，首个标记"主 Agent"且立场固定 HOST，其余按 PRO/CON 交替；立场下拉可选 HOST/PRO/CON/NEUTRAL。
- `startDiscussion()`：组装 `agents`（含 `stance`）→ `/api/configure` → 渲染席位 → `/api/start`。
- `renderSeats(agents)`：**席位等分算法**——`angle = (-90 + i*360/n)`，以圆心 50% 为圆心、半径 38% 用百分比定位；头像为首字母 + 立场配色。
- `avatarClass(stance, idx)`：立场 → 头像配色类映射（含 `av-seatN` 兜底）；`stanceOf(name)` 从 `state.agents` 反查立场。
- `connectWS()` / `handleEvent(ev)`：按事件类型更新 UI（见 §15 事件协议）。
- **并发流式气泡**：`state.streamBoxes = {}`（speaker → el），`speech_start` 按 speaker 建独立气泡（`.bubble-row` + 空 `.body` + `.caret`），`speech_delta` 把增量插入到光标前，`speech_end` 用完整文本替换（移除光标）并删除该 key——第 1 轮并行立论时多个 Agent 可同时流式发言而不互相覆盖；**每次气泡只承载当前发言内容，不拼接历史**。
- `renderRunStatus(data)`：展示阶段/轮次/**Token 遥测（输出 / 累计）**/用时/Trace ID；缓存完整快照供 `speech_end` 按最新 `output_tokens` 立即刷新。
- **状态面板**：`renderBudgetPanel(data)`（HTML id 沿用 `budget-panel`，语义已改为 Token 统计 + 公平性）渲染只读遥测（`spent_tokens` 累计用量 / `output_tokens` 累计输出，无预算总量与进度条）+ **公平性 L1–L6 各层状态**（`data.fairness.layers` 逐层 `L{n} {name}: {status} — {detail}`，`ok` 绿 / 告警金 / 违约红）；`case "status"`（含 WS 连接快照）、`case "status_sync"`、每秒 `/api/status` 轮询、配置后 fairness 预览共四处接线；`addBudgetEvent(ev)` 命中 `FAIR_EVENT_TEXT`（`fairness.output_truncate`「L1 输出截断」warn / `fairness.report`「公平性报告」ok）时以彩色标签入「公平性事件」列表（保留 6 条，全量文本 `textContent` 填充防 XSS）。
- `exportMarkdown()` / `downloadMarkdown()`：拉取 `/api/export` 弹出/下载 Markdown（下载同时调用 `/api/save_markdown` 落盘并写入历史索引）。
- **历史面板**：`openHistory()` 打开弹窗并 `loadHistory()` 拉取 `/api/history`；搜索框 300ms 防抖后带 `?q=` 重新检索；点击列表项 `viewHistory(filename)` 拉取 `/api/history/{filename}` 预览；`downloadHistoryMarkdown()` 把当前查看内容下载为 Markdown 文件。

---

## 15. 事件协议（WebSocket JSON）

| `type` | 关键字段 | 含义 |
| --- | --- | --- |
| `status` | `status, phase, topic, max_rounds, trace_id, agents, session_id, spent_tokens, output_tokens, fairness, cost_rmb, elapsed_time_ms` | 状态变化/初始化（含 Token 遥测、费用估算与公平性报告） |
| `round` | `round, max` | 进入新一轮 |
| `speech` | `speaker, text, is_main, round` | 发言（非流式回退时使用） |
| `speech_start` | `speaker, round, is_main` | 流式发言开始（前端建气泡 + 席位高亮） |
| `speech_delta` | `speaker, delta` | 流式文本增量（追加到当前气泡） |
| `speech_end` | `speaker, round, text, output_tokens, cost_rmb` | 流式发言结束（以完整文本收尾，携带最新输出 token 遥测与费用估算） |
| `tool_call_start` | `speaker, tool, index` | 工具调用开始（P1-4，气泡显示"正在调用工具 X…"；纯工具回合先补 `speech_start` 保证气泡存在） |
| `tool_call_args` | `speaker, tool, index, delta` | 工具参数增量（P1-4，可选展示） |
| `tool_call_end` | `speaker, tool, index, result` | 工具调用结束（P1-4） |
| `task_assigned` | `from, to, content, expected, var, task_id, timestamp` | 任务分派 |
| `task_result` | `from, var, value, completed, message, task_id, timestamp, token_cost` | 任务回报/变量回填 |
| `status_sync` | `trace_id, phase, current_round, agent_states, spent_tokens, output_tokens, fairness, cost_rmb, elapsed_time_ms` | 状态同步（架构状态机/遥测心跳，驱动状态面板实时刷新） |
| `judgment` | `judge, scores, agent_scores, model_performance, composite_scores, winner, critique, convergence_flag, round` | 裁判裁决（`scores` 后端均值；`composite_scores` 仅展示；`winner` 纯内容判定） |
| `summary` | `speaker, text, round` | 最终总结 |
| `conclusion` | `core, positions, judge_ruling, pending, winner, round` | 结构化结论（P0-1 四段式，前端核心结论面板展示） |
| `agent_error` | `agent, error` | 某 Agent LLM 调用失败（前端展示错误气泡） |
| `log` | `text, event` | 系统日志（带 trace_id）；`event` 为 `fairness.*` 时前端同步入「公平性事件」告警列表 |

---

## 16. 容错与降级

| 环节 | 降级行为 |
| --- | --- |
| 嵌入模型加载失败/无网络 | 确定性哈希向量（384 维），离线可用；后台预热不阻塞启动；**收敛检测同步降级为文本相似度（difflib，`CONVERGENCE_TEXT_SIMILARITY`=0.6，P0-3）** |
| Qdrant 不可达 | `QDRANT_TIMEOUT=3` 快速失败 → 进程内内存向量库（仍对所有 Agent 共享） |
| 联网搜索（SerpApi）失败/未配 key | 工具返回友好提示，不崩溃 |
| LLM 调用异常（429/5xx/超时） | `_run_llm_retry` 指数退避重试（1s、3s） |
| 流式失败 | 回退非流式重试调用，保证发言不丢失 |
| 流式整体超时（>STREAM_TIMEOUT） | `_consume_stream` 抛超时 → 回退非流式 |
| LLM 摘要失败 / 返回超长 | `summarize_text` 回退 `_fallback_truncate` 确定性头尾截断（`…` 相连），绝不超过 `max_chars`，摘要流程不抛异常 |
| 裁判调用失败 / JSON 解析失败 | 确定性 `Judgment`（DRAW + 说明）；`scores` 由后端按内容均分计算，不因无 LLM 输出而缺失 |
| 输出超长（公平性 L1） | 提示词字数要求失效时，`_agent_turn` 按 `ceiling_for` 事后 `truncate_output` 头尾截断兜底（事件 `fairness.output_truncate`），绝不超长输出 |
| 强制压缩历史但历史过短 | 日志如实报告"实际压缩 X 个 / 历史过短跳过 Y 个"（短历史无可压内容，属正常空转） |
| 主持人汇总失败 | `_final_summary` 失败时可用 `_fallback_summary` 确定性汇总（不调用 LLM，按发言记录 + 裁决拼装；公平性治理下无预算熔断触发路径，作为系统兜底保留） |
| 非法 session_id | `DebateManager._meta_path` 白名单清洗回 `"default"`，读写均无法越出 `data/sessions/`（P1-7） |
| 协作 Agent 未回报 | 发言记录 + `rounds_waiting` 计数，超 3 轮强制标记完成 |
| `_create_task` 接收方不存在 | 忽略该任务并记日志（P15 修复） |
| lifespan 自动落盘失败 | 结构化日志（`lifespan.save_markdown`）+ 幂等重试（1, 2 次），不再静默吞掉（P1-3） |
| 后台任务被 GC 提前回收 | 模块级 `_background_tasks` set 持有强引用，`_spawn_task` 完成回调自动移除（RUF006） |
| 非法 REST 请求体 | FastAPI 依据 `app/schemas.py` 返回 `422`；业务语义非法返回 `{ok, error}` `400`（P0-2） |
| `save_markdown` filename 含目录/绝对路径/`..` | `_safe_discussion_filename` 清洗为 basename + 强制 `.md`，无法越出讨论目录（P0-1） |

---

## 17. 需求实现对照（架构文档 ↔ 代码）

| 架构文档要求 | 实现位置 |
| --- | --- |
| Supervisor：总调度、状态机、收敛触发、结果聚合；不亲自生成论点 | `orchestrator.run_discussion`（主持人分派/追问 + Judge 独立裁决 + 汇总） |
| Pro/Con 立场绑定、禁止倒戈（§2.2/2.3） | `agent._stance_declaration` + `configure` 立场分配 |
| Judge Agent：多维度评分 + winner + 收敛标记（§2.4） | `orchestrator._judge_round` → `Judgment`（仅辩手发言评分；few-shot 示例池防照抄） |
| Judge 全局得分后端计算 + 纯内容判定胜方 | `_compute_composite` 逐维均值算 `scores`（不依赖 LLM）；`agent_scores` 内容均分最高方为 winner（并列 DRAW）；模型质量分 / `composite_scores` 仅展示 |
| RAG Agent：断言提取 + 检索 + 置信度 + 缓存（§2.5） | `app/rag.py`（`rag_service`，24h 缓存） |
| 消息协议：TaskDispatch / ArgumentSubmit / Rebuttal / Judgment / StatusSync（§3） | `app/protocol.py`（pydantic + task_id UUID + token_cost + 毫秒时间戳） |
| 状态机：Idle→Preparing→Debating→Judging→Finished（§4.1） | `PHASES` + `self.phase`（异常熔断回 Idle） |
| 多客户端隔离 / 会话路由（P5 → P1-7） | `app/manager.py` `DebateManager` 按 `session_id` 独立 `Orchestrator`；`data/sessions/{sid}.json` 元数据快照；重启后 `mark_interrupted()` 中断恢复横幅；前端 `localStorage` 持久化 `session_id` |
| 并行立论（§4.2/6.2） | 第 1 轮 `asyncio.gather` 并发 `_debate_turn` |
| 终止条件：max_rounds + 收敛 + 人工（§4.3） | `run_discussion` 终止判断（收敛/轮次/手动 end；公平性治理下无预算熔断） |
| 收敛检测：语义相似度 + 新证据增量（§4.4） | `_check_convergence`（embedding 余弦 + RAG 新证据判断；`CONVERGENCE_PER_SPEAKER` 按发言者逐人平均，§5.7） |
| 超时与重试（§4.5） | `_run_llm_retry`（1s/3s 指数退避）+ `STREAM_TIMEOUT` 单步超时 |
| 消息队列（§5.1） | 进程内 `EventBus`（单进程部署可接受，未采用 Redis） |
| 共享黑板（§5.2） | 内存 `shared_context` + `transcript`（未持久化） |
| 心跳与故障恢复（§5.3） | 未实现（`status_sync` 事件承担部分状态广播） |
| 公平性治理（替代旧 Token 预算控制 §6.3） | `FairnessGovernor` L1–L6（`app/fairness.py`）：输出字数上限/有效回答占比/输入对称/轮次对称/采样对齐/长度中立裁判；`spent`/`output_tokens` 只读遥测；不再下发 `max_tokens` |
| 对手定向反驳 + 输出字数约束 + 按轮强压缩 | `_find_opponent`（`config.OPPONENT_STANCE` 立场映射，回退全局最近）/ `_prepare_opponent_context`（第 2 轮原文截断、第 3 轮起 `summarize_text` 摘要 + 缓存 + **关键论据句 `_extract_argument_sentences`**）/ `_debate_prompt` 注入字数提示（`governor.ceiling_for`）与论据句逐条回应指令 / `_agent_turn` 超长截断兜底 / `compress_history(keep_rounds=N)` 按轮保留 |
| 任务与辩论分离（§5.8） | `_task_turn`（只回填 `shared_context`，任务输出不进 `round_arguments`/记忆）/ `_debate_turn`（正常发言记录）；主循环两阶段调用 |
| 缓存策略（§6.4） | RAG 检索 24h 缓存（`rag.py:_search_cached`） |
| 可观测性：Trace ID（§7.2） | `trace_id` 贯穿事件/日志/导出 |
| 审计回溯：快照 + 回放（§7.3） | 未实现（仅最终 Markdown 落盘） |
| 人工审核 human_in_loop（§7.4） | 部分：暂停/继续/结束（无 intervene API） |

> 未对齐项（消息队列、黑板持久化、心跳恢复、快照回放、human_in_loop API、OTel）属于《运行分析与改造方案.md》规划中的 Phase 3 生产化能力，单进程演示场景下可不实现。

---

## 18. 典型调用时序（一次讨论）

```
浏览器
  └─ POST /api/configure   → orchestrator.configure()（生成 trace_id、分配 stance；请求体经 schemas 校验）
  └─ POST /api/start       → _spawn_task(run_discussion())（登记后台任务，防 GC）
                                ├─ phase=preparing → 发布 status + status_sync
                                ├─ 每轮:
                                │    ├─ phase=debating
                                │    ├─ 公平性治理日志（发布 governor.report() L1–L6 状态）
                                │    ├─ 主持人 _agent_turn（分派 delegate_task → _create_task）
                                │    ├─ 任务回合 _task_turn（待办任务 → 回填 shared_context，不进 round_arguments）
                                │    ├─ 第1轮: asyncio.gather 并行 _debate_turn 立论（并发流式）
                                │    │   后序: 串行 _debate_turn 反驳/辩护（摘要轮注入关键论据句）
                                │    ├─ _rag_check_round（按立场分桶证据注入下一轮）
                                │    ├─ _memory_housekeeping
                                │    ├─ _check_convergence → 收敛提前 break
                                ├─ phase=judging → _judge_round（调用失败 → 确定性回退 DRAW）
                                ├─ phase=finished → _final_summary（失败时 _fallback_summary 兜底）
                                └─ status=done → 发布 status + status_sync
  WebSocket /ws
  └─ 实时推送 status / round / speech_* / task_* / status_sync / judgment / summary
  status=done → lifespan 监听器自动保存 data/discussions/*.md
```

---

## 19. 端到端验证记录（E2E）

> ⚠️ 历史记录（2026-08-31 之前）：以下实测数据反映**旧规则**——胜方曾由"综合评分（内容×0.8+模型质量×0.2）"推导，且系统仍带 Token 预算规划。当前规则已改为**纯内容判定**（winner 取 `agent_scores` 四维均分最高方，模型质量分仅展示）并移除预算系统（见 §12.7/§12.12）。数据保留作为回归基线参考。

使用 `tests/test_e2e.py`（配置 → 注入 → 开始 → WebSocket 收事件 → 导出）对真实运行做验证。被测配置：3 个 Agent（主持人=HOST，技术派=PRO，人文派=CON）、**全部使用 qwen**（`qwen3.7-plus`）、独立裁判同样 qwen、`max_rounds=2`。观察到的事实：

- **配置与注入**：`/api/configure` 成功创建 3 个 Agent（stance=[HOST, PRO, CON]），`/api/ingest` 写入 2 段知识，返回 `backend=in-memory`。
- **状态机**：`status_sync` 事件中观察到的 `phase` 集合含 `preparing / debating / judging`，最终 `status=done`、`phase=finished`。
- **多轮辩论**：实际走满 `max_rounds=2`，`round` 事件集合为 `{1,2}`；第 1 轮并行立论，第 2 轮主持人抛疑、辩手反驳。
- **预算统计（旧规则）**：预算规划器全程把 `projected_total` 控制在预算附近；预算吃紧时走降级阶梯（降 `effective_max_tokens` / 强制压缩 / 允许小幅超支），辩论不中断。允许超支时 `spent` 可能略超 `budget`（2 轮短辩论真实冒烟实测超支约 4–8%；4 轮长辩论因历史输入成本被低估而超支约 72%，详见 §20）。
- **裁判裁决（旧规则）**：收到 `judgment` 事件，`winner=CON`；含 `agent_scores`（按辩手四维评分）、`model_performance` 与 `composite_scores`。实测 `model_performance={'技术派': {'avg_ttft_ms': 35169.3, 'avg_gen_rate': 4.0, 'performance_score': 2.0}, '人文派': {'avg_ttft_ms': None, 'avg_gen_rate': None, 'performance_score': 5.0}}`（人文派流式数据缺失取中性 5.0）、`composite_scores={'技术派': 5.6, '人文派': 8.2}`，当时胜方与综合分最高辩手（人文派=CON）一致——现规则下胜方改由内容均分决定，不再受模型质量分/综合分影响。
- **流式输出**：`speech_start == speech_end`，多个 speaker 的 delta 内容累计 > 0，第 1 轮并发流式正常。
- **导出**：`/api/export` 返回约 19KB Markdown（含发言记录、裁判裁决、**「模型性能与综合评分」表**、任务结果表、记忆快照），落盘 `data/discussions/e2e_export.md`。
- **校验输出**：`ARCHITECTURE CHECKS PASS (state machine / budget / judge)`（budget 字样为旧版断言残留）、`STREAMING CHECKS PASS`，断言覆盖 `agent_scores / model_performance / composite_scores` 非空且 `winner ∈ (PRO, CON, DRAW)`。

> 该验证同时覆盖了降级路径（本机无 Qdrant → `in-memory`；无 HF 网络 → 哈希向量）。无需服务器的快速回归见 `tests/test_ci_fast.py`（状态机 / **公平性治理 L1–L6** / 裁决分档 / 综合评分逻辑 / Token 遥测）与 `tests/test_api_flow.py`（HTTP 接口全链路 + 路径穿越防护，LLM 打桩）及 `tests/test_features_v2.py`（费用估算 / 收敛降级 / 工具可视化 / 结构化结论 / **论据句抽取 `ArgumentSnippetTest`** / **任务-辩论分离 `TaskDebateSeparationTest`**）。全量回归：`python -m unittest discover tests` → **82 tests OK**。

---

## 20. 预算校准真实冒烟记录（2026-08-28，历史存档）

> ⚠️ **历史存档**：本章记录旧的「全局 Token 预算 + 自适应校准」系统的真实冒烟验证（2026-08-28 执行）。该系统已于 **2026-08-31 整体移除**，由 `FairnessGovernor` L1–L6 公平性治理替代（见 §12.7）；`app/budget.py`、`data/budget_profile.json`、`_est_factor/_call_factor/_growth_ratio` 三路 EMA 及预算熔断均不复存在。本章保留仅作历史参考，其结论**不再适用于当前代码**。

对自适应校准（`_est_factor` / `_call_factor`）做真实冒烟验证（临时脚本直连本机服务，非测试套件）。固定配置：3 个 Agent（主持=HOST + 正方=PRO + 反方=CON，全部 default=MiMo）、裁判同模型、`max_tokens=4096`。预算与轮次见各场：

| 冒烟 | 轮次 | 预算 | spent | 超支 | est_factor 收敛 | call_factor 样本 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| #3 | 2 | 300000 | ≈426000 | 42% | — | — | 自适应校准引入前的基线（硬熔断旧逻辑） |
| #4 | 2 | 300000 | 316328 | 5.4% | ~1.3 | — | est_factor 生效；force_compress 日志如实化 |
| #5 | 2 | 300000 | 312919 | 4.3% | 1.26 | 2.40×→1.28 / 1.20×→1.26 | call_factor 首次生效 |
| #6 | 4 | 600000 | 1031695 | 71.9% | 1.25~1.30 | 3.20×→1.44 / 3.00×→1.75 / 2.20×→1.84 | 4 轮长辩论暴露历史输入成本模型缺口 |

**冒烟 #6 根因复盘**（trace `c4bd22e94743`）：

- **首轮规划基于因子 1.0**：configure 时预测 4 轮全量 `projected_total=248112`，但仅第 1 轮就实耗 241218（占预算 40%），est_factor 首轮样本达 2.68× → 首轮严重低估。
- **输入成本随轮次累积增长**：规划公式用常数 P（按"当前历史"估算）乘以剩余轮次 R，未建模后续轮次历史变长、每轮输入 token 递增。降级阶梯只压输出上限 E（`effective_max_tokens` 4096→1045→256 下限），而长辩论的成本大头是**输入**（累积历史）——emax 降到下限后第 3/4 轮仍各消耗 ~15–20 万 token。
- **call_factor 收敛方向正确但节奏太慢**：实测每轮调用 16/15/11 次 vs 基数 5（比值 2.2–3.2×），EMA 只影响"下一轮"规划，3 次样本后因子到 1.84，来不及阻止已发生的超支。
- **裁判在 E 下限时输出为空** → 确定性回退（winner=CON, `scores={}`, `critique_len=14`），复现旧版已知边界（原 §12.12「预算兜底」；现规则下裁判输出为空仅发生在调用失败时，且 `scores` 由后端按内容均分计算不会缺失）。

**结论**：两路 EMA 校准对 2 轮短辩论有效（超支 42%→4.3%），但**规划器对"历史累积输入成本"建模不足**——轮次越多超支越差。

**实现更新（同日志实现）**：上述缺口已通过三处补齐（§12.7）——
1. **历史增长模型**：规划公式升级为 `prompt_cost(R) = R·P + g·P·R·(R−1)/2`（`app/budget.py`），g 为每轮历史增长比，由新增 `_growth_ratio` 环比 EMA 校准（α=0.2，钳制 [0.05, 2.0]，负增长钳为 0，跳过首轮）。
2. **保守先验 + 档案持久化**：`est_factor / call_factor / growth_ratio` 落盘 `data/budget_profile.json`，启动载入；无档案时用 `EST_FACTOR_INIT=2.0`（对照冒烟 #6 首轮 2.68× 实测，2.0 先验避免首轮 40% 预算被单轮烧掉），历史乐观档案钳制 ≥1.0。
3. **预算不足自动削轮次**：规划超支且质量下限（`JUDGE_MIN_OUTPUT` / `SUMMARY_MIN_OUTPUT` = 512）仍无法覆盖全部轮次时，按 `feasible_rounds` 把 `max_rounds` 裁剪到可支撑范围（`BUDGET_AUTO_TRIM_ROUNDS=1`，事件 `budget.round_trim`）；阶段输出保底保证裁决/汇总 JSON 不再因 E 触底为空。

**已知边界**（尚未覆盖，后续可选）：EMA 收敛仍需数轮样本，极端首轮暴涨仍可能短期超支；`growth_ratio` 校准依赖轮间环比，单轮辩论无样本可循；自动削轮次会改变对话深度，实际生效轮数已由前端预算面板展示（`feasible_rounds` + `budget.round_trim` 告警标签）。

**实现更新（前端展示，已随系统移除）**：当时讨论页曾新增**预算状态面板**（旧 §14），经 `/api/status` / `/ws` / `status_sync` 实时展示进度条、输出上限降级、预计总耗超支比、可支撑轮数与三路校准因子，并把 `budget.*` 告警事件渲染为结构化彩色标签——该面板已随预算系统移除，现前端为「Token 遥测 + 公平性事件」面板（新 §14）。

---

## 21. 质量基线评估 — `scripts/eval_baseline.py`（P0-2）

**动机**：系统此前只有"跑通"（功能正确），没有"质量"度量。改一个 prompt / 换一个模型后，辩手发言更短了 / 收敛更慢了 / 裁判缺失了，无法被自动发现。

**用法**：
```bash
python scripts/eval_baseline.py data/discussions/discussion_xxx.md          # 可读报告
python scripts/eval_baseline.py data/discussions/discussion_xxx.md --json   # JSON 输出
python scripts/eval_baseline.py data/discussions/discussion_xxx.md --make-baseline data/baseline.json  # 首跑生成基线
python scripts/eval_baseline.py data/discussions/discussion_xxx.md --baseline data/baseline.json      # 回归则退出码 2
```

**解析**：`parse_export(text)` 按 `##` 章节 / `- key：value` 键值 / `**名**（第N轮）：` 发言行的固定格式解析导出 Markdown 为结构化信号（轮次、收敛、裁判、结论四段式、公平性、Token / 费用、发言列表等）。

**评分**：`score_report()` 按五维加权（`WEIGHTS` 总和必须为 1.0）：

| 维度 | 权重 | 评分要点 |
| --- | --- | --- |
| 结构完整 `structure` | 0.20 | 必备章节是否齐全（摘要/发言/结果/记忆，含裁判则加裁判章） |
| 完成度 `completeness` | 0.25 | 是否产出裁判裁决 / 四段式结论 / 收敛 / 任务收尾 |
| 参与度 `engagement` | 0.25 | 发言人数与轮次覆盖、发言长度是否落在合理区间（30–3000 字） |
| 公平性 `fairness` | 0.20 | 违约 0 为硬性要求，警告逐条扣分 |
| 效率 `efficiency` | 0.10 | 费用/Token 遥测是否齐备、输出占比是否健康 |

**基线对比**：`compare_baseline(report, baseline, tolerance=5.0)` 比较 overall 与各维度分，任一维度跌幅超过容差判为回归 → CLI 退出码 2（CI 中"回归即失败"）。

> 测试：`tests/test_eval_baseline.py` 覆盖解析与评分（内置样例 Markdown，不依赖真实讨论文件）。

---

*文档覆盖 `app/config / app/protocol / app/schemas / app/llm / app/embeddings / app/vector_store / app/memory / app/tools / app/rag / app/agent / app/orchestrator / app/manager / app/bgtasks / app/history / app/file_parser / app/main / app/static` 全部模块，可作为二次开发与维护的权威参考。*
