"""
prompts.py — 提示词资产与指令构建（纯函数，可直接单测）

从 orchestrator.py 抽出的「文案层」：
  - 专用工具 schema：`delegate_task` / `claim_task` / `report_result`
  - 立场显示名 `STANCE_LABEL`、架构状态机阶段 `PHASES`
  - 结构化结论模板 `CONCLUSION_TEMPLATE`（四段式，P0-1）
  - 合作模式反思与自我修正的 prompt / 解析（§12.18）
  - 主持人与协作 Agent 的指令构建（辩论 / 合作两种模式）

这些内容此前与编排逻辑混在同一个 2300 行文件里：改一句文案要翻遍编排代码，
也没法在不构造整个 Orchestrator 的情况下测试。抽成纯函数后：
文案改动集中在一处，`main_directive(...)` 之类可以单测。
"""
from __future__ import annotations

import re

# ---- 专用工具 schema（任务分派 / 认领 / 回报） ----
DELEGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": "把一项任务分派给某个协作 Agent（recipient 指定接收者），"
                       "或放入共享任务池待协作 Agent 主动认领（recipient 留空）。"
                       "明确说明任务内容与期望效果，并提供用于接收结果的变量名(var_name)。",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string",
                              "description": "接收任务的协作 Agent 名称；留空 = 放入任务池待认领"},
                "content": {"type": "string", "description": "任务内容说明"},
                "expected_outcome": {"type": "string", "description": "期望达到的效果"},
                "var_name": {"type": "string", "description": "用于接收结果的变量名(任务接收方变量)"},
            },
            "required": ["content", "expected_outcome", "var_name"],
        },
    },
}

CLAIM_SCHEMA = {
    "type": "function",
    "function": {
        "name": "claim_task",
        "description": "从共享任务池认领一个未分配任务（task_id 为任务列表中的数字编号）；"
                       "认领后由你独占负责，后续用 report_result 回报完成结果。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "任务编号（列表中的数字 id）"},
            },
            "required": ["task_id"],
        },
    },
}

REPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "report_result",
        "description": "向主 Agent 回报任务最终结果，并填充任务接收方变量。",
        "parameters": {
            "type": "object",
            "properties": {
                "var_name": {"type": "string", "description": "主 Agent 指定的变量名"},
                "var_value": {"type": "string", "description": "回填到该变量的结果内容"},
                "completed": {"type": "boolean", "description": "任务是否完成"},
                "message": {"type": "string", "description": "结果摘要（仅最终结论，不含中间过程）"},
            },
            "required": ["var_name", "var_value", "completed", "message"],
        },
    },
}

# 立场显示名
STANCE_LABEL = {"PRO": "正方", "CON": "反方", "NEUTRAL": "中立", "HOST": "主持人"}

# 架构状态机（对齐架构文档 §4.1）
PHASES = ("idle", "preparing", "debating", "judging", "finished")

# P0-1 结构化结论：最终报告固定四段式模板（主持人 LLM 必须保留每个一级标题）
CONCLUSION_TEMPLATE = (
    "## 核心结论\n"
    "（用 1-3 句话概括整场讨论最重要的结论，必须为独立段落）\n\n"
    "## 各方立场与论点摘要\n"
    "（按立场/辩手分别列出核心论点，每人 1-3 条）\n\n"
    "## 裁判裁决\n"
    "（引用裁判给出的胜方、评分与评语）\n\n"
    "## 分歧与待办\n"
    "（列出仍未解决的分歧点与可选的后续待办；若无则写“无”）\n"
)


# ---------------------------------------------------------------------------
# 合作模式：反思与自我修正（§12.18）
# ---------------------------------------------------------------------------
def reflection_prompt(task: dict, draft: str) -> str:
    """构建反思 prompt：要求对照期望效果对草稿自我批判并输出修订版。

    强制三个 Markdown 二级标题（顺序不变），解析方按关键词容错取段。
    """
    return (f"【任务内容】{task.get('content', '')}\n"
            f"【期望效果】{task.get('expected_outcome', '')}\n"
            f"【你的任务结果草稿】\n{draft or '（空）'}\n\n"
            "请对照期望效果对上述草稿做一次自我批判与修订：指出缺失、错误或不够严谨之处；"
            "若已完全达标可保持原样，并在修订说明中说明理由。"
            "严格按以下三个 Markdown 二级标题输出（顺序不变）：\n"
            "## 自我反思\n[对草稿的不足/达标分析]\n"
            "## 修订后结果\n[修订后的最终内容，若无需修改则原样保留草稿]\n"
            "## 修订说明\n[改了什么、为什么]")


def parse_reflection(text: str) -> dict:
    """解析反思输出为 {reflection, revision, explanation}；缺失段回退空串。

    按「自我反思 / 修订后结果 / 修订说明」关键词匹配 Markdown 标题（# 或 ##），
    与 `_split_sections` 相比增加「标题含前后缀容错 + 缺段回退」语义。
    """
    out = {"reflection": "", "revision": "", "explanation": ""}
    cur = None
    for line in (text or "").splitlines():
        m = re.match(r"^#{1,2}\s+(.+)$", line.strip())
        if m:
            key = m.group(1).strip()
            if "自我反思" in key:
                cur = "reflection"
            elif "修订后结果" in key:
                cur = "revision"
            elif "修订说明" in key:
                cur = "explanation"
            else:
                cur = None
        elif cur is not None:
            out[cur] = (out[cur] + "\n" + line).strip()
    return out


