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
    # ---- 费用估算（P1-6，人民币 ¥）----
    # 按「每百万 token 单价」估算整场讨论费用，输入/输出分开计价。
    # 默认参考 DeepSeek-chat 官方定价（输入 ¥2/百万、输出 ¥8/百万），
    # 若使用 MiMo / Qwen 等可按实际供应商价格在 .env 覆盖（COST_INPUT_PER_1M / COST_OUTPUT_PER_1M）。
    COST_INPUT_PER_1M = _float("COST_INPUT_PER_1M", 2.0)
    COST_OUTPUT_PER_1M = _float("COST_OUTPUT_PER_1M", 8.0)

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

    # ---- 收敛检测（架构文档 §4.3/4.4） ----
    CONVERGENCE_SIMILARITY = _float("CONVERGENCE_SIMILARITY", 0.85)
    CONVERGENCE_ROUNDS = _int("CONVERGENCE_ROUNDS", 2)        # 连续 N 轮高度相似即收敛
    # 降级阈值：嵌入模型不可用时（哈希向量无语义），改用 difflib 字符级文本相似度，
    # 其数值分布低于向量余弦相似度，故单独设置较低阈值（P0-3）。
    CONVERGENCE_TEXT_SIMILARITY = _float("CONVERGENCE_TEXT_SIMILARITY", 0.6)
    # 收敛检测是否按发言者逐人计算（§5.7）：True 时对连续两轮都发言的每个辩手
    # 单独算相似度再取平均，避免拼接全文时单一长文本主导而误判收敛。
    CONVERGENCE_PER_SPEAKER = _int("CONVERGENCE_PER_SPEAKER", 1) == 1

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
    # 摘要轮额外保留的对手关键论据句上限（字，§5.4）：LLM 摘要可能丢失具体论点，
    # 补充保留对手发言中带序号/论据标记的关键句子，避免辩手反驳"泛泛而谈"。
    OPPONENT_ARGUMENT_SNIPPET_MAX_CHARS = _int("OPPONENT_ARGUMENT_SNIPPET_MAX_CHARS", 160)

    # ---- 记忆压缩（短期 → 情境的 LLM 摘要上限） ----
    MEMORY_SUMMARY_MAX_CHARS = _int("MEMORY_SUMMARY_MAX_CHARS", 200)          # 短期记忆聚合摘要上限（字）

    # ---- 多层公平性治理（FairnessGovernor，替代旧的 token 预算总控） ----
    # 不同模型量化方式与 embedding 维度不同，token 数不是跨模型公平的度量单位，
    # 因此以「字符数 + 相对比例」为公平度量单位，不做全局 total_token 约束，
    # 也不把 max_tokens 下发给 API（改为事后字符截断兜底）。
    # L1 输出上限：
    FAIR_OUTPUT_RATIO = _float("FAIR_OUTPUT_RATIO", 1.5)            # 相对上限 = 对手近 N 轮有效输出均值 × 比例
    FAIR_HARD_CHAR_CEILING = _int("FAIR_HARD_CHAR_CEILING", 3000)   # 单次发言硬性字符安全上限
    FAIR_FLOOR_CHAR_LIMIT = _int("FAIR_FLOOR_CHAR_LIMIT", 400)      # 相对基准下限（对手极短时仍给最低输出空间）
    FAIR_OPENING_CHAR_LIMIT = _int("FAIR_OPENING_CHAR_LIMIT", 1200) # 立论 / 无对手参照时的上限
    FAIR_REFERENCE_ROUNDS = _int("FAIR_REFERENCE_ROUNDS", 2)        # 参照对手最近 N 轮有效输出
    # L2 有效回答比例（思考/推理占比）：
    FAIR_THINK_RATIO_MAX = _float("FAIR_THINK_RATIO_MAX", 0.5)      # 思考占比上限，超过记 warning
    # L3 输入对称性：
    FAIR_MAX_SEEN_DIFF = _int("FAIR_MAX_SEEN_DIFF", 1)              # PRO/CON 可见对手轮数允许的最大差值
    # L5 采样参数对齐：
    FAIR_TEMPERATURE = _float("FAIR_TEMPERATURE", 0.7)              # 辩论发言统一温度
    FAIR_TOP_P = _float("FAIR_TOP_P", 1.0)                          # 辩论发言统一 top_p
    FAIR_JUDGE_TEMPERATURE = _float("FAIR_JUDGE_TEMPERATURE", 0.3)  # 辅助 LLM（裁判/摘要）统一温度
    FAIR_JUDGE_TOP_P = _float("FAIR_JUDGE_TOP_P", 1.0)              # 辅助 LLM 统一 top_p

    # ---- 讨论模式（双模式） ----
    # 服务端兜底模式：debate（辩论）/ collaboration（合作）；
    # 前端 /api/configure 可显式覆盖。
    DISCUSSION_MODE = _get("DISCUSSION_MODE", "debate")


settings = Settings()


def resolve_provider(prefix: str, provider: str = "", base_url: str = "",
                     api_key: str = "", model_id: str = ""):
    """把显式字段与 .env 的 `{prefix}_*` 配置合并为一份 provider 规格（§1.4 统一入口）。

    例：prefix="JUDGE" 时，空字段依次回退 settings.JUDGE_PROVIDER /
    JUDGE_BASE_URL / JUDGE_API_KEY / JUDGE_MODEL_ID。返回过滤掉空值的规格 dict；
    全部字段为空时返回空 dict（调用方据此判断「未配置」）。
    本函数只做合并，不解析供应商预设——预设解析仍由 app.llm.resolve_model_config 负责。
    """
    def _env(name: str) -> str:
        if not prefix:
            return ""
        return getattr(settings, f"{prefix}_{name}", "") or ""

    spec = {
        "provider": provider or _env("PROVIDER"),
        "base_url": base_url or _env("BASE_URL"),
        "api_key": api_key or _env("API_KEY"),
        "model_id": model_id or _env("MODEL_ID"),
    }
    return {k: v.strip() for k, v in spec.items() if v and v.strip()}


# 立场对手映射（PRO↔CON 互为对手；NEUTRAL/HOST 无固定对手）。
# 收敛到 config 后由 orchestrator/fairness 共享，避免重复定义漂移（§3.4）。
OPPONENT_STANCE = {"PRO": "CON", "CON": "PRO"}
