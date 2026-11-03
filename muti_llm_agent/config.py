"""
config.py — 统一配置加载 (从项目根目录的 .env 读取)

针对本仓库 .env 文件存在个别行首多余空格、值带引号等不规范写法，
这里实现一个健壮的手写解析器，避免依赖 python-dotenv 的兼容性问题。
"""
import re
from pathlib import Path

# 项目根目录（本文件位于 muti_llm_agent/ 包内，其上一级为项目根目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def _parse_dotenv(path: Path) -> dict:
    data: dict = {}
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

    # ---- 搜索工具（联网搜索；未配置时 web_search 工具返回友好提示） ----
    SERPAPI_API_KEY = _get("SERPAPI_API_KEY", "")

    # ---- Qdrant 向量数据库 ----
    QDRANT_URL = _get("QDRANT_URL", "http://localhost:6333")
    QDRANT_API_KEY = _get("QDRANT_API_KEY", "")
    QDRANT_COLLECTION = _get("QDRANT_COLLECTION", "hello_agents_vectors")
    QDRANT_VECTOR_SIZE = _int("QDRANT_VECTOR_SIZE", 384)
    # 距离度量：cosine（默认）/ euclid / dot，映射到 qdrant-client 的 Distance 枚举
    QDRANT_DISTANCE = _get("QDRANT_DISTANCE", "cosine")
    QDRANT_TIMEOUT = _int("QDRANT_TIMEOUT", 3)  # 短超时保证启动快速失败降级

    # ---- 嵌入模型 ----
    # 仅支持本地 sentence-transformers；模型缺失时降级为确定性哈希向量（见 muti_llm_agent/embeddings.py）。
    EMBED_MODEL_NAME = _get("EMBED_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2")

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

    # ---- LLM 流式线程池容量（每路流式调用占一个 worker；超出则排队） ----
    # 早期实现每次调用新建裸线程，超时后线程无法回收会累积；改为有界线程池。
    LLM_STREAM_WORKERS = _int("LLM_STREAM_WORKERS", 8)

    # ---- 单 Agent 单轮工具调用循环上限（超出后按已有结果收尾） ----
    TOOL_LOOP_MAX = _int("TOOL_LOOP_MAX", 5)

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

    # ---- 多会话管理（DebateManager）----
    # 会话注册表上限：超过后按 LRU 淘汰「空闲」会话（running/paused 永不淘汰）。
    # 防止任意 session_id 反复请求把 Orchestrator（含 N 个 MemorySystem 与磁盘目录）
    # 无限灌进注册表造成内存/句柄/目录膨胀。
    MAX_SESSIONS = _int("MAX_SESSIONS", 8)

    # ---- 访问控制（可选）----
    # 非空时，所有 /api/* 与 /ws 请求必须携带该令牌（Authorization: Bearer xxx
    # 或 ?token=xxx）。默认空 = 不校验，配合 start.py 默认只绑 127.0.0.1。
    # 一旦用 HOST=0.0.0.0 对外提供服务，**务必**设置。
    AUTH_TOKEN = _get("AUTH_TOKEN", "")

    # ---- 出站地址安全（防 SSRF，见 muti_llm_agent/netguard.py）----
    # 默认（0）拒绝把自定义 base_url 指向回环 / 内网 / 保留地址；
    # 本地部署自建模型服务（Ollama / vLLM 等）时设为 1 放开。
    CUSTOM_BASE_URL_ALLOW_PRIVATE = _int("CUSTOM_BASE_URL_ALLOW_PRIVATE", 0) == 1

    # ---- 讨论模式（双模式） ----
    # 服务端兜底模式：debate（辩论）/ collaboration（合作）；
    # 前端 /api/configure 可显式覆盖。
    DISCUSSION_MODE = _get("DISCUSSION_MODE", "debate")

    # ---- 合作模式子 Agent 反思与自我修正（仅 collaboration/collab 生效） ----
    # 子 Agent 回报任务前做至多 N 轮反思 LLM 调用（每轮把上一轮修订版作新草稿继续自评，
    # 无修订/无变化即收敛提前终止），对照 expected_outcome 自我批判并修订后再落库。
    REFLECTION_ENABLED = _int("REFLECTION_ENABLED", 1) == 1    # 是否启用
    REFLECTION_MAX_PASSES = _int("REFLECTION_MAX_PASSES", 2)   # 单任务反思轮数上限（0=完全跳过）

    # ================= 以下为「多模型协同试点」新增配置 =================
    # ---- 部署形态：单用户个人级（PERSONAL_MODE） ----
    # 试点定位是「一个人的多模型协同工作台」：不做多租户隔离，只保留一个稳定工作区，
    # 会话（session）退化为「本次运行」的容器，注册表上限默认收敛到 8。
    # 仍然保留 session_id 接口约定（对外集成 / DSH 调用需要稳定标识）。
    PERSONAL_MODE = _int("PERSONAL_MODE", 1) == 1
    OWNER_NAME = _get("OWNER_NAME", "本机用户")
    WORKSPACE_NAME = _get("WORKSPACE_NAME", "多模型协同工作台")
    # 个人级默认协同配方（见 muti_llm_agent.models_registry.RECIPES）
    DEFAULT_RECIPE = _get("DEFAULT_RECIPE", "collab-4")
    # 单场运行允许的最大迭代次数（第 1 轮讨论 + N 次「补充材料续轮」）
    MAX_ITERATIONS = _int("MAX_ITERATIONS", 5)

    # ---- 结构化日志落盘（logger.py） ----
    # 日志同时写 stdout（JSON 行）、data/logs/app-YYYYMMDD.jsonl（按天滚动）
    # 与进程内环形缓冲（供 /api/logs 查询最近日志）。
    LOG_TO_FILE = _int("LOG_TO_FILE", 1) == 1
    LOG_RING_SIZE = _int("LOG_RING_SIZE", 2000)
    LOG_LEVEL = _get("LOG_LEVEL", "info")

    # ---- 运行事件日志（runlog.py：复盘事件时间线） ----
    RUNLOG_ENABLED = _int("RUNLOG_ENABLED", 1) == 1
    RUNLOG_MAX_EVENTS = _int("RUNLOG_MAX_EVENTS", 20000)

    # ---- 中途注入纠偏（steering.py） ----
    # 讨论进行中由用户 / 外部系统下发「纠偏 / 约束 / 补充事实 / 转向」指令，
    # 在下一个 Agent 回合注入 prompt，防止整体方向跑偏。
    STEERING_ENABLED = _int("STEERING_ENABLED", 1) == 1
    STEERING_MAX_PENDING = _int("STEERING_MAX_PENDING", 20)   # 待生效指令队列上限
    STEERING_TEXT_MAX_CHARS = _int("STEERING_TEXT_MAX_CHARS", 1000)  # 单条指令长度上限

    # ---- 会话结束后的「补充材料续轮」（followup.py） ----
    # 会话结束后可补充新材料再跑一轮迭代，带着上一轮结论做增量优化。
    FOLLOWUP_ENABLED = _int("FOLLOWUP_ENABLED", 1) == 1
    FOLLOWUP_MAX_MATERIAL_CHARS = _int("FOLLOWUP_MAX_MATERIAL_CHARS", 12000)
    # 续轮时注入的上一轮结论 / 分歧摘要截断长度
    FOLLOWUP_CARRYOVER_CHARS = _int("FOLLOWUP_CARRYOVER_CHARS", 4000)

    # ---- 流式推送（SSE） ----
    # /api/stream 的心跳间隔（秒）：长连接无事件时定期发注释行，防中间层超时断流。
    SSE_KEEPALIVE_SEC = _float("SSE_KEEPALIVE_SEC", 15.0)
    # SSE 断线重连时回放的历史事件条数上限（?replay=1）
    SSE_REPLAY_MAX = _int("SSE_REPLAY_MAX", 200)

    # ---- 对外集成：飞书 ----
    # 出站：群机器人自定义 webhook 地址；入站：事件订阅回调。
    FEISHU_WEBHOOK_URL = _get("FEISHU_WEBHOOK_URL", "")
    FEISHU_VERIFICATION_TOKEN = _get("FEISHU_VERIFICATION_TOKEN", "")
    FEISHU_PUSH_ON_DONE = _int("FEISHU_PUSH_ON_DONE", 0) == 1
    # 入站消息是否自动触发一场协同讨论（个人级试点默认关闭，避免误触发消耗额度）
    FEISHU_AUTO_RUN = _int("FEISHU_AUTO_RUN", 0) == 1
    FEISHU_AUTO_RUN_MAX_ROUNDS = _int("FEISHU_AUTO_RUN_MAX_ROUNDS", 3)

    # ---- 对外集成：通用出站 webhook ----
    # 逗号分隔的多个地址；运行完成 / 失败时推送结构化摘要（见 integrations/dispatch.py）
    WEBHOOK_URLS = _get("WEBHOOK_URLS", "")
    WEBHOOK_ON_DONE = _int("WEBHOOK_ON_DONE", 0) == 1
    WEBHOOK_TIMEOUT = _float("WEBHOOK_TIMEOUT", 8.0)

    # ---- 对外集成：DSH（DeepSeek Harness） ----
    DSH_ENABLED = _int("DSH_ENABLED", 1) == 1
    # 非空时 /api/integrations/dsh/* 必须带 Authorization: Bearer <secret>
    DSH_SHARED_SECRET = _get("DSH_SHARED_SECRET", "")
    DSH_BASE_URL = _get("DSH_BASE_URL", "")

    # ---- 开发/调试接口（/api/dev/*，接口级测试控制台） ----
    # 个人级试点默认开启；若要对外暴露服务，建议设 DEV_API_ENABLED=0。
    DEV_API_ENABLED = _int("DEV_API_ENABLED", 1) == 1
    DEV_API_TIMEOUT_MS = _int("DEV_API_TIMEOUT_MS", 60000)


