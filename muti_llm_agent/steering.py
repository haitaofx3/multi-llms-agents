"""
steering.py — 中途注入纠偏（Mid-course Steering）

为什么需要它：多模型协同是「多轮自主推进 + 长上下文」的组合，一旦第 1 轮把方向
理解偏了，后面每一轮都会在错误前提上继续加码，直到跑满轮次 —— 用户只能在结束后
重开一场。本模块把「人的判断」以**指令对象**的形式插进正在运行的编排循环：

  用户在 UI / 飞书 / DSH 里下发一条指令
    → 进入指令队列（带 kind / 目标 / 生效轮次 / 优先级）
    → 编排器在每个 Agent 回合之前调用 `take()`，把命中的指令渲染成 prompt 片段
    → 该片段被追加到本回合 user 消息末尾（优先级声明高于既有方向）
    → 同时写入 transcript 与运行事件日志，复盘时可看到「指令在第几轮改变了什么」

五种指令类型（kind）：
  correct    方向纠偏 ── 指出当前方向/结论错在哪，要求改
  constrain  新增约束 ── 追加硬性边界（字数、口径、合规、技术栈…）
  fact       补充事实 ── 补材料/数据，要求各模型据此修正
  redirect   焦点转向 ── 改变讨论焦点或交付物形态
  stop       禁止方向 ── 明确某些方向不得再出现

三种作用域（scope）：
  all    所有 Agent（默认）
  agent  指定单个 Agent（target=名字）
  main   仅主持人（负责任务拆解与汇总，通常是最有效的纠偏点）

优先级（priority）：
  normal 一次性 ── 注入给第一个命中的回合后即消费
  high   整轮生效 ── 本轮内每个 Agent 回合都注入，轮次结束时消费
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from muti_llm_agent.config import settings
from muti_llm_agent.logger import log
from muti_llm_agent.protocol import new_uuid

# 指令类型 → 中文标签（前端 / 导出共用，避免多处硬编码漂移）
KIND_LABELS: dict[str, str] = {
    "correct": "方向纠偏",
    "constrain": "新增约束",
    "fact": "补充事实",
    "redirect": "焦点转向",
    "stop": "禁止方向",
}

# 指令类型 → 注入 prompt 时的行为引导（让模型知道「该怎么用这条指令」）
KIND_HINTS: dict[str, str] = {
    "correct": "你的既有方向被判定为偏航：立即调整，不要继续沿用被否定的前提。",
    "constrain": "这是新的硬性约束，后续所有输出必须满足，冲突时以约束为准。",
    "fact": "这是新补充的事实材料，请据此修正此前与材料矛盾的论断。",
    "redirect": "讨论焦点已调整，请转向新的目标推进，旧焦点不再展开。",
    "stop": "以下方向被明确禁止，不得再提出、不得变相重述。",
}

SCOPES = ("all", "agent", "main")
PRIORITIES = ("normal", "high")
STATUS_PENDING = "pending"     # 尚未生效
STATUS_ACTIVE = "active"       # 整轮生效中（high 优先级）
STATUS_APPLIED = "applied"     # 已注入并消费
STATUS_CANCELLED = "cancelled"  # 用户撤销

# 单次注入的指令条数上限（防止一次塞满上下文）
MAX_INJECT_PER_TURN = 5


@dataclass
class Directive:
    """一条中途指令。"""

    text: str
    kind: str = "correct"
    scope: str = "all"
    target: str = ""
    priority: str = "normal"
    source: str = "user"          # user / feishu / dsh / api
    at_round: int = 0             # >0 表示「从该轮起生效」
    directive_id: str = field(default_factory=new_uuid)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    status: str = STATUS_PENDING
    applied_round: int = 0
    applied_agents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "directive_id": self.directive_id,
            "kind": self.kind,
            "kind_label": KIND_LABELS.get(self.kind, self.kind),
            "scope": self.scope,
            "target": self.target,
            "priority": self.priority,
            "source": self.source,
            "at_round": self.at_round,
            "text": self.text,
            "created_at": self.created_at,
            "status": self.status,
            "applied_round": self.applied_round,
            "applied_agents": list(self.applied_agents),
        }


class SteeringQueue:
    """会话级指令队列（挂在 Orchestrator 上，每次运行重置）。"""

    def __init__(self, max_pending: int | None = None, text_max: int | None = None):
        self.max_pending = max(1, int(max_pending or settings.STEERING_MAX_PENDING))
        self.text_max = max(20, int(text_max or settings.STEERING_TEXT_MAX_CHARS))
        self._items: list[Directive] = []
        self.current_round = 0
        # 累计统计（写进运行摘要，便于判断「这场运行被人工干预了多少次」）
        self.total_added = 0
        self.total_injected = 0
        self.by_kind: dict[str, int] = {}

    # ---------------- 写入 ----------------
    def add(self, text: str, kind: str = "correct", scope: str = "all",
            target: str = "", priority: str = "normal", source: str = "user",
            at_round: int = 0) -> dict:
        """加入一条指令；返回指令 dict（失败时抛 ValueError）。"""
        if not bool(getattr(settings, "STEERING_ENABLED", True)):
            raise ValueError("中途纠偏功能已关闭（STEERING_ENABLED=0）")
        text = (text or "").strip()
        if not text:
            raise ValueError("指令内容为空")
        if len(text) > self.text_max:
            text = text[: self.text_max] + "…"
        kind = (kind or "correct").strip().lower()
        if kind not in KIND_LABELS:
            kind = "correct"
        scope = (scope or "all").strip().lower()
        if scope not in SCOPES:
            scope = "all"
        priority = (priority or "normal").strip().lower()
        if priority not in PRIORITIES:
            priority = "normal"
        if scope == "agent" and not (target or "").strip():
            # 指定 Agent 却没给名字：退化为全员，避免指令被静默丢弃
            scope = "all"
        if len(self.pending()) >= self.max_pending:
            raise ValueError(f"待生效指令已达上限 {self.max_pending} 条，请先撤销或等待生效")
        d = Directive(
            text=text, kind=kind, scope=scope, target=(target or "").strip(),
            priority=priority, source=source or "user",
            at_round=max(0, int(at_round or 0)),
        )
        self._items.append(d)
        self.total_added += 1
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
        log("info", "steering.add",
            f"收到中途指令【{KIND_LABELS.get(kind, kind)}】",
            directive_id=d.directive_id, kind=kind, scope=scope, target=d.target,
            priority=priority, source=d.source)
        return d.to_dict()

    # ---------------- 查询 ----------------
    def pending(self) -> list[Directive]:
        return [d for d in self._items
                if d.status in (STATUS_PENDING, STATUS_ACTIVE)]

    def all(self) -> list[dict]:
        return [d.to_dict() for d in self._items]

    def get(self, directive_id: str) -> Directive | None:
        for d in self._items:
            if d.directive_id == directive_id:
                return d
        return None

    # ---------------- 撤销 ----------------
    def cancel(self, directive_id: str) -> bool:
        """撤销一条尚未生效的指令；已生效 / 已撤销 / 不存在都返回 False。"""
        d = self.get(directive_id)
        if d is None or d.status not in (STATUS_PENDING, STATUS_ACTIVE):
            return False
        d.status = STATUS_CANCELLED
        log("info", "steering.cancel", "中途指令已撤销",
            directive_id=directive_id, kind=d.kind)
        return True

    # ---------------- 注入 ----------------
    def _matches(self, d: Directive, agent_name: str, is_main: bool,
                 round_no: int) -> bool:
        if d.status not in (STATUS_PENDING, STATUS_ACTIVE):
            return False
        if d.at_round and round_no < d.at_round:
            return False
        if d.scope == "main":
            return is_main
        if d.scope == "agent":
            return (d.target or "") == agent_name
        return True

    def take(self, agent_name: str, is_main: bool, round_no: int) -> list[dict]:
        """取出本回合要注入的指令（并推进其状态）。

        - normal：命中后立即消费（status=applied）
        - high ：本轮内持续返回（status=active），`end_round` 时统一消费
        """
        self.current_round = max(self.current_round, int(round_no or 0))
        out: list[dict] = []
        for d in self.pending():
            if not self._matches(d, agent_name, is_main, round_no):
                continue
            if d.status == STATUS_ACTIVE and d.applied_round != round_no:
                # 跨轮了还没有 end_round 收尾（异常路径）：先消费，避免无限生效
                d.status = STATUS_APPLIED
                continue
            if agent_name in d.applied_agents and d.priority != "high":
                continue
            if len(out) >= MAX_INJECT_PER_TURN:
                break
            d.applied_agents.append(agent_name)
            d.applied_round = int(round_no or 0)
            d.status = STATUS_ACTIVE if d.priority == "high" else STATUS_APPLIED
            self.total_injected += 1
            out.append(d.to_dict())
        return out

    def end_round(self, round_no: int) -> list[dict]:
        """轮次结束：把本轮的 high 指令消费掉；返回被消费的指令。"""
        done = []
        for d in self._items:
            if d.status == STATUS_ACTIVE and d.applied_round <= int(round_no or 0):
                d.status = STATUS_APPLIED
                done.append(d.to_dict())
        return done

    # ---------------- 渲染 ----------------
    @staticmethod
    def render(directives: list[dict] | list[Directive]) -> str:
        """把指令渲染成注入 prompt 的文本块（无指令返回空串）。"""
        items: list[Directive] = []
        for item in directives or []:
            obj = item if isinstance(item, Directive) else _from_dict(item)
            if obj is not None and obj.text:
                items.append(obj)
        if not items:
            return ""
        lines = ["", "", "【用户中途指令 · 优先级高于既有讨论方向】"]
        for i, d in enumerate(items, 1):
            label = KIND_LABELS.get(d.kind, d.kind)
            hint = KIND_HINTS.get(d.kind, "")
            lines.append(f"{i}. [{label}] {d.text}")
            if hint:
                lines.append(f"   → {hint}")
        lines.append("若以上指令与此前的任务描述、既有论点冲突，一律以以上指令为准，"
                     "并在本轮发言开头用一句话说明你做了哪些调整。")
        return "\n".join(lines)

    # ---------------- 统计 / 导出 ----------------
    def stats(self) -> dict:
        counts: dict[str, int] = {}
        for d in self._items:
            counts[d.status] = counts.get(d.status, 0) + 1
        return {
            "enabled": bool(getattr(settings, "STEERING_ENABLED", True)),
            "total": len(self._items),
            "added": self.total_added,
            "injected": self.total_injected,
            "pending": len(self.pending()),
            "by_status": counts,
            "by_kind": dict(self.by_kind),
        }

    def summary_lines(self) -> list[str]:
        """供导出 / 复盘使用的纯文本行。"""
        if not self._items:
            return []
        out = []
        for d in self._items:
            out.append(
                f"- [第{d.applied_round or d.at_round or '-'}轮] "
                f"{KIND_LABELS.get(d.kind, d.kind)}"
                f"（{d.scope}{('/' + d.target) if d.target else ''}）"
                f"：{d.text}  〔{d.status}〕")
        return out


def _from_dict(d: dict) -> Directive | None:
    """把 dict 还原成 Directive（供 render 同时接受两种入参）。"""
    if not isinstance(d, dict):
        return None
    return Directive(
        text=d.get("text", ""),
        kind=d.get("kind", "correct"),
        scope=d.get("scope", "all"),
        target=d.get("target", ""),
        priority=d.get("priority", "normal"),
        source=d.get("source", "user"),
        at_round=int(d.get("at_round", 0) or 0),
        directive_id=d.get("directive_id", new_uuid()),
        applied_round=int(d.get("applied_round", 0) or 0),
        status=d.get("status", STATUS_APPLIED),
    )
