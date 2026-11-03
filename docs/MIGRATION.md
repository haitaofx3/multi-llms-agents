# 迁移说明：`mini_test` → `Muti_LLM_Agent`

> 这次不是「给旧系统加功能」，而是**迁移 + 转型**：从「单模型扮多角色的圆桌辩论演示」变成「单用户个人级的真实多模型协同工作台」。
> 本文只讲**迁移面**（目录/包名/工程配置/测试）与**行为差异**；能力怎么用见 [README](../README.md)，接口怎么调见 [API.md](API.md)。

## 1. 迁移做了什么

| 项 | 旧 | 新 |
| --- | --- | --- |
| 项目目录 | `program_Agent/mini_test` | `program_Agent/Muti_LLM_Agent` |
| 应用包 | `app/`（含 `app/static/`） | `muti_llm_agent/`（含 `muti_llm_agent/static/`） |
| 导入路径 | `from app.main import app` / `app.orchestrator` / `app.config` … | `from muti_llm_agent.main import app` / `muti_llm_agent.orchestrator` / `muti_llm_agent.config` … |
| pyproject | `name="multi-agent-debate"`、`version="0.1.0"`、mypy 只查 `app/protocol.py` | `name="muti-llm-agent"`、`version="1.0.0"`、mypy `files=["muti_llm_agent"]`（整包） |
| CI | `ruff check app tests scripts`、`mypy --strict app/protocol.py`、`node --check app/static/app.js` | 同上全部换成 `muti_llm_agent/...`，并新增 `studio.js`、`console.js` 的语法检查 |
| 启动 | `from app.main import app`；`access_log` 默认开 | `from muti_llm_agent.main import app`；`access_log=False`（避免与自带的结构化 JSON 日志重复） |
| `main.py` 规模 | 550 行，装配 + 全部业务端点混在一起 | 194 行，**只做装配**；业务端点进 `api/` |

具体四步：

1. **目录 + 包名重命名**：`app` → `muti_llm_agent`，静态资源随之迁移到 `muti_llm_agent/static/`。
2. **导入路径批量替换**：源码、测试、脚本、文档里的 `app.xxx` → `muti_llm_agent.xxx`（包括 `patch("app.main.DISCUSSIONS_DIR")` 这类**字符串形式的导入路径**，它们不报语法错，只会静默失效，是这次最容易漏的地方）。
3. **工程配置跟随**：`pyproject.toml`（包名 / 版本 / 描述 / mypy 覆盖范围）、`.github/workflows/ci.yml`（ruff / mypy / node 三条命令的目标路径）、`start.py`（导入与日志策略）。
4. **路由拆分**：`main.py` 里的端点按职责拆到 `api/router_setup.py` / `router_kb.py` / `router_run.py` / `router_stream.py` / `router_export.py` / `router_dev.py` / `router_integration.py`，共享逻辑进 `api/deps.py`，模块清单进 `api/__init__.py` 的 `ALL_MODULES`（新增模块只在这里登记一次）。

本次同时新增的模块（迁移之外的新能力）：`models_registry.py`、`steering.py`、`followup.py`、`summary.py`、`review.py`、`runlog.py`、`integrations/`（`feishu.py` / `dsh.py` / `dispatch.py`）、`logger.py` 升级为三条出口、`static/studio.js` + `studio.css`、`static/console.html` + `console.js` + `console.css`、`tests/test_multi_llm_collab.py`、`scripts/api_smoke.py`。

## 2. 与旧项目的行为差异

### 2.1 端点：14 → 49 个 HTTP 接口（外加 `WS /ws`）

- **原样保留、契约不变（13 个旧 HTTP 接口 + `WS /ws`，都在新的 49 里）**：`GET /api/defaults`、`POST /api/configure`、`POST /api/ingest`、`POST /api/upload`、`POST /api/start`、`POST /api/pause`、`POST /api/resume`、`POST /api/end`、`GET /api/status`、`GET /api/export`、`POST /api/save_markdown`、`GET /api/history`、`GET /api/history/{filename}`、`WS /ws`。路径一个都没改，只是实现从 `main.py` 搬到了对应的 router；`GET /` 仍是页面路由，不进接口清单。
- **语义扩展但向后兼容**（不带新参数时行为与旧版一致）：
  - `GET /api/export`：新增 `format=json|html`、`download=1`、`mode=review`。**不带参数仍返回 `{"markdown": "..."}`**——这是旧前端与旧测试依赖的契约，刻意保留（见 `api/router_export.py` 顶部注释）。
  - `GET /api/defaults`：追加 `recipes` / `default_recipe` / `workspace` / `owner` / `personal_mode` / `features`，全部是**新增字段**。
  - `GET /api/status`：追加 `steering` / `iteration` / `iterations` / `followup_focus` / `eventbus` 等字段。
  - `POST /api/configure`：新增 `mode`（`debate` / `collaboration`）；运行中重新配置由 `400` 改为 **`409`**（语义更准确，旧前端只判非 200，不受影响）。
  - `POST /api/start`：新增 `mode`（可覆盖 configure 时的模式）。
  - `WS /ws`：新增 `directive` / `iteration` / `run_summary` 等事件；旧事件类型与字段未变。
