"""
fairness.py — 多层公平性治理 (FairnessGovernor)

替代旧的 token 预算总控逻辑（budget planner）。不同模型的量化方式与
embedding 向量维度不同，token 数不是跨模型公平的度量单位，因此这里
以「字符数 + 相对比例」作为跨模型公平的度量单位，不再做全局 total_token
约束，也不把 max_tokens 下发给 API。

公平性层次：
  L1 输出上限     —— 相对（参照对手有效输出均值）× 比例 + 硬性字符安全上限，
                     超限后事后截断（不阻止生成，防止截断竞态）
  L2 有效回答比例 —— 剥离 <think> 思考片段后统计有效回答占比
  L3 输入对称性   —— PRO/CON 反驳时可见的对手历史轮数相等（或差 ≤ 1，
                     且下一回合落后方紧跟补齐）
  L4 轮次对称性   —— 每个模型每轮最多一次发言
  L5 采样参数对齐 —— temperature / top_p 在辩论发言中统一显式传入，
                     辅助 LLM（裁判/摘要）也走同一套对齐入口
  L6 长度中性评审 —— 裁判提示词中明确「不以长度论优劣，只看有效论证密度」

可扩展：新增层时实现一个返回 LayerReport 的方法，并注册到 report() 即可。
"""
import re
from dataclasses import dataclass, field

from muti_llm_agent.config import OPPONENT_STANCE, settings

# <think> 片段正则（与 llm._strip_think 一致）
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# 公平性层标识
L1_OUTPUT_CEILING = "L1"
L2_EFFECTIVE_RATIO = "L2"
L3_INPUT_SYMMETRY = "L3"
L4_TURN_SYMMETRY = "L4"
L5_SAMPLING_ALIGN = "L5"
L6_LENGTH_NEUTRAL = "L6"

# 配置默认值（可被 .env 同名变量覆盖，也可在构造时显式覆盖）
DEFAULTS = {
    # L1：相对上限 = max(对手近 N 轮有效输出均值 × 比例, 下限)；再与硬上限取 min
    "output_ratio": 1.5,            # FAIR_OUTPUT_RATIO
    "hard_char_ceiling": 3000,      # FAIR_HARD_CHAR_CEILING 单次发言硬性字符上限
    "floor_char_limit": 400,        # FAIR_FLOOR_CHAR_LIMIT 相对基准下限
    "opening_char_limit": 1200,     # FAIR_OPENING_CHAR_LIMIT 立论/无参照时的上限
    "reference_rounds": 2,          # FAIR_REFERENCE_ROUNDS 参照最近 N 轮对手输出
    # L2：思考/推理占比上限，超过记 warning（不打断生成）
    "think_ratio_max": 0.5,         # FAIR_THINK_RATIO_MAX
    # L3：双方可见对手轮数允许的最大差值
    "max_seen_diff": 1,             # FAIR_MAX_SEEN_DIFF
    # L5：采样参数（辩论发言统一；辅助 LLM 走 judge 参数，同样统一）
    "temperature": 0.7,             # FAIR_TEMPERATURE
    "top_p": 1.0,                   # FAIR_TOP_P
    "judge_temperature": 0.3,       # FAIR_JUDGE_TEMPERATURE
    "judge_top_p": 1.0,             # FAIR_JUDGE_TOP_P
}


def _setting(name: str, default):
    return getattr(settings, name, default)


# ---------------------------------------------------------------------------
# 纯函数助手（可直接单测）
# ---------------------------------------------------------------------------

def split_think(text: str) -> tuple[str, str]:
    """把原始文本拆成 (think_text, visible_text)。

    think_text 为全部 <think>...</think> 片段拼接；visible_text 为剥离后正文。
    """
    if not text:
        return "", ""
    think_parts = _THINK_RE.findall(text)
    visible = _THINK_RE.sub("", text).strip()
    return "".join(think_parts), visible


def think_chars(text: str) -> int:
    """原始文本中思考/推理片段的总字符数。"""
    return len(split_think(text)[0])


def effective_answer_chars(text: str) -> int:
    """原始文本剥离思考片段后的有效回答字符数。"""
    return len(split_think(text)[1])


