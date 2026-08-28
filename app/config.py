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
    # 历史增长模型：每轮提示词成本随历史累积按算术级数递增（P_r = P·(1 + (r-1)·g)）
    BUDGET_GROWTH_RATIO_INIT = _float("BUDGET_GROWTH_RATIO_INIT", 0.5)  # 每轮历史增长比初始值（EMA 自适应）
    BUDGET_GROWTH_RATIO_MIN = _float("BUDGET_GROWTH_RATIO_MIN", 0.05)   # 增长比钳制下限
    BUDGET_GROWTH_RATIO_MAX = _float("BUDGET_GROWTH_RATIO_MAX", 2.0)    # 增长比钳制上限
    BUDGET_SAFETY_MARGIN = _float("BUDGET_SAFETY_MARGIN", 0.08)         # 规划预留的安全余量比例（只压低 E，不计入超支判定）
    # 校准先验与持久化：首场无档案时用保守先验，解决首轮规划无法校准的问题
    EST_FACTOR_INIT = _float("EST_FACTOR_INIT", 2.0)                    # 中文 len//2 启发式偏乐观（实测 3~5×），先验取 2.0
    BUDGET_PROFILE_PATH = _get("BUDGET_PROFILE_PATH", "data/budget_profile.json")  # 跨场次校准档案
    # 尾段输出保底：裁判/汇总即使超支也保留最小输出，避免裁判 JSON 截断 → 空评语确定性回退
    JUDGE_MIN_OUTPUT = _int("JUDGE_MIN_OUTPUT", 512)
    SUMMARY_MIN_OUTPUT = _int("SUMMARY_MIN_OUTPUT", 512)
    # 自动削减轮次：预算不足（即便下限输出也无法跑满）时，把 max_rounds 削减到可行轮次
    BUDGET_AUTO_TRIM_ROUNDS = _int("BUDGET_AUTO_TRIM_ROUNDS", 1)   # 1=开（推荐）；0=保持现状跑满

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

    # ---- 辩论对手上下文（需求 2/3：对手发言的引用与压缩） ----
    OPPONENT_SNIPPET_MAX_CHARS = _int("OPPONENT_SNIPPET_MAX_CHARS", 500)      # 第 2 轮对手上轮发言原文截断上限（字）
    OPPONENT_SUMMARY_MAX_CHARS = _int("OPPONENT_SUMMARY_MAX_CHARS", 200)      # 第 3 轮起对手发言 LLM 摘要上限（字）

    # ---- 记忆压缩（短期 → 情境的 LLM 摘要上限） ----
    MEMORY_SUMMARY_MAX_CHARS = _int("MEMORY_SUMMARY_MAX_CHARS", 200)          # 短期记忆聚合摘要上限（字）

    # ---- 输出 token 约束（需求 5：按字符数控制发言长度） ----
    OUTPUT_CHAR_PER_TOKEN = _int("OUTPUT_CHAR_PER_TOKEN", 2)                  # 中文约 2 字符/token，用于换算字数上限

    # ---- 预算吃紧时的保留轮数（需求 6：只保留最近 N 轮输出） ----
    BUDGET_KEEP_LAST_ROUNDS = _int("BUDGET_KEEP_LAST_ROUNDS", 2)


settings = Settings()
