"""
review.py — 复盘导出（Markdown / JSON / 自包含 HTML）

「讨论记录」与「复盘」的区别正是本模块存在的理由：
  - 讨论记录（exporter.py）回答「说了什么」：发言、任务、裁决；
  - 复盘（本模块）回答「**为什么走到这一步**」：谁在第几轮改变了方向、中途指令
    在哪一轮生效、token 与耗时花在哪个模型上、迭代之间有什么差异。

三种导出格式：
  - `json`  机器可读，含运行摘要 + 事件时间线 + 指令 + 迭代链（供二次分析）
  - `md`    沿用 exporter 的发言记录，再追加「复盘补充」章节
  - `html`  自包含单文件（内联 CSS，无外链、无 JS 依赖），可直接发给他人查看

导出的文件统一落在 `data/reviews/`，文件名形如 `review_{trace_id}_{fmt}.{ext}`。
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any

from muti_llm_agent.config import REVIEWS_DIR, safe_path_component
from muti_llm_agent.exporter import render_markdown
from muti_llm_agent.logger import log
from muti_llm_agent.runlog import load_events
from muti_llm_agent.steering import KIND_LABELS
from muti_llm_agent.summary import build_summary

FORMATS = ("md", "json", "html")

# 事件类型 → 复盘时间线里的中文动作名
EVENT_LABELS = {
    "run_start": "开始运行",
    "run_end": "结束运行",
    "round_start": "进入轮次",
    "speech": "完成发言",
    "speech_start": "开始发言",
    "task_assigned": "分派任务",
    "task_result": "任务回报",
    "directive_applied": "指令生效",
    "judge": "裁判裁决",
    "summary": "生成总结",
    "error": "异常",
    "followup": "补充材料续轮",
    "iteration": "迭代切换",
}


def _ts_text(ms: Any) -> str:
    """毫秒时间戳 → HH:MM:SS（复盘时间线用）。"""
    try:
        return time.strftime("%H:%M:%S", time.localtime(int(ms) / 1000))
    except Exception:
        return "--:--:--"


def build_timeline(trace_id: str, limit: int = 400) -> list[dict]:
    """从运行事件日志提取时间线（只保留有信息量的事件类型）。"""
    events = load_events(trace_id)
    header_ts = events[0].get("ts", 0) if events else 0
    out: list[dict] = []
    for ev in events:
        etype = str(ev.get("type", ""))
        if etype in ("header",):
            continue
        label = EVENT_LABELS.get(etype, etype)
        text = ev.get("text") or ev.get("summary") or ""
        if not text:
            if etype == "speech":
                text = f"{ev.get('speaker', '')}：{len(str(ev.get('chars', '')))} 字"
            elif etype == "directive_applied":
                text = f"{ev.get('kind_label', ev.get('kind', ''))}：{ev.get('text', '')}"
            elif etype == "task_assigned":
                text = f"{ev.get('from', '')} → {ev.get('to', '') or '任务池'}：{ev.get('content', '')}"
            elif etype == "round_start":
                text = f"第 {ev.get('round', '')}/{ev.get('max', '')} 轮"
            else:
                text = ""
        out.append({
            "seq": ev.get("seq", 0),
            "ts": ev.get("ts", 0),
            "time": _ts_text(ev.get("ts", 0)),
            "offset_ms": max(0, int(ev.get("ts", 0) or 0) - int(header_ts or 0)),
            "type": etype,
            "label": label,
            "round": ev.get("round", 0),
            "agent": ev.get("speaker") or ev.get("agent") or ev.get("from") or "",
            "text": str(text)[:500],
        })
    if len(out) > limit:
        # 保留头尾：开场的准备动作和收尾的裁决/总结都不能丢
        head = out[: limit // 4]
        tail = out[-(limit - len(head)):]
        skipped = len(out) - len(head) - len(tail)
        elision = {"seq": -1, "ts": 0, "time": "...", "offset_ms": 0,
                   "type": "elision", "label": "省略",
                   "round": 0, "agent": "",
                   "text": f"（中间省略 {skipped} 条事件）"}
        out = [*head, elision, *tail]
    return out


def build_review(orch: Any) -> dict:
    """构建复盘的完整结构化对象。"""
    summary = build_summary(orch)
    trace_id = summary.get("trace_id", "")
    steering = getattr(orch, "steering", None)
    return {
        "kind": "run_review",
        "generated_at": int(time.time() * 1000),
        "summary": summary,
        "timeline": build_timeline(trace_id) if trace_id else [],
        "directives": steering.all() if steering is not None else [],
        "transcript": list(getattr(orch, "transcript", []) or []),
        "tasks": list(getattr(orch, "tasks", []) or []),
        "iterations": list(getattr(orch, "iterations", []) or []),
        "judgment": _judgment_view(getattr(orch, "judgment", None)),
    }


def _judgment_view(judgment) -> dict | None:
    if judgment is None:
        return None
    return {
        "scores": getattr(judgment, "scores", {}) or {},
        "agent_scores": getattr(judgment, "agent_scores", {}) or {},
        "model_performance": getattr(judgment, "model_performance", {}) or {},
        "composite_scores": getattr(judgment, "composite_scores", {}) or {},
        "winner": getattr(judgment, "winner", ""),
        "critique": getattr(judgment, "critique", ""),
        "convergence_flag": bool(getattr(judgment, "convergence_flag", False)),
    }


def render_markdown_review(orch: Any) -> str:
    """Markdown 复盘 = 原有讨论记录 + 复盘补充章节。"""
    base = render_markdown(orch)
    summary = build_summary(orch)
    steering = getattr(orch, "steering", None)
    iterations = getattr(orch, "iterations", []) or []

    lines: list[str] = [base, "", "---", "", "## 复盘摘要", ""]
    lines.append(f"- 运行标识：{summary.get('session_id')} / trace `{summary.get('trace_id')}`")
    lines.append(f"- 模型阵容：{'、'.join(summary.get('models_used') or []) or '（无）'}")
    lines.append(f"- 耗时：{summary.get('elapsed_ms', 0) / 1000:.1f} 秒"
                 f"｜轮次 {summary.get('rounds_executed')}/{summary.get('max_rounds')}")
    tokens = summary.get("tokens", {})
    lines.append(f"- Token：总量 {tokens.get('total', 0)}"
                 f"（输出 {tokens.get('output', 0)} / 估算输入 {tokens.get('input_est', 0)}）"
                 f"｜预估费用 ≈ ¥{float(summary.get('cost_rmb', 0) or 0):.4f}")
    lines.append(f"- 中途指令：{summary.get('steering', {}).get('added', 0)} 条"
                 f"（生效 {summary.get('steering', {}).get('injected', 0)} 次）")
    if summary.get("error_count"):
        lines.append(f"- 异常：{summary['error_count']} 条（详见运行日志）")
    lines.append("")
    lines.append("### 关键节点")
    for h in summary.get("highlights", []):
        lines.append(f"- {h}")
    lines.append("")

    lines.append("### 各模型席位指标")
    lines.append("| 席位 | 模型 | 发言次数 | 字符数 | 平均TTFT | 生成速率 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for a in summary.get("agents", []):
        ttft = f"{a['avg_ttft_ms']}ms" if a.get("avg_ttft_ms") is not None else "-"
        rate = f"{a['avg_gen_rate']}字/s" if a.get("avg_gen_rate") is not None else "-"
        lines.append(f"| {a.get('name')} | {a.get('model_id') or '-'} | {a.get('speeches', 0)} "
                     f"| {a.get('chars', 0)} | {ttft} | {rate} |")
    lines.append("")

    items = steering.summary_lines() if steering is not None else []
    lines.append("### 中途注入纠偏记录")
    if items:
        lines.extend(items)
    else:
        lines.append("（本次运行未收到中途指令）")
    lines.append("")

    lines.append("### 迭代链（补充材料续轮）")
    if iterations:
        for it in iterations:
            lines.append(f"- 第 {it.get('iteration')} 轮迭代｜trace `{it.get('trace_id')}`"
                         f"｜{it.get('rounds', 0)} 轮｜输出 {it.get('output_tokens', 0)} tokens"
                         f"｜{it.get('status', '')}")
            if it.get("focus"):
                lines.append(f"  - 本轮焦点：{it['focus']}")
            if it.get("materials_chars"):
                lines.append(f"  - 补充材料：{it['materials_chars']} 字")
            if it.get("conclusion_core"):
                lines.append(f"  - 结论摘要：{str(it['conclusion_core'])[:200]}")
    else:
        lines.append("（未进行补充材料续轮）")
    lines.append("")

    lines.append("### 事件时间线（节选）")
    for ev in build_timeline(summary.get("trace_id", ""), limit=120):
        if ev["type"] == "elision":
            lines.append(f"- …{ev['text']}")
            continue
        who = f" @{ev['agent']}" if ev.get("agent") else ""
        rnd = f"[第{ev['round']}轮]" if ev.get("round") else ""
        lines.append(f"- `{ev['time']}` {rnd}{who} {ev['label']}：{ev['text']}")
    return "\n".join(lines)


# ---------------- 自包含 HTML 复盘 ----------------
_HTML_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:28px;font:14px/1.65 -apple-system,"Segoe UI","PingFang SC",
 "Microsoft YaHei",sans-serif;background:#f6f7f9;color:#1f2933}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:#66707a;font-size:13px;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:0 0 22px}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:12px 14px}
.card .k{color:#7b8794;font-size:12px}
.card .v{font-size:20px;font-weight:600;margin-top:2px}
section{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:16px 18px;margin:0 0 16px}
h2{font-size:15px;margin:0 0 12px;padding-bottom:8px;border-bottom:1px solid #eef1f4}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eef1f4;vertical-align:top}
th{color:#7b8794;font-weight:500;background:#fafbfc}
code{background:#eef1f4;padding:1px 5px;border-radius:4px;font-size:12px}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;
 background:#eef1f4;color:#42505c}
.tag.correct{background:#fdecec;color:#b42318}
.tag.constrain{background:#fff4e5;color:#b25e00}
.tag.fact{background:#e8f4fd;color:#0b6bb4}
.tag.redirect{background:#eef0ff;color:#3b3fa8}
.tag.stop{background:#f3e8fd;color:#7b2ea8}
pre{white-space:pre-wrap;word-break:break-word;background:#fafbfc;border:1px solid #eef1f4;
 border-radius:8px;padding:12px;font-size:12.5px;max-height:520px;overflow:auto}
.hl li{margin:2px 0}
"""


