"""
models_registry.py — 多模型花名册与协同配方

个人级试点的核心不是「一个更强的模型」，而是「几个不同取向的模型各干擅长的活」：
长文写作、严谨推理、资料整合、挑错的能力分布在不同供应商的模型上，让它们在同一场
运行里分工，比让单一模型自我扮演多个角色更接近真实协作。

本模块提供两件事：

1. **花名册（roster）**：从 `.env` 读出各供应商的配置状态，回答「我现在到底能用
   哪几个模型」。未配置 Key 的供应商标记为 unavailable，而不是等到发起请求才 401。
2. **协同配方（recipe）**：预置的席位组合（角色 / 立场 / 供应商 / 轮次 / 裁判），
   前端一键套用即可生成 `/api/configure` 需要的 agent 列表。
   配方里写的是「供应商意图」，`build_agent_configs` 会在目标供应商不可用时自动
   改派给当前可用的模型 —— 保证换一台机器（只配了一个 Key）也能直接跑起来。

对外只暴露脱敏信息（绝不返回 api_key）。
"""
from __future__ import annotations

import threading
import time

from muti_llm_agent.config import settings

# 供应商 → 展示名（与 llm.PROVIDER_PRESETS 的键保持一致）
PROVIDER_LABELS: dict[str, str] = {
    "default": "默认模型（.env LLM_*）",
    "qwen": "通义千问 Qwen",
    "deepseek": "DeepSeek",
    "mimo": "MiMo",
    "custom": "自定义（OpenAI 兼容）",
}

# 供应商 → 一句话擅长（配方说明与 UI 提示复用）
PROVIDER_STRENGTH: dict[str, str] = {
    "default": "通用兜底",
    "qwen": "长文写作与中文表达",
    "deepseek": "严谨推理与结构化拆解",
    "mimo": "综合生成与细节打磨",
    "custom": "自建 / 私有模型",
}


def _providers() -> dict:
    """惰性导入 llm 的预设，避免 config → llm → config 的导入环。"""
    from muti_llm_agent.llm import PROVIDER_PRESETS
    return PROVIDER_PRESETS


# ---------------- 运行期失败登记（模型级健康度） ----------------
# 「Key 配了」不等于「模型能用」：模型 ID 与供应商服务端不匹配（常见于改名 / 下线）、
# 额度耗尽、被限流，都只会在真正调用时暴露。把这类失败按 model_id 记账，
# 花名册就能告诉用户「这个席位上次调用挂了、为什么」，而不是让他去翻日志。
#
# 记录会落盘到 data/model_health.json：重启后仍然有效 —— 否则「上次调用失败」
# 这条信息每次重启就丢一次，用户永远不会在花名册上看到它。
_FAILURES: dict[str, dict] = {}
_FAILURES_LOCK = threading.Lock()
_FAILURES_LOADED = False


def _health_path():
    from muti_llm_agent.config import DATA_DIR
    return DATA_DIR / "model_health.json"


def _ensure_loaded() -> None:
    """首次访问时从磁盘载入历史失败记录（容错：坏文件视为空）。"""
    global _FAILURES_LOADED
    if _FAILURES_LOADED:
        return
    _FAILURES_LOADED = True
    try:
        import json
        path = _health_path()
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                with _FAILURES_LOCK:
                    for k, v in data.items():
                        if isinstance(v, dict):
                            _FAILURES[str(k)] = v
    except Exception:
        pass


def _persist_failures() -> None:
    """把失败记录写盘（尽力而为：写不进去只影响跨重启可见性）。"""
    try:
        import json
        with _FAILURES_LOCK:
            payload = dict(_FAILURES)
        path = _health_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception:
        pass


def record_failure(model_id: str, provider: str = "", error: str = "") -> None:
    """登记一次模型调用失败（供 Orchestrator 在 LLM 异常时调用）。"""
    _ensure_loaded()
    key = (model_id or provider or "unknown").strip() or "unknown"
    with _FAILURES_LOCK:
        rec = _FAILURES.setdefault(key, {
            "model_id": model_id or "", "provider": provider or "", "count": 0,
        })
        rec["count"] += 1
        rec["error"] = str(error)[:300]
        rec["last_at"] = int(time.time() * 1000)
    _persist_failures()


