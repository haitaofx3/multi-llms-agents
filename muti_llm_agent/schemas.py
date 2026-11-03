"""
schemas.py — REST 请求体模型（FastAPI 请求校验）

为 /api/configure、/api/start、/api/ingest、/api/save_markdown 等端点提供
pydantic 类型校验（请求体不合法时 FastAPI 返回 422，并自动生成 OpenAPI 文档）。

设计约定：
  - 只做**类型/形状**校验；业务语义校验（如 Agent 数量 1-6、讨论状态冲突）
    仍保留在各端点内，维持既有 `{ok, error}` 400 错误契约（前端依赖该结构）。
  - 与 muti_llm_agent/protocol.py（Agent 间通信协议模型）分离：这里是 REST 边界模型。
  - 本文件同时是「接口控制台」的请求体示例来源：/api/dev/endpoints 会读出每个
    请求模型的 JSON Schema 交给前端自动生成表单，因此字段名与默认值需保持语义清晰。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    """POST /api/configure 中单个 Agent 的配置项（字段与前端 Agent 卡片对齐）。"""
    name: str = ""
    role: str = ""
    personality: str = ""
    provider: str = "default"
    base_url: str = ""
    api_key: str = ""
    model_id: str = ""
    stance: str = ""
    agent_id: str = ""
    forget_config: dict | None = None


class ConfigureRequest(BaseModel):
    """POST /api/configure 请求体。"""
    session_id: str | None = None
    agents: list[AgentConfig] = Field(default_factory=list)
    topic: str = ""
    max_rounds: int = 8
    judge: dict | None = None
    # 讨论模式：debate（辩论，默认）/ collaboration（合作）
    mode: str = "debate"


class StartRequest(BaseModel):
    """POST /api/start 请求体（字段可省略，省略时沿用 configure 时的值）。"""
    session_id: str | None = None
    topic: str = ""
    max_rounds: int | None = None
    # 可覆盖 configure 时的模式；缺省（None）沿用 configure 值
    mode: str | None = None


class SessionRequest(BaseModel):
    """POST /api/pause、/api/resume、/api/end 请求体（仅需 session_id，可省略）。"""
    session_id: str | None = None


class IngestRequest(BaseModel):
    """POST /api/ingest 请求体。"""
    text: str = ""
    source: str = "用户注入"


class SaveMarkdownRequest(BaseModel):
    """POST /api/save_markdown 请求体（filename 可省略，省略时自动生成）。"""
    session_id: str | None = None
    filename: str = ""


# ================= 多模型协同试点新增的请求模型 =================
class SteerRequest(BaseModel):
    """POST /api/steer —— 中途注入纠偏指令。

    kind     指令类型：correct（纠偏）/ constrain（约束）/ fact（补充事实）
                       / redirect（转向）/ stop（禁止方向）
    scope    作用域：all（全员）/ main（仅主持人）/ agent（指定 target）
    priority normal（一次性）/ high（整轮生效）
    at_round >0 时表示「从第 N 轮起生效」，0 表示下个回合即生效
    """
    session_id: str | None = None
    text: str = ""
    kind: str = "correct"
    scope: str = "all"
    target: str = ""
    priority: str = "normal"
    source: str = "user"
    at_round: int = 0


class FollowupRequest(BaseModel):
    """POST /api/followup —— 会话结束后补充材料续轮。

    materials 可以是字符串、字符串数组，或 [{"source": "...", "text": "..."}] 结构
    （前端上传文件解析后的结果可直接透传）。
    """
    session_id: str | None = None
    materials: list | str | dict | None = None
    focus: str = ""
    max_rounds: int | None = None
    # True：仅准备承接包（不发车）；False：准备后立即开始新一轮迭代
    auto_start: bool = True


class ExportRequest(BaseModel):
    """POST /api/review/save 请求体。"""
    session_id: str | None = None
    format: str = "md"


class DevInvokeRequest(BaseModel):
    """POST /api/dev/invoke —— 接口控制台的通用调用器。

    统一走 ASGI 内部转发（httpx.ASGITransport），因此与外部调用权限、
    中间件、依赖注入完全一致，不会出现「控制台能调、外部调不了」的偏差。
    """
    method: str = "GET"
    path: str = "/api/status"
    query: dict = Field(default_factory=dict)
    body: dict | None = None
    headers: dict = Field(default_factory=dict)
    timeout_ms: int | None = None


class FeishuPushRequest(BaseModel):
    """POST /api/integrations/feishu/push —— 把运行摘要推到飞书群机器人。"""
    session_id: str | None = None
    webhook_url: str = ""
    as_card: bool = True
    text: str = ""


class DshInvokeRequest(BaseModel):
    """POST /api/integrations/dsh/invoke —— DSH 以工具调用方式驱动本系统。"""
    tool: str = ""
    arguments: dict = Field(default_factory=dict)


class WebhookTestRequest(BaseModel):
    """POST /api/integrations/webhooks/test —— 向配置的地址推送一条测试载荷。"""
    url: str = ""
    payload: dict | None = None
