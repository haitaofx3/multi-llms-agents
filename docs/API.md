# 接口参考（API）

> **本文档的核对方式**：接口清单直接取自运行中实例的 `GET /api/dev/endpoints`（7 个模块分组），并与 `muti_llm_agent/api/router_*.py` 的源码逐条对照过方法、路径、参数与返回字段。若两者不一致，**以代码为准**，并请顺手修本文档。接口总数会随迭代变化，本文不写死数量——需要数字时直接看该端点返回的 `count` 字段。
>
> 在线版：`/docs`（Swagger UI，含每个请求体的 JSON Schema）；机器可读版：`GET /api/dev/endpoints`。

全部接口前缀 `/api`（`/ws`、`/console`、`/healthz` 除外）。下表 `S` 列表示是否按 `session_id` 寻址。

## 三类横切约定（先看这个，能省掉一半排障时间）

### 1. `session_id` 的语义

- 默认值 `"default"`；**空串 / 缺省 / `None` 一律归一为 `default`**（`api/deps.py: normalize_sid`）。
- 本项目是**单用户个人级**：`session_id` 不再是多租户隔离键，它只是「本次运行」的容器标识。对外集成（飞书 / DSH / 脚本）仍需要一个稳定可寻址的号，所以这个约定保留了。
- **请求体优先，其次查询串**（`resolve_sid`）：`pause` / `resume` / `end` / `save_markdown` 这类控制端点两种传法都接受——前端习惯把 `session_id` 放 `?session_id=`，只读请求体会静默落到 `default` 会话，是个很容易踩的坑。
- **读端点不建会话**：`GET /api/status`、`/api/export`、`/api/workspace/summary` 等走 `manager.get()`（不存在就返回 idle 快照或 404），只有写端点（`configure` / `start` / `steer` / `followup`）才 `get_or_create`。这样轮询不会把 `Orchestrator` 灌进注册表。
- 会话注册表上限 `MAX_SESSIONS`（默认 32），超限按 LRU 淘汰**空闲**会话，`running` / `paused` 永不淘汰。

### 2. 统一错误形状

业务错误一律 HTTP 4xx/5xx + JSON 体：

```json
{"ok": false, "error": "会话不存在或已过期"}
```

- `{ok:false, error}` 是**旧契约，保持向后兼容**，前端依赖它；不要改成 FastAPI 默认的 `{"detail": ...}`。
- 例外：**请求体形状/类型非法**由 pydantic 校验拦截，返回 FastAPI 标准的 `422`（带 `detail`）。业务语义校验（Agent 数量 1–6、运行中拒绝、队列满）仍走 `400 {ok:false,error}`。
- 集成端点（飞书回调）**绝不抛 500**：畸形请求体返回 `{"code":0,"msg":"ok","note":"…已忽略"}`。

### 3. 响应头与鉴权

- `X-Trace-Id`：每个 HTTP 响应都有（`main.py: observability_middleware`）。客户端可用请求头 `X-Trace-Id` **自带**链路标识，便于把外部系统调用链与本系统对齐；不带则就地生成。
- `X-Elapsed-Ms`：本请求耗时（毫秒，一位小数）。实测样例：`x-trace-id: f2a2f353fbf1` / `x-elapsed-ms: 2.7`。
- 拿到 `trace_id` 后：`GET /api/trace/{trace_id}` 一次取回「运行事件 + 诊断日志 + 索引记录」。
- **`AUTH_TOKEN`（用户令牌）**：非空时，除 `PUBLIC_PATHS`（`/`、`/favicon.ico`、`/console`）与 `PUBLIC_PREFIXES`（`/static/`）外的所有请求都要令牌。两种传法：`Authorization: Bearer <token>` 或 `?token=<token>`（WebSocket 无法自定义请求头，只能走 `?token=`；令牌错则关闭码 `4401`）。常量时间比较。默认空 = 不校验，靠只绑 `127.0.0.1` 保证安全。
  - 注意白名单很短：设了 `AUTH_TOKEN` 后，`/healthz`、`/docs`、`/openapi.json` **同样需要令牌**（`/api/dev/*` 也需要），探活与 Swagger 要带上 `Authorization` 或 `?token=`。
