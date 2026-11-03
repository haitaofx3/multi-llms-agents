"""
eval_baseline.py — 讨论质量基线评估脚本（P0-2）

对一场导出的讨论 Markdown（muti_llm_agent/orchestrator.export_markdown 的输出格式）进行
**可复现的质量评估**，输出多维质量报告（0-100），并可与历史基线 JSON 对比，
用于回归测试：代码或提示词改动后，质量是否退化一测便知。

设计动机（P0-2：质量基线缺失）
  系统此前只有"跑通"（功能正确），没有"质量"度量。改一个 prompt / 换一个模型
  后，辩手发言更短了 / 收敛更慢了 / 裁判缺失了，无法被自动发现。本脚本把一场
  讨论导出为结构化质量分数，可手动单跑，也可在 CI 里与基线比对。

用法
  # 评估单场讨论，输出可读报告
  python scripts/eval_baseline.py data/discussions/discussion_xxx.md

  # 输出 JSON（便于脚本消费）
  python scripts/eval_baseline.py data/discussions/discussion_xxx.md --json

  # 生成基线文件（首跑）：把当前报告存为基线
  python scripts/eval_baseline.py data/discussions/discussion_xxx.md --make-baseline data/baseline.json

  # 与基线对比，发现退化则退出码 2
  python scripts/eval_baseline.py data/discussions/discussion_xxx.md --baseline data/baseline.json

质量维度与权重（总计 1.00）
  结构完整 (structure)   0.20  必备章节是否齐全（摘要/发言/结果/记忆，含裁判则加裁判章）
  完成度   (completeness) 0.25  是否产出裁判裁决 / 四段式结论 / 收敛 / 任务收尾
  参与度   (engagement)   0.25  发言人数与轮次覆盖、发言长度是否落在合理区间
  公平性   (fairness)     0.20  公平性治理：违约 0 为硬性要求，警告逐条扣分
  效率     (efficiency)   0.10  费用/Token 遥测是否齐备、输出占比是否健康

评分口径在 score_report() 内集中维护；改动维度权重需同步更新本 docstring。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 解析

SECTION_RE = re.compile(r"^## (.+)$")
SPEECH_RE = re.compile(r"^\*\*(.+?)\*\*（第(\d+)轮）：")
KV = lambda key: re.compile(rf"^- {key}：(.+)$")  # noqa: E731


def parse_export(text: str) -> dict:
    """把导出的 Markdown 文本解析为结构化信号字典。"""
    lines = text.splitlines()
    signals = {
        "topic": None, "status": None, "phase": None,
        "rounds_actual": 0, "rounds_max": 0,
        "duration_s": 0.0, "rag_evidence": 0, "compressions": 0,
        "tasks_total": 0, "tasks_completed": 0,
        "tokens_total": None, "tokens_output": None, "cost_rmb": None,
        "converged": False, "convergence_mode": None,
        "agents": [], "judge": False, "speeches": [],
        "fairness": {"ok": False, "warnings": 0, "violations": 0, "layers": []},
        "has_judgment": False, "judge_winner": None, "has_conclusion": False,
        "conclusion_sections": [], "memory_snapshots": 0,
    }

    section = None          # 当前顶级章节
    in_agent_list = False
    in_fairness = False
    current_speech = None

    for raw in lines:
        line = raw.rstrip()
        m = SECTION_RE.match(line)
        if m:
            section = m.group(1)
            in_agent_list = section == "参与 Agent"
            in_fairness = section == "运行摘要"
            current_speech = None
            continue

        stripped = line.strip()
        if not stripped:
            continue

        # ---- 头部元信息 ----
        if (m := KV("主题").match(line)):
            signals["topic"] = m.group(1).strip()
        elif (m := re.match(r"^- 轮次：(\d+)/(\d+)$", line)):
            signals["rounds_actual"], signals["rounds_max"] = map(int, m.groups())
        elif (m := re.match(r"^- 状态：(.+) / 阶段：(.+)$", line)):
            signals["status"], signals["phase"] = m.group(1).strip(), m.group(2).strip()
        elif (m := re.match(r"^- Token 用量（只读遥测）：累计 (\d+) / 输出 (\d+)（不再作为约束）$", line)):
            signals["tokens_total"], signals["tokens_output"] = map(int, m.groups())
        elif (m := KV("预估费用（P1-6）").match(line)):
            raw_cost = re.sub(r"[^0-9.]", "", m.group(1))
            try:
                signals["cost_rmb"] = float(raw_cost)
            except ValueError:
                signals["cost_rmb"] = None

        # ---- 参与 Agent 列表 ----
        if in_agent_list and line.startswith("  - "):
            a = re.match(r"^  - (.+?)(（主）)? \| 角色：(.+?) \| 立场：(.+?) \| 模型：(.+)$", line)
            if a:
                signals["agents"].append({
                    "name": a.group(1).strip(),
                    "role": a.group(3).strip(),
                    "stance": a.group(4).strip(),
                })
            continue
        if not in_agent_list and line.startswith("- 参与 Agent："):
            in_agent_list = True
            continue
        if in_agent_list and line.startswith("- 独立裁判："):
            signals["judge"] = True
            in_agent_list = False
            continue

        # ---- 运行摘要 ----
        if (m := KV("持续时间").match(line)):
            signals["duration_s"] = float(re.sub(r"[^0-9.]", "", m.group(1)))
        elif (m := re.match(r"^- 收敛触发：是（连续\d+轮相似）$", line)):
            signals["converged"] = True
        elif (m := KV("收敛检测模式").match(line)):
            signals["convergence_mode"] = m.group(1).strip()
        elif (m := KV("RAG 证据轮数").match(line)):
            signals["rag_evidence"] = int(re.sub(r"\D", "", m.group(1)))
        elif (m := re.match(r"^- 任务：(\d+)（完成 (\d+)）$", line)):
            signals["tasks_total"], signals["tasks_completed"] = map(int, m.groups())
        elif (m := KV("历史压缩次数").match(line)):
            signals["compressions"] = int(re.sub(r"\D", "", m.group(1)))
        elif (m := re.match(r"^- 公平性治理：(全部通过|存在违约)（(\d+) 警告 / (\d+) 违约）$", line)):
            signals["fairness"]["ok"] = m.group(1) == "全部通过"
            signals["fairness"]["warnings"] = int(m.group(2))
            signals["fairness"]["violations"] = int(m.group(3))
        elif in_fairness and stripped.startswith("- L") and "：" in stripped:
            signals["fairness"]["layers"].append(stripped[2:].strip())

        # ---- 发言记录 ----
        if section == "发言记录":
            m = SPEECH_RE.match(line)
            if m:
                current_speech = {"speaker": m.group(1).strip(), "round": int(m.group(2)),
                                  "text": ""}
                signals["speeches"].append(current_speech)
            elif current_speech is not None and not line.startswith("##"):
                current_speech["text"] += "\n" + stripped

        # ---- 裁判裁决 / 最终结果 / 记忆 ----
        if section == "裁判裁决":
            if (m := KV("胜方").match(line)):
                signals["has_judgment"] = True
                signals["judge_winner"] = m.group(1).strip()
        if section == "各 Agent 记忆快照" and stripped.startswith("- "):
            signals["memory_snapshots"] += 1

    # ---- 结论（四段式）检测：核心结论 / 各方立场 / 裁判裁决 / 分歧与待办 ----
    for marker in ("## 核心结论", "## 各方立场与论点摘要", "## 裁判裁决", "## 分歧与待办"):
        if marker in text:
            signals["conclusion_sections"].append(marker)
    signals["has_conclusion"] = (
        "## 核心结论" in signals["conclusion_sections"]
        and "## 分歧与待办" in signals["conclusion_sections"]
    )

    # 发言文本 trim 并统计长度
    for s in signals["speeches"]:
        s["text"] = s["text"].strip()
        s["length"] = len(s["text"])
    signals["speakers"] = sorted({s["speaker"] for s in signals["speeches"]})
    return signals


# ---------------------------------------------------------------- 评分

# 维度权重（总和必须等于 1.0）
WEIGHTS = {"structure": 0.20, "completeness": 0.25, "engagement": 0.25,
           "fairness": 0.20, "efficiency": 0.10}

REASONABLE_SPEECH_MIN, REASONABLE_SPEECH_MAX = 30, 3000  # 合理单条发言字符区间


def _score_structure(s: dict) -> tuple[float, list[str]]:
    required = ["运行摘要", "发言记录", "任务结果", "各 Agent 记忆快照"]
    if s["judge"]:
        required.append("裁判裁决")
    present = [sec for sec in required if f"## {sec}" in s.get("_raw", "")]
    notes = [f"章节缺失：{sec}" for sec in required if sec not in present]
    return round(len(present) / len(required) * 100, 1), notes


def _score_completeness(s: dict) -> tuple[float, list[str]]:
    score, notes = 0.0, []
    if s["has_judgment"]:
        score += 30
    else:
        notes.append("未产出裁判裁决")
    if s["has_conclusion"]:
        score += 30
    else:
        notes.append("未产出四段式结论")
    if s["converged"]:
        score += 20
    else:
        notes.append("未触发收敛（可能已达最大轮次）")
    if s["status"] == "done" or s["phase"] == "finished":
        score += 10
    else:
        notes.append("状态未完成")
    if s["tasks_total"] == 0 or s["tasks_completed"] == s["tasks_total"]:
        score += 10
    else:
        notes.append(f"任务未收尾（{s['tasks_completed']}/{s['tasks_total']}）")
    return round(min(score, 100.0), 1), notes


def _score_engagement(s: dict) -> tuple[float, list[str]]:
    score, notes = 0.0, []
    n_agents = max(len(s["agents"]), 1)
    n_speakers = len(s["speakers"])
    ratio = n_speakers / n_agents
    score += ratio * 40
    if ratio < 1.0:
        notes.append(f"发言人数 {n_speakers}/{n_agents}")

    expected = max(s["rounds_actual"], 1) * max(len(s["agents"]), 1)
    n_speeches = len(s["speeches"])
    if n_speeches >= expected * 0.8:
        score += 25
    else:
        score += round(n_speeches / max(expected * 0.8, 1) * 25, 1)
        notes.append(f"发言条数偏少（{n_speeches}/{expected}）")

    if s["speeches"]:
        lengths = [x["length"] for x in s["speeches"]]
        avg = sum(lengths) / len(lengths)
        empty = sum(1 for x in lengths if x == 0)
        if REASONABLE_SPEECH_MIN <= avg <= REASONABLE_SPEECH_MAX:
            score += 25
        else:
            notes.append(f"平均发言长度 {avg:.0f} 字不在合理区间")
            score += 25 * (0 if avg < 1 else min(avg / REASONABLE_SPEECH_MIN, 1))
        if empty:
            score -= 10 * empty
            notes.append(f"{empty} 条空发言")
        # 长度健康度：过长的单条发言提示模型失控（最多扣 10）
        longest = max(lengths)
        if longest > REASONABLE_SPEECH_MAX * 3:
            score -= 10
            notes.append(f"最长发言 {longest} 字，疑似失控")
    else:
        notes.append("无发言记录")
    return round(max(0.0, min(score, 100.0)), 1), notes


def _score_fairness(s: dict) -> tuple[float, list[str]]:
    score, notes = 0.0, []
    if s["fairness"]["ok"] or s["fairness"]["violations"] == 0:
        score += 70
    else:
        score += max(0, 70 - 20 * s["fairness"]["violations"])
        notes.append(f"{s['fairness']['violations']} 项公平性违约")
    score -= 10 * s["fairness"]["warnings"]
    for _ in range(s["fairness"]["warnings"]):
        notes.append("公平性警告")
    if s["fairness"]["layers"]:
        score += 30
    else:
        notes.append("缺少公平性治理明细")
    return round(max(0.0, min(score, 100.0)), 1), notes


def _score_efficiency(s: dict) -> tuple[float, list[str]]:
    score, notes = 0.0, []
    if s["cost_rmb"] is not None and s["cost_rmb"] >= 0:
        score += 40
    else:
        notes.append("缺少费用估算")
    if s["tokens_total"] and s["tokens_output"] is not None:
        total = max(s["tokens_total"], 1)
        ratio = s["tokens_output"] / total
        if 0.05 <= ratio <= 0.95:      # 输出占比在健康区间
            score += 30
        else:
            notes.append(f"输出/累计 token 占比 {ratio:.0%} 异常")
    else:
        notes.append("缺少 token 遥测")
    if s["rounds_actual"] > 0:
        score += 30
    else:
        notes.append("无实际讨论轮次")
    return round(max(0.0, min(score, 100.0)), 1), notes


def score_report(s: dict, raw_text: str = "") -> dict:
    """按五维度打分，返回带权重说明的报告。"""
    s["_raw"] = raw_text or ""
    dims = {}
    for key, fn in (("structure", _score_structure), ("completeness", _score_completeness),
                    ("engagement", _score_engagement), ("fairness", _score_fairness),
                    ("efficiency", _score_efficiency)):
        val, notes = fn(s)
        dims[key] = {"score": val, "notes": notes}
    overall = round(sum(dims[k]["score"] * WEIGHTS[k] for k in WEIGHTS), 1)
    return {"overall": overall, "dimensions": dims, "signals": s}


# ---------------------------------------------------------------- 展示

def format_text(report: dict) -> str:
    s = report["signals"]
    lines = [
        f"质量基线评估：overall {report['overall']}/100",
        f"主题：{s['topic'] or '（无）'}",
        f"轮次：{s['rounds_actual']}/{s['rounds_max']} 状态：{s['status']}/{s['phase']}",
        f"发言：{len(s['speeches'])} 条 / {len(s['speakers'])} 人 / {len(s['agents'])} Agent",
        f"收敛：{'是' if s['converged'] else '否'}（{s['convergence_mode'] or '—'}）",
        f"裁判：{'有' if s['has_judgment'] else '无'} / 结论：{'四段式' if s['has_conclusion'] else '缺失'}",
        f"公平性：{'通过' if s['fairness']['ok'] else '违约'} "
        f"（{s['fairness']['warnings']} 警告 / {s['fairness']['violations']} 违约）",
        f"Token：累计 {s['tokens_total']} / 输出 {s['tokens_output']} / 费用 ≈ ¥{s['cost_rmb'] or 0}",
        "",
        "维度得分：",
    ]
    for k, info in report["dimensions"].items():
        label = {"structure": "结构完整", "completeness": "完成度", "engagement": "参与度",
                 "fairness": "公平性", "efficiency": "效率"}[k]
        lines.append(f"  {label:<6} {info['score']:>6.1f}  （权重 {WEIGHTS[k]:.2f}）")
        for note in info["notes"][:6]:
            lines.append(f"      · {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 基线对比

def compare_baseline(report: dict, baseline: dict, tolerance: float = 5.0) -> dict:
    """与基线对比 overall 与各维度分，返回 deltas 与回归列表。"""
    cur = {"overall": report["overall"],
           **{k: report["dimensions"][k]["score"] for k in WEIGHTS}}
    ref = {"overall": baseline.get("overall", 0),
           **{k: baseline.get("dimensions", {}).get(k, {}).get("score", 0) for k in WEIGHTS}}
    deltas = {k: round(cur[k] - ref[k], 1) for k in cur}
    regress = [k for k, d in deltas.items() if d < -tolerance]
    return {"deltas": deltas, "regressed": regress, "tolerance": tolerance}


def format_baseline(diff: dict) -> str:
    lines = [f"基线对比（容差 {diff['tolerance']} 分）："]
    for k, d in diff["deltas"].items():
        flag = "  <<< 回归" if k in diff["regressed"] else ""
        lines.append(f"  {k:<12} {d:+.1f}{flag}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="讨论质量基线评估（P0-2）")
    ap.add_argument("file", help="讨论导出的 Markdown 文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    ap.add_argument("--baseline", metavar="JSON", help="与基线 JSON 对比，回归则退出码 2")
    ap.add_argument("--make-baseline", metavar="JSON", help="把当前报告写为基线文件")
    ap.add_argument("--tolerance", type=float, default=5.0, help="基线回归容差（默认 5 分）")
    args = ap.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"错误：文件不存在 {path}", file=sys.stderr)
        return 1
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"错误：读取失败 {e}", file=sys.stderr)
        return 1

    report = score_report(parse_export(raw), raw)

    if args.make_baseline:
        out = Path(args.make_baseline)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已生成基线：{out}")

    if args.json:
        payload = {"overall": report["overall"], "dimensions": report["dimensions"]}
        if args.baseline:
            payload["baseline"] = compare_baseline(report, _load_baseline(args.baseline),
                                                   args.tolerance)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_text(report))

    if args.baseline:
        diff = compare_baseline(report, _load_baseline(args.baseline), args.tolerance)
        if not args.json:
            print("\n" + format_baseline(diff))
        if diff["regressed"]:
            print(f"回归项：{', '.join(diff['regressed'])}", file=sys.stderr)
            return 2
    return 0


def _load_baseline(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"错误：基线文件读取失败 {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
