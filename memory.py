"""
memory.py — 每个 Agent 的记忆系统

记忆分为三类，并支持互相转化与遗忘：
  - 短期记忆 (short_term):    当前讨论近期的对话/事件，滚动窗口
  - 长期记忆 (long_term):     经提炼沉淀的重要事实 / 决策 / 结论
  - 情境记忆 (contextual):    对知识/讨论的"摘要式"情境理解（知识摘要）

转化规则：
  - 短期 → 长期:  当某条短期记忆重要性高、或短期容量溢出时，提升为长期
  - 短期 → 情境:  周期性把短期缓冲压缩成情境摘要（知识摘要）
  - 长期 ⇄ 情境:  检索时合并长期事实与情境摘要，互相补充

遗忘体系（每个 agent 可独立配置）：
  - 基于时间衰减 + 访问频次的"有效重要性"，低于阈值或超出容量则遗忘
"""
import json
import time
import threading
from pathlib import Path

from config import settings

DEFAULT_FORGET_CONFIG = {
    "short_capacity": 24,     # 短期记忆最大条数
    "long_capacity": 200,     # 长期记忆最大条数
    "context_capacity": 30,   # 情境记忆最大条数
    "decay_per_day": 0.05,    # 每天重要性衰减比例
    "min_importance": 0.08,   # 有效重要性低于此值则遗忘
    "promote_importance": 0.75,  # 短期记忆重要性高于此值直接提升为长期
}