- **`DSH_SHARED_SECRET`（集成共享密钥）**：**只**作用于 `/api/integrations/dsh/*` 两个端点，传法 `Authorization: Bearer <secret>` 或 `?secret=`。默认空 = 放行。它与 `AUTH_TOKEN` **独立**：外部 Agent 的凭据生命周期通常比个人令牌长，因此单独一套。两者都配时，DSH 请求需要**同时**满足（`AUTH_TOKEN` 走中间件、`DSH_SHARED_SECRET` 走端点内校验）。

## 模块 1 · setup（配置与模型）7 个

| S | 方法 | 路径 | 用途 | 关键参数 / 返回要点 |
| --- | --- | --- | --- | --- |
| | GET | `/api/defaults` | 默认配置与运行环境 | 返回 `providers`（**已剥离 api_key**）、`default_provider`、`vector_backend`、`max_agents=6`、`upload_exts`、`auth_required`、`stream_pool`、`recipes`、`default_recipe`、`workspace`、`owner`、`personal_mode`、`features{steering,followup,review,sse,dev_console,feishu,dsh}` |
| | GET | `/api/models` | 模型花名册 | `{ok, workspace, owner, personal_mode, providers[], configured_count, available[], default_recipe, recipes[]}`；`providers[].configured` 表示该供应商是否已配 Key |
| | GET | `/api/recipes` | 协同配方列表 | `{ok, items[], default}`；每项含 `name/title/mode/max_rounds/description/agents[]/judge/is_default` |
| | GET | `/api/recipes/{name}` | 按配方生成席位配置 | 返回可直接喂给 `/api/configure` 的 `agents`、`judge`、`providers_used`、`notes[]`（改派说明）、`available_providers[]`；未知配方 `404` |
| ✓ | POST | `/api/configure` | 配置席位 / 主题 / 轮次 / 模式 | body：`session_id, agents[1..6], topic, max_rounds=8, judge, mode`；运行中 `409`，席位数量非法 `400`；返回 `ok/mode/agents/judge/status/phase/trace_id/max_rounds/fairness/iteration` |
| | GET | `/api/workspace` | 工作区概览 | `workspace/owner/personal_mode/auth_required/max_sessions/active_sessions/sessions/paths{project_root,data,discussions,runs,reviews,logs}/discussions{total,latest}/runlog/default_recipe/now` |
| | GET | `/api/session/{session_id}` | 会话快照 | 存在则返回完整状态（同 `/api/status`），不存在返回 idle 形状；**不创建会话** |

```bash
curl -s http://127.0.0.1:8000/api/models | jq '{configured_count, available, default_recipe}'
curl -s -X POST http://127.0.0.1:8000/api/configure -H 'Content-Type: application/json' -d '{
  "session_id":"demo","mode":"collaboration","topic":"写一份上线检查单","max_rounds":4,
  "agents":[{"name":"规划者","role":"主控规划","provider":"deepseek","stance":"HOST"}]}'
```

## 模块 2 · kb（共享知识库）3 个

| S | 方法 | 路径 | 用途 | 关键参数 / 返回要点 |
| --- | --- | --- | --- | --- |
| | POST | `/api/ingest` | 注入纯文本知识 | body：`text, source`（默认「用户注入」）；按段落切块后写入共享向量库；返回 `{ok, added, backend}`；空文本 `400` |
| | POST | `/api/upload` | 上传文件解析后注入 | `multipart/form-data` 字段名 `file`；支持 `txt/md/csv/log/docx/xlsx/pdf/图片 OCR`；超限 `413`（先查声明 size，再按上限 +1 字节读）；解析失败或无文本 `400`；返回 `{ok, added, chars, truncated, backend, source}` |
| | GET | `/api/kb/stats` | 知识库统计 | `{ok, backend, chunks, upload_exts, max_upload_bytes}`；`backend` 是**真实**后端（`in-memory` 或 `qdrant`） |

```bash
curl -s -X POST http://127.0.0.1:8000/api/upload -F "file=@./资料.docx"
curl -s http://127.0.0.1:8000/api/kb/stats
```

## 模块 3 · run（运行控制）11 个

