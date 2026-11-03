"""
history.py — 历史讨论记忆化保存与检索服务

把每场讨论的「题目 + 元信息」持久化到 data/discussions/index.json，
作为历史讨论的记忆索引，支持后续的关键词检索与一键查看/导出 Markdown。

数据文件：
  data/discussions/index.json
    按时间倒序保存的记录列表，单条结构：
    {
      "filename":  "discussion_20260816_113806.md",
      "topic":     "当博物馆里着火…应该先救猫还是先救画？",
      "trace_id":  "be075641cd67",
      "timestamp": "2026-08-16 11:38:06",
      "ts":        1784302686000,
      "status":    "done",
      "phase":     "finished",
      "round":     4,
      "max_rounds": 4,
      "token_spent": 183468,
      "agents":    [{"name","role","stance","is_main"}, ...]
    }

设计说明：
  - 记忆化保存：讨论完成 / 保存 Markdown 时自动 upsert 一条索引（按 filename 去重）。
  - 检索：list_discussions(q) 对题目 / trace_id 做不区分大小写的关键词过滤；
    前端历史面板据此实现「查询 + 搜索」。
  - 安全：get_markdown 仅允许读取 DISCUSSIONS_DIR 内的文件，防路径穿越。
"""
from __future__ import annotations

import json
import time

from muti_llm_agent.config import PROJECT_ROOT

DISCUSSIONS_DIR = PROJECT_ROOT / "data" / "discussions"
INDEX_PATH = DISCUSSIONS_DIR / "index.json"

_INDEX_LOCK = __import__("threading").Lock()


def _load() -> list:
    """读取历史索引；文件不存在或损坏时返回空列表。"""
    if not INDEX_PATH.exists():
        return []
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(records: list):
    DISCUSSIONS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def record_discussion(topic: str = "", trace_id: str = "", filename: str = "",
                      status: str = "", phase: str = "", round_no: int = 0,
                      max_rounds: int = 0, token_spent: int = 0,
                      agents: list | None = None) -> dict:
    """记录 / 更新一场讨论的记忆索引（按 filename 去重，新记录插入头部）。"""
    if not filename:
        return {}
    agents = agents or []
    rec = {
        "filename": filename,
        "topic": topic or "",
        "trace_id": trace_id or "",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": int(time.time() * 1000),
        "status": status or "",
        "phase": phase or "",
        "round": int(round_no or 0),
        "max_rounds": int(max_rounds or 0),
        "token_spent": int(token_spent or 0),
        "agents": [
            {
                "name": a.get("name", ""),
                "role": a.get("role", ""),
                "stance": a.get("stance", ""),
                "is_main": bool(a.get("is_main", False)),
            }
            for a in agents
        ],
    }
    with _INDEX_LOCK:
        records = [r for r in _load() if r.get("filename") != filename]
        records.insert(0, rec)
        # 控制索引体积：仅保留最近 200 条
        _save(records[:200])
    return rec


def list_discussions(q: str = "") -> list:
    """列出历史讨论；q 非空时对题目 / trace_id 做关键词过滤。"""
    records = _load()
    q = (q or "").strip().lower()
    if q:
        records = [
            r for r in records
            if q in (r.get("topic") or "").lower()
            or q in (r.get("trace_id") or "").lower()
        ]
    return records


def get_markdown(filename: str):
    """读取指定讨论记录的 Markdown 内容；非法 / 不存在返回 None。"""
    if not filename:
        return None
    base = DISCUSSIONS_DIR.resolve()
    p = (DISCUSSIONS_DIR / filename).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        return None  # 路径穿越防护
    if not p.is_file() or p.suffix != ".md":
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None