def _e(value: Any) -> str:
    """HTML 转义（所有外部文本必须转义，避免复盘页成为 XSS 载体）。"""
    return html.escape("" if value is None else str(value))


def render_html_review(orch: Any, markdown_text: str | None = None) -> str:
    """渲染自包含 HTML 复盘页（无外链 CSS/JS）。"""
    summary = build_summary(orch)
    md = markdown_text if markdown_text is not None else render_markdown_review(orch)
    steering = getattr(orch, "steering", None)
    directives = steering.all() if steering is not None else []
    iterations = getattr(orch, "iterations", []) or []
    tokens = summary.get("tokens", {}) or {}

    cards = [
        ("轮次", f"{summary.get('rounds_executed')}/{summary.get('max_rounds')}"),
        ("参与模型", len(summary.get("models_used") or [])),
        ("耗时", f"{summary.get('elapsed_ms', 0) / 1000:.1f}s"),
        ("输出 Token", tokens.get("output", 0)),
        ("预估费用", f"¥{float(summary.get('cost_rmb', 0) or 0):.4f}"),
        ("中途指令", summary.get("steering", {}).get("added", 0)),
    ]
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>复盘 · {_e(summary.get('topic', '多模型协同'))}</title>",
        f"<style>{_HTML_CSS}</style></head><body><div class=\"wrap\">",
        "<h1>多模型协同 · 复盘报告</h1>",
        f'<div class="sub">{_e(summary.get("topic", ""))}｜'
        f'状态 {_e(summary.get("status"))}｜模式 {_e(summary.get("mode"))}｜'
        f'trace <code>{_e(summary.get("trace_id"))}</code>｜'
        f'{time.strftime("%Y-%m-%d %H:%M:%S")}</div>',
        '<div class="cards">',
    ]
    for k, v in cards:
        parts.append(f'<div class="card"><div class="k">{_e(k)}</div>'
                     f'<div class="v">{_e(v)}</div></div>')
    parts.append("</div>")

    # 关键节点
    parts.append('<section><h2>关键节点</h2><ul class="hl">')
    for h in summary.get("highlights", []):
        parts.append(f"<li>{_e(h)}</li>")
    parts.append("</ul></section>")

    # 模型指标
    parts.append("<section><h2>各模型席位指标</h2><table><tr><th>席位</th><th>模型</th>"
                 "<th>立场</th><th>发言</th><th>字符</th><th>平均TTFT</th>"
                 "<th>生成速率</th></tr>")
    for a in summary.get("agents", []):
        ttft = f"{a['avg_ttft_ms']}ms" if a.get("avg_ttft_ms") is not None else "-"
        rate = f"{a['avg_gen_rate']}字/s" if a.get("avg_gen_rate") is not None else "-"
        main_tag = ' <span class="tag">主</span>' if a.get("is_main") else ""
        parts.append(
            f"<tr><td>{_e(a.get('name'))}{main_tag}</td>"
            f"<td><code>{_e(a.get('model_id') or '-')}</code></td>"
            f"<td>{_e(a.get('stance'))}</td><td>{a.get('speeches', 0)}</td>"
            f"<td>{a.get('chars', 0)}</td><td>{_e(ttft)}</td><td>{_e(rate)}</td></tr>")
    parts.append("</table></section>")

    # 指令
    parts.append("<section><h2>中途注入纠偏记录</h2>")
    if directives:
        parts.append("<table><tr><th>类型</th><th>作用域</th><th>生效轮次</th>"
                     "<th>内容</th><th>状态</th></tr>")
        for d in directives:
            kind = d.get("kind", "")
            scope = d.get("scope", "")
            if scope == "agent" and d.get("target"):
                scope = f"agent:{d['target']}"
            parts.append(
                f"<tr><td><span class=\"tag {_e(kind)}\">"
                f"{_e(KIND_LABELS.get(kind, kind))}</span></td>"
                f"<td>{_e(scope)}</td><td>{_e(d.get('applied_round') or '-')}</td>"
                f"<td>{_e(d.get('text'))}</td><td>{_e(d.get('status'))}</td></tr>")
        parts.append("</table>")
    else:
        parts.append('<div class="sub">本次运行未收到中途指令</div>')
    parts.append("</section>")

    # 迭代链
    if iterations:
        parts.append("<section><h2>迭代链（补充材料续轮）</h2><table>"
                     "<tr><th>迭代</th><th>trace</th><th>轮次</th><th>输出</th>"
                     "<th>焦点</th><th>材料</th></tr>")
        for it in iterations:
            parts.append(
                f"<tr><td>{_e(it.get('iteration'))}</td>"
                f"<td><code>{_e(it.get('trace_id'))}</code></td>"
                f"<td>{_e(it.get('rounds'))}</td>"
                f"<td>{_e(it.get('output_tokens'))}</td>"
                f"<td>{_e(it.get('focus') or '-')}</td>"
                f"<td>{_e(it.get('materials_chars') or 0)} 字</td></tr>")
        parts.append("</table></section>")

    # 时间线
    parts.append("<section><h2>事件时间线</h2><table>"
                 "<tr><th>时间</th><th>轮次</th><th>动作</th><th>对象</th><th>说明</th></tr>")
    for ev in build_timeline(summary.get("trace_id", ""), limit=300):
        if ev["type"] == "elision":
            parts.append(f'<tr><td colspan="5">{_e(ev["text"])}</td></tr>')
            continue
        parts.append(
            f"<tr><td><code>{_e(ev['time'])}</code></td>"
            f"<td>{_e(ev.get('round') or '-')}</td>"
            f"<td>{_e(ev['label'])}</td>"
            f"<td>{_e(ev.get('agent') or '-')}</td>"
            f"<td>{_e(ev['text'])}</td></tr>")
    parts.append("</table></section>")

    if summary.get("errors"):
        parts.append("<section><h2>异常记录</h2><ul>")
        for err in summary["errors"]:
            parts.append(f"<li>{_e(err)}</li>")
        parts.append("</ul></section>")

    parts.append("<section><h2>完整讨论记录（Markdown）</h2>"
                 f"<pre>{_e(md)}</pre></section>")
    parts.append('<div class="sub">由 muti_llm_agent 自动生成</div>')
    parts.append("</div></body></html>")
    return "\n".join(parts)