| S | 方法 | 路径 | 用途 | 关键参数 / 返回要点 |
| --- | --- | --- | --- | --- |
| ✓ | POST | `/api/start` | 开始运行 | body：`session_id, topic, max_rounds, mode`（都可省略，省略沿用 configure）；`is_active` → `400`，未配置席位 → `400`；返回 `{ok, session_id, status:"running", phase:"preparing", trace_id, iteration}` |
| ✓ | POST | `/api/pause` | 暂停 | `session_id` 查询串或 body；会话不存在 `404`；返回 `{ok, session_id, status, phase}` |
| ✓ | POST | `/api/resume` | 继续 | 同上 |
| ✓ | POST | `/api/end` | 结束 | 同上（跳过裁判与汇总，直接收尾） |
| | GET | `/api/status` | 运行状态快照 | `session_id/status/phase/mode/trace_id/round/max_rounds/topic/agents/judge/tasks/shared_context/spent_tokens/output_tokens/fairness/elapsed_time_ms/judgment/eventbus/steering/iteration/iterations/error_count/followup_focus`；未创建的会话返回 idle 形状（含 `fairness`，中断会话额外带 `status=interrupted`） |
| ✓ | POST | `/api/steer` | 下发中途纠偏指令 | body：`text, kind, scope, target, priority, source, at_round, session_id`；`STEERING_ENABLED=0` → `403`；`kind/scope/priority` 非法 → `400`；返回 `{ok, session_id, directive, steering, will_apply, hint}`（`will_apply=false` 表示当前没在跑，指令要等下次 start） |
| | GET | `/api/steer` | 查看指令队列 | `{ok, session_id, items[], steering}`；无会话时 `items` 为空 |
| ✓ | DELETE | `/api/steer/{directive_id}` | 撤销未生效指令 | `session_id` 查询串；不存在或已生效 → `404` |
| ✓ | POST | `/api/followup` | 补充材料续轮 | body：`materials`（str / list / dict）、`focus`、`max_rounds`、`auto_start=true`；`403` 关闭 / `404` 无会话 / `400` 运行中或超 `MAX_ITERATIONS`；返回 `iteration, previous_iteration, previous_trace_id, trace_id, materials_chars, carryover_chars, notes[], started, status` |
| | GET | `/api/iterations` | 迭代链 | `{ok, session_id, current, items[], running{...}}`；`items` 是已归档的历史迭代 |
| | GET | `/api/summary` | 结构化运行摘要 | `{ok, summary{...}}`；无会话时返回最小摘要（`highlights:["该会话尚未开始运行"]`）。与 `/api/status` 的分工：status 是实时状态，summary 是聚合视图 |

```bash
curl -s -X POST http://127.0.0.1:8000/api/start -H 'Content-Type: application/json' -d '{"session_id":"demo"}'
curl -s -X POST http://127.0.0.1:8000/api/steer -H 'Content-Type: application/json' \
  -d '{"session_id":"demo","text":"只保留 3 个方案","kind":"constrain","scope":"main"}'
curl -s 'http://127.0.0.1:8000/api/status?session_id=demo'
```

## 模块 4 · stream（实时推送与可观测）6 个

| S | 方法 | 路径 | 用途 | 关键参数 / 返回要点 |
| --- | --- | --- | --- | --- |
| ✓ | GET | `/api/stream` | **SSE 事件流** | `session_id`、`token`、`replay=1`；帧格式见下文；空闲每 `SSE_KEEPALIVE_SEC` 发 `: keep-alive`；`text/event-stream` + `X-Accel-Buffering: no` |
| | GET | `/api/logs` | 查结构化日志 | `trace_id`、`level`（返回**不低于**该级别）、`event`（前缀匹配）、`limit=100`；返回 `{ok, items[], count, ring_size, ring_capacity, file_enabled, file_path, level}` |
| | GET | `/api/trace/{trace_id}` | 按 trace 一键排障 | 返回 `{ok, trace_id, events[], event_count, logs[], index, error}`；`ok=false` 表示该 trace 没有事件记录 |
| | GET | `/api/timeline` | 复盘时间线 | `session_id`、`trace_id`（为空则取该会话当前 trace）、`limit=300`；每项 `{seq, ts, time, offset_ms, type, label, round, agent, text}` |
| | GET | `/api/runs` | 运行索引 | `q`（按题目 / trace_id / session_id 关键词）、`limit=50`；返回 `{ok, items[], runlog{enabled,runs,indexed,bytes,dir}}` |
| | GET | `/api/runs/{trace_id}` | 单场运行的事件明细 | `limit=1000`；返回 `{ok, trace_id, items[], count, index}` |