class MemorySystem:
    def __init__(self, agent_id: str, memory_dir: Path, forget_config: dict = None):
        self.agent_id = agent_id
        self.dir = Path(memory_dir) / agent_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "memory.json"
        self.lock = threading.Lock()
        self.forget = dict(DEFAULT_FORGET_CONFIG)
        if forget_config:
            self.forget.update(forget_config)

        self.short_term = []
        self.long_term = []
        self.contextual = []
        self._id = 1
        self._load()

    # ---------------- 基础存取 ----------------
    def _new_id(self):
        self._id += 1
        return self._id

    def _now(self):
        return time.time()

    def _load(self):
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                self.short_term = data.get("short_term", [])
                self.long_term = data.get("long_term", [])
                self.contextual = data.get("contextual", [])
                self._id = data.get("id_counter", 1)
                if "forget" in data:
                    self.forget.update(data["forget"])
            except Exception:
                pass

    def _save(self):
        data = {
            "short_term": self.short_term,
            "long_term": self.long_term,
            "contextual": self.contextual,
            "id_counter": self._id,
            "forget": self.forget,
        }
        self.file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- 写入 ----------------
    def add_short(self, text: str, importance: float = 0.5):
        with self.lock:
            self.short_term.append({
                "id": self._new_id(),
                "text": text,
                "ts": self._now(),
                "importance": float(importance),
                "access": 1,
            })
            # 容量溢出 → 最不重要的提升或遗忘
            while len(self.short_term) > self.forget["short_capacity"]:
                self._promote_or_drop_short()
            self._save()

    def add_long(self, text: str, importance: float = 0.8):
        with self.lock:
            self.long_term.append({
                "id": self._new_id(),
                "text": text,
                "ts": self._now(),
                "importance": float(importance),
                "access": 1,
            })
            self._trim("long_term", self.forget["long_capacity"])
            self._save()

    def add_context(self, text: str, importance: float = 0.7):
        with self.lock:
            self.contextual.append({
                "id": self._new_id(),
                "text": text,
                "ts": self._now(),
                "importance": float(importance),
                "access": 1,
            })
            self._trim("contextual", self.forget["context_capacity"])
            self._save()

    # ---------------- 转化 ----------------
    def _promote_or_drop_short(self):
        """短期容量溢出时：重要者提升为长期，其余丢弃。"""
        if not self.short_term:
            return
        item = min(self.short_term, key=lambda x: x["importance"])
        self.short_term.remove(item)
        if item["importance"] >= self.forget["promote_importance"] * 0.6:
            self.long_term.append({**item, "ts": self._now(), "access": 1})
            self._trim("long_term", self.forget["long_capacity"])

    def promote_short_to_long(self, min_importance: float = None):
        """把重要性达标的短期记忆提升为长期记忆。"""
        thr = min_importance if min_importance is not None else self.forget["promote_importance"]
        promoted = []
        with self.lock:
            remain = []
            for it in self.short_term:
                if it["importance"] >= thr:
                    promoted.append(it)
                    self.long_term.append({**it, "ts": self._now(), "access": 1})
                else:
                    remain.append(it)
            self.short_term = remain
            self._trim("long_term", self.forget["long_capacity"])
            self._save()
        return [p["text"] for p in promoted]

    def summarize_short_to_context(self, summarizer=None):
        """
        把短期记忆压缩为一条情境摘要（知识摘要）。
        summarizer: 可选的可调用对象(texts:list)->str，用于调用 LLM 生成摘要；
                    未提供时使用简单拼接摘要。
        """
        with self.lock:
            if not self.short_term:
                return None
            texts = [it["text"] for it in self.short_term]
            if summarizer:
                try:
                    summary = summarizer(texts)
                except Exception:
                    summary = "；".join(texts)
            else:
                summary = "近期要点：" + "；".join(texts[:8])
            self.contextual.append({
                "id": self._new_id(),
                "text": summary,
                "ts": self._now(),
                "importance": 0.7,
                "access": 1,
            })
            self._trim("contextual", self.forget["context_capacity"])
            # 短期清空（已沉淀为情境）
            self.short_term = []
            self._save()
        return summary

    # ---------------- 遗忘 ----------------
    def _effective_importance(self, item):
        age_days = (self._now() - item.get("ts", self._now())) / 86400.0
        decay = (1.0 - self.forget["decay_per_day"]) ** max(age_days, 0)
        access_factor = 1.0 + 0.05 * (item.get("access", 1) - 1)
        return item.get("importance", 0.5) * decay * access_factor

    def _trim(self, cat_name: str, cap: int):
        """按有效重要性保留前 cap 条，超出部分丢弃。"""
        lst = getattr(self, cat_name)
        if len(lst) > cap:
            lst.sort(key=self._effective_importance, reverse=True)
            setattr(self, cat_name, lst[:cap])

    def forget(self):
        """应用遗忘策略：有效重要性低于阈值或超出容量则移除。"""
        with self.lock:
            for cat in ("short_term", "long_term", "contextual"):
                lst = getattr(self, cat)
                kept = [it for it in lst if self._effective_importance(it) >= self.forget["min_importance"]]
                # 容量保护：保留有效重要性高的
                cap = self.forget.get(f"{cat.split('_')[0]}_capacity", 200)
                if len(kept) > cap:
                    kept.sort(key=self._effective_importance, reverse=True)
                    kept = kept[:cap]
                setattr(self, cat, kept)
            self._save()

    def touch(self, item_ids):
        with self.lock:
            all_items = self.short_term + self.long_term + self.contextual
            idset = set(item_ids)
            for it in all_items:
                if it["id"] in idset:
                    it["access"] = it.get("access", 1) + 1
            self._save()

    # ---------------- 检索 ----------------
    def query(self, query: str = "", top_k: int = 4):
        """检索长期 + 情境记忆；query 为空时返回最近重要的记忆。"""
        with self.lock:
            long = sorted(self.long_term, key=self._effective_importance, reverse=True)
            ctx = sorted(self.contextual, key=self._effective_importance, reverse=True)
            recent_short = sorted(self.short_term, key=lambda x: x.get("ts", 0), reverse=True)
        # 简单关键词匹配（轻量，无需再 embedding）
        if query:
            q = set(query.lower().split())
            def score(items):
                res = []
                for it in items:
                    text = it["text"].lower()
                    s = sum(1 for w in q if w and w in text)
                    res.append((s + self._effective_importance(it) * 0.1, it))
                res.sort(key=lambda x: -x[0])
                return [it for _, it in res]
            long = score(long)
            ctx = score(ctx)
        result = []
        result += [f"[情境] {it['text']}" for it in ctx[:top_k]]
        result += [f"[长期] {it['text']}" for it in long[:top_k]]
        result += [f"[短期] {it['text']}" for it in recent_short[:3]]
        return result

    def context_text(self, query: str = "", top_k: int = 3) -> str:
        items = self.query(query, top_k)
        if not items:
            return "（暂无记忆）"
        return "\n".join(f"- {it}" for it in items)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "short_term": len(self.short_term),
                "long_term": len(self.long_term),
                "contextual": len(self.contextual),
                "forget_config": self.forget,
            }
