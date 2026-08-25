"""
protocol.py — 多 Agent 辩论系统消息协议（对齐《多Agent辩论系统架构设计文档》§3）

定义 Agent 间通信的 JSON Schema（pydantic 模型）：
  - TaskDispatch    任务分发（Supervisor → 辩手）
  - ArgumentSubmit  论点提交（辩手 → Supervisor）
  - Rebuttal        反驳消息（辩手 → 辩手 / Supervisor）
  - Judgment        裁决消息（Judge → Supervisor）
  - StatusSync      状态同步（心跳 / 轮次状态广播）
  - EvidenceItem    RAG 检索结果（带置信度）

统一字段约定：
  - 所有消息带 task_id（UUID 全局唯一）
  - 时间戳统一 Unix 毫秒
  - 论点/任务附带 token_cost，用于预算统计与审计
"""
from __future__ import annotations

import time
from typing import Dict, List
from uuid import uuid4

from pydantic import BaseModel, Field


def new_trace_id() -> str:
    """生成链路追踪 ID（12 位十六进制）。"""
    return uuid4().hex[:12]


def new_uuid() -> str:
    return str(uuid4())


def now_ms() -> int:
    """当前 Unix 毫秒时间戳。"""
    return int(time.time() * 1000)


class TaskDispatch(BaseModel):
    """§3.1 任务分发消息。"""
    task_id: str = Field(default_factory=new_uuid)
    topic: str = ""
    stance: str = "NEUTRAL"          # PRO / CON / NEUTRAL / HOST
    round: int = 0
    budget_token: int = 0            # 本任务剩余 Token 预算
    timestamp: int = Field(default_factory=now_ms)


class ArgumentSubmit(BaseModel):
    """§3.2 论点提交消息。"""
    arg_id: str = Field(default_factory=new_uuid)
    agent_id: str = ""
    content: str = ""                # 论点正文（Markdown）
    evidence_refs: List[str] = []    # RAG 证据 ID 列表（可选）
    confidence: float = 0.5          # 0.0-1.0 自我评估置信度
    round: int = 0
    token_cost: int = 0


class Rebuttal(BaseModel):
    """§3.3 反驳消息。"""
    target_arg_id: str = ""
    rebuttal_content: str = ""
    logic_flaws: List[str] = []      # 逻辑谬误类型，如 ["StrawMan", "AdHominem"]
    counter_evidence: List[str] = [] # 反证证据 ID


class Judgment(BaseModel):
    """§3.4 裁决消息。"""
    scores: Dict[str, float] = {}    # 全局维度 {logic, evidence, rebuttal, clarity}
    agent_scores: Dict[str, Dict[str, float]] = {}  # 各辩手评分 {辩手名: {logic, evidence, rebuttal, clarity}}
    # 模型质量实测（系统确定性计算，非 LLM 输出）：
    #   {辩手名: {avg_ttft_ms, avg_gen_rate, performance_score}}
    model_performance: Dict[str, Dict[str, float]] = {}
    # 最终综合评分（内容分 × 内容权重 + 模型质量分 × 性能权重）：
    #   {辩手名: composite}
    composite_scores: Dict[str, float] = {}
    winner: str = "DRAW"             # PRO / CON / DRAW
    critique: str = ""
    convergence_flag: bool = False


class StatusSync(BaseModel):
    """§3.5 状态同步消息。"""
    trace_id: str = ""
    phase: str = "idle"              # idle/preparing/debating/judging/finished
    current_round: int = 0
    agent_states: Dict[str, str] = {}  # {name: IDLE/THINKING/WAITING}
    remaining_budget: int = 0
    spent_budget: int = 0
    elapsed_time_ms: int = 0


class EvidenceItem(BaseModel):
    """RAG 证据片段（Source, Content, Confidence, URL）。"""
    evidence_id: str = Field(default_factory=new_uuid)
    source: str = ""
    content: str = ""
    confidence: float = 0.0
    url: str = ""