- **全新接口（36 个）**：花名册与配方（`/api/models`、`/api/recipes`、`/api/recipes/{name}`、`/api/workspace`、`/api/session/{id}` 5 个）、知识库统计（`/api/kb/stats` 1 个）、纠偏（`/api/steer` 的 POST/GET/DELETE 3 个）、续轮与迭代（`/api/followup`、`/api/iterations` 2 个）、摘要与可观测（`/api/summary`、`/api/stream`、`/api/logs`、`/api/trace/{id}`、`/api/timeline`、`/api/runs`、`/api/runs/{id}` 7 个）、复盘（`/api/review`、`/api/review/export`、`/api/review/save`、`/api/history/{filename}/review`、`/api/history/{filename}/download`、`/api/workspace/summary` 6 个）、调试（`/api/dev/endpoints`、`/api/dev/modules`、`/api/dev/invoke`、`/api/dev/smoke`、`/console` 5 个）、集成（`/api/integrations`、`dsh/manifest`、`dsh/invoke`、`feishu/webhook`、`feishu/push`、`webhooks/dispatch`、`webhooks/test` 7 个）。完整清单以 `GET /api/dev/endpoints` 为准。

### 2.2 其它行为差异

- **日志**：从 print / 手工拼串升级为 JSON 行结构化日志，三条出口（stdout、`data/logs/app-YYYYMMDD.jsonl`、进程内环形缓冲），并支持 `trace_id` / `session_id` / `run_id` 上下文。响应头新增 `X-Trace-Id` 与 `X-Elapsed-Ms`。
- **鉴权**：`AUTH_TOKEN` 保留，另加**独立的** `DSH_SHARED_SECRET`（只作用于 `/api/integrations/dsh/*`）。
- **配置**：旧键全部保留（`LLM_*` / `QWEN_*` / `DEEPSEEK_*` / `MIMO_*` / `JUDGE_*` / `FAIR_*` / `CONVERGENCE_*` / `REFLECTION_*` / `QDRANT_*` / `EMBED_*` / `AUTH_TOKEN` / `MAX_SESSIONS` …）；新增一批键（`PERSONAL_MODE` / `OWNER_NAME` / `WORKSPACE_NAME` / `DEFAULT_RECIPE` / `MAX_ITERATIONS` / `LOG_*` / `RUNLOG_*` / `STEERING_*` / `FOLLOWUP_*` / `SSE_*` / `FEISHU_*` / `WEBHOOK_*` / `DSH_*` / `DEV_API_*`），默认值全部写在 `muti_llm_agent/config.py`，含义见 [README 的配置表](../README.md#配置项)。
- **测试规模**：157 → **206** 个用例（新增 `tests/test_multi_llm_collab.py` 覆盖六项新能力）。

## 3. 三个测试为什么改了 patch 目标（别误判为回归）

路由拆分后，一些**模块级常量**（`DISCUSSIONS_DIR`、`MAX_UPLOAD_BYTES`）的所有权变了：它们原本在 `app/main.py` 里被定义/使用，现在由 `api/deps.py` 定义、真正**使用**它们的是 `api/router_export.py` 与 `api/router_kb.py`。

关键机制：`patch("模块.名字")` 只改**那个模块命名空间里的引用**。`main.py` 里仍有 `DISCUSSIONS_DIR` 这个名字（完成回调落盘用它），但 `/api/save_markdown` 写文件用的是 `api/router_export.py` 里的同名引用——**只 patch `main` 根本拦不住路由写盘**，测试会真的写进 `data/discussions/`。

| 用例 | 迁移前 | 迁移后 | 原因 |
| --- | --- | --- | --- |
| `tests/test_p0_fixes.py::test_save_markdown_follows_query_session_id` | `patch("app.main.DISCUSSIONS_DIR")` + `patch("app.history.DISCUSSIONS_DIR")` | `patch("muti_llm_agent.api.router_export.DISCUSSIONS_DIR")` + `patch("muti_llm_agent.main.DISCUSSIONS_DIR")` + `patch("muti_llm_agent.history.DISCUSSIONS_DIR")` + `patch("muti_llm_agent.history.INDEX_PATH")` | 落盘目录的所有权转移到 `api/router_export.py`；`main` 那一处保留是为了让完成回调也写到临时目录；索引路径一并隔离，避免污染真实 `index.json` |
| `tests/test_p1_p2.py` 的上传超限用例 | `patch.object(main_module, "MAX_UPLOAD_BYTES", 16)` | `patch("muti_llm_agent.api.router_kb.MAX_UPLOAD_BYTES", 16)` | 大小上限的判定已经搬进 `api/router_kb.py`，patch 必须打在**真正读这个常量的模块**上 |
| 接口冒烟相关（`tests/test_multi_llm_collab.py::test_dev_smoke_all_pass` 与 `scripts/api_smoke.py`） | 旧版无此用例 | 新增用例**不需要**任何 patch | `/api/dev/smoke` 走 `httpx.ASGITransport` 的进程内真实 ASGI 链路、只跑只读接口；脚本连真实端口。两者都不直接引用上述模块级常量，所以拆分后自动适配 |

补充：`tests/test_api_flow.py` 里 `target = main_module.DISCUSSIONS_DIR / fname` 这类**断言**仍然有效（`main.py` 确实还持有该引用），因此没有改。只有当用例需要**隔离写入**（而不是事后检查）时，才必须 patch 到 `api/router_export.py`——这两类需求别混。

## 4. 旧项目遗留文档怎么读

**已核对的事实**：当前仓库所有 `.md` 里已经**没有** `app/` 前缀（批量替换覆盖了 docs 与根目录三份长文）。所以看到旧文档写 `app/xxx` 时，直接按 `muti_llm_agent/xxx` 理解即可。

| 文档 | 状态 | 说明 |
| --- | --- | --- |
| [docs/CODE_LOGIC.md](CODE_LOGIC.md) | **有效，但有明确过时点** | 辩论 / 合作主体逻辑（状态机、公平性 L1–L6、记忆、RAG、工具、收敛）仍是当前实现，文中的模块路径已是 `muti_llm_agent/`。过时的地方：① 目录树根名仍写 `mini_test/`；② 接口清单只到迁移前的 14 个端点，没有 `api/` 路由、纠偏、续轮、复盘、dev、集成；③ 完全没有覆盖 `models_registry` / `steering` / `followup` / `summary` / `runlog` / `review` / `integrations`。 |
| [多Agent辩论系统架构设计文档.md](../多Agent辩论系统架构设计文档.md) | 参考 | 架构目标态（Supervisor / 辩手 / Judge / RAG），与实现基本对应；不含本次新增的协同/集成层。 |
| [P2优化方案.md](../P2优化方案.md) | 参考 | 平台化演进方案（多实例 / 人工审核 / 知识库 / 模板），属"想做但没做"，别当现状读。 |
| [三方Agent框架对比与迁移利弊分析.md](../三方Agent框架对比与迁移利弊分析.md) | 参考 | 决策记录（自研 vs 三方框架），解释为什么继续自研。 |
| [docs/DELIVERY_READINESS.md](DELIVERY_READINESS.md) | 部分过时 | 框架仍可用，但基线数字是迁移前的（"157 用例全绿、ruff 与 mypy 双门禁通过"）。现状：**206 用例通过、ruff 全绿、mypy 有 4 处未过**。 |
| [docs/archive/](archive/) | **已过时** | 历史决策记录，仅作追溯，不要按它改代码。 |
| [README.md](../README.md) / [docs/API.md](API.md) / 本文 | **现行** | 本次重写的三份。 |

> 注意：`docs/README.md`（文档索引）**没有**同步加上本文件与 `API.md` 两行——按本次「只改三份 markdown」的约束没动它，谁顺手谁补。

## 5. 迁移自查清单（换机器 / 重跑迁移时照这个走）

1. `rg -n "\bapp\." --glob '*.py'`（或 PowerShell：`Select-String -Path **\*.py -Pattern '\bapp\.'`）→ 应只剩第三方 import，不应出现 `app.main` / `app.orchestrator`。
2. `rg -n 'patch\("app\.' tests` → 应为空（字符串形式的 patch 目标不报语法错，最容易漏）。
3. `python -c "from muti_llm_agent.main import app; print(len(app.routes))"` → 能导入即说明包名替换彻底。
4. `python -m pytest tests -q` → 206 passed。
5. `curl -s http://127.0.0.1:8000/api/dev/endpoints | python -c "import sys,json;print(json.load(sys.stdin)['count'])"` → 49。
6. `python -m ruff check muti_llm_agent tests scripts` → 0 问题（mypy 现状见第 4 节）。
