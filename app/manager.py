"""
manager.py — 多会话管理（DebateManager）

为同一 FastAPI 进程内的多路并发辩论提供会话隔离：
  - 每个客户端通过稳定的 session_id 绑定一个独立 Orchestrator 实例；
  - 会话元数据轻量持久化到 data/sessions/{session_id}.json
    （仅保存身份 / 状态快照，不保存完整发言记录，供 UI 展示与中断恢复提示）；
  - 进程启动时 mark_interrupted() 把上一进程遗留的 running/paused 会话
    标记为 interrupted，前端据此展示「上次讨论中断」横幅；
  - 通过 set_done_handler() 注册「讨论完成」回调（如自动落盘 Markdown）。

默认会话（"default"）通过 get_or_create 惰性创建并缓存；会话实例统一由
DebateManager 注册表持有（§1.3 解耦：不再存在模块级单例与注册表两份身份）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from app.config import PROJECT_ROOT
from app.logger import log

SESSIONS_DIR = PROJECT_ROOT / "data" / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# session_id 允许的安全字符（防路径穿越 / 非法文件名）
_SAFE_SID = re.compile(r"^[A-Za-z0-9._-]+$")


class DebateManager:
    """按 session_id 管理多个 Orchestrator 实例的会话注册表。"""

    def __init__(self):
        self._sessions: dict[str, object] = {}
        self._done_handler = None

    # ---------------- 会话获取 ----------------
    def get_or_create(self, session_id: str = "default"):
        """按 session_id 取会话；不存在时创建新的 Orchestrator（惰性导入防循环依赖）。"""
        if session_id is None or not str(session_id).strip():
            session_id = "default"
        if session_id not in self._sessions:
            from app.orchestrator import Orchestrator
            self._sessions[session_id] = Orchestrator(session_id=session_id)
        return self._sessions[session_id]

    def get(self, session_id: str = "default"):
        """仅返回已存在的会话；不存在返回 None（不创建）。"""
        return self._sessions.get(session_id)

    def list_sessions(self) -> dict:
        return dict(self._sessions)

    # ---------------- 完成回调 ----------------
    def set_done_handler(self, handler):
        """注册完成回调 handler(orchestrator)。每个会话讨论完成时都会调用。"""
        self._done_handler = handler

    def notify_done(self, orch):
        if self._done_handler is not None:
            self._done_handler(orch)

    # ---------------- 元数据持久化 ----------------
    @staticmethod
    def _meta_path(session_id: str) -> Path:
        safe = session_id if _SAFE_SID.match(session_id) else "default"
        return SESSIONS_DIR / f"{safe}.json"

    @staticmethod
    def _snapshot(orch) -> dict:
        """把会话的关键状态抽取成轻量元数据快照。"""
        agents = []
        for a in getattr(orch, "agents", []) or []:
            model_id = ""
            try:
                model_id = a.model_config.get("model_id", "")
            except Exception:
                model_id = ""
            agents.append({
                "name": a.name, "role": a.role, "stance": a.stance,
                "is_main": bool(a.is_main), "model_id": model_id,
            })
        return {
            "session_id": getattr(orch, "session_id", "default"),
            "status": getattr(orch, "status", "idle"),
            "phase": getattr(orch, "phase", "idle"),
            "topic": getattr(orch, "topic", ""),
            "trace_id": getattr(orch, "trace_id", ""),
            "round": getattr(orch, "round", 0),
            "max_rounds": getattr(orch, "max_rounds", 8),
            "spent_tokens": getattr(orch, "spent", 0),
            "agents": agents,
            "judge": getattr(orch, "judge", None).name if getattr(orch, "judge", None) else None,
            "updated_at": time.time(),
        }

    def save_meta(self, orch) -> dict:
        """保存会话元数据快照；返回快照 dict。"""
        snap = self._snapshot(orch)
        path = self._meta_path(snap["session_id"])
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        return snap

    def load_meta(self, session_id: str = "default") -> dict | None:
        """读取指定会话的元数据；不存在 / 损坏返回 None。"""
        path = self._meta_path(session_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    # ---------------- 中断恢复 ----------------
    def mark_interrupted(self) -> list:
        """启动时调用：把上一进程遗留的 running/paused 会话标记为 interrupted。

        返回被标记的会话元数据列表。仅在进程启动时调用一次，
        避免把本次运行中正在进行的会话误标记。
        """
        marked = []
        for path in SESSIONS_DIR.glob("*.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log("warning", "manager.meta_read",
                    f"读取会话元数据失败，跳过：{path.name}",
                    file=str(path), error=str(exc))
                continue
            if meta.get("status") in ("running", "paused"):
                meta["status"] = "interrupted"
                meta["phase"] = "idle"
                meta["note"] = "服务重启，讨论中断"
                meta["updated_at"] = time.time()
                try:
                    path.write_text(
                        json.dumps(meta, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    marked.append(meta)
                except Exception as exc:
                    log("warning", "manager.meta_write",
                        f"标记中断会话写入失败：{path.name}",
                        file=str(path), error=str(exc))
                    continue
        return marked

    def interrupted_metas(self) -> list:
        """返回所有标记为 interrupted 的会话元数据（供前端中断横幅展示）。"""
        out = []
        for path in SESSIONS_DIR.glob("*.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log("warning", "manager.meta_read",
                    f"读取会话元数据失败，跳过：{path.name}",
                    file=str(path), error=str(exc))
                continue
            if meta.get("status") == "interrupted":
                out.append(meta)
        return out

    def interrupted_meta_for(self, session_id: str = "default") -> dict | None:
        """指定会话是否为中断状态；是则返回其元数据，否则返回 None。"""
        meta = self.load_meta(session_id)
        if meta and meta.get("status") == "interrupted":
            return meta
        return None


# 全局会话管理器
manager = DebateManager()
