# muti_llm_agent · 多模型协同工作台

**一句话定位**：一个人用的多模型协同工作台 —— 你出题、定方向、做判断，DeepSeek / Qwen / MiMo 等**真实不同的模型**按「规划 · 研究 · 撰写 · 审核」分工推进；你可以在运行**中途纠偏**，结束后**补材料续轮**，最后把整场运行导出成可复盘的材料。

## 与旧项目的关系：迁移 + 转型

本次不是「给旧系统加功能」，而是**迁移 + 转型**：

| | 旧项目（`mini_test`，包名 `app`） | 本项目（`Muti_LLM_Agent`，包名 `muti_llm_agent`） |
| --- | --- | --- |
| 定位 | 单模型扮多角色的**圆桌辩论演示** | 单用户个人级的**真实多模型协同工作台** |
| 模型 | 一个模型扮演正方 / 反方 / 主持人 / 裁判 | 不同供应商的模型各干擅长的活（也可切回辩论模式） |
| 运行期干预 | 只能暂停 / 继续 / 结束 | 中途注入纠偏指令；结束后补材料续轮 |
| 接口 | 15 个（14 HTTP + 1 WebSocket），全部挤在 `main.py` | 按 7 个模块拆到 `api/router_*.py`（清单由 `GET /api/dev/endpoints` 导出） |
| 可观测性 | `trace_id` + 散落的 print 日志 | 结构化 JSON 日志（三条出口）+ 运行事件日志 + 运行摘要 + 复盘导出 |
| 对外集成 | 无 | 飞书入站 / 出站、DSH 工具调用、通用 webhook、SSE |

辩论能力没丢：`MODE=debate` 仍是原来的圆桌辩论 + 独立裁判；`MODE=collaboration` 是本项目的主用法。迁移细节（包名批量替换、pyproject / CI / start.py、以及三个测试的 patch 目标为什么变了）见 [docs/MIGRATION.md](docs/MIGRATION.md)；49 个接口的完整参考见 [docs/API.md](docs/API.md)。

## 快速开始

```powershell
# 1) 环境（本机固定用 agent_env，Python 3.10）
conda activate agent_env
pip install -r requirements.txt          # 首次；质量工具另装 requirements-dev.txt

# 2) 配置（可选）：cp .env.example .env 后再改
#    不建 .env 也能直接启动——所有键都有内置默认值（见 muti_llm_agent/config.py）。
#    要真正跑起来，最少在 .env 里填 LLM_MODEL_ID / LLM_API_KEY / LLM_BASE_URL；
#    想真正「多模型」，再把 QWEN_* / DEEPSEEK_* / MIMO_* 三组 Key 填上。
#    .env 已被 gitignore（存真实密钥）；.env.example 是可提交的模板，二者键集合保持同步。

# 3) 启动
python start.py                          # 默认 http://127.0.0.1:8000
```

三个入口（均已在运行中的实例上实测返回 200）：

| 入口 | 用途 |
| --- | --- |
| `/` | 圆桌 UI（WebSocket 实时流式 + 协同配方 / 中途纠偏 / 运行摘要 / 补充材料续轮面板） |
| `/console` | 接口控制台（49 个接口按模块自动生成表单，可一键冒烟） |
| `/docs` | FastAPI 自动生成的 OpenAPI 文档 |

- **端口与绑定**：`start.py` 读环境变量 `HOST`（默认 `127.0.0.1`）与 `PORT`（默认 `8000`）。例：`$env:PORT=8123; python start.py`。默认只绑回环。
- **健康检查**：`GET /healthz` → `{"ok":true,"service":"muti_llm_agent","workspace":...,"sessions":N,"auth_required":false}`。
- **一旦对外暴露**（`HOST=0.0.0.0`）：**必须**在 `.env` 设置 `AUTH_TOKEN`，否则任何能访问该端口的人都能改写配置、发起消耗真实模型额度的运行。注意白名单只有 `/`、`/favicon.ico`、`/console`、`/static/*`——设了 `AUTH_TOKEN` 之后，`/healthz`、`/docs`、`/openapi.json` **也要带令牌**，探活脚本要相应加 `Authorization: Bearer`（或 `?token=`）。
- `start.py` 关掉了 uvicorn 访问日志（`access_log=False`）：本项目自己输出带 `trace_id` 的 JSON 日志，两套日志并存反而难对齐。

