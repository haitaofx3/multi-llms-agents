# 多 Agent 圆桌协作系统

一个支持多个 LLM Agent 围绕"圆桌"进行**辩论式多轮协作讨论**的 Web 项目。

- **统一架构**：所有 Agent 共享同一套框架——相同的记忆系统（短期/长期/情境 + 遗忘机制）、共享同一个向量知识库。
- **独立模型**：每个 Agent 可单独挂载不同的基础模型（DeepSeek / Qwen / 自定义，默认取 `.env`）。除基础模型外架构完全一致，便于观察 Agent 对话逻辑、对比不同基础模型的辩论能力。
- **主持人机制**：首个 Agent 担任辩论主持人，负责任务分派与多轮追问；其余 Agent 立论、辩护、交锋并回报结果；达到最大轮次后由主持人综合总结。
- **流式输出**：所有发言以 token 增量实时推送到界面，显著降低首字等待时间。

## 功能特性

1. **环境变量**：读取 `.env`（内置健壮解析器，兼容不规范写法）。
2. **多 Agent 对话**：支持 2–6 个 Agent 同时对话，每个 Agent 独立绑定基础模型。
3. **共享向量库 + 记忆系统**：所有 Agent 共享同一向量知识库（Qdrant，不可用时降级为内存库）；每个 Agent 拥有短期 / 长期 / 情境三类记忆，支持互相转化与可配置的遗忘机制。
4. **任务协作分配**：首个 Agent 为主 Agent（主持人），只做任务分派（内容 + 期望效果）；协作 Agent 只回报最终结果与是否完成，不反馈中间过程。
5. **MCP 工具对象**：每个 Agent 拥有自己的 MCP 注册表，可"搜索工具"完成检索 / 联网 / 记忆回忆。
6. **上下文变量**：主 Agent 分派时提供"任务接收方变量"，协作 Agent 回填后反馈给分派方。
7. **多轮辩论**：轮次未达上限时持续多轮，主持人逐轮抛出质疑、要求论据、指出分歧，协作 Agent 辩护 / 反驳。
8. **角色 / 性格约束**：对话严格遵循用户设定的角色与性格，未填写则按"机器人"角色带入。
9. **流式输出**：发言实时流式推送，剥离 `<think>` 思考片段，结束以完整文本收尾。
10. **圆桌 UI**：席位按数量等分，可配置角色 / 性格、发布讨论、暂停 / 继续 / 结束，结果以 Markdown 保存与下载。

## 目录结构

```
mini_test/
├── start.py              启动入口（uvicorn）
├── requirements.txt      依赖清单
├── .env                  环境变量配置（不提交到版本库）
├── app/                  核心应用包
│   ├── __init__.py
│   ├── config.py         环境变量与配置（Settings 单例、PROJECT_ROOT）
│   ├── llm.py            LLM 统一接入（OpenAI 兼容 + function calling + 流式）
│   ├── embeddings.py     文本向量化（sentence-transformers，失败降级哈希）
│   ├── vector_store.py   共享向量知识库（Qdrant，失败降级内存库）
│   ├── memory.py         每个 Agent 的记忆系统（三类记忆 + 转化 + 遗忘）
│   ├── tools.py          每个 Agent 的 MCP 工具注册表 + 内置工具
│   ├── agent.py          Agent 定义（角色/性格/模型/记忆/MCP/上下文构建）
│   ├── orchestrator.py   圆桌编排器（辩论主循环、任务分派/回报、停止判定）
│   ├── main.py           FastAPI 应用（REST + WebSocket + 静态服务）
│   └── static/           前端 UI
│       ├── index.html    页面骨架（配置区 + 讨论区）
│       ├── style.css     圆桌/席位/发言样式
│       └── app.js        前端逻辑（配置、席位等分、WebSocket 事件）
├── tests/
│   └── test_e2e.py       端到端集成测试（需先启动服务器）
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
| `LLM_MODEL_ID` / `LLM_API_KEY` / `LLM_BASE_URL` | 默认基础模型（OpenAI 兼容协议，如 DeepSeek / Qwen / 自建服务） |
| `DEEPSEEK_API_KEY` 等 | DeepSeek 独立配置（可选） |
| `SERPAPI_API_KEY` / `TAVILY_API_KEY` | 联网搜索工具（可选） |
| `QDRANT_URL` 等 | 向量数据库（可选，未配置时自动降级为内存库） |
| `EMBED_MODEL_TYPE` / `EMBED_MODEL_NAME` | 嵌入模型（默认本地 sentence-transformers） |

### 3. 启动

```bash
python start.py
# 浏览器打开 http://localhost:8000
```

> **无外网环境**：本地嵌入模型（sentence-transformers）首次加载会尝试从 HuggingFace 下载，网络不通会卡住启动。此时加离线变量强制走确定性哈希向量兜底：
>
> ```bash
> NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
> HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python start.py
> ```
>
> `NO_PROXY` 用于避免本地请求被系统代理拦截；未配置代理时可省略。

## 使用流程

1. **设置 Agent**：选择 Agent 数量（2–6），逐个填写名称、角色、性格，并为每个 Agent 选择基础模型（默认 `qwen`，可选 deepseek / qwen / 自定义）。
2. **设置讨论**：填写讨论主题 / 任务，设置最大轮次；可选在"共享知识"中注入文本作为全体 Agent 的知识背景。
3. **进入圆桌**：点击"进入圆桌并开始讨论"，观察圆桌席位上的 Agent 轮流流式发言、任务分派与回报。
4. **随时控制**：可暂停 / 继续 / 结束讨论。
5. **导出记录**：讨论完成或结束后，可导出 / 下载 Markdown 记录（自动保存于 `data/discussions/`）。

## 运行测试

端到端集成测试会真实调用 LLM 发起一场讨论（3 Agent × 3 轮），并校验流式事件与 Markdown 导出：

```bash
# 终端 1：启动服务器
python start.py

# 终端 2：运行测试
python tests/test_e2e.py
```

## 深入学习

模块职责、关键函数与数据流的完整梳理见 [`docs/CODE_LOGIC.md`](docs/CODE_LOGIC.md)。

## 项目依赖

- **Web 框架**：`fastapi` + `uvicorn`
- **LLM**：`openai`（OpenAI 兼容协议）
- **向量库**：`qdrant-client`（可选，不可用时降级内存库）
- **嵌入**：`sentence-transformers` + `numpy`
- **其他**：`httpx`
