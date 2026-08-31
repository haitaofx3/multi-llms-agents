# 多 Agent 圆桌辩论系统

一个支持多个 LLM Agent 围绕"圆桌"进行**辩论式多轮协作讨论**的 Web 项目。

- **统一架构**：所有 Agent 共享同一套框架——相同的记忆系统（短期/长期/情境 + 遗忘机制）、共享同一个向量知识库。
- **独立模型**：每个 Agent 可单独挂载不同的基础模型（DeepSeek / Qwen / MiMo / 自定义，默认取 `.env` 的 `LLM_*` 配置，当前为 MiMo）。除基础模型外架构完全一致，便于观察 Agent 对话逻辑、对比不同基础模型的辩论能力。
- **主持人机制**：首个 Agent 担任辩论主持人，负责任务分派与多轮追问；其余 Agent 立论、辩护、交锋并回报结果；达到最大轮次后由主持人综合总结。
- **流式输出**：所有发言以 token 增量实时推送到界面，覆盖普通发言与任务协作模式，显著降低首字等待时间。

## 功能特性

1. **环境变量**：读取 `.env`（内置健壮解析器，兼容不规范写法）。
2. **多 Agent 对话**：支持 2–6 个 Agent 同时对话，每个 Agent 独立绑定基础模型。
3. **共享向量库 + 记忆系统**：所有 Agent 共享同一向量知识库（Qdrant，不可用时降级为内存库）；每个 Agent 拥有短期 / 长期 / 情境三类记忆，支持互相转化与可配置的遗忘机制。
4. **记忆向量检索（混合召回）**：记忆检索采用向量语义检索，综合 **语义余弦相似度（50%）+ 关键词命中覆盖率（30%）+ 有效重要度（20%）** 加权排序；内置中文友好的分词器（ASCII 词 + 中文单字），无需外部分词依赖。
5. **结构化日志治理**：统一 JSON 结构化日志（`app/logger.py`），支持 `trace_id` 全链路追踪、按事件类型（`embeddings.fallback` / `vector_store.fallback` / `judge.result` 等）归类；前端实时接收 `{"type":"log"}` 事件，Markdown 导出包含 `## 运行摘要` 统计段。
6. **任务协作分配**：首个 Agent 为主 Agent（主持人），只做任务分派（内容 + 期望效果）；协作 Agent 只回报最终结果与是否完成，不反馈中间过程；任务模式同样流式输出。
7. **MCP 工具对象**：每个 Agent 拥有自己的 MCP 注册表，可"搜索工具"完成检索 / 联网 / 记忆回忆。
8. **上下文变量**：主 Agent 分派时提供"任务接收方变量"，协作 Agent 回填后反馈给分派方。
9. **多轮辩论**：轮次未达上限时持续多轮，主持人逐轮抛出质疑、要求论据、指出分歧，协作 Agent 辩护 / 反驳。
10. **角色 / 性格约束**：对话严格遵循用户设定的角色与性格，未填写则按"机器人"角色带入。
11. **流式输出**：发言实时流式推送，剥离 `<think>` 思考片段，结束以完整文本收尾。
12. **上下文压缩与长对话管理**：当历史上下文超过阈值时自动调用 LLM 摘要压缩（不可用时回退确定性裁剪），保留最近轮次原文，压缩摘要注入系统提示，防止长对话上下文溢出。
13. **圆桌 UI（简约大气）**：席位按数量等分，可配置角色 / 性格 / 立场、发布讨论、暂停 / 继续 / 结束；界面采用低饱和深色 + 暖金点缀的克制设计语言，避免"AI 感"。
14. **聊天气泡式发言**：每次发言以独立气泡弹出（带弹出动画），只展示当前发言内容，不拼接历史；并发流式时多个 Agent 各自独立气泡同时滚动。
15. **首字母文字头像**：Agent 头像按立场配色（主持人暖金、正方鼠尾草绿、反方灰玫瑰、中立石板蓝），不依赖 emoji，降低"AI 味"。
16. **历史讨论记忆检索**：每场讨论的题目 + 元信息自动记忆化保存至 `data/discussions/index.json`（按 filename 去重，保留最近 200 条）；前端提供历史面板，支持关键词搜索题目 / trace_id、查看完整 Markdown 与一键下载。
17. **默认仅本地访问**：`start.py` 默认绑定 `127.0.0.1`，避免远端暴露 / 长期挂载；需要局域网访问时用 `HOST=0.0.0.0` 显式覆盖。
18. **文件上传解析**：共享知识库支持直接上传文档（`txt/md/csv/log`、`docx`、`xlsx`、`pdf`、图片 OCR），后端解析文本后按段注入共享向量库，方便把本地资料作为全体 Agent 的知识背景。
19. **裁判纯内容判定**：辩论结束后独立裁判对**每位辩手**分别按四维（逻辑 / 证据 / 反驳 / 清晰度）打分；全局综合得分 `scores` 由后端按各辩手分数逐维取均值**确定性计算**（避免"综合得分照抄某辩手分数"），**胜方仅由内容四维均分决定**（内容并列最高取 DRAW，**纯内容判定**）；系统同时实测各辩手流式发言的 **TTFT 与生成速率**产出模型质量分，与内容分合成综合评分（权重经 `.env` 可调）**仅作展示、不参与胜负**。评分范围仅限两位辩手，主持人发言不参与评分。
20. **REST 请求体校验**：所有写接口（`configure` / `start` / `ingest` / `save_markdown`）改用 pydantic 请求体模型（`app/schemas.py`），类型 / 形状非法时 FastAPI 返回 `422`；业务语义校验（Agent 数量、讨论状态冲突等）保留在各端点内，维持前端依赖的既有 `{ok, error}` `400` 契约，并自动生成 OpenAPI 文档。
21. **导出与落盘安全加固**：`save_markdown` 文件名经 `Path(fname).name` 清洗（只保留 basename、强制 `.md`、空名回退自动时间戳），彻底防路径穿越；讨论完成自动落盘失败不再静默吞掉，改为结构化日志 + 幂等重试（1, 2 次）。
22. **工程化与质量门禁**：`pyproject.toml` 统一 ruff / mypy / coverage 配置；`requirements-dev.txt` 提供开发依赖；新增 `tests/test_api_flow.py` 接口级测试（`httpx.ASGITransport` 直接驱动应用 + LLM 调用点打桩），无需服务器 / 真实模型即可在 CI 全量覆盖 HTTP 链路，与 `test_ci_fast` 一并构成快速回归门禁。
23. **公平性治理（FairnessGovernor L1–L6）**：以「字符数 + 相对比例」为跨模型公平的度量单位（Token 数非跨模型公平度量），替代旧的全局 Token 预算总控——L1 输出上限（相对对手近 N 轮有效输出均值 × 比例 + 硬性字符安全上限，超限事后截断）、L2 有效回答比例（剥离 `<think>` 统计）、L3 输入对称性（双方可见对手轮数一致）、L4 轮次对称性（每模型每轮至多一次发言）、L5 采样参数对齐（辩论/裁判统一 temperature / top_p）、L6 长度中性评审（裁判不以长度论优劣）。详见 `app/fairness.py`。
24. **实时状态面板（遥测 + 公平性）**：讨论页侧栏实时展示运行时长（`elapsed_time_ms`）、Token 用量（累计 `spent_tokens` / 输出 `output_tokens`，**只读遥测**，不做任何约束）与公平性治理报告（L1–L6 各层状态 + 截断/思考占比/可见轮数等指标）；前端每秒轮询 + `status_sync` 事件推送。
25. **对手定向反驳（辩论机制）**：第 1 轮基于角色/性格立论；第 2 轮引用**对手上轮发言原文**（截断至 `OPPONENT_SNIPPET_MAX_CHARS` 字）逐条反驳；第 3 轮起引用对手上轮发言的 **LLM 摘要**（`OPPONENT_SUMMARY_MAX_CHARS` 字，按 `(对手,轮次)` 缓存防重复调用，usage 计入只读遥测）——PRO↔CON 互为对手，NEUTRAL/HOST 无固定对手时回退「上一发言人」逻辑。
26. **输出字数约束（需求 5，公平性 L1）**：每轮发言受 `FairnessGovernor.ceiling_for` 字符上限约束（立论/无参照时 `FAIR_OPENING_CHAR_LIMIT`，否则相对对手近 N 轮有效输出均值 × `FAIR_OUTPUT_RATIO`，再与硬上限 `FAIR_HARD_CHAR_CEILING` 取 min）——提示词显式给出字数要求，LLM 仍超长时按字符硬截断兜底（`fairness.output_truncate` 告警）；不再把 `max_tokens` 下发给 API。
27. **按轮历史强压缩（长对话管理）**：`compress_history(keep_rounds=N)` 支持只保留最近 N 个不同轮次的原文、更早轮次经 LLM 摘要压入 `compressed_summary`（由系统提示注入）；`history` 与 `history_rounds` 平行维护、三处裁剪同步。轮次边界由 `_memory_housekeeping` 调用默认 A9 压缩（`CONTEXT_COMPRESS_THRESHOLD` 触发），保持长对话输入成本可控。
28. **记忆 LLM 摘要压缩**：记忆管家偶数轮把短期记忆聚合为一条情境记忆时，调用 `summarize_text` 生成 LLM 摘要（上限 `MEMORY_SUMMARY_MAX_CHARS` 字，usage 计入只读遥测；LLM 失败回退确定性拼接），替代原来的简单 `"近期要点："` 拼接，情境记忆更精炼、可检索性更好。
29. **多会话隔离（P1-7）**：`app/manager.py` 的 `DebateManager` 按 `session_id` 管理**独立 Orchestrator 实例**，不同浏览器 / 客户端互不干扰；前端每个浏览器在 `localStorage` 持久化一个稳定 `session_id`；会话元数据落盘 `data/sessions/{sid}.json`，进程重启后把遗留 `running/paused` 会话标记为 `interrupted` 并在前端展示「上次讨论中断」横幅。缺省会话 `"default"` 保持向后兼容。
30. **工具调用可视化（P1-4）**：流式通道新增 `tool_call_start` / `tool_call_args` 事件，前端发言气泡内实时显示"正在调用工具 X…"；`_agent_turn` 自动去重（流式已发出的工具开始事件不再重复发），纯工具回合补发 `speech_start`/`speech_end` 保证气泡不悬空——消除工具调用的"黑盒等待"。
31. **结构化结论（P0-1）**：讨论结束的最终报告固定**四段式**——「核心结论 / 各方立场与论点摘要 / 裁判裁决 / 分歧与待办」；模型输出的裁决段被权威数据（`_judge_ruling_text`）**确定性重写**，其余段缺失时确定性补齐；前端「核心结论」面板实时展示 `conclusion` 事件。
32. **费用估算展示（P1-6）**：按 token 遥测估算本场费用并展示为**人民币 ¥**（`_estimate_cost_rmb`：输入 = 累计 − 输出，单价 `COST_INPUT_PER_1M`（默认 ¥2） / `COST_OUTPUT_PER_1M`（默认 ¥8）每百万 token，可按供应商在 `.env` 覆盖）；作为**只读遥测**随 `speech_end` / `status_sync` / `/api/status` / 导出文档展示，不参与任何约束。
33. **收敛检测降级回退（P0-3）**：语义嵌入不可用（`is_fallback()`，如离线哈希向量模式）时，收敛检测自动切换到**文本相似度**（difflib `SequenceMatcher`，阈值 `CONVERGENCE_TEXT_SIMILARITY` 默认 0.6），连续 `CONVERGENCE_ROUNDS` 轮高度相似且无新证据即提前结束——嵌入缺失不再让"收敛提前终止"失效。
34. **讨论模板持久化（P1-5）**：前端支持把当前席位配置保存为模板（`localStorage`，`debate.templates.v1`），一键载入 / 删除，重复配置零成本。
35. **质量基线评估脚本（P0-2）**：`scripts/eval_baseline.py` 对一场导出的讨论 Markdown 做**五维质量评分**（结构 0.20 / 完成度 0.25 / 参与度 0.25 / 公平性 0.20 / 效率 0.10，0–100 分），可与历史基线 JSON 对比，发现质量退化时以退出码 2 报回归——改 prompt / 换模型后的质量是否退化"一测便知"。

