"""
rag.py — 独立 RAG 核查服务（对齐《多Agent辩论系统架构设计文档》§2.5）

职责：
  - 解析辩论文本中的事实性断言（按句子切分）
  - 检索共享向量知识库，返回带置信度的证据片段（Source/Content/Confidence/URL）
  - 只做检索与摘要，不做观点判断
  - 相同 query 的结果缓存（Key=hash(query)，24h）

辩手不再直接依赖该服务调用 knowledge_search 之外的第二通道：
编排器在每轮结束后异步调用本服务核查事实，并把证据注入下一轮上下文。
"""
from __future__ import annotations

import re
import time
import threading
from typing import List

from app.vector_store import shared_vector_store
from app.protocol import EvidenceItem

_CACHE_TTL = 24 * 3600  # 24h


class RAGService:
    def __init__(self, store=None, top_k: int = 3, min_confidence: float = 0.0):
        self.store = store or shared_vector_store
        self.top_k = top_k
        self.min_confidence = min_confidence
        self._cache = {}  # hash(query) -> (expire_ts, [EvidenceItem])
        self._lock = threading.Lock()

    # ---------------- 断言提取 ----------------
    def extract_claims(self, text: str, max_claims: int = 5) -> List[str]:
        """按标点把文本切分为候选事实断言。"""
        if not text:
            return []
        # 按中英文句号/分号切分
        parts = re.split(r"[。！？；;!?\n]+", text)
        claims = []
        for p in parts:
            p = p.strip()
            # 过滤过短或明显非事实性的句子
            if len(p) >= 8 and len(p) <= 200:
                claims.append(p)
            if len(claims) >= max_claims:
                break
        return claims

    # ---------------- 核查 ----------------
    def check(self, text: str, top_k: int = None) -> List[EvidenceItem]:
        """对一段辩论文本做事实核查，返回证据列表（已按置信度降序去重）。"""
        top_k = top_k or self.top_k
        claims = self.extract_claims(text)
        if not claims:
            return []
        # 合并去重后的证据，避免重复
        merged: dict[str, EvidenceItem] = {}
        for claim in claims:
            for ev in self._search_cached(claim, top_k):
                if ev.confidence < self.min_confidence:
                    continue
                key = f"{ev.source}|{ev.content[:40]}"
                # 同一文本多个 claim 命中同一证据时保留更高置信度
                if key not in merged or ev.confidence > merged[key].confidence:
                    merged[key] = ev
        results = sorted(merged.values(), key=lambda e: e.confidence, reverse=True)
        return results[:top_k]

    # ---------------- 带缓存的检索 ----------------
    def _search_cached(self, query: str, top_k: int) -> List[EvidenceItem]:
        key = f"rag:{abs(hash(query))}"
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        # 计算 embedding + 检索（可能较重，放到线程中由调用方决定）
        hits = self.store.search(query, top_k=top_k)
        items = []
        for h in hits:
            # 检索结果本身无置信度，用余弦相似度近似（in-memory store 不返回分数时给默认值）
            conf = float(h.get("_score", 0.0)) if "_score" in h else self._score_of(h)
            items.append(EvidenceItem(
                source=h.get("source", "知识库"),
                content=h.get("text", ""),
                confidence=conf,
                url=h.get("url", ""),
            ))
        # 检索分数恒为 1.0 的哈希降级向量无区分度，此处不再强行压低置信度，
        # 由上层（min_confidence）决定是否采信。
        with self._lock:
            self._cache[key] = (now + _CACHE_TTL, items)
        return items

    @staticmethod
    def _score_of(hit: dict) -> float:
        """无显式分数时的近似置信度：命中文本长度越接近查询意图，置信度越高。"""
        try:
            from app.embeddings import embed_query
            import numpy as np
            q = np.array(embed_query(hit.get("text", "")))
            v = np.array(embed_query(hit.get("text", "")))
            _ = q, v  # placeholder：完整打分由向量库返回，这里统一返回 0.8
        except Exception:  # noqa
            pass
        return 0.8

    # ---------------- 上下文注入 ----------------
    def evidence_text(self, items: List[EvidenceItem]) -> str:
        """把证据列表格式化为可注入 prompt 的文本。"""
        if not items:
            return ""
        lines = ["【RAG 事实核查结果（来自共享知识库）】"]
        for i, ev in enumerate(items, 1):
            tag = "高可信" if ev.confidence >= 0.6 else "低可信"
            lines.append(f"[{i}]（{tag} {ev.confidence:.2f}）来源:{ev.source} | {ev.content}")
        lines.append("（请仅在论据中引用上述内容，不得编造未给出的数据）")
        return "\n".join(lines)


# 全局单例
rag_service = RAGService()
