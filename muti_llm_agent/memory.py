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

多会话隔离：落盘路径为 `{memory_dir}/{session_id}/{agent_id}/`（default 会话
沿用旧布局），不同 session 的同名席位不会共享记忆。
"""
import inspect
import json
import re
import threading
import time
from pathlib import Path

import numpy as np

from muti_llm_agent.config import safe_path_component
from muti_llm_agent.logger import log

DEFAULT_FORGET_CONFIG = {
    "short_capacity": 24,     # 短期记忆最大条数
    "long_capacity": 200,     # 长期记忆最大条数
    "context_capacity": 30,   # 情境记忆最大条数
    "decay_per_day": 0.05,    # 每天重要性衰减比例
    "min_importance": 0.08,   # 有效重要性低于此值则遗忘
    "promote_importance": 0.75,  # 短期记忆重要性高于此值直接提升为长期
}


def _tokenize(text: str) -> set:
    """中文友好的轻量分词：ASCII 单词 + CJK 单字。

    原有关键词匹配使用 `query.lower().split()`，对无空格分隔的中文完全失效
    （整句被当作一个 token）。这里改为抽取 ASCII 单词与 CJK 单字作为 token，
    使中文关键词检索可用（优化 A3）。
    """
    if not text:
        return set()
    words = re.findall(r"[A-Za-z0-9_]+", text.lower())
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    return set(words) | set(cjk)


class MemorySystem:
    def __init__(self, agent_id: str, memory_dir: Path,
                 forget_config: dict | None = None, session_id: str = "default"):
        """单个 Agent 的记忆系统。

        session_id 决定记忆落盘目录，保证**多会话隔离**：不同 session 即使
        席位 agent_id 相同（前端默认 agent_1/agent_2…），记忆也互不可见。
        「default」会话沿用旧布局 `{memory_dir}/{agent_id}/`（无需迁移历史数据），
        其余会话落到 `{memory_dir}/{session}/{agent_id}/`。
        agent_id / session_id 均经过 safe_path_component 清洗，防目录穿越。
        """
        self.agent_id = safe_path_component(agent_id, default="agent")
        self.session_id = safe_path_component(session_id, default="default")
        base = Path(memory_dir)
        if self.session_id != "default":
            base = base / self.session_id
        self.dir = base / self.agent_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "memory.json"
        self.lock = threading.Lock()
        # 注意：属性不能叫 forget —— 本类有同名方法 forget()，属性会把它覆盖掉，
        # 导致 memory.forget() 变成"调用一个 dict"（历史 bug，已被 mypy 方法赋值
        # 检查发现：遗忘机制长期因此静默失效）。属性统一命名 forget_config。
        self.forget_config: dict = dict(DEFAULT_FORGET_CONFIG)
        if forget_config:
            self.forget_config.update(forget_config)

        self.short_term: list[dict] = []
        self.long_term: list[dict] = []
        self.contextual: list[dict] = []
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
            except Exception as exc:
                # 坏文件 / 半截写入：按空记忆继续，但必须留痕——否则现场表现是
                # 「记忆莫名清空」，无从追查。
                log("warning", "memory.load_failed",
                    "记忆文件读取失败，按空记忆继续", path=str(self.file),
                    error=str(exc))

    def _save(self):
        data = {
            "short_term": self.short_term,
            "long_term": self.long_term,
            "contextual": self.contextual,
            "id_counter": self._id,
            "forget": self.forget_config,
        }
        self.file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- 向量嵌入（优化 A3） ----------------
    def _try_embed(self, text: str):
        """计算单条文本嵌入向量；任何失败返回 None（调用方回退关键词检索）。"""
        if not text:
            return None
        try:
            from muti_llm_agent.embeddings import embed_query
            return embed_query(text)
        except Exception:
            return None

    def _batch_embed(self, texts):
        """批量计算嵌入；失败返回 None。"""
        if not texts:
            return None
        try:
            from muti_llm_agent.embeddings import embed
            return embed(texts)
        except Exception:
            return None

    # ---------------- 写入 ----------------
    def add_short(self, text: str, importance: float = 0.5):
        with self.lock:
            self.short_term.append({
                "id": self._new_id(),
                "text": text,
                "ts": self._now(),
                "importance": float(importance),
                "access": 1,
                "embedding": self._try_embed(text),  # 写入时缓存向量，检索免重复计算
            })
            # 容量溢出 → 最不重要的提升或遗忘
            while len(self.short_term) > self.forget_config["short_capacity"]:
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
                "embedding": self._try_embed(text),
            })
            self._trim("long_term", self.forget_config["long_capacity"])
            self._save()

    def add_context(self, text: str, importance: float = 0.7):
        with self.lock:
            self.contextual.append({
                "id": self._new_id(),
                "text": text,
                "ts": self._now(),
                "importance": float(importance),
                "access": 1,
                "embedding": self._try_embed(text),
            })
            self._trim("contextual", self.forget_config["context_capacity"])
            self._save()

    # ---------------- 转化 ----------------
    def _promote_or_drop_short(self):
        """短期容量溢出时：重要者提升为长期，其余丢弃。"""
        if not self.short_term:
            return
        item = min(self.short_term, key=lambda x: x["importance"])
        self.short_term.remove(item)
        if item["importance"] >= self.forget_config["promote_importance"] * 0.6:
            self.long_term.append({**item, "ts": self._now(), "access": 1})
            self._trim("long_term", self.forget_config["long_capacity"])

    def promote_short_to_long(self, min_importance: float | None = None):
        """把重要性达标的短期记忆提升为长期记忆。"""
        thr = min_importance if min_importance is not None else self.forget_config["promote_importance"]
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
            self._trim("long_term", self.forget_config["long_capacity"])
            self._save()
        return [p["text"] for p in promoted]

    async def summarize_short_to_context(self, summarizer=None):
        """
        把短期记忆压缩为一条情境摘要（知识摘要）。
        summarizer: 可选的可调用对象(texts:list)->str（同步或 async 均可），
                    用于调用 LLM 生成摘要；未提供时使用简单拼接摘要。
        注意：为避免跨 await 持有 threading.Lock，LLM 摘要在锁外执行；
              摘要结果落库与短期清空仍在锁内完成。
        """
        with self.lock:
            if not self.short_term:
                return None
            texts = [it["text"] for it in self.short_term]
        if summarizer:
            try:
                result = summarizer(texts)
                if inspect.isawaitable(result):
                    result = await result
                summary = result or ""
            except Exception:
                summary = ""
        else:
            summary = "近期要点：" + "；".join(texts[:8])
        if not summary:
            summary = "；".join(texts) or "（空摘要）"
        with self.lock:
            self.contextual.append({
                "id": self._new_id(),
                "text": summary,
                "ts": self._now(),
                "importance": 0.7,
                "access": 1,
                "embedding": self._try_embed(summary),
            })
            self._trim("contextual", self.forget_config["context_capacity"])
            # 短期清空（已沉淀为情境）
            self.short_term = []
            self._save()
        return summary

    # ---------------- 遗忘 ----------------
    def _effective_importance(self, item):
        age_days = (self._now() - item.get("ts", self._now())) / 86400.0
        decay = (1.0 - self.forget_config["decay_per_day"]) ** max(age_days, 0)
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
                kept = [it for it in lst if self._effective_importance(it) >= self.forget_config["min_importance"]]
                # 容量保护：保留有效重要性高的
                cap = self.forget_config.get(f"{cat.split('_')[0]}_capacity", 200)
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
        """检索长期 + 情境记忆；query 为空时返回最近重要的记忆。

        优化 A3：query 非空时采用「向量语义 + 关键词 + 重要性」混合排序：
          - 向量语义：embed_query 计算查询向量，与条目缓存的 embedding 做余弦相似度；
          - 关键词：中文友好的 _tokenize 分词覆盖率（解决原 `lower().split()` 中文失效）；
          - 重要性：_effective_importance（时间衰减 + 访问频次）。
        分数 = 0.5*语义 + 0.3*关键词 + 0.2*重要性（RRF 加权思想）。
        嵌入不可用（模型加载失败 / 旧数据无缓存）时自动回退关键词 + 重要性排序。
        """
        with self.lock:
            long = sorted(self.long_term, key=self._effective_importance, reverse=True)
            ctx = sorted(self.contextual, key=self._effective_importance, reverse=True)
            recent_short = sorted(self.short_term, key=lambda x: x.get("ts", 0), reverse=True)

        if not query:
            result = []
            result += [f"[情境] {it['text']}" for it in ctx[:top_k]]
            result += [f"[长期] {it['text']}" for it in long[:top_k]]
            result += [f"[短期] {it['text']}" for it in recent_short[:3]]
            return result

        pool = [("long", it) for it in long] + \
               [("ctx", it) for it in ctx] + \
               [("short", it) for it in recent_short]
        scored = self._hybrid_search(query, pool)
        top_ctx = [it for s, c, it in scored if c == "ctx"][:top_k]
        top_long = [it for s, c, it in scored if c == "long"][:top_k]
        top_short = [it for s, c, it in scored if c == "short"][:3]

        result = []
        result += [f"[情境] {it['text']}" for it in top_ctx]
        result += [f"[长期] {it['text']}" for it in top_long]
        result += [f"[短期] {it['text']}" for it in top_short]
        return result

    def _hybrid_search(self, query: str, pool):
        """向量语义 + 关键词 + 重要性的加权混合排序，返回 [(score, cat, item)]。"""
        q_tokens = _tokenize(query)
        q_vec = self._try_embed(query)
        # 为缺失向量缓存的条目批量计算并回填（写入时已缓存，仅旧数据/降级路径需要）
        missing = [it for _, it in pool if not it.get("embedding")]
        if q_vec is not None and missing:
            vecs = self._batch_embed([it["text"] for it in missing])
            if vecs:
                with self.lock:
                    for it, v in zip(missing, vecs, strict=False):
                        it["embedding"] = v

        qv = np.array(q_vec, dtype="float32") if q_vec is not None else None
        scored = []
        for cat, it in pool:
            sem = 0.0
            emb = it.get("embedding")
            if qv is not None and emb:
                try:
                    v = np.array(emb, dtype="float32")
                    sim = float(np.dot(qv, v) /
                                (np.linalg.norm(qv) * np.linalg.norm(v) + 1e-9))
                    sem = max(0.0, sim)  # 余弦可为负，截断避免惩罚
                except Exception:
                    sem = 0.0
            t_text = _tokenize(it.get("text", ""))
            kw = len(q_tokens & t_text) / max(1, len(q_tokens))
            imp = min(1.0, self._effective_importance(it))
            score = 0.5 * sem + 0.3 * kw + 0.2 * imp
            scored.append((score, cat, it))
        scored.sort(key=lambda x: -x[0])
        return scored

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
                "forget_config": dict(self.forget_config),
            }