```bash
curl -s 'http://127.0.0.1:8000/api/runs?limit=5'
curl -sN 'http://127.0.0.1:8000/api/stream?session_id=demo&replay=1'   # -N 关闭缓冲才能看到流式
```

## 模块 5 · export（导出与复盘）10 个

| S | 方法 | 路径 | 用途 | 关键参数 / 返回要点 |
| --- | --- | --- | --- | --- |
| ✓ | GET | `/api/export` | 导出运行记录 | `format=md`（默认，返回 `{"markdown":"…"}`，**旧契约**）/ `json`（`{ok, review}`）/ `html`（自包含单文件）；`download=1` 附件下载；`mode=review` 让 md 走复盘版；`format` 非法 `400` |
| ✓ | POST | `/api/save_markdown` | 落盘到 `data/discussions` | body：`filename`（可省略，自动生成）；文件名只保留 basename + 强制 `.md`（防穿越）；返回 `{ok, path, filename}`；无会话 `404` |
| | GET | `/api/review` | 复盘报告（结构化） | `session_id`（返回 `{ok, review}`）或 `trace_id`（返回 `{ok, trace_id, index, timeline}`，可复用历史 trace）；无会话 `404` |
| ✓ | GET | `/api/review/export` | 导出复盘 | `format` 取 `md` / `json` / `html`、`download=1`（默认 1）；直接返回原始内容（非 JSON 包裹） |
| ✓ | POST | `/api/review/save` | 复盘落盘 | body：`session_id, format`；落 `data/reviews/review_{trace_id}.{ext}`；返回 `{ok, path, filename, format, trace_id}`；写盘失败 `500` |
| | GET | `/api/history` | 历史讨论列表 | `q` 关键词；`{ok, items[]}` |
| | GET | `/api/history/{filename}` | 查看历史 Markdown | `{ok, markdown, filename}`；不存在 `404` |
| | GET | `/api/history/{filename}/review` | 把历史记录当复盘看 | `{ok, filename, meta, markdown}`；历史记录**没有**编排器实例，所以只有落盘 Markdown + 索引元数据 |
| | GET | `/api/history/{filename}/download` | 下载 Markdown | `text/markdown` + `Content-Disposition: attachment` |
| | GET | `/api/workspace/summary` | 最近若干场回顾 | `limit=10`；把 `data/discussions` 索引摊平成 `{ok, items[], session, generated_at}` |

```bash
curl -s -o review.md 'http://127.0.0.1:8000/api/review/export?session_id=demo&format=md'
curl -s -X POST http://127.0.0.1:8000/api/review/save -H 'Content-Type: application/json' \
  -d '{"session_id":"demo","format":"html"}'
```

## 模块 6 · dev（接口调试）5 个

| S | 方法 | 路径 | 用途 | 关键参数 / 返回要点 |
| --- | --- | --- | --- | --- |
| | GET | `/api/dev/endpoints` | 接口清单（按模块分组） | `module` 可过滤；返回 `{ok, count, modules[], titles, session_default, auth_required, generated_at}`；每项带 `request_schema` / `example_body` / `parameters` / `is_stream` / `dangerous` / `session_scoped` |
| | GET | `/api/dev/modules` | 模块导航 | `{ok, items[{name,title,description,endpoints,paths[]}]}` |
| | POST | `/api/dev/invoke` | 通用接口调用器 | body：`method, path, query, body, headers, timeout_ms`；走 `httpx.ASGITransport` 进程内真实链路；`path` 不以 `/` 开头或指向自身 → `400`，method 不支持 → `400`，超时 `504`，转发异常 `502`；返回 `{ok, status_code, elapsed_ms, method, path, response, trace_id}` |
| | POST | `/api/dev/smoke` | 一键冒烟（只读） | 顺序跑 10 个只读接口（`/api/defaults`、`/api/models`、`/api/recipes`、`/api/status`、`/api/summary`、`/api/kb/stats`、`/api/logs`、`/api/history`、`/api/runs`、`/api/workspace`）；返回 `{ok, passed, failed, total, results[], generated_at}`，可在真实运行进行中安全执行 |
| | GET | `/console` | 接口控制台页面 | 纯静态页（`static/console.html`）；不在 OpenAPI 里（`include_in_schema=False`） |

`DEV_API_ENABLED=0` 时前四个端点统一返回 `403`（`/console` 页面本身仍可打开，但调不通接口）。