def clear_failures() -> None:
    """清空失败记录（修好配置后由 /api/models?reset=1 触发）。"""
    global _FAILURES_LOADED
    _ensure_loaded()
    with _FAILURES_LOCK:
        _FAILURES.clear()
    _FAILURES_LOADED = True
    _persist_failures()


def runtime_failures() -> list[dict]:
    """当前已知的模型失败记录（新→旧，含跨进程持久化的历史）。"""
    _ensure_loaded()
    with _FAILURES_LOCK:
        items = [dict(v) for v in _FAILURES.values()]
    items.sort(key=lambda r: r.get("last_at", 0), reverse=True)
    return items



def provider_status() -> list[dict]:
    """各供应商的配置状态（脱敏：只返回 model_id / base_url / 是否配置 Key）。"""
    presets = _providers()
    out = []
    for key, label in PROVIDER_LABELS.items():
        preset = presets.get(key) or {}
        api_key = str(preset.get("api_key", "") or "")
        model_id = preset.get("model_id", "") if key != "custom" else ""
        failures = [f for f in runtime_failures()
                    if f.get("model_id") == model_id or f.get("provider") == key]
        fail_count = sum(int(f.get("count", 0) or 0) for f in failures)
        out.append({
            "provider": key,
            "label": label,
            "strength": PROVIDER_STRENGTH.get(key, ""),
            "model_id": model_id,
            "base_url": preset.get("base_url", "") if key != "custom" else "",
            "configured": bool(api_key.strip()) and key != "custom",
            "is_default": key == "default",
            # 运行期健康度：Key 配了不代表模型能用，这里反映「上次真调用有没有挂」
            "healthy": fail_count == 0,
            "failure_count": fail_count,
            "last_error": (failures[0].get("error", "") if failures else ""),
        })
    return out


def available_providers(only_configured: bool = True) -> list[str]:
    """当前可用供应商列表。

    优先级：已配置且最近调用正常 → 已配置（可能报错）→ ["default"] 兜底。
    「报错的也算已配置」是刻意的：用户可能刚修好 .env 还没验证，此时完全不给他
    可选模型反而更糟；但只要能排出健康模型，就先避开坏的。
    """
    items = provider_status()
    if not only_configured:
        names = [p["provider"] for p in items]
        return names or ["default"]
    healthy = [p["provider"] for p in items if p["configured"] and p.get("healthy", True)]
    configured = [p["provider"] for p in items if p["configured"]]
    names = healthy or configured
    if not names:
        names = ["default"]
    return names


def roster() -> dict:
    """花名册快照（/api/models 直接返回）。"""
    providers = provider_status()
    configured = [p for p in providers if p["configured"]]
    healthy = [p for p in configured if p.get("healthy", True)]
    return {
        "workspace": settings.WORKSPACE_NAME,
        "owner": settings.OWNER_NAME,
        "personal_mode": settings.PERSONAL_MODE,
        "providers": providers,
        "configured_count": len(configured),
        "healthy_count": len(healthy),
        # 可用 = 配了 Key 且最近没有调用失败；若全部都不健康仍回退到 default，
        # 让调用方拿到一个可尝试的目标而不是空列表。
        "available": [p["provider"] for p in healthy]
        or [p["provider"] for p in configured]
        or ["default"],
        "runtime_failures": runtime_failures(),
        "default_recipe": settings.DEFAULT_RECIPE,
        "recipes": list_recipes(),
    }


