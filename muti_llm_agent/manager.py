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
注册表有容量上限（settings.MAX_SESSIONS），超限按 LRU 淘汰空闲会话。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from muti_llm_agent.config import PROJECT_ROOT, safe_path_component, settings
from muti_llm_agent.logger import log

SESSIONS_DIR = PROJECT_ROOT / "data" / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


class DebateManager:
    """按 session_id 管理多个 Orchestrator 实例的会话注册表。

    容量治理（P0）：注册表上限由 `settings.MAX_SESSIONS` 控制，超出后按
    「最久未使用」淘汰**空闲**会话；running / paused 的会话永不淘汰。
    没有上限时，任何客户端只要反复请求不同 session_id 就能无限新建
    Orchestrator（每个都持有 N 个 MemorySystem 与磁盘目录），属可放大的资源泄漏。
    """

    def __init__(self, max_sessions: int | None = None):
        self._sessions: dict[str, object] = {}
        # LRU 序号：用自增计数器而不是 time.monotonic()，避免时钟分辨率不足时
        # 同一时刻创建的多个会话序号相同、淘汰顺序退化为「字典插入顺序」。
        self._last_used: dict[str, int] = {}
        self._lru_seq = 0
        self._done_handler = None
        limit = settings.MAX_SESSIONS if max_sessions is None else max_sessions
        self.max_sessions = max(1, int(limit))

    def _touch(self, session_id: str) -> None:
        """标记会话刚被使用（LRU 序号越大越新）。"""
        self._lru_seq += 1
        self._last_used[session_id] = self._lru_seq

    # ---------------- 会话获取 ----------------
    def get_or_create(self, session_id: str = "default"):
        """按 session_id 取会话；不存在时创建新的 Orchestrator（惰性导入防循环依赖）。

        创建后会把磁盘上的「迭代链」恢复进新实例：个人级试点常常是「今晚跑一轮、
        明天接着补材料续轮」，若迭代链只存在内存里，一次进程重启就会让
        `/api/iterations` 与复盘页看不到「第 2 轮是从第 1 轮续出来的」。
        """
        sid = self._normalize(session_id)
        sess = self._sessions.get(sid)
        if sess is not None:
            self._touch(sid)
            return sess
        # 先腾出容量再创建：淘汰最久未用的空闲会话（活动会话绝不淘汰）
        self._evict_idle()
        from muti_llm_agent.orchestrator import Orchestrator
        orch = Orchestrator(session_id=sid)
        self._restore_iterations(orch)
        self._sessions[sid] = orch
        self._touch(sid)
        return orch

    def _restore_iterations(self, orch) -> None:
        """从会话元数据里恢复迭代链（仅在内存中为空时；失败静默）。"""
        try:
            meta = self.load_meta(getattr(orch, "session_id", "default"))
            if not meta:
                return
            iterations = meta.get("iterations") or []
            if isinstance(iterations, list) and iterations:
                orch.iterations = [it for it in iterations if isinstance(it, dict)]
            iteration_no = int(meta.get("iteration") or 0)
            if iteration_no > 1:
                orch.iteration_no = iteration_no
            log("info", "manager.restore_iterations",
                f"恢复迭代链：当前第 {orch.iteration_no} 轮，历史 {len(orch.iterations)} 条",
                session_id=orch.session_id)
        except Exception as e:
            log("warning", "manager.restore_iterations",
                f"恢复迭代链失败：{e}", error=str(e))

    def get(self, session_id: str = "default"):
        """仅返回已存在的会话；不存在返回 None（不创建）。"""
        sid = self._normalize(session_id)
        sess = self._sessions.get(sid)
        if sess is not None:
            self._touch(sid)
        return sess

    def list_sessions(self) -> dict:
        return dict(self._sessions)

    @staticmethod
    def _normalize(session_id) -> str:
        """空 / None 回退 "default"；其余原样保留（落盘时再清洗）。"""
        if session_id is None or not str(session_id).strip():
            return "default"
        return str(session_id)

    def _evict_idle(self) -> list:
        """容量已满时，按 LRU 淘汰空闲会话，为新会话腾出位置。

        只淘汰 `is_active` 为假的会话；全部活动时宁可不腾（不打断正在跑的讨论），
        返回本次淘汰的 session_id 列表。
        """
        if len(self._sessions) < self.max_sessions:
            return []
        candidates = sorted(self._sessions, key=lambda s: self._last_used.get(s, 0))
        evicted = []
        for sid in candidates:
            if len(self._sessions) <= self.max_sessions - 1:
                break
            orch = self._sessions.get(sid)
            if orch is not None and getattr(orch, "is_active", False):
                continue
            self._sessions.pop(sid, None)
            self._last_used.pop(sid, None)
            evicted.append(sid)
        if evicted:
            log("info", "manager.evict",
                f"会话数达上限 {self.max_sessions}，淘汰最久未用的空闲会话",
                session_ids=evicted, max_sessions=self.max_sessions)
        return evicted

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
        # 会话标识来自客户端，统一清洗为安全单级文件名（防目录穿越）
        return SESSIONS_DIR / f"{safe_path_component(session_id)}.json"

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
        judge_agent = getattr(orch, "judge", None)
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
            "judge": judge_agent.name if judge_agent is not None else None,
            # 迭代链：续轮之后即使进程重启，也能看出「第 N 轮承接自哪一轮」
            "iteration": getattr(orch, "iteration_no", 1),
            "iterations": list(getattr(orch, "iterations", []) or [])[-20:],
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