## 目录结构

```
mini_test/
├── start.py              启动入口（uvicorn）
├── requirements.txt      依赖清单（含版本下限约束）
├── requirements-dev.txt  开发 / 质量工具依赖（ruff / mypy / coverage）
├── pyproject.toml        工程化配置（ruff / mypy / coverage）
├── .env                  环境变量配置（不提交到版本库）
├── app/                  核心应用包
│   ├── __init__.py
│   ├── config.py         环境变量与配置（Settings 单例、PROJECT_ROOT）
│   ├── schemas.py        REST 请求体 pydantic 模型（configure/start/ingest/save_markdown）
│   ├── llm.py            LLM 统一接入（OpenAI 兼容 + function calling + 流式）
│   ├── embeddings.py     文本向量化（sentence-transformers；本地无缓存模型时秒级降级哈希向量，不卡启动）
│   ├── vector_store.py   共享向量知识库（Qdrant，TCP 探活失败降级内存库，网络探测不阻塞请求线程）
│   ├── memory.py         每个 Agent 的记忆系统（三类记忆 + 转化 + 遗忘 + 混合向量检索）
│   ├── logger.py         结构化 JSON 日志（trace_id 追踪、事件归类）
│   ├── tools.py          每个 Agent 的 MCP 工具注册表 + 内置工具
│   ├── agent.py          Agent 定义（角色/性格/模型/记忆/MCP/上下文构建 + 历史压缩）
│   ├── orchestrator.py   圆桌编排器（辩论主循环、任务分派/回报、停止判定、运行摘要）
│   ├── manager.py        多会话管理器（DebateManager 会话注册表 + 元数据持久化 + 中断恢复）
│   ├── history.py        历史讨论记忆索引（data/discussions/index.json + 检索 + Markdown 读取，防路径穿越）
│   ├── file_parser.py    上传文件文本提取（txt/md/docx/xlsx/pdf/图片 OCR，懒加载依赖 + 大小/字符上限）
│   ├── main.py           FastAPI 应用（REST + WebSocket + 静态服务）
│   └── static/           前端 UI
│       ├── index.html    页面骨架（配置区 + 讨论区 + 历史面板）
│       ├── style.css     设计变量 / 圆桌席位 / 聊天气泡 / 首字母头像 / 历史面板
│       └── app.js        前端逻辑（配置、席位等分、WebSocket 事件、并发流式气泡、历史检索）
├── scripts/
│   └── eval_baseline.py 质量基线评估脚本（五维评分 0-100 + 基线对比，P0-2）
├── tests/
│   ├── test_e2e.py       端到端集成测试（需先启动服务器）
│   ├── test_ci_fast.py   快速单元测试（状态机 / 公平性 / 裁决）
│   ├── test_api_flow.py  HTTP 接口级测试（ASGITransport，无需服务器 / 无需真实 LLM）
│   ├── test_multi_session.py  多会话隔离测试（DebateManager / 会话元数据 / 中断恢复）
│   └── test_features_v2.py    新特性测试（费用估算 / 收敛降级 / 工具可视化 / 结构化结论）
├── docs/
│   └── CODE_LOGIC.md     代码逻辑详解（模块职责 → 关键函数 → 数据流）
├── data/                 运行时数据（自动生成，不提交到版本库）
│   ├── discussions/      自动保存的 Markdown 结果
│   ├── sessions/         各会话元数据快照（data/sessions/{session_id}.json）
│   └── memories/         各 Agent 记忆持久化（json）
└── README.md             本文档
```