settings = Settings()

# ---- 数据目录（运行期产物） ----
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = DATA_DIR / "logs"        # 诊断日志（logger.py）
RUNS_DIR = DATA_DIR / "runs"        # 运行事件日志与摘要（runlog.py / summary.py）
REVIEWS_DIR = DATA_DIR / "reviews"  # 复盘导出（review.py）
for _d in (DATA_DIR, LOGS_DIR, RUNS_DIR, REVIEWS_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:  # pragma: no cover - 目录不可写时由使用方逐次降级
        pass


def webhook_urls() -> list[str]:
    """解析 WEBHOOK_URLS（逗号 / 分号 / 换行分隔），过滤空串与非法协议。"""
    raw = settings.WEBHOOK_URLS or ""
    out: list[str] = []
    for part in raw.replace("\n", ",").replace(";", ",").split(","):
        url = part.strip()
        if url and url.lower().startswith(("http://", "https://")) and url not in out:
            out.append(url)
    return out


def resolve_provider(prefix: str, provider: str = "", base_url: str = "",
                     api_key: str = "", model_id: str = ""):
    """把显式字段与 .env 的 `{prefix}_*` 配置合并为一份 provider 规格（§1.4 统一入口）。

    例：prefix="JUDGE" 时，空字段依次回退 settings.JUDGE_PROVIDER /
    JUDGE_BASE_URL / JUDGE_API_KEY / JUDGE_MODEL_ID。返回过滤掉空值的规格 dict；
    全部字段为空时返回空 dict（调用方据此判断「未配置」）。
    本函数只做合并，不解析供应商预设——预设解析仍由 muti_llm_agent.llm.resolve_model_config 负责。
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


# 允许作为单级路径片段的字符（防目录穿越 / 非法文件名）。
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_path_component(name: str, default: str = "default") -> str:
    """把外部可控标识（session_id / agent_id）清洗为安全的单级路径片段。

    只允许字母、数字与 `.` `_` `-`；`.` / `..` 与含分隔符的取值一律回退 default，
    保证 `data/sessions/{sid}.json`、`data/memories/{sid}/...` 这类路径拼接
    无法越出预期目录（会话标识来自客户端，属不可信输入）。
    """
    name = (name or "").strip()
    if not name or name in (".", "..") or not _SAFE_COMPONENT.match(name):
        return default
    return name