# ---------------- 协同配方 ----------------
# 说明：配方是「意图」而非硬绑定 —— provider 会被 build_agent_configs 按可用性改派。
RECIPES: dict[str, dict] = {
    "collab-4": {
        "title": "四模型协同（规划 · 研究 · 撰写 · 审核）",
        "mode": "collaboration",
        "max_rounds": 4,
        "description": "一个人带三个模型干活：规划者拆解任务并验收，研究员整合资料，"
                       "撰写者出稿，审核者挑错并给修订建议。适合方案、文档、调研报告。",
        "judge": None,
        "agents": [
            {"name": "规划者", "role": "主控规划", "provider": "deepseek",
             "personality": "目标导向、擅长把模糊需求拆成可交付的任务，并严格验收结果",
             "stance": "HOST"},
            {"name": "研究员", "role": "资料研究", "provider": "qwen",
             "personality": "重视事实与出处，善于归纳分散信息，不做无依据的断言",
             "stance": "NEUTRAL"},
            {"name": "撰写者", "role": "方案撰写", "provider": "mimo",
             "personality": "结构清晰、语言凝练，擅长把要点写成可直接使用的文档",
             "stance": "NEUTRAL"},
            {"name": "审核者", "role": "质量审核", "provider": "deepseek",
             "personality": "怀疑一切未经验证的结论，逐条指出漏洞并给出修订建议",
             "stance": "NEUTRAL"},
        ],
    },
    "collab-3": {
        "title": "三模型极速协同（规划 · 执行 · 审核）",
        "mode": "collaboration",
        "max_rounds": 3,
        "description": "最省额度的组合：一个模型拆任务，一个模型干活，一个模型验收。",
        "judge": None,
        "agents": [
            {"name": "规划者", "role": "主控规划", "provider": "deepseek",
             "personality": "简洁果断，只产出可执行的任务清单",
             "stance": "HOST"},
            {"name": "执行者", "role": "任务执行", "provider": "qwen",
             "personality": "动手能力强，按任务要求直接给成品而不是讨论方法",
             "stance": "NEUTRAL"},
            {"name": "审核者", "role": "质量审核", "provider": "mimo",
             "personality": "逐项对照验收标准打分，给出可落地的修改清单",
             "stance": "NEUTRAL"},
        ],
    },
    "panel-5": {
        "title": "五模型专家组（多视角评审）",
        "mode": "collaboration",
        "max_rounds": 5,
        "description": "把一个议题拆给五个不同取向的模型（规划/研究/撰写/审核/反方），"
                       "用多视角降低单一模型的盲区。适合重要方案定稿前的评审。",
        "judge": None,
        "agents": [
            {"name": "主持人", "role": "主控仲裁", "provider": "deepseek",
             "personality": "中立、善于收敛分歧，只在证据不足时才追加任务",
             "stance": "HOST"},
            {"name": "研究员", "role": "资料研究", "provider": "qwen",
             "personality": "重事实、重出处，输出必带依据",
             "stance": "NEUTRAL"},
            {"name": "方案作者", "role": "方案撰写", "provider": "mimo",
             "personality": "结构优先，先给骨架再填内容",
             "stance": "NEUTRAL"},
            {"name": "审核者", "role": "质量审核", "provider": "deepseek",
             "personality": "严苛、只讲问题不讲情面",
             "stance": "NEUTRAL"},
            {"name": "反方", "role": "风险挑战", "provider": "qwen",
             "personality": "专门寻找方案失败的可能路径，提出备选方案",
             "stance": "NEUTRAL"},
        ],
    },
    "debate-3": {
        "title": "三方辩论（正 · 反 · 独立裁判）",
        "mode": "debate",
        "max_rounds": 4,
        "description": "保留原项目的辩论证伪能力：正反方对抗，独立裁判按维度评分并裁决。",
        "judge": {"name": "独立裁判", "provider": "mimo"},
        "agents": [
            {"name": "主持人", "role": "主持与追问", "provider": "mimo",
             "personality": "客观中立，负责推进议程与追问薄弱环节",
             "stance": "HOST"},
            {"name": "正方", "role": "立论辩护", "provider": "qwen",
             "personality": "论证严密、善于用类比与数据支撑观点",
             "stance": "PRO"},
            {"name": "反方", "role": "质疑反驳", "provider": "deepseek",
             "personality": "擅长找出隐含前提与逻辑漏洞",
             "stance": "CON"},
        ],
    },
    "dual-2": {
        "title": "双模型对练（主控 · 执行）",
        "mode": "collaboration",
        "max_rounds": 2,
        "description": "最小可用协同：一个模型定方向，一个模型出成品。用于快速试跑与接口联调。",
        "judge": None,
        "agents": [
            {"name": "主控", "role": "主控规划", "provider": "deepseek",
             "personality": "简短直接，只给任务与验收标准",
             "stance": "HOST"},
            {"name": "执行", "role": "任务执行", "provider": "qwen",
             "personality": "按指令直接产出成品",
             "stance": "NEUTRAL"},
        ],
    },
}


