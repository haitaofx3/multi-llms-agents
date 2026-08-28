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
19. **裁判综合评分**：辩论结束后独立裁判对**每位辩手**分别按四维（逻辑 / 证据 / 反驳 / 清晰度）打分；系统同时实测各辩手流式发言的 **TTFT 与生成速率**产出模型质量分，最终按「内容分 × 80% + 模型质量分 × 20%」得到综合评分（权重经 `.env` 可调），并由综合分推导胜方。评分范围仅限两位辩手，主持人发言不参与评分。
20. **REST 请求体校验**：所有写接口（`configure` / `start` / `ingest` / `save_markdown`）改用 pydantic 请求体模型（`app/schemas.py`），类型 / 形状非法时 FastAPI 返回 `422`；业务语义校验（Agent 数量、讨论状态冲突等）保留在各端点内，维持前端依赖的既有 `{ok, error}` `400` 契约，并自动生成 OpenAPI 文档。
21. **导出与落盘安全加固**：`save_markdown` 文件名经 `Path(fname).name` 清洗（只保留 basename、强制 `.md`、空名回退自动时间戳），彻底防路径穿越；讨论完成自动落盘失败不再静默吞掉，改为结构化日志 + 幂等重试（1, 2 次）。
22. **工程化与质量门禁**：`pyproject.toml` 统一 ruff / mypy / coverage 配置；`requirements-dev.txt` 提供开发依赖；新增 `tests/test_api_flow.py` 接口级测试（`httpx.ASGITransport` 直接驱动应用 + LLM 调用点打桩），无需服务器 / 真实模型即可在 CI 全量覆盖 HTTP 链路，与 `test_ci_fast` 一并构成快速回归门禁。
23. **Token 预算规划与自适应校准**：每轮开始前用纯数学规划求解本轮生效输出上限 `effective_max_tokens`（`_compute_plan`），让「剩余轮次 + 裁判 + 汇总」落在预算内；预算吃紧时走降级阶梯（降输出上限 → 强制压缩历史 → 允许小幅超支），辩论永不因预算中断。规划用量经两路 EMA 自适应校准（`_est_factor` 提示词尺寸、`_call_factor` 每轮调用次数），真实冒烟 2 轮短辩论超支从 42% 收敛到 4.3%（长辩论输入成本建模见 `docs/CODE_LOGIC.md` §20）。
24. **预算状态前端面板**：讨论页侧栏实时可视化 token 预算运行状态——已用/剩余/总量进度条（超预算标红）、生效输出上限（降级标注）、预计总耗超支比、可支撑轮数与安全余量、三路校准因子（提示词/调用/增长）；预算告警事件（削轮次/超支/保底/压缩/熔断）以彩色标签结构化展示，全量文本 `textContent` 填充防 XSS。
25. **对手定向反驳（辩论机制）**：第 1 轮基于角色/性格立论；第 2 轮引用**对手上轮发言原文**（截断至 `OPPONENT_SNIPPET_MAX_CHARS` 字）逐条反驳；第 3 轮起引用对手上轮发言的 **LLM 摘要**（`OPPONENT_SUMMARY_MAX_CHARS` 字，按 `(对手,轮次)` 缓存防重复调用，usage 计入预算）——PRO↔CON 互为对手，NEUTRAL/HOST 无固定对手时回退「上一发言人」逻辑。
26. **输出字数约束（需求 5）**：每轮发言都受 `effective_max_tokens × OUTPUT_CHAR_PER_TOKEN` 字数上限约束——提示词显式给出字数要求（API `max_tokens` 同步生效），LLM 仍超长时按字数硬截断兜底（`debate.output_truncate` 告警），保证单轮输出可控、不挤占后续轮次预算。
27. **预算吃紧按轮强压缩（需求 6）**：预算不足触发 `_force_compress_all` 时，只保留最近 `BUDGET_KEEP_LAST_ROUNDS`（默认 2）轮的原文，更早轮次经 LLM 摘要压入 `compressed_summary`（由系统提示注入）；`history` 与 `history_rounds` 平行维护、三处裁剪同步，长辩论下输入成本可控。
28. **记忆 LLM 摘要压缩**：记忆管家偶数轮把短期记忆聚合为一条情境记忆时，调用 `summarize_text` 生成 LLM 摘要（上限 `MEMORY_SUMMARY_MAX_CHARS` 字，usage 计入预算；LLM 失败回退确定性拼接），替代原来的简单 `"近期要点："` 拼接，情境记忆更精炼、可检索性更好。

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
│   ├── history.py        历史讨论记忆索引（data/discussions/index.json + 检索 + Markdown 读取，防路径穿越）
│   ├── file_parser.py    上传文件文本提取（txt/md/docx/xlsx/pdf/图片 OCR，懒加载依赖 + 大小/字符上限）
│   ├── main.py           FastAPI 应用（REST + WebSocket + 静态服务）
│   └── static/           前端 UI
│       ├── index.html    页面骨架（配置区 + 讨论区 + 历史面板）
│       ├── style.css     设计变量 / 圆桌席位 / 聊天气泡 / 首字母头像 / 历史面板
│       └── app.js        前端逻辑（配置、席位等分、WebSocket 事件、并发流式气泡、历史检索）
├── tests/
│   ├── test_e2e.py       端到端集成测试（需先启动服务器）
│   ├── test_ci_fast.py   快速单元测试（状态机 / 预算 / 裁决）
│   └── test_api_flow.py  HTTP 接口级测试（ASGITransport，无需服务器 / 无需真实 LLM）
├── docs/
│   └── CODE_LOGIC.md     代码逻辑详解（模块职责 → 关键函数 → 数据流）
├── data/                 运行时数据（自动生成，不提交到版本库）
│   ├── discussions/      自动保存的 Markdown 结果
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
| `JUDGE_CONTENT_WEIGHT` / `JUDGE_PERFORMANCE_WEIGHT` | 综合评分权重（内容分 × 前者 + 模型质量分 × 后者，默认 0.8 / 0.2） |
| `SERPAPI_API_KEY` / `TAVILY_API_KEY` | 联网搜索工具（可选） |
| `QDRANT_URL` 等 | 向量数据库（可选，未配置时自动降级为内存库） |
| `EMBED_MODEL_TYPE` / `EMBED_MODEL_NAME` | 嵌入模型（默认本地 sentence-transformers） |
| `CONTEXT_MAX_CHARS` | 单 Agent 上下文字符硬上限，超出时丢弃最旧轮次（默认 8000） |
| `CONTEXT_COMPRESS_THRESHOLD` | 触发 LLM 摘要压缩的字符阈值（默认 6000） |
| `CONTEXT_COMPRESS_KEEP_RECENT` | 摘要压缩后保留的最近轮次条数（默认 8） |
| `OPPONENT_SNIPPET_MAX_CHARS` / `OPPONENT_SUMMARY_MAX_CHARS` | 辩论机制：第 2 轮对手发言原文截断上限（默认 500 字）、第 3 轮起对手发言 LLM 摘要上限（默认 200 字） |
| `MEMORY_SUMMARY_MAX_CHARS` | 记忆压缩：短期 → 情境的 LLM 聚合摘要上限（默认 200 字），摘要 usage 计入预算 |
| `OUTPUT_CHAR_PER_TOKEN` | 输出字数约束：每 token 换算字数（中文约 2 字符/token，默认 2），用于把 `effective_max_tokens` 换算成发言字数上限 |
| `BUDGET_KEEP_LAST_ROUNDS` | 预算吃紧强压缩时保留的最近轮次数（默认 2），更早轮次压入压缩摘要 |
| `BUDGET_*`（`BUDGET_GROWTH_RATIO_INIT` / `BUDGET_GROWTH_RATIO_MIN` / `BUDGET_GROWTH_RATIO_MAX` / `BUDGET_SAFETY_MARGIN` / `EST_FACTOR_INIT` / `BUDGET_PROFILE_PATH` / `JUDGE_MIN_OUTPUT` / `SUMMARY_MIN_OUTPUT` / `BUDGET_AUTO_TRIM_ROUNDS` / `BUDGET_OUTPUT_FLOOR` / `BUDGET_TAIL_RESERVE` / `BUDGET_TOOL_OVERHEAD`） | token 预算规划器：历史增长比（EMA 校准，默认 0.5）、安全余量（默认 8%）、初始估算因子（默认 2.0 保守先验）、校准档案路径（默认 `data/budget_profile.json`）、裁判/汇总输出保底（默认 512）、预算不足时自动削减轮次（默认开启）。预算吃紧时按「降输出上限 → 强制压缩历史 → 削轮次 → 允许小幅超支」阶梯降级，辩论不中断 |

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
5. **导出记录**：讨论完成或结束后，可导出 / 下载 Markdown 记录（自动保存于 `data/discussions/`，末尾附 `## 运行摘要` 统计：时长 / 轮次 / 收敛 / RAG 证据 / 任务 / 历史压缩 / 预算）。
6. **历史检索**：顶部"历史记录"面板按时间倒序展示全部历史讨论（题目 + 状态 + 轮次 + 人数），支持关键词搜索（题目 / trace_id）、点击查看完整 Markdown、一键下载；每场讨论完成或保存时自动写入记忆索引。