## 六项能力现状

### 1. 单用户个人级 + 多模型协同

**做了什么**：把「模型」当可编排资源列出来（花名册），把「谁干什么」固化成 5 个可直接套用的配方；配方写的是**供应商意图**，下发时按你当前配好的 Key 自动改派。

**在哪个文件**：`muti_llm_agent/models_registry.py`（`PROVIDER_LABELS` / `provider_status()` / `roster()` / `RECIPES` / `build_agent_configs()`）。

**怎么用**：

- `GET /api/models` → 花名册：每个供应商是否已配置 Key（脱敏，不含 api_key）、默认配方、配方列表。
- `GET /api/recipes` → 配方列表；`GET /api/recipes/{name}` → 直接生成 `/api/configure` 可用的 `agents`（含 `judge`、`notes` 改派说明、`available_providers`）。
- 5 个配方：`collab-4`（规划/研究/撰写/审核，默认）、`collab-3`（极速三席）、`panel-5`（五模型多视角评审）、`debate-3`（三方辩论 + 独立裁判）、`dual-2`（双模型对练，适合联调）。
- 讨论模式：`debate`（辩论，原能力）/ `collaboration`（合作）。三处可设，优先级从高到低：`POST /api/configure` 的 `mode` → `POST /api/start` 的 `mode`（可覆盖）→ `.env` 的 `DISCUSSION_MODE`（默认 `debate`）；非法值一律落回 `debate`。

```bash
curl -s http://127.0.0.1:8000/api/recipes/collab-4
curl -s -X POST http://127.0.0.1:8000/api/configure -H 'Content-Type: application/json' \
  -d '{"session_id":"demo","mode":"collaboration","topic":"写一份上线检查单","max_rounds":4,"agents":[...]}'
```

**注意**：`PERSONAL_MODE=1` 目前只是**定位标记**（`/api/defaults`、`/api/models`、`/api/workspace` 与启动日志会带出 `personal_mode`），它**不会**改变会话上限——上限始终是 `MAX_SESSIONS`（个人级默认已收敛到 8）。系统里没有租户概念，所以谈不上「隔离」，只能靠只绑回环 + `AUTH_TOKEN` 保护。

### 2. 流式 UI + 复盘导出 + trace_id + 结构化日志 + 运行摘要

**做了什么**：把「一场运行发生过什么」拆成四层可查的证据，并给每次请求 / 每次运行一个可对齐的 `trace_id`。

| 层 | 文件 | 出口 |
| --- | --- | --- |
| 实时流 | `api/router_stream.py` + `orchestrator.py`（`EventBus`） | `WS /ws`（浏览器 UI）、`GET /api/stream`（SSE，给外部集成用） |
| 诊断日志 | `logger.py` | 三条出口同时生效：stdout（JSON 行）、`data/logs/app-YYYYMMDD.jsonl`（按天滚动）、进程内环形缓冲；查询用 `GET /api/logs` |
| 运行事件日志 | `runlog.py` | `data/runs/{trace_id}.jsonl`（一事件一行 JSON，带单调 `seq`）+ `data/runs/index.json`；查询用 `GET /api/runs`、`GET /api/runs/{trace_id}`、`GET /api/timeline` |
| 运行摘要 | `summary.py` | `GET /api/summary`（各席位发言数 / 字符数 / TTFT / 速率、token 与费用、裁判、纠偏统计、异常、`highlights`） |
| 复盘 | `review.py` | `GET /api/review`（结构化）、`GET /api/review/export`（`format` 取 `md` / `json` / `html`）、`POST /api/review/save` → `data/reviews/review_{trace_id}.{ext}` |

**怎么用**：先拿 `X-Trace-Id`（任何请求的响应头都有，另附 `X-Elapsed-Ms`），再一条命令把「事件 + 日志 + 索引」全捞出来：

