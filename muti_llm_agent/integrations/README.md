# integrations — 对外集成层

本包只做两件事：**把运行结果送出去**（webhook / 飞书），**让外部 Agent 调进来**
（DSH 工具清单与参数校验）。它不碰核心编排逻辑，且只依赖标准库，任何进程都可以
直接 `import` 使用。

| 模块 | 职责 | 主要入口 |
| --- | --- | --- |
| `dispatch.py` | 出站 webhook 传输：单地址 POST + 多地址并发分发，失败只体现在返回值里 | `post_json_webhook`、`dispatch_run_summary`、`summary_sentence` |
| `feishu.py` | 飞书协议适配：消息卡片 / 纯文本构建、webhook 推送、事件回调解析 | `build_feishu_card`、`build_feishu_text`、`push_feishu_webhook`、`parse_feishu_event` |
| `dsh.py` | 供 DSH 等外部 Agent 使用的工具清单与参数校验 | `TOOL_SPECS`、`dsh_manifest`、`validate_arguments`、`DshToolError` |

依赖方向是单向的：`feishu` → `dispatch`，`dsh` 独立；`dispatch` 不反向依赖任何上层。

```bash
# 自检（不依赖仓库其它模块，也不需要启动 Web 服务）
python -c "from muti_llm_agent.integrations import *"
```

## 飞书接入

### 1. 出站通知（群机器人 webhook）

1. 在飞书群里添加「自定义机器人」，拿到 webhook 地址；
2. 把地址写进 `.env`（**不要提交真实地址**，仓库只提供占位示例）：

   ```bash
   FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
   ```

3. 运行结束（`run.finished`）时读取该变量并推送，例如：

   ```python
   import os

   from muti_llm_agent.integrations import dispatch_run_summary, push_feishu_webhook

   url = os.getenv("FEISHU_WEBHOOK_URL", "")
   push_feishu_webhook(url, summary, as_card=True)    # 交互卡片
   push_feishu_webhook(url, summary, as_card=False)   # 纯文本
   dispatch_run_summary([url], summary)               # 统一事件格式，多地址并发（≤4 线程）
   ```

   推送失败只会得到 `{"ok": False, "status": ..., "error": ...}`，**不会抛异常**，
   因此可以直接放在主流程里做 best-effort 通知，不需要 `try/except`。

4. 通用出站 webhook（非飞书）用 `WEBHOOK_URLS`（逗号 / 分号 / 换行分隔，可多地址）
   与 `WEBHOOK_TIMEOUT`；打开 `FEISHU_PUSH_ON_DONE` / `WEBHOOK_ON_DONE` 后，Web 层会在
   运行结束时自动调用本包推送。

Webhook 事件格式固定为：

```json
{"event": "run.finished", "summary": {"...": "..."}, "text": "【完成】主题《…》｜3 个模型协同｜6 轮｜输出 12345 tokens｜预估 ¥0.0321｜trace abc123"}
```

### 2. 事件回调（入站）

- 回调地址：`POST /api/integrations/feishu/webhook`
- 飞书配置回调时会先发 `url_verification`：把 `parse_feishu_event(body)["challenge"]`
  原样回显即可；
- 消息事件 `im.message.receive_v1`：`parse_feishu_event(body)` 返回
  `{"type": "message", "text": ..., "user": ..., "chat_id": ..., "message_id": ...}`
  （正文已从 JSON 字符串里解析并去掉 `@_user_1` 这类 @提及）；
- 其它事件 / 畸形请求体（`None`、缺字段、类型不符）一律返回 `type=unknown`，不抛异常。

> 生产环境必须补上签名校验：本包**未实现** `verify_signature`，需要按飞书文档用
> `timestamp + nonce + body` 做 SHA256 校验后再信任回调内容。

### 3. 长度与转义约定

- `FEISHU_TEXT_LIMIT = 28000`（整体安全上限），单个 `lark_md` 块不超过 4000 字符；
- 用户可控文本统一做最小清洗：`\r\n` 归一为 `\n`、移除控制字符、按需截断，
  单行字段折叠换行，避免伪造出额外的卡片字段。

## DSH 接入

1. **拉 manifest**：`GET /api/integrations/dsh/manifest`，返回
   `dsh_manifest(base_url=...)` 的结果：

   ```json
   {
     "name": "muti_llm_agent",
     "version": "1.0.0",
     "description": "多模型协同 / 辩论系统：…",
     "base_url": "https://your-host",
     "transport": {"type": "http", "invoke_path": "/api/integrations/dsh/invoke"},
     "tools": [{"name": "collab_start", "description": "…", "parameters": {"type": "object", "properties": {}, "required": []}}]
   }
   ```

2. **按 tools 调用 invoke**：`POST /api/integrations/dsh/invoke`，请求体
   `{"tool": "collab_start", "arguments": {"topic": "…", "mode": "debate"}}`。
   服务端先 `validate_arguments(tool, arguments)` 再分发：

   ```python
   from muti_llm_agent.integrations import DshToolError, validate_arguments

   try:
       args = validate_arguments("collab_start", {"topic": "RAG 是否该上生产", "max_rounds": "5"})
   except DshToolError as exc:
       return {"ok": False, "code": exc.code, "error": str(exc)}   # HTTP 400
   ```

   校验能力是 JSON Schema 的子集：`type`（string / integer / boolean / object / array）、
   `required`、`enum`、`default`；标量支持宽松转换（`"5"` → `5`、`"true"` → `True`），
   schema 之外的字段既不透传也不报错。错误码只有两种：
   `unknown_tool`（工具名不在清单里）与 `invalid_arguments`（缺必填 / 类型不符 / 枚举越界）。

3. **工具一览**

   | 工具 | 用途 | 必填参数 |
   | --- | --- | --- |
   | `collab_start` | 开始一次协同 / 辩论 | `topic`（可选 `mode` / `max_rounds` / `recipe`） |
   | `collab_status` | 查询运行状态 | 无（可选 `session_id`） |
   | `collab_steer` | 中途注入纠偏 | `text`（可选 `kind` / `target`） |
   | `collab_followup` | 补充材料续轮 | `materials`（可选 `focus` / `max_rounds`） |
   | `collab_summary` | 取结构化运行摘要 | 无（可选 `session_id`） |
   | `collab_export` | 导出记录（md / json / html） | 无（可选 `format` / `session_id`） |
   | `collab_history` | 历史讨论列表 | 无（可选 `query`） |

> 说明：本包只提供「描述 + 校验」层，**不注册任何路由**。端点由 Web 层（`main.py` /
> `api/` 下的 integration 路由器）挂载，请求体模型见 `schemas.py` 的
> `FeishuPushRequest` / `DshInvokeRequest` / `WebhookTestRequest`。

### 认证与开关

- `DSH_SHARED_SECRET` 非空时，调用 `/api/integrations/dsh/*` 必须带
  `Authorization: Bearer <DSH_SHARED_SECRET>`；`DSH_ENABLED=0` 可整体关闭这组端点。
- manifest 的 `base_url` 建议取 `DSH_BASE_URL`（或按请求头推导），外部 Agent 可据此
  直接拼出 `invoke_path`，无需再猜部署地址。

## 约定

- 只依赖标准库（`urllib.request` / `json` / `concurrent.futures`…），不引入 requests、httpx、jsonschema；
- 所有出站失败都转成返回值，绝不抛异常；
- 所有解析不可信输入的函数都返回固定结构，畸形输入走兜底分支。