## 快速开始

### 1. 安装依赖

```bash
# 建议使用 conda 环境（Python 3.10+）
conda create -n agent_env python=3.10
conda activate agent_env
pip install -r requirements.txt
```

### 2. 配置 .env

复制 `.env`（项目根目录）中的模板，填入：

| 配置 | 说明 |
| --- | --- |
| `LLM_MODEL_ID` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_TIMEOUT` | 默认基础模型（OpenAI 兼容协议，当前默认 MiMo `mimo-v2.5`）；`LLM_TIMEOUT` 为请求超时秒数（默认 300） |
| `QWEN_API_KEY` / `QWEN_BASE_URL` / `QWEN_MODEL_ID` | Qwen 独立配置（DashScope compatible-mode，默认 `qwen3.7-plus`） |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL_ID` | DeepSeek 独立配置（默认 `deepseek-chat`） |
| `MIMO_API_KEY` / `MIMO_BASE_URL` / `MIMO_MODEL_ID` | MiMo 独立配置（默认 `mimo-v2.5`） |
| `JUDGE_NAME` / `JUDGE_PROVIDER` / `JUDGE_BASE_URL` / `JUDGE_API_KEY` / `JUDGE_MODEL_ID` | 独立裁判（可选，未配置时回退用主 Agent 模型） |
| `JUDGE_CONTENT_WEIGHT` / `JUDGE_PERFORMANCE_WEIGHT` | 综合评分权重（内容分 × 前者 + 模型质量分 × 后者，默认 0.8 / 0.2）——综合评分**仅作展示**，胜方由内容四维均分决定（纯内容判定） |
| `SERPAPI_API_KEY` / `TAVILY_API_KEY` | 联网搜索工具（可选） |
| `QDRANT_URL` 等 | 向量数据库（可选，未配置时自动降级为内存库） |
| `EMBED_MODEL_TYPE` / `EMBED_MODEL_NAME` | 嵌入模型（默认本地 sentence-transformers） |
| `CONTEXT_MAX_CHARS` | 单 Agent 上下文字符硬上限，超出时丢弃最旧轮次（默认 8000） |
| `CONTEXT_COMPRESS_THRESHOLD` | 触发 LLM 摘要压缩的字符阈值（默认 6000） |
| `CONTEXT_COMPRESS_KEEP_RECENT` | 摘要压缩后保留的最近轮次条数（默认 8） |
| `OPPONENT_SNIPPET_MAX_CHARS` / `OPPONENT_SUMMARY_MAX_CHARS` | 辩论机制：第 2 轮对手发言原文截断上限（默认 500 字）、第 3 轮起对手发言 LLM 摘要上限（默认 200 字） |
| `MEMORY_SUMMARY_MAX_CHARS` | 记忆压缩：短期 → 情境的 LLM 聚合摘要上限（默认 200 字），摘要 usage 计入只读遥测 |
| `FAIR_OUTPUT_RATIO` | 公平性 L1：输出上限 = 对手近 N 轮有效输出均值 × 该比例（默认 1.5） |
| `FAIR_HARD_CHAR_CEILING` | 公平性 L1：单次发言硬性字符安全上限（默认 3000），超限事后截断 |
| `FAIR_FLOOR_CHAR_LIMIT` / `FAIR_OPENING_CHAR_LIMIT` / `FAIR_REFERENCE_ROUNDS` | 公平性 L1：相对基准下限（默认 400）、立论/无参照时上限（默认 1200）、参照对手最近 N 轮有效输出（默认 2） |
| `FAIR_THINK_RATIO_MAX` | 公平性 L2：思考/推理占比上限（默认 0.5），超过记 warning |
| `FAIR_MAX_SEEN_DIFF` | 公平性 L3：PRO/CON 可见对手轮数允许的最大差值（默认 1） |
| `CONVERGENCE_SIMILARITY` / `CONVERGENCE_ROUNDS` | 收敛检测：语义余弦相似度阈值（默认 0.85）、连续相似轮数（默认 2） |
| `CONVERGENCE_TEXT_SIMILARITY` | 收敛检测降级（P0-3）：嵌入不可用时文本相似度阈值（默认 0.6，difflib SequenceMatcher） |
| `COST_INPUT_PER_1M` / `COST_OUTPUT_PER_1M` | 费用估算单价（P1-6，人民币 ¥/百万 token，默认 2.0 / 8.0；按供应商价格覆盖） |
| `FAIR_TEMPERATURE` / `FAIR_TOP_P` | 公平性 L5：辩论发言统一采样参数（默认 0.7 / 1.0） |
| `FAIR_JUDGE_TEMPERATURE` / `FAIR_JUDGE_TOP_P` | 公平性 L5：辅助 LLM（裁判/摘要）统一采样参数（默认 0.3 / 1.0） |