```bash
curl -s -i http://127.0.0.1:8000/api/status          # 响应头里看 X-Trace-Id
curl -s http://127.0.0.1:8000/api/trace/<trace_id>    # 事件 + 日志 + 索引一次拿全
curl -s 'http://127.0.0.1:8000/api/logs?level=error&limit=50'
curl -s 'http://127.0.0.1:8000/api/stream?session_id=default&replay=1'
```

`GET /api/export` 仍是旧契约（默认返回 `{"markdown": "..."}`）；新增的 `format=json|html`、`download=1`、`mode=review` 都是**追加**参数。

### 3. 接口级测试与模块化

**做了什么**：`main.py` 只做装配，业务端点按 7 个模块拆开；每个路由模块带 `MODULE_NAME` / `MODULE_TITLE` / `MODULE_DESC` 三个模块级常量，`api/__init__.py` 的 `ALL_MODULES` 是模块清单的**唯一事实来源**——接口控制台、冒烟、OpenAPI 分组都从它推出来，新增模块只需登记一次。

**七个模块**：`setup`（配置与模型）、`kb`（共享知识库）、`run`（运行控制）、`stream`（实时推送与可观测）、`export`（导出与复盘）、`dev`（接口调试）、`integration`（对外集成）。

**怎么用**：

- `GET /api/dev/endpoints`：全部接口按模块分组导出，带请求 JSON Schema 与示例请求体（控制台据此自动生成表单）。
- `GET /api/dev/modules`：模块导航（每模块接口数与路径清单）。
- `POST /api/dev/invoke`：通用调用器 `{method,path,query,body,headers,timeout_ms}`，走 `httpx.ASGITransport` 的**进程内真实 ASGI 链路**——中间件、鉴权、依赖注入全生效，所以「控制台调通 = 外部调通」。
- `POST /api/dev/smoke`：一键跑 10 个只读接口（不消耗额度、不改状态、不需要先配置席位），返回逐项耗时与 `passed/failed`。
- `/console`：接口控制台页面（`static/console.html` + `console.js` + `console.css`）。
- `DEV_API_ENABLED=0` 可整组关闭（403）；对外暴露服务时建议关掉。

`scripts/api_smoke.py` 补的是另一件事：连**真实端口**验证部署形态（静态页、SSE 是否真的在流、中间件响应头、鉴权），这些在进程内 `ASGITransport` 里测不出来。

### 4. 中途注入纠偏（steering）

**做了什么**：运行中把「人的判断」以**指令对象**插进正在跑的编排循环，而不是等跑完再重开一场。

**在哪个文件**：`muti_llm_agent/steering.py`（`Directive` / `SteeringQueue`）；注入点在 `Orchestrator._agent_turn`（`orchestrator.py`）。

**语义**：5 种 `kind` × 3 种 `scope` × 2 种优先级。

| 维度 | 取值 |
| --- | --- |
| `kind` | `correct`（方向纠偏）/ `constrain`（新增约束）/ `fact`（补充事实）/ `redirect`（焦点转向）/ `stop`（禁止方向） |
| `scope` | `all`（全员，默认）/ `main`（仅主持人）/ `agent`（指定 `target=席位名`；给了 agent 却没给 target 会退化为 `all` 而不是静默丢弃） |
| `priority` | `normal`（命中即消费）/ `high`（本轮内每个席位都注入，轮末统一消费） |

**怎么用**：

```bash
curl -s -X POST http://127.0.0.1:8000/api/steer -H 'Content-Type: application/json' \
  -d '{"session_id":"demo","text":"只保留 3 个可落地方案，不要展开理论","kind":"constrain","scope":"main","priority":"normal"}'
curl -s 'http://127.0.0.1:8000/api/steer?session_id=demo'          # 看队列与统计
curl -s -X DELETE 'http://127.0.0.1:8000/api/steer/<directive_id>?session_id=demo'
```

注入后发生三件事：① 指令文本追加到该席位本回合 user 消息末尾（带「与既有方向冲突时以指令为准」的显式声明），随历史留在该席位上下文里，后续轮次不会忘；② 写进 transcript（`type=directive`）；③ 写运行事件（`directive_added` / `directive_applied`）并广播 `directive` 事件——复盘时能看到「第几轮、对谁、生效了哪条」。

