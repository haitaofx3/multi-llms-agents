"""
api/router_export.py — 导出、历史与复盘

三件不同的事，刻意放在同一个模块里，因为它们共享「读取一次运行的结果」这条路径：

  - 导出（export）  把当前运行渲染成 md / json / html，用于带走与归档；
  - 历史（history） 列表 + 查看已落盘的讨论记录（data/discussions）；
  - 复盘（review）  把运行摘要 + 事件时间线 + 指令 + 迭代链打包，用于「事后搞清楚
                    为什么走到这一步」。

兼容性约定：`GET /api/export` 不带参数时仍然返回 `{"markdown": "..."}` ——
既有前端与测试依赖这个形状，新增格式一律通过 `format` 参数显式选择。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from muti_llm_agent.api.deps import (
    DISCUSSIONS_DIR,
    existing_orch,
    normalize_sid,
    resolve_sid,
    ts,
)
from muti_llm_agent.history import get_markdown, list_discussions, record_discussion
from muti_llm_agent.logger import log
from muti_llm_agent.review import (
    FORMATS,
    build_review,
    export_content,
    render_html_review,
    render_markdown_review,
    save_review,
)
from muti_llm_agent.schemas import ExportRequest, SaveMarkdownRequest

MODULE_NAME = "export"
MODULE_TITLE = "导出与复盘"
MODULE_DESC = "Markdown / JSON / HTML 导出、历史记录、复盘报告与落盘"

router = APIRouter()


def _safe_discussion_filename(fname: str) -> str:
    """清洗讨论导出文件名，防路径穿越。

    只保留 basename（丢弃目录部分 / 绝对路径 / `..`），并强制 `.md` 后缀；
    清洗后为空则回退为自动生成的默认文件名。
    """
    if fname:
        fname = Path(fname).name
    if not fname:
        fname = f"discussion_{ts()}.md"
    if not fname.endswith(".md"):
        fname += ".md"
    return fname


def _record(orch, filename: str) -> None:
    """把一次运行写进历史讨论索引（供后续检索）。"""
    record_discussion(
        topic=orch.topic,
        trace_id=orch.trace_id,
        filename=filename,
        status=orch.status,
        phase=orch.phase,
        round_no=orch.round,
        max_rounds=orch.max_rounds,
        token_spent=orch.spent,
        agents=[{"name": a.name, "role": a.role, "stance": a.stance,
                 "is_main": a.is_main} for a in orch.agents],
    )


# ---------------- 导出 ----------------
@router.get("/api/export", summary="导出运行记录（md 为默认，兼容旧契约）")
async def export(session_id: str = "default", format: str = "md",
                 download: int = 0, mode: str = ""):
    """导出当前运行。

    - `format=md`（默认）→ JSON `{"markdown": ...}`（兼容旧前端）
    - `format=json`        → `{"ok":true,"review":{...}}`
    - `format=html`        → 自包含 HTML（可直接浏览器打开 / 发送给他人）
    - `download=1`         → 以附件形式返回原始内容（md / html）
    - `mode=review`        → md 走「复盘版」（含复盘补充章节）
    """
    fmt = (format or "md").lower()
    if fmt not in FORMATS:
        return JSONResponse({"ok": False, "error": f"format 必须是 {list(FORMATS)} 之一"},
                            status_code=400)
    orch = existing_orch(session_id)
    if orch is None:
        if fmt == "md":
            return JSONResponse({"markdown": ""})
        return JSONResponse({"ok": False, "error": "会话不存在或已过期"}, status_code=404)

    if fmt == "json":
        return JSONResponse({"ok": True, "review": build_review(orch)})
    if fmt == "html":
        html_text = render_html_review(orch)
        if download:
            return HTMLResponse(
                html_text,
                headers={"Content-Disposition":
                         f'attachment; filename="review_{orch.trace_id or "unknown"}.html"'})
        return HTMLResponse(html_text)

    md = render_markdown_review(orch) if mode == "review" else orch.export_markdown()
    if download:
        return PlainTextResponse(
            md,
            headers={"Content-Disposition":
                     f'attachment; filename="discussion_{orch.trace_id or ts()}.md"'})
    return JSONResponse({"markdown": md})


@router.post("/api/save_markdown", summary="保存讨论记录到 data/discussions")
async def save_markdown(session_id: str = "default", payload: SaveMarkdownRequest = None):
    payload = payload or SaveMarkdownRequest()
    # session_id 可能来自查询串（前端 save 调用只带 query，不带 body）
    orch = existing_orch(resolve_sid(payload, session_id))
    if orch is None:
        return JSONResponse({"ok": False, "error": "会话不存在或已过期"}, status_code=404)
    md = orch.export_markdown()
    fname = _safe_discussion_filename(payload.filename)
    path = DISCUSSIONS_DIR / fname
    path.write_text(md, encoding="utf-8")
    _record(orch, fname)
    return {"ok": True, "path": str(path), "filename": fname}


# ---------------- 复盘 ----------------
@router.get("/api/review", summary="复盘报告（结构化）")
async def review_json(session_id: str = "default", trace_id: str = ""):
    """返回复盘对象：运行摘要 + 时间线 + 指令 + 转录 + 迭代链。"""
    if trace_id:
        from muti_llm_agent.review import build_timeline
        from muti_llm_agent.runlog import get_run
        return {"ok": True, "trace_id": trace_id,
                "index": get_run(trace_id),
                "timeline": build_timeline(trace_id)}
    orch = existing_orch(session_id)
    if orch is None:
        return JSONResponse({"ok": False, "error": "会话不存在或已过期"}, status_code=404)
    return {"ok": True, "review": build_review(orch)}


@router.get("/api/review/export", summary="导出复盘（md / json / html）")
async def review_export(session_id: str = "default", format: str = "md", download: int = 1):
    fmt = (format or "md").lower()
    if fmt not in FORMATS:
        return JSONResponse({"ok": False, "error": f"format 必须是 {list(FORMATS)} 之一"},
                            status_code=400)
    orch = existing_orch(session_id)
    if orch is None:
        return JSONResponse({"ok": False, "error": "会话不存在或已过期"}, status_code=404)
    content, media_type, filename = export_content(orch, fmt)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'} if download else {}
    return PlainTextResponse(content, media_type=media_type, headers=headers)


@router.post("/api/review/save", summary="把复盘落盘到 data/reviews")
async def review_save(payload: ExportRequest):
    orch = existing_orch(payload.session_id)
    if orch is None:
        return JSONResponse({"ok": False, "error": "会话不存在或已过期"}, status_code=404)
    try:
        path = save_review(orch, payload.format)
    except Exception as e:
        log("error", "review.save_failed", f"复盘落盘失败：{e}", error=str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "path": str(path), "filename": path.name,
            "format": payload.format, "trace_id": orch.trace_id}


# ---------------- 历史 ----------------
@router.get("/api/history", summary="历史讨论列表")
async def history(q: str = ""):
    """历史讨论列表；q 为可选关键词（按题目 / trace_id 过滤）。"""
    return {"ok": True, "items": list_discussions(q)}


@router.get("/api/history/{filename}", summary="查看某场历史讨论的 Markdown")
async def history_view(filename: str):
    """查看某场历史讨论的完整 Markdown 内容。"""
    md = get_markdown(filename)
    if md is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    return {"ok": True, "markdown": md, "filename": filename}


@router.get("/api/history/{filename}/review", summary="把历史记录当作复盘查看")
async def history_review(filename: str):
    """历史记录没有编排器实例，只能返回已落盘的 Markdown + 索引元数据。"""
    md = get_markdown(filename)
    if md is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    meta: dict = next(
        (r for r in list_discussions() if r.get("filename") == filename), {})
    return {"ok": True, "filename": filename, "meta": meta, "markdown": md}


@router.get("/api/history/{filename}/download", summary="下载某场历史讨论")
async def history_download(filename: str):
    md = get_markdown(filename)
    if md is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    return PlainTextResponse(
        md, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{Path(filename).name}"'})


@router.get("/api/workspace/summary", summary="工作区复盘总览（最近若干场运行）")
async def workspace_summary(limit: int = 10):
    """把最近若干场运行的关键指标摊平，用于「这一周协同得怎么样」这类回顾。"""
    items = []
    for rec in list_discussions()[:max(1, int(limit))]:
        items.append({
            "filename": rec.get("filename", ""),
            "topic": rec.get("topic", ""),
            "trace_id": rec.get("trace_id", ""),
            "timestamp": rec.get("timestamp", ""),
            "round": rec.get("round", 0),
            "max_rounds": rec.get("max_rounds", 0),
            "token_spent": rec.get("token_spent", 0),
            "status": rec.get("status", ""),
            "agents": rec.get("agents", []),
        })
    return {"ok": True, "items": items,
            "session": normalize_sid("default"), "generated_at": ts()}