# ---------------- 保存 ----------------
def review_path(trace_id: str, fmt: str) -> Path:
    """复盘文件路径（trace_id 清洗为安全文件名）。"""
    fmt = fmt if fmt in FORMATS else "md"
    ext = {"md": "md", "json": "json", "html": "html"}[fmt]
    return REVIEWS_DIR / f"review_{safe_path_component(trace_id, 'unknown')}.{ext}"


def save_review(orch: Any, fmt: str = "md") -> Path:
    """把复盘写入 data/reviews/，返回文件路径。"""
    fmt = fmt if fmt in FORMATS else "md"
    trace_id = getattr(orch, "trace_id", "") or "unknown"
    path = review_path(trace_id, fmt)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        payload = build_review(orch)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    elif fmt == "html":
        path.write_text(render_html_review(orch), encoding="utf-8")
    else:
        path.write_text(render_markdown_review(orch), encoding="utf-8")
    log("info", "review.save", f"复盘已导出（{fmt}）", path=str(path),
        trace_id=trace_id, fmt=fmt)
    return path


def export_content(orch: Any, fmt: str = "md") -> tuple[str, str, str]:
    """返回 (内容, 媒体类型, 建议文件名)，供 HTTP 接口直接回包。"""
    trace_id = getattr(orch, "trace_id", "") or "unknown"
    if fmt == "json":
        payload = build_review(orch)
        return (json.dumps(payload, ensure_ascii=False, indent=2),
                "application/json; charset=utf-8",
                f"review_{safe_path_component(trace_id, 'unknown')}.json")
    if fmt == "html":
        return (render_html_review(orch), "text/html; charset=utf-8",
                f"review_{safe_path_component(trace_id, 'unknown')}.html")
    return (render_markdown_review(orch), "text/markdown; charset=utf-8",
            f"review_{safe_path_component(trace_id, 'unknown')}.md")