**边界**：单回合最多注入 5 条（`MAX_INJECT_PER_TURN`）；待生效队列上限 `STEERING_MAX_PENDING=20`；单条文本上限 1000 字（超出截断）；`STEERING_ENABLED=0` 时端点 403。

### 5. 会话结束后补充材料续轮（followup）

**做了什么**：一场跑完发现是「缺料」而不是「跑错」时，带着新材料在上一轮结论之上做**增量优化**，而不是重开一场把老结论再贴一遍。续轮做成一次受控的**迭代**。

**在哪个文件**：`muti_llm_agent/followup.py`（`normalize_materials` / `validate_followup` / `build_carryover` / `build_seed_block` / `snapshot_iteration`）+ `Orchestrator.start_followup()`。

**流程**：归档当前迭代快照 → `iterations.append(snap)` → 生成**承接包**（上一轮核心结论 / 各方立场 / 裁判裁决 / 遗留分歧 / 末轮发言要点 / 已交付任务结果）→ 清运行态但**保留席位与模型** → 承接包只在第 1 轮注入每个席位 → `iteration_no + 1`。

**怎么用**：

```bash
curl -s -X POST http://127.0.0.1:8000/api/followup -H 'Content-Type: application/json' \
  -d '{"session_id":"demo","materials":["访谈记录：…","竞品报价：…"],"focus":"收敛成可报价的版本","auto_start":true}'
curl -s 'http://127.0.0.1:8000/api/iterations?session_id=demo'   # 迭代链 + 当前一轮
```

- `materials` 接受字符串 / 字符串数组 / `[{"source","text"}]`（前端文件解析结果可直接透传）；`auto_start=false` 只准备承接包不发车。
- 只允许在终态续轮（`done` / `stopped` / `interrupted` / `idle`）；运行中或暂停中续轮返回 400。
- 上限 `MAX_ITERATIONS=5`（含首发那一轮）；`FOLLOWUP_ENABLED=0` 时返回 403。

**边界**：承接包是**按字符截断**（各段上限按 `FOLLOWUP_CARRYOVER_CHARS` 的比例切分，末尾标注「已按上限截断」），**不是语义压缩**——料特别多时，先自己写成摘要再传。

### 6. 对外集成

**做了什么**：让本系统能被外部系统驱动、也能把结果推出去。三个子模块在 `muti_llm_agent/integrations/`，只依赖标准库，不 import FastAPI（脚本 / CI 可直接 import）。

| 模块 | 职责 |
| --- | --- |
| `integrations/feishu.py` | 飞书协议适配：事件解析、卡片 / 文本构造（`FEISHU_TEXT_LIMIT=28000` 截断）、群机器人推送 |
| `integrations/dsh.py` | DSH 工具清单 `TOOL_SPECS` + 参数校验规整 `validate_arguments` |
| `integrations/dispatch.py` | 通用出站 webhook：`post_json_webhook` / `dispatch_run_summary` / `summary_sentence` |

**怎么用**：

- **飞书入站**：`POST /api/integrations/feishu/webhook`。`url_verification` 握手原样返回 `{"challenge": ...}`；普通消息默认只回执，`FEISHU_AUTO_RUN=1` 时用默认配方以消息文本为主题自动发起（会话 `feishu`，轮次 `FEISHU_AUTO_RUN_MAX_ROUNDS=3`）。畸形请求体也只返回 `{"code":0}` 或明确错误，不抛 500。
- **飞书出站**：`POST /api/integrations/feishu/push`（`{session_id, webhook_url, as_card, text}`；不给 url 就用 `.env` 的 `FEISHU_WEBHOOK_URL`）；`FEISHU_PUSH_ON_DONE=1` 时运行结束自动推。
- **DSH**：`GET /api/integrations/dsh/manifest` 拿 7 个工具（`collab_start` / `collab_status` / `collab_steer` / `collab_followup` / `collab_summary` / `collab_export` / `collab_history`），再用 `POST /api/integrations/dsh/invoke` 的 `{tool, arguments}` 驱动；未知工具 / 坏参数是 400 不是 500。
- **通用 webhook**：`POST /api/integrations/webhooks/dispatch`（推当前运行摘要）与 `POST /api/integrations/webhooks/test`（连通性测试）；地址在 `WEBHOOK_URLS`（逗号 / 分号 / 换行分隔），`WEBHOOK_ON_DONE=1` 时结束自动推。
- **SSE**：`GET /api/stream` 是最省事的集成出口，`curl` / 脚本 / 网关都能消费。
- `GET /api/integrations` 一次看清三条集成路径的配置状态（不含密钥）。

