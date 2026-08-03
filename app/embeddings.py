"""
embeddings.py — 文本向量化

优先使用本地 sentence-transformers 模型；若加载失败（无网络/无依赖），
自动降级为确定性的哈希向量，保证项目在任何环境下都能运行。
"""
import numpy as np
from app.config import settings

_model = None
_dim = None
_loaded = False


def _load_model():
    global _model, _dim, _loaded
    if _loaded:
        return _model
    _loaded = True
    try:
        from sentence_transformers import SentenceTransformer
        name = settings.EMBED_MODEL_NAME
        print(f"[embeddings] 正在加载嵌入模型: {name}")
        _model = SentenceTransformer(name)
        _dim = _model.get_sentence_embedding_dimension()
        print(f"[embeddings] 模型加载完成, 维度={_dim}")
    except Exception as e:  # noqa
        print(f"[embeddings] 本地模型加载失败({e})，启用哈希降级向量, 维度={settings.QDRANT_VECTOR_SIZE}")
        _model = None
        _dim = settings.QDRANT_VECTOR_SIZE
    return _model


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