### 3. 启动

```bash
python start.py
# 浏览器打开 http://localhost:8000
```

> **默认仅绑定本地**：`start.py` 监听 `127.0.0.1`，不会被局域网 / 公网访问到（调试结束即关闭，避免长期挂载）。如需临时开放局域网访问：
>
> ```bash
> HOST=0.0.0.0 PORT=8000 python start.py
> ```

> **无外网环境**：本地嵌入模型（sentence-transformers）若未缓存，启动时通过毫秒级缓存探测直接降级为确定性哈希向量，**不会卡住启动**；需要完整语义检索能力时请提前下载模型缓存：
>
> ```bash
> NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
> HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python start.py
> ```
>
> `NO_PROXY` 用于避免本地请求被系统代理拦截；未配置代理时可省略。即使缺少模型缓存，混合检索仍可基于关键词覆盖度与重要度正常排序。

## 使用流程

1. **设置 Agent**：选择 Agent 数量（2–6），逐个填写名称、角色、性格，并为每个 Agent 选择基础模型（默认取 `.env` 的 `LLM_*`（当前为 MiMo），可选 qwen / deepseek / mimo / 自定义）。
2. **设置讨论**：填写讨论主题 / 任务，设置最大轮次；可选在"共享知识"中注入文本作为全体 Agent 的知识背景。
3. **进入圆桌**：点击"进入圆桌并开始讨论"，观察圆桌席位上的 Agent 轮流流式发言（聊天气泡）、任务分派与回报。
4. **随时控制**：可暂停 / 继续 / 结束讨论。
5. **导出记录**：讨论完成或结束后，可导出 / 下载 Markdown 记录（自动保存于 `data/discussions/`，末尾附 `## 运行摘要` 统计：时长 / 轮次 / 收敛 / RAG 证据 / 任务 / 历史压缩 / Token 遥测 / 公平性）。
6. **历史检索**：顶部"历史记录"面板按时间倒序展示全部历史讨论（题目 + 状态 + 轮次 + 人数），支持关键词搜索（题目 / trace_id）、点击查看完整 Markdown、一键下载；每场讨论完成或保存时自动写入记忆索引。

