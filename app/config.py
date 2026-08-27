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
    # ---- LLM 默认配置 (来自 .env，所有 agent 的兜底模型；当前默认 MiMo) ----
    LLM_MODEL_ID = _get("LLM_MODEL_ID", "mimo-v2.5")
    LLM_API_KEY = _get("LLM_API_KEY", "")
    LLM_BASE_URL = _get("LLM_BASE_URL", "https://api.xiaomimimo.com/v1")
    LLM_TIMEOUT = _int("LLM_TIMEOUT", 300)  # 默认模型 MiMo 生成较慢，放宽到 300s

    # ---- Qwen / DashScope (可选，用户可单独配置) ----
    QWEN_API_KEY = _get("QWEN_API_KEY", "")
    QWEN_BASE_URL = _get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    QWEN_MODEL_ID = _get("QWEN_MODEL_ID", "qwen3.7-plus")

    # ---- DeepSeek (可选，用户可单独配置) ----
    DEEPSEEK_API_KEY = _get("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL_ID = _get("DEEPSEEK_MODEL_ID", "deepseek-chat")

    # ---- MiMo (可选，用户可单独配置) ----
    # 注意：BASE_URL 只写到 /v1，OpenAI SDK 会自动拼接 /chat/completions
    MIMO_API_KEY = _get("MIMO_API_KEY", "")
    MIMO_BASE_URL = _get("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
    MIMO_MODEL_ID = _get("MIMO_MODEL_ID", "mimo-v2.5")

    # ---- 独立裁判（Judge Agent，可选） ----
    # 裁判只对各位辩手的辩论表现评分，不参与辩论；未配置时回退使用主 Agent 模型
    JUDGE_NAME = _get("JUDGE_NAME", "独立裁判")
    JUDGE_PROVIDER = _get("JUDGE_PROVIDER", "")   # 空则取 default 预设
    JUDGE_BASE_URL = _get("JUDGE_BASE_URL", "")
    JUDGE_API_KEY = _get("JUDGE_API_KEY", "")
    JUDGE_MODEL_ID = _get("JUDGE_MODEL_ID", "")
    # 综合评分权重：最终综合分 = 内容分(agent_scores 均值) × 内容权重
    #              + 模型质量分(TTFT/生成速率) × 性能权重
    JUDGE_CONTENT_WEIGHT = _float("JUDGE_CONTENT_WEIGHT", 0.8)
    JUDGE_PERFORMANCE_WEIGHT = _float("JUDGE_PERFORMANCE_WEIGHT", 0.2)

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
    MIN_TOKEN_THRESHOLD = _int("MIN_TOKEN_THRESHOLD", 1000)   # 低于此阈值强制结束（预算规划器最后防线）
    MAX_TOKENS = _int("MAX_TOKENS", 4096)                     # 单次模型输出上限（全局）

    # ---- 预算规划与降级（budget planner） ----
    BUDGET_OUTPUT_FLOOR = _int("BUDGET_OUTPUT_FLOOR", 256)        # 单次输出下限；低于此触发压缩/超支
    BUDGET_TAIL_RESERVE = _int("BUDGET_TAIL_RESERVE", 8000)       # 裁判+汇总 尾段保留下限（动态估算的下限）
    BUDGET_TOOL_OVERHEAD = _float("BUDGET_TOOL_OVERHEAD", 1.5)    # 工具调用迭代对 LLM 调用次数的乘数

    # ---- 收敛检测（架构文档 §4.3/4.4） ----
    CONVERGENCE_SIMILARITY = _float("CONVERGENCE_SIMILARITY", 0.85)
    CONVERGENCE_ROUNDS = _int("CONVERGENCE_ROUNDS", 2)        # 连续 N 轮高度相似即收敛

    # ---- LLM 重试（架构文档 §4.5：最多重试 2 次，间隔 1s、3s） ----
    LLM_RETRY_ATTEMPTS = _int("LLM_RETRY_ATTEMPTS", 3)
    LLM_RETRY_DELAYS = (1.0, 3.0)

    # ---- 单步流式超时（秒；默认模型 MiMo 单次完整流式发言可能达 2-5 分钟） ----
    STREAM_TIMEOUT = _int("STREAM_TIMEOUT", 300)

    # ---- 上下文压缩与长对话管理（优化 A9） ----
    CONTEXT_MAX_CHARS = _int("CONTEXT_MAX_CHARS", 8000)               # 单 Agent 上下文字符硬上限
    CONTEXT_COMPRESS_THRESHOLD = _int("CONTEXT_COMPRESS_THRESHOLD", 6000)  # 触发 LLM 摘要的字符阈值
    CONTEXT_COMPRESS_KEEP_RECENT = _int("CONTEXT_COMPRESS_KEEP_RECENT", 8)  # 摘要后保留的最近条数


settings = Settings()
