"""
embeddings.py — 文本向量化

优先使用本地 sentence-transformers 模型；若加载失败（无网络/无依赖），
自动降级为确定性的哈希向量，保证项目在任何环境下都能运行。

懒加载设计：
  - 模型加载不阻塞服务启动（由 start_background_warmup() 在后台线程预热）；
  - 首次真正调用 embed()/dim() 时才同步加载，保证向量维度一致；
  - 所有加载路径线程安全。
"""
import os
import threading

import numpy as np

from app.config import settings
from app.logger import log

_model = None
_dim = None
_load_lock = threading.Lock()
_ready = threading.Event()
_warmup_started = False


def _model_cached(name: str) -> bool:
    """快速检查模型是否已存在于本地 HF 缓存。

    仅查目录（毫秒级），避免模型未缓存时仍执行 `import sentence_transformers`
    —— 该导入会拉起 torch/transformers，可达数十秒，随后再做必然失败的加载，
    首次请求被 _load_lock 阻塞（历史问题：/api/ingest 客户端 30s 超时）。
    """
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        cache_root = HF_HUB_CACHE
    except Exception:
        cache_root = os.path.join(
            os.path.expanduser("~"), ".cache", "huggingface", "hub")
    repo = "models--" + name.replace("/", "--")
    return os.path.isdir(os.path.join(cache_root, repo))


def _load_model():
    """
    线程安全地懒加载嵌入模型；失败时降级为确定性哈希向量。

    关键：`_ready` 仅在加载完成后置位，因此任何并发调用者要么阻塞到加载
    完成，要么看到完全就绪的状态（_model/_dim 均已赋值），不会读到半初始化结果。
    """
    global _model, _dim
    if _ready.is_set():
        return _model
    with _load_lock:
        if _ready.is_set():
            return _model
        try:
            os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "10")
            name = settings.EMBED_MODEL_NAME
            if not _model_cached(name):
                raise RuntimeError(
                    f"本地无缓存模型 {name}，跳过导入直接降级为哈希向量")
            from sentence_transformers import SentenceTransformer
            log("info", "embeddings.load", "尝试加载本地嵌入模型", model=name)
            # 仅使用本地缓存：模型已下载则秒级就绪；
            # 未缓存时（_model_cached 已拦截）立即失败并降级为哈希向量，避免
            # 无网络时 HuggingFace 下载重试长时间阻塞首次调用。
            _model = SentenceTransformer(name, local_files_only=True)
            _dim = _model.get_sentence_embedding_dimension()
            log("info", "embeddings.load", "模型加载完成(本地缓存)", dim=_dim)
        except Exception as e:
            log("warning", "embeddings.fallback",
                "本地模型不可用，启用哈希降级向量",
                error=str(e), dim=settings.QDRANT_VECTOR_SIZE)
            _model = None
            _dim = settings.QDRANT_VECTOR_SIZE
        finally:
            _ready.set()
    return _model


def start_background_warmup():
    """后台线程预热嵌入模型，避免阻塞服务启动（可重复调用，仅首次生效）。"""
    global _warmup_started
    if _warmup_started:
        return
    _warmup_started = True
    t = threading.Thread(target=_load_model, daemon=True, name="emb-warmup")
    t.start()


def dim() -> int:
    _load_model()
    return _dim


def embed(texts):
    if isinstance(texts, str):
        texts = [texts]
    model = _load_model()
    if model is None:
        return _fallback_embed(texts)
    vecs = model.encode(texts, normalize_embeddings=True)
    return [v.tolist() for v in vecs]


def embed_query(text: str):
    return embed(text)[0]


def _fallback_embed(texts):
    size = settings.QDRANT_VECTOR_SIZE
    out = []
    for t in texts:
        # 确定性哈希种子，保证同一文本向量一致
        seed = abs(hash(t)) % (2 ** 32)
        rng = np.random.RandomState(seed)
        v = rng.rand(size).astype("float32")
        norm = np.linalg.norm(v) + 1e-9
        out.append((v / norm).tolist())
    return out