## 目录结构

```
Muti_LLM_Agent/
├── start.py                    启动入口（uvicorn，默认 127.0.0.1:8000，关掉访问日志）
├── pyproject.toml              工程化配置（ruff / mypy / coverage）
├── requirements.txt            运行依赖（含可选重型依赖）
├── requirements-ci.txt         门禁/CI 的最小依赖清单（受 test_dependency_closure 校验）
├── requirements-dev.txt        质量工具链（ruff / mypy / coverage）
├── .env                        本地密钥与开关（gitignore，不提交）
├── .env.example                配置模板：全部 93 个键 + 默认值，可提交
├── docs/
│   ├── README.md               文档索引（哪份是现行、哪份只是参考）
│   ├── API.md                  接口参考：横切约定 + SSE/WS 事件（接口清单以 /api/dev/endpoints 为准）
│   ├── MIGRATION.md            从 mini_test 迁到本项目的说明
│   ├── CODE_LOGIC.md           代码逻辑详解（迁移前的辩论主体逻辑，仍可参考）
│   └── archive/                历史存档：过期评估与旧方案（不维护）
├── muti_llm_agent/             核心包（旧项目的 app/）
│   ├── main.py                 FastAPI 装配：静态目录、中间件、页面、lifespan、完成回调
│   ├── api/                    路由层（一个模块一件事）
│   │   ├── __init__.py         模块注册表 ALL_MODULES（唯一事实来源）
│   │   ├── deps.py             共享依赖：session 归一、状态快照、鉴权、DSH 密钥
│   │   ├── router_setup.py     配置与模型花名册
│   │   ├── router_kb.py        共享知识库（注入 / 上传解析 / 统计）
│   │   ├── router_run.py       运行控制 / 纠偏 / 续轮 / 摘要
│   │   ├── router_stream.py    WebSocket、SSE、日志与 trace 检索
│   │   ├── router_export.py    导出 / 历史 / 复盘
│   │   ├── router_dev.py       接口清单 / 通用调用 / 冒烟 / console 页面
│   │   └── router_integration.py  飞书 / DSH / webhook
│   ├── models_registry.py      模型花名册 + 5 个协同配方（含自动改派）
│   ├── steering.py             中途注入纠偏（指令对象 + 会话级队列）
│   ├── followup.py             补充材料续轮（承接包 + 迭代快照）
│   ├── summary.py              运行摘要（一次运行的聚合视图）
│   ├── runlog.py               运行事件日志（复盘时间线的唯一事实来源）
│   ├── review.py               复盘导出（md / json / 自包含 html）
│   ├── logger.py               结构化日志（stdout / 按天文件 / 环形缓冲）
│   ├── integrations/           对外集成层（feishu / dsh / dispatch）
│   ├── orchestrator.py         编排器：状态机、回合驱动、纠偏注入点、迭代切换
│   ├── manager.py              会话注册表（LRU 淘汰 + 元数据落盘 + 中断标记）
│   ├── agent.py / memory.py / llm.py / tools.py / rag.py / fairness.py / prompts.py
│   ├── events.py               事件总线（有界队列 512 + 背压）
│   ├── static/                 前端
│   │   ├── index.html+app.js+style.css           圆桌 UI（既有实时流式界面，1134 行 app.js）
│   │   ├── studio.js+studio.css                  增量层：配方 / 纠偏 / 摘要 / 续轮面板
│   │   └── console.html+console.js+console.css   接口控制台
├── scripts/
│   ├── gate.py                 质量门禁唯一入口（本地与 CI 共用同一段代码）
│   ├── api_smoke.py            对已运行服务做接口冒烟（两种用法见下）
│   └── eval_baseline.py        讨论质量基线评测
├── tests/                      快速回归（ASGITransport + LLM 打桩，无需服务器/真实 API）
└── data/                       运行期产物（gitignore）：discussions / runs / reviews / logs / sessions / memories
```