def truncate_to_chars(text: str, ceiling: int) -> tuple[str, bool]:
    """事后字符截断：超出 ceiling 时保留头尾并返回 (截断文本, True)。

    保留头尾的论证骨架，避免只留开头导致结论丢失。
    """
    if not text or len(text) <= ceiling:
        return text, False
    if ceiling <= 1:
        return text[:ceiling], True
    half = ceiling // 2
    return (text[:half] + "…" + text[-half:])[:ceiling], True


def effective_density(visible_chars: int, total_chars: int) -> float:
    """有效论证密度 = 有效回答字符 / 总字符（含思考）。供 L2/L6 使用。"""
    if total_chars <= 0:
        return 1.0
    return min(1.0, visible_chars / total_chars)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class TurnStat:
    """单次发言的输出统计（用于 L1 相对参照与 L2 思考占比）。"""
    stance: str
    round_no: int
    visible_chars: int
    think_chars: int
    total_chars: int
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "stance": self.stance,
            "round": self.round_no,
            "visible_chars": self.visible_chars,
            "think_chars": self.think_chars,
            "total_chars": self.total_chars,
            "truncated": self.truncated,
        }


@dataclass
class LayerReport:
    """单层公平性约束的报告。"""
    layer: str
    name: str
    status: str          # ok / warning / violation
    detail: str = ""
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "metrics": self.metrics,
        }


class FairnessViolation(Exception):
    """硬性公平性违约（当前仅 L4 重复发言可选触发，默认只记录不抛）。"""


# ---------------------------------------------------------------------------
# 公平性治理器
# ---------------------------------------------------------------------------