## 运行测试

快速单元测试（状态机 / 预算 / 裁决，无需启动服务器）：

```bash
python -m unittest tests.test_ci_fast -v
```

HTTP 接口级测试（`httpx.ASGITransport` 直接驱动 FastAPI 应用，LLM 调用点打桩，无需服务器 / 无需真实模型；覆盖 configure → start → done → export → save_markdown → history 全链路与路径穿越防护）：

```bash
python -m unittest tests.test_api_flow -v
```

静态检查与类型检查（需先安装 `requirements-dev.txt`）：

```bash
python -m ruff check app tests
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
- **辩论机制：对手定向反驳 + 输出约束 + 按轮强压缩**：`orchestrator._find_opponent`（PRO↔CON 立场映射，回退全局最近）→ `_prepare_opponent_context`（第 2 轮原文截断 / 第 3 轮起 `summarize_text` LLM 摘要，缓存 + 预算记账）→ `_debate_prompt` 注入 + 全轮字数提示；`_agent_turn` 超长截断兜底；`agent.history_rounds` 平行维护、`compress_history(keep_rounds=BUDGET_KEEP_LAST_ROUNDS)` 按轮强压缩（详见 `docs/CODE_LOGIC.md` §12.7–12.8）。

## 项目依赖

- **Web 框架**：`fastapi` + `uvicorn`
- **LLM**：`openai`（OpenAI 兼容协议）
- **向量库**：`qdrant-client`（可选，不可用时降级内存库）
- **嵌入**：`sentence-transformers` + `numpy`（无缓存模型时自动降级哈希向量）
- **其他**：`httpx`
- **开发 / 质量工具**（`requirements-dev.txt`）：`ruff`（静态检查）、`mypy`（类型检查，当前覆盖 `app/protocol.py`）、`coverage`（测试覆盖率）

> 生产 / 运行依赖与开发依赖分离：`pip install -r requirements.txt` 即可运行；`pip install -r requirements-dev.txt` 额外安装静态检查与测试工具。`requirements.txt` 中的依赖均带 `>=` 版本下限，`pyproject.toml` 声明 `requires-python = ">=3.10"`。
