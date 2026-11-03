"""
api/router_kb.py — 共享知识库

职责：把外部资料（手写文本 / 上传文件）解析成段落并写入共享向量库，
供所有席位在发言时检索。对应端点：/api/ingest、/api/upload、/api/kb/stats
"""
from __future__ import annotations

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse

from muti_llm_agent.file_parser import (
    MAX_UPLOAD_BYTES,
    SUPPORTED_EXTS,
    ParseError,
    parse_file,
    split_paragraphs,
)
from muti_llm_agent.schemas import IngestRequest
from muti_llm_agent.vector_store import shared_vector_store

MODULE_NAME = "kb"
MODULE_TITLE = "共享知识库"
MODULE_DESC = "文本注入、文件上传解析、向量库统计"

router = APIRouter()


@router.post("/api/ingest", summary="注入共享知识（纯文本）")
async def ingest(payload: IngestRequest):
    text = payload.text.strip()
    source = payload.source or "用户注入"
    if not text:
        return JSONResponse({"ok": False, "error": "文本为空"}, status_code=400)
    # 与 /api/upload 共用同一套分块规则（file_parser.split_paragraphs），
    # 避免手工注入与文件上传两条路径切块粒度不一致。
    chunks = split_paragraphs(text, source)
    if not chunks:
        return JSONResponse({"ok": False, "error": "文本为空"}, status_code=400)
    shared_vector_store.add(chunks, [{"source": source} for _ in chunks])
    return {"ok": True, "added": len(chunks), "backend": shared_vector_store.backend}


@router.post("/api/upload", summary="上传文档并解析注入共享知识库")
async def upload_file(file: UploadFile = File(...)):  # noqa: B008  FastAPI 标准注入写法
    """上传文档 / 表格 / PDF / 图片，解析为文本后注入共享向量库。"""
    if not file.filename:
        return JSONResponse({"ok": False, "error": "缺少文件名"}, status_code=400)

    # 大小预检（两道）：先看声明的 size（Content-Length），再按上限 +1 字节读取，
    # 避免超大文件先被整块读进内存、再由 parse_file 事后拒绝。
    limit_msg = f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限"
    declared = getattr(file, "size", None)
    if isinstance(declared, int) and declared > MAX_UPLOAD_BYTES:
        return JSONResponse({"ok": False, "error": limit_msg}, status_code=413)
    try:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
    except Exception:
        return JSONResponse({"ok": False, "error": "读取上传文件失败"}, status_code=400)
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse({"ok": False, "error": limit_msg}, status_code=413)

    try:
        text, meta = parse_file(file.filename, data)
    except ParseError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    if not text.strip():
        return JSONResponse(
            {"ok": False, "error": "未能从文件中提取到文本内容（可能是扫描件/图片型 PDF）"},
            status_code=400)

    source = f"{meta['source']}（文件上传）"
    chunks = split_paragraphs(text, source)
    shared_vector_store.add(chunks, [{"source": source} for _ in chunks])
    return {
        "ok": True,
        "added": len(chunks),
        "chars": meta["chars"],
        "truncated": meta["truncated"],
        "backend": shared_vector_store.backend,
        "source": source,
    }


@router.get("/api/kb/stats", summary="共享知识库统计")
async def kb_stats():
    """向量库后端 / 当前条目数（只读，供接口控制台与复盘页确认资料是否注入成功）。"""
    count = None
    for attr in ("count", "size", "__len__"):
        fn = getattr(shared_vector_store, attr, None)
        if callable(fn):
            try:
                count = fn()
                break
            except Exception:
                count = None
    return {
        "ok": True,
        "backend": shared_vector_store.backend,
        "chunks": count,
        "upload_exts": SUPPORTED_EXTS,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    }
