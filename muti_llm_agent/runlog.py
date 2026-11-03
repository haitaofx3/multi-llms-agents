"""
runlog.py — 运行事件日志（复盘时间线的唯一事实来源）

与 `logger.py` 的分工：
  - `logger.py`  是**跨模块诊断日志**：格式自由、用于排障，不保证顺序语义；
  - `runlog.py`  是**单场运行的事件日志**：一次运行一个文件，一条事件一行 JSON，
    带单调递增 `seq`，因此可以「按序号重放」出完整时间线
    （谁在第几轮说了什么、token 何时增长、纠偏指令何时生效、任务何时完成）。

存储布局：
  data/runs/{trace_id}.jsonl   逐条事件（首行是 header，含主题 / 模式 / 席位）
  data/runs/index.json         全部运行索引（新→旧，供 /api/runs 与复盘列表）

为什么单独落一份而不是直接复用日志：复盘要的是「结构化、有序、可对齐轮次」的
事件流；把它混在诊断日志里，任何一次日志级别调整都会破坏复盘数据完整性。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from muti_llm_agent.config import RUNS_DIR, safe_path_component, settings
from muti_llm_agent.logger import log

INDEX_PATH = RUNS_DIR / "index.json"
_LOCK = threading.Lock()

# 写入索引的运行条数上限（超出后最旧的被裁剪；jsonl 明细文件保留）
MAX_INDEX = 300


def _trace_file(trace_id: str) -> Path:
    """trace_id → 明细文件路径（清洗为安全单级文件名，防目录穿越）。"""
    return RUNS_DIR / f"{safe_path_component(trace_id, 'unknown')}.jsonl"


def _read_index() -> list[dict]:
    if not INDEX_PATH.is_file():
        return []
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_index(records: list[dict]) -> None:
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        INDEX_PATH.write_text(
            json.dumps(records[:MAX_INDEX], ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:  # 索引写失败不影响事件明细
        log("warning", "runlog.index_write", f"运行索引写入失败：{e}", error=str(e))


def enabled() -> bool:
    """事件日志是否启用（可被测试 / 配置关闭）。"""
    return bool(getattr(settings, "RUNLOG_ENABLED", True))


def write_event(trace_id: str, event: dict) -> dict | None:
    """追加一条运行事件；返回带 seq / ts 的事件（未启用时原样返回 None）。

    事件写入失败一律静默降级：观测层故障绝不能中断讨论主流程。
    单场事件数超过 `RUNLOG_MAX_EVENTS` 后停止写入（防止异常循环把磁盘写满，
    此时摘要里的 events 计数仍会体现异常量）。
    """
    if not trace_id or not enabled():
        return None
    max_events = int(getattr(settings, "RUNLOG_MAX_EVENTS", 20000) or 20000)
    rec = dict(event)
    rec.setdefault("trace_id", trace_id)
    rec["ts"] = rec.get("ts") or int(time.time() * 1000)
    try:
        with _LOCK:
            path = _trace_file(trace_id)
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            seq = _next_seq(path)
            if seq > max_events:
                return None
            rec["seq"] = seq
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec
    except Exception as e:  # pragma: no cover - 磁盘异常
        log("warning", "runlog.write", f"运行事件写入失败：{e}", error=str(e))
        return None


def _next_seq(path: Path) -> int:
    """读取文件最后一行拿 seq（只读尾行，避免整文件解析）。"""
    try:
        if not path.is_file():
            return 1
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, 4096)
            f.seek(size - block)
            tail = f.read().decode("utf-8", "ignore").strip().splitlines()
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                return int(json.loads(line).get("seq", 0)) + 1
            except Exception:  # noqa: S112 - 末行损坏时继续向前找，属刻意降级
                continue
    except Exception as exc:
        # seq 续接失败不影响事件可读性，只影响严格重放顺序；留痕便于排查。
        log("debug", "runlog.seq_scan_failed",
            "运行事件文件 seq 扫描失败，回退为按行数估算", error=str(exc))
    try:
        return max(1, sum(1 for _ in open(path, "rb")))
    except Exception:
        return 1


def write_header(trace_id: str, session_id: str = "", topic: str = "",
                 mode: str = "", iteration: int = 1, agents: list | None = None) -> None:
    """写入运行头部（一次运行一条），便于复盘时无索引也能自描述。"""
    if not trace_id or not enabled():
        return
    header = {
        "type": "header",
        "session_id": session_id,
        "topic": topic,
        "mode": mode,
        "iteration": iteration,
        "agents": agents or [],
        "started_at": int(time.time() * 1000),
    }
    # 头部固定为第 0 条：直接覆盖写（重跑同一 trace 时不会叠加多个 header）
    try:
        with _LOCK:
            path = _trace_file(trace_id)
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            rec = dict(header)
            rec.update({"trace_id": trace_id, "seq": 0,
                        "ts": header["started_at"]})
            path.write_text(
                json.dumps(rec, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8")
    except Exception as e:  # pragma: no cover
        log("warning", "runlog.header", f"运行头部写入失败：{e}", error=str(e))


def load_events(trace_id: str, limit: int | None = None) -> list[dict]:
    """读取一次运行的全部事件（按 seq 升序；损坏行跳过）。"""
    if not trace_id:
        return []
    path = _trace_file(trace_id)
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: S112 - 跳过损坏行，保证复盘不因一行脏数据整体失败
                continue
            if isinstance(rec, dict):
                out.append(rec)
    except Exception as e:
        log("warning", "runlog.read", f"运行事件读取失败：{e}", error=str(e))
        return []
    out.sort(key=lambda r: (r.get("seq", 0), r.get("ts", 0)))
    if limit and len(out) > int(limit):
        return out[-int(limit):]
    return out


def header_of(events: list[dict]) -> dict:
    """从事件列表中取运行头部（没有则返回空 dict）。"""
    for rec in events:
        if rec.get("type") == "header":
            return rec
    return {}


def register_run(summary: dict) -> dict:
    """把一次运行的摘要登记进索引（按 trace_id upsert，新记录插入头部）。"""
    trace_id = (summary or {}).get("trace_id") or ""
    if not trace_id:
        return {}
    rec = {
        "trace_id": trace_id,
        "session_id": summary.get("session_id", ""),
        "topic": summary.get("topic", ""),
        "mode": summary.get("mode", ""),
        "status": summary.get("status", ""),
        "iteration": summary.get("iteration", 1),
        "rounds_executed": summary.get("rounds_executed", 0),
        "max_rounds": summary.get("max_rounds", 0),
        "output_tokens": (summary.get("tokens") or {}).get("output", 0),
        "cost_rmb": summary.get("cost_rmb", 0.0),
        "agents": [a.get("name", "") for a in summary.get("agents", [])],
        "models": [a.get("model_id", "") for a in summary.get("agents", [])],
        "updated_at": int(time.time() * 1000),
        "started_at": summary.get("started_at"),
        "ended_at": summary.get("ended_at"),
        "file": f"{safe_path_component(trace_id, 'unknown')}.jsonl",
    }
    with _LOCK:
        records = [r for r in _read_index() if r.get("trace_id") != trace_id]
        records.insert(0, rec)
    _write_index(records)
    return rec


def list_runs(q: str = "", limit: int = 50) -> list[dict]:
    """运行索引列表（可按题目 / trace_id 关键词过滤）。"""
    records = _read_index()
    q = (q or "").strip().lower()
    if q:
        records = [
            r for r in records
            if q in str(r.get("topic", "")).lower()
            or q in str(r.get("trace_id", "")).lower()
            or q in str(r.get("session_id", "")).lower()
        ]
    return records[:max(1, int(limit or 50))]


def get_run(trace_id: str) -> dict | None:
    """按 trace_id 取索引记录。"""
    for rec in _read_index():
        if rec.get("trace_id") == trace_id:
            return rec
    return None


def stats() -> dict:
    """运行日志统计（文件数 / 索引条数 / 是否启用）。"""
    try:
        files = list(RUNS_DIR.glob("*.jsonl"))
    except Exception:
        files = []
    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except Exception as exc:
            # 单个文件 stat 失败（被删 / 权限）不该让整个统计失败，但要知道少算了谁。
            log("debug", "runlog.stat_failed",
                "运行事件文件大小统计失败，已跳过", path=str(p), error=str(exc))
    return {
        "enabled": enabled(),
        "runs": len(files),
        "indexed": len(_read_index()),
        "bytes": total,
        "dir": str(RUNS_DIR),
    }