## 运行测试

快速单元测试（状态机 / 公平性 / 裁决，无需启动服务器）：

```bash
python -m unittest tests.test_ci_fast -v
```

HTTP 接口级测试（`httpx.ASGITransport` 直接驱动 FastAPI 应用，LLM 调用点打桩，无需服务器 / 无需真实模型；覆盖 configure → start → done → export → save_markdown → history 全链路与路径穿越防护）：

```bash
python -m unittest tests.test_api_flow -v
```

多会话隔离与新特性测试（DebateManager 会话注册表 / 会话元数据 / 中断恢复；费用估算 / 收敛降级 / 工具调用可视化 / 结构化结论，均无需服务器与真实模型）：

```bash
python -m unittest tests.test_multi_session tests.test_features_v2 -v
```

质量基线评估（P0-2）：对一场导出的讨论 Markdown 打五维质量分，并可与基线对比发现回归：

```bash
python scripts/eval_baseline.py data/discussions/discussion_xxx.md          # 可读报告
python scripts/eval_baseline.py data/discussions/discussion_xxx.md --json   # JSON 输出
python scripts/eval_baseline.py data/discussions/discussion_xxx.md --make-baseline data/baseline.json  # 首跑生成基线
python scripts/eval_baseline.py data/discussions/discussion_xxx.md --baseline data/baseline.json      # 回归则退出码 2
```

