"""
logger.py — 结构化日志与追踪上下文（观测层基础设施）

统一输出 **JSON 行** 结构化日志（含 trace_id / level / event / msg / 附加字段），
替代散落在各模块的 print 与手工拼串，便于检索、采集与问题定位。

一次运行的可观测性由三件套构成（职责严格分离，互不重叠）：

  1. `logger.py`（本文件）── **诊断日志**。跨模块、无结构约束、用于排障；
     三条出口同时生效：stdout（JSON 行，兼容既有控制台）、
     `data/logs/app-YYYYMMDD.jsonl`（按天滚动，可被采集器 tail）、
     进程内环形缓冲（`/api/logs` 直接查询最近 N 条，免读盘）。
  2. `runlog.py` ── **运行事件日志**。一条事件一行 JSON，带单调 seq，
     是「复盘时间线」的唯一事实来源（可重放、可对齐轮次与 token）。
  3. `summary.py` ── **运行摘要**。一次运行结束时的聚合视图（指标 / 结论 / 异常）。

追踪上下文用 `contextvars` 而非 `threading.local`：asyncio 任务创建时会复制当前
上下文，因此 `asyncio.create_task` / `asyncio.to_thread` 派生出的执行单元能自动
继承 trace_id；而 threading.local 在协程切换、线程池回调用时会丢上下文。
为兼容历史调用点，仍保留 `set_trace_id` / `get_trace_id` 两个函数名。

用法：
    from muti_llm_agent.logger import log, set_trace_id, log_context, query_logs

    set_trace_id("abc123")                        # 只设 trace_id（历史用法）
    with log_context(trace_id="abc123", session_id="default"):
        log("info", "run.start", "讨论开始", round=1)
    query_logs(trace_id="abc123", level="error", limit=50)

日志级别：debug < info < warning < error。
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager, suppress
from typing import Any

# 强制 stdout/stderr 使用 UTF-8。
# 中文 Windows 上 Python 重定向后的 stdout 默认按 ANSI（GBK/cp936）编码，
# 导致 JSON 结构化日志中的中文在 UTF-8 终端（如 Git Bash）里显示为乱码（□□□□）。
# 这里在应用启动早期统一修复，不影响实际内容，仅改变编码方式。
for _stream in (sys.stdout, sys.stderr):
    try:
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: S110 - 日志系统此刻正在初始化，还不能调用 log()
        # 用回退编码继续跑即可。
        pass

# ---------------- 追踪上下文（contextvars，协程安全） ----------------
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="")
_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")

# ---------------- 进程内环形缓冲 ----------------
# 容量取 settings.LOG_RING_SIZE；logger 在 config 之前就被 import 过（历史依赖顺序），
# 因此这里用惰性读取，避免引入循环导入。
_ring: deque[dict] = deque(maxlen=2000)
_ring_lock = threading.Lock()

_LEVEL_NUM = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
_LEVEL_ORDER = {"debug": 10, "info": 20, "warning": 30, "error": 40}

# ---------------- stdout 后端（Python 标准 logging） ----------------
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
_LOGGER = logging.getLogger("muti_llm")
_LOGGER.setLevel(logging.DEBUG)
_LOGGER.addHandler(_handler)
_LOGGER.propagate = False

# ---------------- 文件后端（惰性初始化，按天滚动） ----------------
_file_lock = threading.Lock()
_file_state: dict[str, Any] = {"day": "", "path": None, "enabled": None}


def _settings_value(name: str, default):
    """惰性读 settings（避免 config <-> logger 循环导入，且便于测试打桩）。"""
    try:
        from muti_llm_agent.config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def _ring_size() -> int:
    try:
        return max(50, int(_settings_value("LOG_RING_SIZE", 2000)))
    except Exception:
        return 2000


def _file_enabled() -> bool:
    if _file_state["enabled"] is None:
        _file_state["enabled"] = bool(_settings_value("LOG_TO_FILE", True))
    return bool(_file_state["enabled"])


def _log_path() -> Any:
    """当日日志文件路径（首次调用时创建目录）。"""
    from muti_llm_agent.config import LOGS_DIR
    day = time.strftime("%Y%m%d")
    if _file_state["day"] != day or _file_state["path"] is None:
        _file_state["day"] = day
        _file_state["path"] = LOGS_DIR / f"app-{day}.jsonl"
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: S110 - 调用 log() 会经 _write_file 递归回本函数
            # 真实后果（日志落不了盘）由 _write_file 一次性报到 stderr，见下。
            pass
    return _file_state["path"]


def _write_file(line: str) -> None:
    """最佳努力写盘：日志落盘失败绝不能影响主流程。"""
    if not _file_enabled():
        return
    try:
        with _file_lock:
            path = _log_path()
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:
        # 一次失败后本进程不再尝试，避免每条日志都抛一次异常拖慢主流程。
        _file_state["enabled"] = False
        # 文件出口是整个可观测性里唯一「静默降级」的环节：一旦关掉，
        # 用户只是发现 data/logs 不再增长。这里绕过 log()（会递归）直接写一次 stderr。
        with suppress(Exception):
            sys.stderr.write(
                f"[logger] 诊断日志落盘已关闭（{exc}）；本次进程内只保留 stdout 与环形缓冲\n")


# ---------------- 上下文管理 ----------------
def set_trace_id(trace_id: str) -> None:
    """把 trace_id 写入当前上下文，供后续所有日志携带。"""
    _trace_id.set(trace_id or "")


def get_trace_id() -> str:
    return _trace_id.get()


def set_log_context(trace_id: str = "", session_id: str = "",
                    run_id: str = "") -> None:
    """一次性设置全部追踪字段（空串表示不改动该字段）。"""
    if trace_id:
        _trace_id.set(trace_id)
    if session_id:
        _session_id.set(session_id)
    if run_id:
        _run_id.set(run_id)


def get_log_context() -> dict:
    return {
        "trace_id": _trace_id.get(),
        "session_id": _session_id.get(),
        "run_id": _run_id.get(),
    }


@contextmanager
def log_context(trace_id: str = "", session_id: str = "",
                run_id: str = ""):
    """在 with 块内绑定追踪字段，退出时恢复原值（协程内安全）。"""
    torches = []
    if trace_id:
        torches.append((_trace_id, _trace_id.set(trace_id)))
    if session_id:
        torches.append((_session_id, _session_id.set(session_id)))
    if run_id:
        torches.append((_run_id, _run_id.set(run_id)))
    try:
        yield
    finally:
        for var, token in reversed(torches):
            try:
                var.reset(token)
            except Exception as exc:  # pragma: no cover - 上下文令牌失效属内部不变量破坏
                log("warning", "logger.reset_context_failed",
                    "日志上下文复位失败，后续日志可能带错 trace_id", error=str(exc))


# ---------------- 日志写入 ----------------
def log(level: str = "info", event: str = "", message: str = "", **fields) -> None:
    """输出一条结构化日志（stdout + 文件 + 环形缓冲）。

    Args:
        level: debug / info / warning / error
        event: 事件名（如 embeddings.load、run.start）
        message: 人类可读描述
        **fields: 附加字段（trace_id / session_id / run_id 默认取上下文，可显式覆盖）
    """
    level = (level or "info").lower()
    if level not in _LEVEL_NUM:
        level = "info"
    minimum = str(_settings_value("LOG_LEVEL", "info") or "info").lower()
    if _LEVEL_ORDER.get(level, 20) < _LEVEL_ORDER.get(minimum, 20):
        return
    rec: dict[str, Any] = {
        "ts": int(time.time() * 1000),
        "level": level,
        "event": event or "log",
        "msg": message,
        "trace_id": fields.pop("trace_id", None) or _trace_id.get(),
        "session_id": fields.pop("session_id", None) or _session_id.get(),
        "run_id": fields.pop("run_id", None) or _run_id.get(),
    }
    for k, v in fields.items():
        if v is not None:
            rec[k] = v
    line = json.dumps(rec, ensure_ascii=False, default=str)

    # 1) 环形缓冲（供 /api/logs 查询）
    entry = dict(rec)
    with _ring_lock:
        if _ring.maxlen != _ring_size():
            # 容量配置在运行时可被测试改写，重建缓冲保留最近内容
            new_ring: deque = deque(_ring, maxlen=_ring_size())
            globals()["_ring"] = new_ring
        globals()["_ring"].append(entry)
    # 2) 文件
    _write_file(line)
    # 3) stdout（保持历史控制台行为）
    num = _LEVEL_NUM[level]
    if num == logging.ERROR:
        _LOGGER.error(line)
    elif num == logging.WARNING:
        _LOGGER.warning(line)
    elif num == logging.DEBUG:
        _LOGGER.debug(line)
    else:
        _LOGGER.info(line)


def query_logs(trace_id: str = "", level: str = "", limit: int = 100,
               event: str = "") -> list[dict]:
    """从环形缓冲查询最近日志（新→旧过滤，返回按时间正序）。

    Args:
        trace_id: 非空时只返回该链路的日志
        level: 非空时只返回「不低于」该级别的日志（error 只返回 error）
        limit: 返回条数上限（取最近 N 条）
        event: 事件名前缀过滤
    """
    level = (level or "").lower()
    min_num = _LEVEL_ORDER.get(level, 0) if level else 0
    event = (event or "").strip()
    with _ring_lock:
        items = list(_ring)
    out = []
    for rec in items:
        if trace_id and rec.get("trace_id") != trace_id:
            continue
        if min_num and _LEVEL_ORDER.get(rec.get("level", "info"), 20) < min_num:
            continue
        if event and not str(rec.get("event", "")).startswith(event):
            continue
        out.append(rec)
    limit = max(1, int(limit or 100))
    out = out[-limit:]
    return out


def log_stats() -> dict:
    """环形缓冲与文件出口状态（供 /api/logs 与运维排查）。"""
    with _ring_lock:
        size = len(_ring)
    return {
        "ring_size": size,
        "ring_capacity": _ring_size(),
        "file_enabled": _file_enabled(),
        "file_path": str(_file_state["path"] or ""),
        "level": str(_settings_value("LOG_LEVEL", "info")),
    }


def reset_log_ring() -> None:
    """清空环形缓冲（测试用）。"""
    with _ring_lock:
        _ring.clear()