class FairnessGovernor:
    """多辩手（至少 PRO/CON）的公平性治理状态机。

    用法（编排器侧）：
      governor.ceiling_for(stance, round_no)               # L1 输出上限
      governor.truncate_output(text, ceiling)              # L1 事后截断
      governor.record_turn(stance, round_no, vis, think, truncated)  # L1/L2
      governor.record_opponent_seen(stance, rounds)        # L3 可见轮数
      governor.record_speech(stance, round_no)             # L4 发言记录
      governor.sampling_params() / governor.judge_params() # L5 对齐参数
      governor.judge_guidance()                            # L6 评审引导
      governor.report()                                    # 汇总报告
    """

    def __init__(self, **overrides):
        cfg = dict(DEFAULTS)
        for key, val in overrides.items():
            if key in cfg:
                cfg[key] = val
        # 允许用 .env 覆盖默认值（未在 overrides 中显式给出时）
        self.output_ratio = cfg["output_ratio"]
        self.hard_char_ceiling = int(_setting("FAIR_HARD_CHAR_CEILING", cfg["hard_char_ceiling"]))
        self.floor_char_limit = int(_setting("FAIR_FLOOR_CHAR_LIMIT", cfg["floor_char_limit"]))
        self.opening_char_limit = int(_setting("FAIR_OPENING_CHAR_LIMIT", cfg["opening_char_limit"]))
        self.reference_rounds = int(_setting("FAIR_REFERENCE_ROUNDS", cfg["reference_rounds"]))
        self.think_ratio_max = float(_setting("FAIR_THINK_RATIO_MAX", cfg["think_ratio_max"]))
        self.max_seen_diff = int(_setting("FAIR_MAX_SEEN_DIFF", cfg["max_seen_diff"]))
        self.temperature = float(_setting("FAIR_TEMPERATURE", cfg["temperature"]))
        self.top_p = float(_setting("FAIR_TOP_P", cfg["top_p"]))
        self.judge_temperature = float(_setting("FAIR_JUDGE_TEMPERATURE", cfg["judge_temperature"]))
        self.judge_top_p = float(_setting("FAIR_JUDGE_TOP_P", cfg["judge_top_p"]))

        # 运行期状态
        self._turns: list[TurnStat] = []
        self._turns_by_stance: dict[str, list[TurnStat]] = {}
        self._seen_opponent_rounds: dict[str, int] = {}
        self._speech_counts: dict[tuple[str, int], int] = {}
        self._judge_guidance_injected: bool = False

    # ---- L1：输出上限 ------------------------------------------------------

    def ceiling_for(self, stance: str, round_no: int,
                    opponent_recent_chars: int | None = None) -> int:
        """计算某次发言允许的字符上限（rounding 到整数）。

        - 立论（round_no == 1）或无对手参照时：使用 opening_char_limit。
        - 否则：max(对手近 N 轮有效输出均值 × output_ratio, floor)，
          再与 hard_char_ceiling 取 min，防止极端长输出造成整体消耗过大。
        """
        if round_no == 1 or opponent_recent_chars is None:
            base = self.opening_char_limit
        else:
            base = max(opponent_recent_chars * self.output_ratio, self.floor_char_limit)
        return min(int(base), self.hard_char_ceiling)

    def recent_opponent_chars(self, stance: str) -> int | None:
        """对手最近 reference_rounds 轮有效输出字符的均值（无则 None）。"""
        opponent = OPPONENT_STANCE.get(stance)
        if opponent is None:
            return None
        turns = self._turns_by_stance.get(opponent, [])
        recent = turns[-self.reference_rounds:]
        if not recent:
            return None
        return int(sum(t.visible_chars for t in recent) / len(recent))

    def truncate_output(self, text: str, ceiling: int) -> tuple[str, bool]:
        """L1 事后截断兜底：超限保留头尾，返回 (text, truncated)。"""
        return truncate_to_chars(text, ceiling)

    # ---- L1/L2：记录输出统计 -----------------------------------------------

    def record_turn(self, stance: str, round_no: int, visible_chars: int,
                    think_chars: int, truncated: bool = False) -> None:
        turn = TurnStat(
            stance=stance,
            round_no=round_no,
            visible_chars=int(visible_chars),
            think_chars=int(think_chars),
            total_chars=int(visible_chars) + int(think_chars),
            truncated=bool(truncated),
        )
        self._turns.append(turn)
        self._turns_by_stance.setdefault(stance, []).append(turn)

    # ---- L3：输入对称性 ----------------------------------------------------

    def record_opponent_seen(self, stance: str, opponent_rounds: int) -> None:
        """记录 stance 在本次反驳时可见的对手历史轮数。"""
        self._seen_opponent_rounds[stance] = int(opponent_rounds)

    def seen_opponent_rounds(self) -> dict[str, int]:
        return dict(self._seen_opponent_rounds)

    # ---- L4：轮次对称性 ----------------------------------------------------

    def record_speech(self, stance: str, round_no: int) -> None:
        """记录一次发言；同一 (stance, round) 重复发言时标记 violation。"""
        key = (stance, int(round_no))
        self._speech_counts[key] = self._speech_counts.get(key, 0) + 1

    def speech_counts(self) -> dict[tuple[str, int], int]:
        return dict(self._speech_counts)

    # ---- L5：采样参数对齐 --------------------------------------------------

    def sampling_params(self) -> dict:
        """辩论发言统一采样参数。"""
        return {"temperature": self.temperature, "top_p": self.top_p}

    def judge_params(self) -> dict:
        """辅助 LLM（裁判 / 摘要）统一采样参数。"""
        return {"temperature": self.judge_temperature, "top_p": self.judge_top_p}

    # ---- L6：长度中性评审 --------------------------------------------------

    def judge_guidance(self) -> str:
        """注入裁判提示词的长度中性要求；调用后标记已注入。"""
        self._judge_guidance_injected = True
        return (
            "评分时禁止以输出长度、字数多寡论优劣：只看有效论证的密度与质量，"
            "思考/推理内容不计入有效论证。若双方论点相近，更短而更凝练的论证"
            "不应被扣分。"
        )

    # ---- 报告 --------------------------------------------------------------

    def _l1_report(self) -> LayerReport:
        truncated = [t for t in self._turns if t.truncated]
        capped = len(truncated)
        status = "warning" if capped else "ok"
        return LayerReport(
            layer=L1_OUTPUT_CEILING,
            name="输出上限",
            status=status,
            detail=f"硬性字符上限 {self.hard_char_ceiling}，相对比例 {self.output_ratio}"
                   f"；共 {capped} 次发言被事后截断" if capped else
                   f"硬性字符上限 {self.hard_char_ceiling}，相对比例 {self.output_ratio}；无截断",
            metrics={"hard_char_ceiling": self.hard_char_ceiling,
                     "output_ratio": self.output_ratio,
                     "truncated_turns": capped},
        )

    def _l2_report(self) -> LayerReport:
        stats = {}
        worst = 0.0
        for stance in ("PRO", "CON"):
            turns = self._turns_by_stance.get(stance, [])
            total_think = sum(t.think_chars for t in turns)
            total_vis = sum(t.visible_chars for t in turns)
            total = total_think + total_vis
            ratio = round(total_think / total, 4) if total else 0.0
            worst = max(worst, ratio)
            stats[stance] = {
                "total_chars": total,
                "think_chars": total_think,
                "visible_chars": total_vis,
                "think_ratio": ratio,
            }
        status = "warning" if worst > self.think_ratio_max else "ok"
        return LayerReport(
            layer=L2_EFFECTIVE_RATIO,
            name="有效回答比例",
            status=status,
            detail=f"思考占比上限 {self.think_ratio_max:.0%}；"
                   f"实际 PRO {stats.get('PRO', {}).get('think_ratio', 0):.0%} / "
                   f"CON {stats.get('CON', {}).get('think_ratio', 0):.0%}",
            metrics={"think_ratio_max": self.think_ratio_max, "by_stance": stats},
        )

    def _l3_report(self) -> LayerReport:
        pro = self._seen_opponent_rounds.get("PRO", 0)
        con = self._seen_opponent_rounds.get("CON", 0)
        diff = abs(pro - con)
        if not self._seen_opponent_rounds:
            status, detail = "ok", "尚无反驳记录，待立论完成后校验"
        elif diff <= self.max_seen_diff:
            status, detail = "ok", f"PRO 可见对手 {pro} 轮 / CON 可见对手 {con} 轮，差值 {diff} ≤ {self.max_seen_diff}"
        else:
            status, detail = "violation", f"PRO 可见对手 {pro} 轮 / CON 可见对手 {con} 轮，差值 {diff} 超过 {self.max_seen_diff}"
        return LayerReport(
            layer=L3_INPUT_SYMMETRY,
            name="输入对称性",
            status=status,
            detail=detail,
            metrics={"pro_seen_rounds": pro, "con_seen_rounds": con,
                     "max_seen_diff": self.max_seen_diff},
        )

    def _l4_report(self) -> LayerReport:
        dup = [{"stance": s, "round": r, "count": c}
               for (s, r), c in sorted(self._speech_counts.items()) if c > 1]
        total = len(self._speech_counts)
        status = "violation" if dup else "ok"
        return LayerReport(
            layer=L4_TURN_SYMMETRY,
            name="轮次对称性",
            status=status,
            detail=f"已记录 {total} 次发言；重复发言 {len(dup)} 处" if dup else f"已记录 {total} 次发言，无重复",
            metrics={"total_speeches": total, "duplicates": dup},
        )

    def _l5_report(self) -> LayerReport:
        return LayerReport(
            layer=L5_SAMPLING_ALIGN,
            name="采样参数对齐",
            status="ok",
            detail=f"辩论 temperature={self.temperature} top_p={self.top_p}；"
                   f"辅助 temperature={self.judge_temperature} top_p={self.judge_top_p}",
            metrics={"debate": {"temperature": self.temperature, "top_p": self.top_p},
                     "aux": {"temperature": self.judge_temperature, "top_p": self.judge_top_p}},
        )

    def _l6_report(self) -> LayerReport:
        status = "ok" if self._judge_guidance_injected else "warning"
        return LayerReport(
            layer=L6_LENGTH_NEUTRAL,
            name="长度中性评审",
            status=status,
            detail="裁判提示词已注入「不以长度论优劣」" if self._judge_guidance_injected
                   else "裁判提示词尚未注入长度中性要求",
            metrics={"injected": self._judge_guidance_injected},
        )

    def report(self) -> dict:
        """汇总全部公平性层的报告。"""
        layers = [
            self._l1_report(),
            self._l2_report(),
            self._l3_report(),
            self._l4_report(),
            self._l5_report(),
            self._l6_report(),
        ]
        violations = [r.to_dict() for r in layers if r.status == "violation"]
        warnings = [r.to_dict() for r in layers if r.status == "warning"]
        return {
            "ok": not violations,
            "layers": [r.to_dict() for r in layers],
            "violations": violations,
            "warnings": warnings,
            "turns": [t.to_dict() for t in self._turns],
        }