静态检查与类型检查（需先安装 `requirements-dev.txt`）：

```bash
python -m ruff check app tests scripts
python -m mypy
python -m coverage run -m unittest discover tests   # 可选：测试覆盖率
```

端到端集成测试会真实调用 LLM 发起一场讨论（3 Agent × 2 轮，全部使用 qwen，含独立裁判），并校验流式事件（`speech_start` / `speech_delta` / `speech_end` 配对、`log` 事件、`judgment` 等，`judgment` 需包含 `agent_scores` / `model_performance` / `composite_scores`）与 Markdown 导出：

```bash
# 终端 1：启动服务器
python start.py

# 终端 2：运行测试
python tests/test_e2e.py
```

## 深入学习

模块职责、关键函数与数据流的完整梳理见 [`docs/CODE_LOGIC.md`](docs/CODE_LOGIC.md)。

P0 / P1 优化项已实施落地：

- **A3 记忆检索升级为向量语义检索**：`memory.py` 混合召回（语义 50% + 关键词 30% + 重要度 20%），嵌入写入时缓存、旧数据按需回填。
- **A4 结构化日志治理**：`logger.py` JSON 结构化日志 + `trace_id` 全链路追踪 + `{"type":"log"}` 实时前端推送 + Markdown 运行摘要。
- **A8 任务协作模式流式输出**：任务分派 / 回报走流式通道，与普通发言一致的增量推送体验。
- **A9 上下文压缩与长对话管理**：超阈值自动摘要压缩，保留最近原文，防止上下文溢出。
- **UI / UX 升级**：简约大气的低饱和深色设计、聊天气泡式发言（并发流式独立气泡）、首字母文字头像（按立场配色）。
- **历史记忆化**：`app/history.py` 将每场讨论题目与元信息持久化到 `data/discussions/index.json`，提供 `/api/history` 检索与一键查看 / 下载。
- **安全加固**：`start.py` 默认绑定 `127.0.0.1`，关闭远端暴露；历史读取带路径穿越防护。
- **裁判综合评分**：`orchestrator._compute_composite` 按「内容分（agent_scores 四维均值）× `JUDGE_CONTENT_WEIGHT` + 模型质量分（TTFT / 生成速率实测，`_score_ttft` / `_score_rate` 分档）× `JUDGE_PERFORMANCE_WEIGHT`」合成综合分并据此推导胜方；裁判只看辩手发言（`_build_judge_debate_text` 剔除主持人），质量分只统计协作 Agent 的流式发言指标。
- **P0-1 路径穿越修复**：`save_markdown` 文件名经 `Path(fname).name` 清洗（只保留 basename、强制 `.md`、空名回退时间戳），配合 `history.get_markdown` 的 `resolve()` + `relative_to()`，读写两侧均无法越出 `data/discussions/`。
- **P0-2 REST 请求体 pydantic 校验**：`app/schemas.py` 为 `configure / start / ingest / save_markdown` 定义请求模型；类型 / 形状非法返回 `422`，业务语义校验保留在各端点内，维持前端依赖的 `{ok, error}` `400` 契约。
- **P1-3 lifespan 落盘可靠性**：讨论完成自动保存失败不再静默吞掉，改为结构化日志（`lifespan.save_markdown`）+ 幂等重试（1, 2 次）。
- **P1-4 接口级测试 + CI 静态检查**：`tests/test_api_flow.py`（`httpx.ASGITransport` + LLM 调用点打桩）覆盖 HTTP 全链路与安全用例；ruff / mypy 纳入 CI 质量门禁。
- **P1-5 依赖与工程化配置**：新增 `pyproject.toml`（ruff / mypy / coverage）与 `requirements-dev.txt`，`requirements.txt` 全部依赖改为 `>=` 版本下限。
- **辩论机制：对手定向反驳 + 输出约束 + 按轮强压缩**：`orchestrator._find_opponent`（PRO↔CON 立场映射，回退全局最近）→ `_prepare_opponent_context`（第 2 轮原文截断 / 第 3 轮起 `summarize_text` LLM 摘要，缓存 + 只读遥测记账）→ `_debate_prompt` 注入 + 全轮字数提示（公平性 L1 字符上限）；`_agent_turn` 超长截断兜底；`agent.history_rounds` 平行维护、`compress_history(keep_rounds=N)` 按轮强压缩（详见 `docs/CODE_LOGIC.md` §12.8）。
- **P1-7 完整多会话重构**：`app/manager.py` `DebateManager` 按 `session_id` 管理独立 `Orchestrator` 实例；前端 `localStorage` 持久化 `session_id`；`data/sessions/{sid}.json` 元数据快照 + 启动时 `mark_interrupted()` 中断恢复横幅；所有 REST/WS 端点经 `_get_orch(session_id)` 路由（详见 `docs/CODE_LOGIC.md` §12.15）。
- **P1-4 工具调用可视化**：`chat_stream` 新增 `tool_call_start`/`tool_call_args`，`_consume_stream` 桥接 `tool_starts` 去重，前端气泡显示"正在调用工具 X…"（详见 `docs/CODE_LOGIC.md` §12.4）。
- **P0-1 结构化结论**：`_normalize_conclusion` 固定四段式 + 裁决段权威重写，`_final_summary` 发布 `conclusion` 事件，前端核心结论面板（详见 `docs/CODE_LOGIC.md` §12.13）。
- **P0-3 收敛检测降级**：`is_fallback()` 时 `_check_convergence` 切换到 `_text_similarity`（difflib，阈值 `CONVERGENCE_TEXT_SIMILARITY`=0.6），保证离线哈希向量模式仍能收敛提前终止（详见 `docs/CODE_LOGIC.md` §12.10）。
- **P1-6 费用估算展示（¥）**：`_estimate_cost_rmb` 只读遥测随 `speech_end`/`status_sync`/导出展示（详见 `docs/CODE_LOGIC.md` §12.16）。
- **P1-5 配置模板**：前端 `localStorage` 模板保存 / 载入 / 删除。
- **P0-2 质量基线**：`scripts/eval_baseline.py` 五维评分 + 基线对比（详见 `docs/CODE_LOGIC.md` §21）。
- **P2 分析与方案**：详见 [`P2优化方案.md`](P2优化方案.md)——平台化主干（A10 多实例 / A11 人工审核 / A12 知识库后台 / A13 模板与快照）+ 增补议题（裁判增强 / 日志落盘与检索 / 前端测试与 CI / 鉴权与网络安全 / 数据治理 / 完整会话恢复）。

## 项目依赖

- **Web 框架**：`fastapi` + `uvicorn`
- **LLM**：`openai`（OpenAI 兼容协议）
- **向量库**：`qdrant-client`（可选，不可用时降级内存库）
- **嵌入**：`sentence-transformers` + `numpy`（无缓存模型时自动降级哈希向量）
- **其他**：`httpx`
- **开发 / 质量工具**（`requirements-dev.txt`）：`ruff`（静态检查）、`mypy`（类型检查，当前覆盖 `app/protocol.py`）、`coverage`（测试覆盖率）

> 生产 / 运行依赖与开发依赖分离：`pip install -r requirements.txt` 即可运行；`pip install -r requirements-dev.txt` 额外安装静态检查与测试工具。`requirements.txt` 中的依赖均带 `>=` 版本下限，`pyproject.toml` 声明 `requires-python = ">=3.10"`。