```bash
curl -s -X POST http://127.0.0.1:8000/api/dev/invoke -H 'Content-Type: application/json' \
  -d '{"method":"GET","path":"/api/status","query":{"session_id":"demo"}}'
curl -s -X POST http://127.0.0.1:8000/api/dev/smoke
```

## 模块 7 · integration（对外集成）7 个

| S | 方法 | 路径 | 用途 | 关键参数 / 返回要点 |
| --- | --- | --- | --- | --- |
| | GET | `/api/integrations` | 集成状态总览 | `feishu{outbound_configured,webhook,verification_token_set,push_on_done,auto_run,auto_run_max_rounds,inbound_path,outbound_path}`、`dsh{enabled,shared_secret_set,base_url,manifest_path,invoke_path,tools[]}`、`webhooks{urls,on_done,timeout}`、`stream{sse_path,ws_path,keepalive_sec}` |
| | GET | `/api/integrations/dsh/manifest` | DSH 工具清单 | `base_url` 可覆盖缺省值；返回 `{name, version, description, base_url, transport{type,invoke_path}, tools[]}`；`DSH_ENABLED=0` → `403`；配了 `DSH_SHARED_SECRET` 时需带密钥（`401`） |
| | POST | `/api/integrations/dsh/invoke` | DSH 工具调用 | body：`{tool, arguments}`；工具名见下；参数先校验规整（补默认值 / 类型转换），未知工具或坏参数 `400`，执行异常 `500`；返回 `{ok, tool, session_id, result}` |
| | POST | `/api/integrations/feishu/webhook` | 飞书事件回调（入站） | `url_verification` 握手原样返回 `{"challenge":…}`；普通消息返回 `{code:0, msg:"ok", session_id, received{text,user}, note?}`；配了 `FEISHU_VERIFICATION_TOKEN` 时 token 不匹配 `401`；`FEISHU_AUTO_RUN=1` 时额外 `started/trace_id/stream` |
| | POST | `/api/integrations/feishu/push` | 推送到飞书群机器人 | body：`session_id, webhook_url, as_card=true, text`；不给 url 用 `FEISHU_WEBHOOK_URL`，都没有 → `400`；返回 `{ok, result, sentence}` |
| ✓ | POST | `/api/integrations/webhooks/dispatch` | 推当前摘要到已配 webhook | `session_id` 查询串；`WEBHOOK_URLS` 为空 → `400`，无会话 → `404`；返回 `{ok, results[]}`（`ok` 为全部成功的与） |
| | POST | `/api/integrations/webhooks/test` | 测试出站 webhook | body：`url`（可省略，取 `WEBHOOK_URLS` 第一个）、`payload`（默认一条 `webhook.test` 载荷）；无 url → `400`；返回 `{ok, result}` |

DSH 的 7 个工具：`collab_start`（`topic` 必填，可选 `session_id/mode/max_rounds/recipe`）、`collab_status`、`collab_steer`、`collab_followup`、`collab_summary`、`collab_export`、`collab_history`。

```bash
curl -s http://127.0.0.1:8000/api/integrations/dsh/manifest | jq '.tools[].name'
curl -s -X POST http://127.0.0.1:8000/api/integrations/dsh/invoke -H 'Content-Type: application/json' \
  -d '{"tool":"collab_start","arguments":{"topic":"三个可选的技术方案","session_id":"dsh1"}}'
```

## SSE 帧格式（`GET /api/stream`）

标准 SSE，每帧 `event: <type>` + 单行 `data: <JSON>` + 空行（`router_stream.py: _sse`）：

```
event: status
data: {"type":"status","session_id":"demo","status":"running",...}

event: stream_open
data: {"type":"stream_open","session_id":"demo","trace_id":"…","keepalive_sec":15.0}

: keep-alive
```

连接顺序固定：① 首帧 `status`（完整状态快照，等价 `/api/status` 的 body）；② `stream_open`；③ `replay=1` 时先回放历史运行事件（`event: replay`，data 是 runlog 原始事件，最多 200 条）；④ 之后实时推送，`event:` 取事件的 `type` 字段。

⚠ `data` 都是**单行** JSON（不美化、不换行），解析时按行读即可。断线重连只能拿到「这次连上之后」的事件 + 一次回放，**没有断点续传**。

