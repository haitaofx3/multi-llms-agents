"""
config.py — 统一配置加载 (从项目根目录的 .env 读取)

针对本仓库 .env 文件存在个别行首多余空格、值带引号等不规范写法，
这里实现一个健壮的手写解析器，避免依赖 python-dotenv 的兼容性问题。
"""
from pathlib import Path

# 项目根目录（本文件位于 app/ 包内，其上一级为项目根目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def _parse_dotenv(path: Path) -> dict:
    data = {}
    if not path.exists():
        return data
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # 去掉可能包裹的引号
        if len(val) >= 2 and (
            (val[0] == '"' and val[-1] == '"')
            or (val[0] == "'" and val[-1] == "'")
        ):
            val = val[1:-1]
        if key:
            data[key] = val
    return data


_raw = _parse_dotenv(ENV_PATH)


def _get(name: str, default: str = "") -> str:
    return _raw.get(name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(_raw.get(name, str(default)))
    except Exception:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_raw.get(name, str(default)))
    except Exception:
        return default


class Settings:
    # ---- LLM 默认配置 (来自 .env，所有 agent 的兜底模型) ----
    LLM_MODEL_ID = _get("LLM_MODEL_ID", "qwen3.7-plus")
    LLM_API_KEY = _get("LLM_API_KEY", "")
    LLM_BASE_URL = _get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    LLM_TIMEOUT = _int("LLM_TIMEOUT", 60)

    # ---- DeepSeek (可选，用户可单独配置) ----
    DEEPSEEK_API_KEY = _get("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL_ID = _get("DEEPSEEK_MODEL_ID", "deepseek-chat")

    # ---- 搜索工具 ----
    TAVILY_API_KEY = _get("TAVILY_API_KEY", "")
    SERPAPI_API_KEY = _get("SERPAPI_API_KEY", "")

    # ---- Qdrant 向量数据库 ----
    QDRANT_URL = _get("QDRANT_URL", "http://localhost:6333")
    QDRANT_API_KEY = _get("QDRANT_API_KEY", "")
    QDRANT_COLLECTION = _get("QDRANT_COLLECTION", "hello_agents_vectors")
    QDRANT_VECTOR_SIZE = _int("QDRANT_VECTOR_SIZE", 384)
    QDRANT_DISTANCE = _get("QDRANT_DISTANCE", "cosine")
    QDRANT_TIMEOUT = _int("QDRANT_TIMEOUT", 3)  # 短超时保证启动快速失败降级

    # ---- Neo4j (保留，可选) ----
    NEO4J_URI = _get("NEO4J_URI", "bolt://localhost:7687")
    NEO4J_USERNAME = _get("NEO4J_USERNAME", "neo4j")
    NEO4J_PASSWORD = _get("NEO4J_PASSWORD", "")

    # ---- 嵌入模型 ----
    EMBED_MODEL_TYPE = _get("EMBED_MODEL_TYPE", "local")
    EMBED_MODEL_NAME = _get("EMBED_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2")
    EMBED_API_KEY = _get("EMBED_API_KEY", "")
    EMBED_BASE_URL = _get("EMBED_BASE_URL", "")

    # ---- Token 预算与熔断（架构文档 §6.3） ----
    TOKEN_BUDGET = _int("TOKEN_BUDGET", 100000)              # 单场辩论全局预算
    MIN_TOKEN_THRESHOLD = _int("MIN_TOKEN_THRESHOLD", 1000)   # 低于此阈值强制结束

    # ---- 收敛检测（架构文档 §4.3/4.4） ----
    CONVERGENCE_SIMILARITY = _float("CONVERGENCE_SIMILARITY", 0.85)
    CONVERGENCE_ROUNDS = _int("CONVERGENCE_ROUNDS", 2)        # 连续 N 轮高度相似即收敛

    # ---- LLM 重试（架构文档 §4.5：最多重试 2 次，间隔 1s、3s） ----
    LLM_RETRY_ATTEMPTS = _int("LLM_RETRY_ATTEMPTS", 3)
    LLM_RETRY_DELAYS = (1.0, 3.0)

    # ---- 单步流式超时（秒） ----
    STREAM_TIMEOUT = _int("STREAM_TIMEOUT", 90)

    # ---- 上下文压缩与长对话管理（优化 A9） ----
    CONTEXT_MAX_CHARS = _int("CONTEXT_MAX_CHARS", 8000)               # 单 Agent 上下文字符硬上限
    CONTEXT_COMPRESS_THRESHOLD = _int("CONTEXT_COMPRESS_THRESHOLD", 6000)  # 触发 LLM 摘要的字符阈值
    CONTEXT_COMPRESS_KEEP_RECENT = _int("CONTEXT_COMPRESS_KEEP_RECENT", 8)  # 摘要后保留的最近条数


settings = Settings()