**前端为什么是「叠加」而不是重写**：`static/app.js` 是既有的圆桌 UI。`static/studio.js` 用 IIFE 包住新代码，**包装全局 `handleEvent`** 来追加处理 `directive` / `run_summary` / `iteration` 三类事件，并复用 `getSessionId()` / `generateAgentCards()` / `addMsg()` 等既有函数——旧界面一个 DOM id 都没动，新面板是外挂上去的。

## 配置项

**全部键（93 个）与默认值的可复制模板在 [.env.example](.env.example)**——每一行都是注释态、与内置默认值等价，取消注释即生效。键的读取逻辑与兜底值的唯一事实来源是 [`muti_llm_agent/config.py`](muti_llm_agent/config.py)；`.env` 只放你的真实密钥与想覆盖的项，且已被 gitignore。

> 历史遗留提示：`NEO4J_*`（7 个）与 `EMBED_MODEL_TYPE` / `EMBED_API_KEY` / `EMBED_BASE_URL` 这三个 `EMBED_*` 键**当前代码不再读取**（嵌入只走本地 sentence-transformers）。若你的 `.env` 里还有，可以删掉，它们不会有任何作用。

下表是「多模型协同试点」新增的那批键，其它键（`LLM_*`、`QWEN_*`、`JUDGE_*`、`FAIR_*`、`CONVERGENCE_*`、`CONTEXT_*` 等）见 `.env.example` 的分组注释。

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `PERSONAL_MODE` | `1` | 单用户个人级的定位标记；`/api/defaults`、`/api/models`、`/api/workspace` 与启动日志会带出 `personal_mode`。**不改会话上限、不做隔离** |
| `OWNER_NAME` | `本机用户` | 工作区主人名，出现在 `/api/models`、`/api/workspace` |
| `WORKSPACE_NAME` | `多模型协同工作台` | 工作区名，出现在 `/healthz`、`/api/workspace`、启动日志 |
| `DEFAULT_RECIPE` | `collab-4` | 默认协同配方：`/api/recipes` 的 `is_default`、`/api/defaults`、DSH `collab_start`、飞书自动发起都用它 |
| `MAX_ITERATIONS` | `5` | 单场运行允许的最大迭代次数（首发 1 轮 + 续轮）；超过后 `/api/followup` 返回 400 |
| `LOG_TO_FILE` | `1` | 诊断日志是否写 `data/logs/app-YYYYMMDD.jsonl`（按天滚动） |
| `LOG_RING_SIZE` | `2000` | 进程内环形缓冲条数，`/api/logs` 只查这个缓冲（不读盘） |
| `LOG_LEVEL` | `info` | 最小日志级别（`debug`/`info`/`warning`/`error`），低于它的日志不进任何出口 |
| `RUNLOG_ENABLED` | `1` | 是否写运行事件日志 `data/runs/{trace_id}.jsonl`；关掉后 `/api/timeline`、`/api/trace` 的事件部分会空 |
| `RUNLOG_MAX_EVENTS` | `20000` | 单场事件条数上限；超出后停止写事件（防止异常循环把磁盘写满），运行索引另受 `runlog.MAX_INDEX=300` 条限制 |
| `STEERING_ENABLED` | `1` | 中途纠偏总开关；`0` 时 `POST /api/steer` 返回 403 |
| `STEERING_MAX_PENDING` | `20` | 待生效指令队列上限，满后 `add` 抛 ValueError（400） |
| `STEERING_TEXT_MAX_CHARS` | `1000` | 单条指令文本上限，超出截断并加 `…` |
| `FOLLOWUP_ENABLED` | `1` | 补充材料续轮总开关；`0` 时 `/api/followup` 返回 403 |
| `FOLLOWUP_MAX_MATERIAL_CHARS` | `12000` | 补充材料字符上限，超出截断并追加「已截断」提示 |
| `FOLLOWUP_CARRYOVER_CHARS` | `4000` | 承接包总长度上限（核心结论 / 立场 / 裁决 / 遗留 / 末轮要点按比例切分） |
| `SSE_KEEPALIVE_SEC` | `15.0` | SSE 心跳间隔：空闲时发一行注释 `: keep-alive`，防中间层掐断长连接 |
| `SSE_REPLAY_MAX` | `200` | `/api/stream?replay=1` 连上后回放的历史事件条数上限 |
| `FEISHU_WEBHOOK_URL` | 空 | 飞书群机器人出站地址；`/api/integrations/feishu/push` 不给 url 时用它 |
| `FEISHU_VERIFICATION_TOKEN` | 空 | 入站事件校验 token；非空时校验 `body.token` 或 `body.header.token` |
| `FEISHU_PUSH_ON_DONE` | `0` | 运行结束自动把摘要推到飞书（需同时配 `FEISHU_WEBHOOK_URL`） |
| `FEISHU_AUTO_RUN` | `0` | 入站消息是否自动发起一场协同；默认关，避免误触消耗额度 |
| `FEISHU_AUTO_RUN_MAX_ROUNDS` | `3` | 自动发起时的最大轮次 |
| `WEBHOOK_URLS` | 空 | 通用出站地址，逗号 / 分号 / 换行分隔，只接受 `http(s)://` |
| `WEBHOOK_ON_DONE` | `0` | 运行结束自动向 `WEBHOOK_URLS` 推送结构化摘要 |
| `WEBHOOK_TIMEOUT` | `8.0` | 所有出站 HTTP（飞书 / webhook）的超时秒数 |
| `DSH_ENABLED` | `1` | DSH 集成开关；`0` 时 `dsh/manifest` 与 `dsh/invoke` 返回 403 |
| `DSH_SHARED_SECRET` | 空 | DSH 专用共享密钥（`Authorization: Bearer` 或 `?secret=`）；与 `AUTH_TOKEN` 分开，生命周期通常更长 |
| `DSH_BASE_URL` | 空 | manifest 里 `base_url` 的缺省值（也可 `?base_url=` 临时指定） |
| `DEV_API_ENABLED` | `1` | `/api/dev/*` 与 `/console` 的开关；`0` 时统一 403，对公网暴露时建议关 |
| `DEV_API_TIMEOUT_MS` | `60000` | `/api/dev/invoke` 的默认超时（毫秒），超时返回 504 |