## WebSocket 事件类型（`WS /ws?session_id=&token=`）

连上先收一帧 `status`（快照），之后收到的每个 JSON 都有 `type` 字段。真实取值（`orchestrator.py` 的 `event_bus.publish` + `router_stream.py`，已逐个核对）

| `type` | 何时发 | 关键字段 |
| --- | --- | --- |
| `status` | 连接时、start/pause/resume/end 后 | `status, phase, topic, round, max_rounds, trace_id, agents[]`（连接帧还带 `iteration/steering/fairness/output_tokens`） |
| `status_sync` | 每轮开始、状态变化后 | `trace_id, session_id, mode, phase, current_round, iteration, agent_states, spent_tokens, output_tokens, cost_rmb, convergence_mode, convergence_streak, fairness, steering, error_count, elapsed_time_ms` |
| `round` | 每轮开始 | `round, max` |
| `speech_start` | 某席位开始流式输出（含纯工具回合） | `speaker, round, is_main` |
| `speech_delta` | 每个流式增量 | `speaker, delta` |
| `speech_end` | 该席位本轮结束（前端用完整文本替换气泡） | `speaker, round, text, tokens, output_tokens, cost_rmb` |
| `speech` | 非流式路径产出的完整发言 | `round, speaker, is_main, text` |
| `tool_call_start` / `tool_call_end` | 工具调用开始 / 结束 | `speaker, round, name, index` / `speaker, round, name, summary`（`summary` 截断到 120 字） |
| `task_assigned` | 主持人分派任务 | `from, to`（空 = 任务池待认领）、任务内容 |
| `task_claimed` | 子 Agent 认领任务 | 任务与认领者 |
| `task_result` | 子 Agent 回报结果 | 任务、结果摘要 |
| `task_revised` | 合作模式反思后修订落库 | 任务、修订标记 |
| `directive` | 纠偏指令入队 / 生效 | `action`（`added` / `applied`）、`round`、`directive` / `directives`、`steering` |
| `iteration` | 续轮准备完成 | `action:"prepared"`、`info{iteration,previous_trace_id,carryover_chars…}`、`iterations[]` |
| `judgment` | 裁判裁决完成 | `judge, scores, agent_scores, model_performance, composite_scores, winner, critique, convergence_flag, round` |
| `summary` | 主持人阶段性总结 / 合作模式三段式汇总 | `round, speaker, text` |
| `conclusion` | 结构化结论生成 | `core, positions, judge_ruling, pending, winner, round`（合作模式为三段式，`judge_ruling` 为空） |
| `run_summary` | **每场运行的收尾事件**（正常 / 手动结束 / 异常熔断都发） | `summary{...}`（同 `/api/summary` 的结构） |
| `log` | 编排器结构化日志（同时进 stdout 与日志文件） | `text, level, event` |
| `error` / `agent_error` | 运行期异常 | `error` / `speaker` + 错误信息 |

> 事件总线是**有界队列**（512），客户端消费太慢会丢**最旧**事件并计数（`dropped`，随 `/api/status` 的 `eventbus` 字段可见）；终态由 `speech_end` / `status_sync` / `run_summary` 兜底，所以丢增量不会让 UI 停留在中间态。

## 状态码速查

| 码 | 含义 | 典型场景 |
| --- | --- | --- |
| `200` | 成功 | 大多数端点（`ok:false` 也可能出现在 200 里，见 `/api/trace`） |
| `400` | 业务参数不合法 | 席位数量、`kind/scope/priority` 非法、运行中续轮、队列满 |
| `401` | 未授权 | `AUTH_TOKEN` 或 `DSH_SHARED_SECRET` 校验失败 |
| `403` | 功能被关闭 | `STEERING_ENABLED=0`、`FOLLOWUP_ENABLED=0`、`DSH_ENABLED=0`、`DEV_API_ENABLED=0` |
| `404` | 目标不存在 | 会话不存在、配方不存在、历史记录不存在、指令不存在 |
| `409` | 状态冲突 | 运行中调用 `/api/configure` |
| `413` | 上传超限 | `/api/upload` 超过 `MAX_UPLOAD_BYTES` |
| `422` | 请求体形状非法 | pydantic 校验（带 `detail`，不是 `{ok,error}`） |
| `502` / `504` | 转发失败 / 超时 | 仅 `/api/dev/invoke` |
