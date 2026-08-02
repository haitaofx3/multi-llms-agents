"""
backend.py — FastAPI 后端

提供：
  - 静态 UI 服务（圆桌界面）
  - REST 接口：配置 Agent / 注入知识 / 开始·暂停·继续·结束讨论 / 导出 Markdown
  - WebSocket(/ws)：把讨论事件实时推送到前端
"""
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings, BASE_DIR
from llm import PROVIDER_PRESETS
from vector_store import shared_vector_store
from orchestrator import orchestrator

STATIC_DIR = BASE_DIR / "static"
DISCUSSIONS_DIR = BASE_DIR / "discussions"
DISCUSSIONS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="多 Agent 圆桌协作系统")

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------- 启动：监听"完成"事件，自动保存 Markdown ----------------
@app.on_event("startup")
async def _startup():
    q = orchestrator.event_bus.subscribe()

    async def _listener():
        while True:
            ev = await q.get()
            if ev.get("type") == "status" and ev.get("status") == "done":
                try:
                    md = orchestrator.export_markdown()
                    fname = f"discussion_{_ts()}.md"
                    (DISCUSSIONS_DIR / fname).write_text(md, encoding="utf-8")
                except Exception:
                    pass

    asyncio.create_task(_listener())


def _ts():
    import time
    return time.strftime("%Y%m%d_%H%M%S")


# ---------------- 页面 ----------------
@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---------------- 默认配置 ----------------
@app.get("/api/defaults")
async def defaults():
    return {
        "providers": PROVIDER_PRESETS,
        "default_provider": "default",
        "vector_backend": shared_vector_store.backend,
        "max_agents": 6,
    }


# ---------------- 配置 Agent ----------------
@app.post("/api/configure")
async def configure(payload: dict):
    agents = payload.get("agents", [])
    if not (1 <= len(agents) <= 6):
        return JSONResponse({"ok": False, "error": "Agent 数量需在 1-6 之间"}, status_code=400)
    topic = payload.get("topic", "")
    max_rounds = int(payload.get("max_rounds", 8))
    orchestrator.configure(agents, topic=topic, max_rounds=max_rounds)
    return {
        "ok": True,
        "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                    "model_id": a.model_config["model_id"]} for a in orchestrator.agents],
        "status": orchestrator.status,
    }


# ---------------- 注入共享知识 ----------------
@app.post("/api/ingest")
async def ingest(payload: dict):
    text = payload.get("text", "").strip()
    source = payload.get("source", "用户注入")
    if not text:
        return JSONResponse({"ok": False, "error": "文本为空"}, status_code=400)
    # 按段落切分
    chunks = [t for t in text.split("\n") if t.strip()]
    if not chunks:
        chunks = [text]
    shared_vector_store.add(chunks, [{"source": source} for _ in chunks])
    return {"ok": True, "added": len(chunks), "backend": shared_vector_store.backend}


# ---------------- 开始讨论 ----------------
@app.post("/api/start")
async def start(payload: dict):
    if orchestrator.status == "running":
        return JSONResponse({"ok": False, "error": "讨论进行中，请先结束或等待"}, status_code=400)
    if not orchestrator.agents:
        return JSONResponse({"ok": False, "error": "请先配置 Agent"}, status_code=400)
    topic = payload.get("topic") or orchestrator.topic
    max_rounds = payload.get("max_rounds")
    asyncio.create_task(orchestrator.run_discussion(topic=topic, max_rounds=max_rounds))
    return {"ok": True, "status": "running"}


@app.post("/api/pause")
async def pause():
    orchestrator.pause()
    return {"ok": True, "status": orchestrator.status}


@app.post("/api/resume")
async def resume():
    orchestrator.resume()
    return {"ok": True, "status": orchestrator.status}


@app.post("/api/end")
async def end():
    orchestrator.end()
    return {"ok": True, "status": orchestrator.status}


# ---------------- 状态 ----------------
@app.get("/api/status")
async def status():
    return {
        "status": orchestrator.status,
        "round": orchestrator.round,
        "max_rounds": orchestrator.max_rounds,
        "topic": orchestrator.topic,
        "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main} for a in orchestrator.agents],
        "tasks": orchestrator.tasks,
        "shared_context": orchestrator.shared_context,
    }


# ---------------- 导出 Markdown ----------------
@app.get("/api/export")
async def export():
    md = orchestrator.export_markdown()
    return JSONResponse({"markdown": md})


@app.post("/api/save_markdown")
async def save_markdown(payload: dict = None):
    md = orchestrator.export_markdown()
    fname = payload.get("filename") if payload else None
    if not fname:
        fname = f"discussion_{_ts()}.md"
    if not fname.endswith(".md"):
        fname += ".md"
    path = DISCUSSIONS_DIR / fname
    path.write_text(md, encoding="utf-8")
    return {"ok": True, "path": str(path), "filename": fname}


# ---------------- WebSocket 实时事件 ----------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    q = orchestrator.event_bus.subscribe()
    try:
        # 连接时立即推送当前状态
        await websocket.send_json({
            "type": "status", "status": orchestrator.status,
            "topic": orchestrator.topic, "max_rounds": orchestrator.max_rounds,
            "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main} for a in orchestrator.agents],
        })
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        orchestrator.event_bus.unsubscribe(q)
    except Exception:
        orchestrator.event_bus.unsubscribe(q)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