## 测试与质量门禁

**门禁只有一个入口**，CI 与本地跑的是同一段代码：

```bash
python scripts/gate.py            # 全部门禁（提交前 / CI 用同一命令）
python scripts/gate.py --fast     # 只跑静态检查（ruff + mypy + mypy --strict），秒级
python scripts/gate.py --list     # 看步骤清单
```

它会依次跑：`ruff` → `mypy`（整包）→ `mypy --strict`（协议层）→ `compileall` →
`node --check`（3 个前端文件）→ `pytest` + 覆盖率下限。

为什么非要收敛成一个入口：之前 CI 有 6 个独立 step、本地靠人记得分别敲，结果出现过
两次同源事故——CI 的依赖清单漏了 `pytest`/`websockets` 导致采集期崩溃（本地因环境齐全
永远复现不出），以及一次重构后本地漏跑 mypy 把类型错误提交上去。**CI 与本地共用一段代码，
就不存在"两边不一致"这回事。**

另有几条"结构性守门"用例（随 pytest 一起跑，不需要额外工具）：

| 用例 | 守住什么 |
| --- | --- |
| [`tests/test_docs_consistency.py`](tests/test_docs_consistency.py) | 文档相对链接可解析、现行文档必须进索引、README 目录树条目真实存在、版本号单一来源 |
| [`tests/test_dependency_closure.py`](tests/test_dependency_closure.py) | [`requirements-ci.txt`](requirements-ci.txt) 必须覆盖所有**模块级**第三方导入（延迟导入的可选依赖不受限） |
| [`tests/test_gate_hygiene.py`](tests/test_gate_hygiene.py) | 测试模块不得在模块级共享管理器单例；手动脚本 `test_e2e.py` 必须留在采集范围外 |