def list_recipes() -> list[dict]:
    """配方列表（脱敏，含席位预览）。"""
    out = []
    for name, r in RECIPES.items():
        out.append({
            "name": name,
            "title": r["title"],
            "mode": r["mode"],
            "max_rounds": r["max_rounds"],
            "description": r["description"],
            "agents": [
                {"name": a["name"], "role": a["role"], "provider": a["provider"],
                 "stance": a["stance"]}
                for a in r["agents"]
            ],
            "judge": ({"name": r["judge"].get("name", ""),
                       "provider": r["judge"].get("provider", "")}
                      if r.get("judge") else None),
            "is_default": name == settings.DEFAULT_RECIPE,
        })
    return out


def get_recipe(name: str) -> dict | None:
    return RECIPES.get(name or "")


def _assign_provider(preferred: str, pool: list[str], used: list[str]) -> str:
    """把「配方里的供应商意图」落到一个当前可用的供应商上。

    分配优先级：偏好供应商可用 → 未被占用的供应商（让多模型真正异构）
    → 循环使用（模型数少于席位数时允许重复，但尽量错开）。
    """
    if preferred in pool:
        return preferred
    for p in pool:
        if p not in used:
            return p
    return pool[len(used) % len(pool)] if pool else "default"


def build_agent_configs(recipe_name: str, provider_pool: list[str] | None = None) -> dict:
    """按配方生成 `/api/configure` 可直接使用的配置。

    返回：{"recipe","mode","max_rounds","agents":[...],"judge":{...}|None,"notes":[...]}
    `notes` 记录因供应商不可用而做的改派，方便用户知道实际用的是哪几个模型。
    """
    recipe = get_recipe(recipe_name)
    if recipe is None:
        raise ValueError(f"未知配方：{recipe_name}")
    pool = provider_pool or available_providers()
    if not pool:
        pool = ["default"]
    notes: list[str] = []
    used: list[str] = []
    agents = []
    for i, a in enumerate(recipe["agents"]):
        provider = _assign_provider(a["provider"], pool, used)
        if provider != a["provider"]:
            notes.append(
                f"席位「{a['name']}」原定 {a['provider']} 不可用，已改派 {provider}")
        used.append(provider)
        agents.append({
            "name": a["name"],
            "role": a["role"],
            "personality": a["personality"],
            "provider": provider,
            "base_url": "",
            "api_key": "",
            "model_id": "",
            "stance": a["stance"],
            "agent_id": f"agent_{i + 1}",
        })
    judge = None
    jr = recipe.get("judge")
    if jr and recipe["mode"] == "debate":
        jp = _assign_provider(jr.get("provider", "default"), pool, used)
        judge = {"name": jr.get("name", "独立裁判"), "provider": jp,
                 "base_url": "", "api_key": "", "model_id": ""}
        if jp != jr.get("provider"):
            notes.append(f"裁判原定 {jr.get('provider')} 不可用，已改派 {jp}")
    return {
        "recipe": recipe_name,
        "title": recipe["title"],
        "mode": recipe["mode"],
        "max_rounds": recipe["max_rounds"],
        "agents": agents,
        "judge": judge,
        "providers_used": sorted(set(used)),
        "notes": notes,
    }