# ---------------------------------------------------------------------------
# 辩论模式：主持人指令
# ---------------------------------------------------------------------------
def main_directive(topic: str, max_rounds: int, round_no: int,
                   pending_exists: bool = False) -> str:
    """辩论模式主持人指令：第 1 轮分派任务，之后轮次抛疑追问。"""
    base = f"【讨论主题】{topic}\n【轮次】第 {round_no}/{max_rounds} 轮（辩论进行中）\n"
    if round_no == 1:
        base += ("这是辩论第 1 轮。请先为协作 Agent 分派调研 / 立论任务（delegate_task），"
                 "明确任务内容与期望效果，并提供接收结果的变量名(var_name)；"
                 "你也可以先陈述你的开场立场（作为主持人保持中立公正）。")
    else:
        base += (f"这是辩论第 {round_no} 轮。请基于各方上一轮发言与已回填的任务结果，"
                 "以主持人身份抛出疑问、要求补充论据、指出分歧或让某方进一步论证；"
                 "可通过 delegate_task 追加追问任务，或直接发言引发交锋。"
                 "请至少提出一个有针对性的质疑，推动多轮辩论。")
        if pending_exists:
            base += "\n（提示：仍存在未完成任务，请先审视其进展。）"
    return base


def final_summary_directive(judge_section: str = "") -> str:
    """辩论模式收尾：主持人四段式汇总报告指令。"""
    return ("所有 Agent 的任务已结束或已达最大轮次。请作为主持人，"
            "综合各方发言、任务结果与裁判裁决，输出最终讨论报告。"
            "请严格按下面的 Markdown 模板组织内容，四个一级标题（##）必须全部保留"
            "且顺序不变：\n\n"
            + CONCLUSION_TEMPLATE
            + "\n请直接输出报告文本，不要再次分派任务。") + (judge_section or "")


# ---------------------------------------------------------------------------
# 合作模式：主 Agent 指令 / 子 Agent 回合 prompt
# ---------------------------------------------------------------------------
def collab_main_directive(topic: str, max_rounds: int, round_no: int) -> str:
    """合作模式主 Agent 指令：第 1 轮拆解整体任务入池，后续轮次统筹协调。"""
    base = f"【整体任务】{topic}\n【轮次】第 {round_no}/{max_rounds} 轮（合作进行中）\n"
    if round_no == 1:
        base += ("这是合作第 1 轮。请作为主 Agent 拆解整体任务："
                 "用 delegate_task 把子任务放入共享任务池（recipient 留空），"
                 "明确任务内容、期望效果与接收结果的变量名(var_name)；"
                 "你也可以先陈述整体思路与分工计划。")
    else:
        base += ("请作为主 Agent 统筹协调：审视任务池与各协作 Agent 的完成情况，"
                 "用 delegate_task 补充或细化子任务（recipient 留空放入任务池），"
                 "或对整体进展给出指引。若所有子任务均已完成，请直接宣布整体任务完成。")
    return base


def _task_line(t: dict) -> str:
    """任务卡片的单行描述（待办 / 可认领两个列表共用）。"""
    return (f"- 任务#{t['id']}（变量 {t['var_name']}）：{t['content']}"
            f"（期望：{t['expected_outcome']}）")


def collab_work_prompt(round_no: int, max_rounds: int,
                       claimed: list, claimable: list) -> str:
    """合作模式子 Agent 回合 prompt：并列「待办（需 report）」与「可认领（可 claim）」。"""
    parts = [f"【当前轮次】第 {round_no}/{max_rounds} 轮（合作进行中）"]
    if claimed:
        todo = "\n".join(_task_line(t) for t in claimed)
        parts.append(f"【你的待办任务（完成后必须用 report_result 回报）】\n{todo}")
    else:
        parts.append("【你的待办任务】当前没有已认领的任务。")
    if claimable:
        can = "\n".join(_task_line(t) for t in claimable)
        parts.append(f"【任务池中可认领的任务（可用 claim_task 领取，认领后独占负责）】\n{can}")
    else:
        parts.append("【任务池】当前没有待认领的任务。")
    parts.append("说明：优先完成你的待办任务并通过 report_result 回报；"
                 "若任务池有可认领任务且你有余力，可先 claim_task 认领再完成回报。"
                 "无需额外发言时保持简洁，直接使用工具即可。")
    return "\n\n".join(parts)


def collab_summary_directive(topic: str, tasks: list) -> str:
    """合作模式收尾：主 Agent 三段式汇总指令（含任务成果清单）。"""
    task_lines = []
    for t in tasks:
        if t["completed"]:
            state = "✅"
        elif t.get("status") == "pending":
            state = "🔵"
        else:
            state = "⏳"
        owner = t["recipient"] or "任务池"
        result = t.get("result_value") or t.get("result_message") or "未完成"
        task_lines.append(f"- 任务#{t['id']}（{owner} {state}）：{t['content']} → {result}")
    if not task_lines:
        task_lines.append("- （本次协作未产生子任务）")
    return (f"【整体任务】{topic}\n"
            "所有子任务已结束或已达最大轮次。请作为主 Agent 输出最终协作总结报告，"
            "严格按下面的 Markdown 模板组织，三个一级标题（##）必须全部保留且顺序不变：\n\n"
            "## 核心结论\n\n[整体任务是否完成、核心成果]\n\n"
            "## 子任务与成果\n\n" + "\n".join(task_lines) + "\n\n"
            "## 遗留问题与后续建议\n\n[未完成项、风险与后续建议]\n\n"
            "请直接输出报告文本，不要再次分派任务。")