覆盖率下限写在 [`scripts/gate.py`](scripts/gate.py) 的 `COVERAGE_MIN`，它的作用是**防回退**，
不是及格线——补测之后请把它上调。用例数以 CI 实际输出为准，本文不写死。

`scripts/api_smoke.py` 有**两种用法**（它连真实端口，和 `tests/` 的进程内测试互补）：

```bash
# 1) 只跑部署形态冒烟：静态页、SSE 是否真在流、响应头、鉴权、接口连通性
python scripts/api_smoke.py
python scripts/api_smoke.py --base-url http://127.0.0.1:8123 --token xxx

# 2) 额外真跑一场协同（会消耗模型额度，默认不跑）
python scripts/api_smoke.py --live-run --recipe collab-3 --rounds 1
```

退出码 `0` = 全通过、`1` = 有失败项，可直接接进部署脚本或 CI。

## 已知边界与后续可做

诚实列出现在**做不到**的：

1. **会话是单进程内存态**。`Orchestrator` 活在 `DebateManager` 的字典里；进程重启只能把 `data/sessions/{sid}.json` 里 running/paused 的会话标记成 `interrupted`（前端显示中断横幅），**不能续跑**。要横向扩展得先把会话外部化，现在没有。
2. **SSE 不支持断点续传**。只有 `?replay=1` 的**一次**回放（从 runlog 读最多 200 条），没有 `Last-Event-ID` 语义，断线期间丢的增量无法按序号补齐。
3. **飞书只做了「事件回调 + 群机器人推送」**。没有卡片交互回调（按钮 / 表单点击不会回来），也没有线程级回复；入站消息触发的是「用默认配方开一场」，不能在飞书里选配方或纠偏（纠偏得走 `/api/steer` 或 DSH）。
4. **续轮承接包是字符截断，不是语义压缩**。各段按 `FOLLOWUP_CARRYOVER_CHARS` 的比例硬切并标注「已截断」，长材料下会丢细节。
5. **向量库默认内存后端**。`QDRANT_URL` 探测失败就降级 `in-memory`（启动时后台线程探测），**进程重启知识库即清空**；嵌入模型缺失时再降级为确定性哈希向量（无语义检索能力）。`GET /api/kb/stats` 的 `backend` 字段是当前真实后端。
6. **纠偏的 `high` 优先级只在「轮内」生效**，不跨轮持续；想跨轮施压就重复下发，或用 `at_round` 指定起始轮。
7. **飞书回调只做了 verification token 校验**，未实现签名（`X-Lark-Signature`）校验；把回调暴露到公网前请自行加网关白名单。
8. **mypy 整包 0 issue**，但 `disallow_untyped_defs` 仍是关闭的（历史模块大量未注解）；`protocol.py` 的 `--strict` 门禁是 CI 里的独立命令。

后续可做（按「一个人用」的收益排序，均**尚未实现**）：会话状态外部化 + SSE 断点续传；承接包改成 LLM 摘要；飞书卡片回调（在 IM 里纠偏 / 续轮）；向量库后端显式化（不再靠探测）；逐步打开 `disallow_untyped_defs`。

## 迁移说明

从 `mini_test`（包名 `app`）迁到本项目（`Muti_LLM_Agent`，包名 `muti_llm_agent`），做了四件事：目录 / 包名重命名、导入路径批量替换、`pyproject.toml` / `ci.yml` / `start.py` 跟随更新、把 `main.py` 拆成 7 个路由模块。行为差异、向后兼容点、**三个测试为什么改了 patch 目标**、旧文档哪些还有效，全部在 [docs/MIGRATION.md](docs/MIGRATION.md)。

接口怎么调、SSE 帧长什么样、WS 有哪些事件类型，见 [docs/API.md](docs/API.md)。
