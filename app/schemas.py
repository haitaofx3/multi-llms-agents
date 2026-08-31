"""
schemas.py — REST 请求体模型（FastAPI 请求校验）

为 /api/configure、/api/start、/api/ingest、/api/save_markdown 提供
pydantic 类型校验（请求体不合法时 FastAPI 返回 422，并自动生成 OpenAPI 文档）。

设计约定：
  - 只做**类型/形状**校验；业务语义校验（如 Agent 数量 1-6、讨论状态冲突）
    仍保留在各端点内，维持既有 `{ok, error}` 400 错误契约（前端依赖该结构）。
  - 与 app/protocol.py（Agent 间通信协议模型）分离：这里是 REST 边界模型。
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


class StartRequest(BaseModel):
    """POST /api/start 请求体（字段可省略，省略时沿用 configure 时的值）。"""
    session_id: str | None = None
    topic: str = ""
    max_rounds: int | None = None


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
