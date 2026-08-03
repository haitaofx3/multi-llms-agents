"""
vector_store.py — 多 Agent 共享的向量知识库

所有 agent 访问同一个向量数据库进行知识检索：
- 若本地/远程 Qdrant 可达则使用 Qdrant；
- 否则自动降级为进程内内存向量库（余弦相似度检索），保证功能可用。

提供：add(texts, metas) 与 search(query, top_k)。
"""
import threading
import numpy as np
from app.config import settings
from app.embeddings import embed, embed_query, dim


class _InMemoryStore:
    def __init__(self, size: int):
        self.size = size
        self.items = []  # (vector, payload)
        self.lock = threading.Lock()
        self._id = 0

    def add(self, texts, metas):
        vecs = embed(texts)
        with self.lock:
            for t, v, m in zip(texts, vecs, metas):
                self._id += 1
                self.items.append((np.array(v, dtype="float32"),
                                   {**m, "text": t, "id": self._id}))

    def search(self, query, top_k=5):
        if not self.items:
            return []
        q = np.array(embed_query(query), dtype="float32")
        qn = np.linalg.norm(q) + 1e-9
        scored = []
        with self.lock:
            items = list(self.items)
        for v, m in items:
            vn = np.linalg.norm(v) + 1e-9
            sim = float(np.dot(q, v) / (qn * vn))
            scored.append((sim, m))
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:top_k]]


class _QdrantStore:
    def __init__(self):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams
        self.client = QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY or None,
            timeout=settings.QDRANT_TIMEOUT,
        )
        self.collection = settings.QDRANT_COLLECTION
        self.size = dim()
        try:
            self.client.get_collection(self.collection)
        except Exception:
            self.client.create_collection(
                self.collection,
                vectors_config=VectorParams(
                    size=self.size, distance=Distance.COSINE
                ),
            )

    def add(self, texts, metas):
        from qdrant_client.models import PointStruct
        vecs = embed(texts)
        points = []
        for i, (t, v, m) in enumerate(zip(texts, vecs, metas)):
            points.append(PointStruct(
                id=abs(hash(t + str(i))) % (2 ** 63),
                vector=v,
                payload={**m, "text": t},
            ))
        self.client.upsert(self.collection, points)

    def search(self, query, top_k=5):
        q = embed_query(query)
        hits = self.client.search(self.collection, query_vector=q, limit=top_k)
        return [h.payload for h in hits]


class SharedVectorStore:
    """所有 agent 共享的单一向量知识库（单例）。"""

    def __init__(self):
        self.backend = "in-memory"
        self.store = None
        self.lock = threading.Lock()
        self._try_qdrant()

    def _try_qdrant(self):
        try:
            self.store = _QdrantStore()
            self.backend = "qdrant"
            print(f"[vector_store] 使用 Qdrant 后端: {settings.QDRANT_URL}")
        except Exception as e:  # noqa
            self.store = _InMemoryStore(dim())
            self.backend = "in-memory"
            print(f"[vector_store] Qdrant 不可用({e})，使用进程内内存向量库")

    def add(self, texts, metas=None):
        if isinstance(texts, str):
            texts = [texts]
        if metas is None:
            metas = [{} for _ in texts]
        self.store.add(texts, metas)

    def search(self, query, top_k=5):
        return self.store.search(query, top_k)

    def reset(self):
        """清空并重建（用于重启会话）。"""
        with self.lock:
            self._try_qdrant()


# 全局共享实例（所有 agent 引用同一个）
shared_vector_store = SharedVectorStore()
