"""
vector_store.py — 多 Agent 共享的向量知识库

所有 agent 访问同一个向量数据库进行知识检索：
- 若本地/远程 Qdrant 可达则使用 Qdrant（短超时快速失败，不拖慢启动）；
- 否则自动降级为进程内内存向量库（余弦相似度检索），保证功能可用。

懒加载设计：
  - 服务启动时不阻塞：后台线程做 Qdrant 连通性探测与嵌入模型预热；
  - 首次 add()/search() 时才真正初始化后端（_ensure_store）；
  - 并发安全：初始化与读写均有锁保护。

提供：add(texts, metas) 与 search(query, top_k)。
"""
import threading
import numpy as np
from app.config import settings
from app.embeddings import embed, embed_query, dim, start_background_warmup
from app.logger import log


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
    """所有 agent 共享的单一向量知识库（单例），后端懒初始化。"""

    def __init__(self):
        self.backend = "in-memory"
        self.store = None
        self.lock = threading.Lock()
        # 后台预热：嵌入模型加载 + Qdrant 连通性探测，均不阻塞服务启动
        start_background_warmup()
        threading.Thread(target=self._try_qdrant, daemon=True, name="vs-warmup").start()

    @staticmethod
    def _probe_qdrant(timeout: float = 1.0) -> bool:
        """探测 Qdrant 端口是否可达（纯 TCP 连接，秒级快速失败）。

        目的：QdrantClient 构造会做版本协商，QDRANT_TIMEOUT=3 并不能真正
        限制其总耗时（实测不可达时约 11s）；先做 TCP 探测可让后台线程
        在 Qdrant 未运行时 <1s 内放弃，不拖慢任何路径。
        """
        import socket
        from urllib.parse import urlparse
        u = urlparse(settings.QDRANT_URL)
        host = u.hostname or "localhost"
        port = u.port or 6333
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _try_qdrant(self):
        """后台探测 Qdrant：全部网络 I/O 均在锁外完成，避免阻塞请求路径。

        请求路径（_ensure_store）已即时创建内存库；这里探测到 Qdrant 可用且
        内存库尚无数据时，才在锁内快速替换为 Qdrant 后端。
        """
        if self.store is not None:
            return
        if not self._probe_qdrant():
            log("warning", "vector_store.fallback",
                "Qdrant 不可达，使用进程内内存向量库",
                backend="in-memory", error="TCP connection refused")
            return
        try:
            store = _QdrantStore()
        except Exception as e:  # noqa
            log("warning", "vector_store.fallback",
                "Qdrant 初始化失败，使用进程内内存向量库",
                backend="in-memory", error=str(e))
            return
        with self.lock:
            if self.store is None or (
                isinstance(self.store, _InMemoryStore) and not self.store.items
            ):
                self.store = store
                self.backend = "qdrant"
                log("info", "vector_store.init", "使用 Qdrant 后端",
                    backend="qdrant", url=settings.QDRANT_URL)

    def _ensure_store(self):
        """确保后端已初始化（懒加载），返回当前存储后端。

        绝不在此路径上阻塞于 Qdrant 网络 I/O：后端未就绪时立即创建内存库，
        后续由后台 _try_qdrant 线程在锁外完成探测后替换。
        """
        with self.lock:
            if self.store is None:
                self.store = _InMemoryStore(dim())
                self.backend = "in-memory"
        return self.store

    @property
    def ready(self) -> bool:
        return self.store is not None

    def add(self, texts, metas=None):
        if isinstance(texts, str):
            texts = [texts]
        if metas is None:
            metas = [{} for _ in texts]
        self._ensure_store().add(texts, metas)

    def search(self, query, top_k=5):
        return self._ensure_store().search(query, top_k)

    def reset(self):
        """清空并重建后端（用于重启会话）。"""
        with self.lock:
            self.store = None
        threading.Thread(target=self._try_qdrant, daemon=True,
                         name="vs-reset").start()


# 全局共享实例（所有 agent 引用同一个）
shared_vector_store = SharedVectorStore()
