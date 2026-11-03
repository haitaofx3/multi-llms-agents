"""
api/router_dev.py — 接口级测试与模块化调试（开发控制台后端）

三个端点解决「改完代码不知道有没有打断别的接口」的问题：

  - `GET  /api/dev/endpoints`  把全部接口按**模块**分组导出（含请求 JSON Schema
                               与示例请求体），前端控制台据此自动生成调用表单；
  - `POST /api/dev/invoke`     通用调用器：用 httpx.ASGITransport 走**进程内真实
                               ASGI 链路**（中间件 / 鉴权 / 依赖注入全部生效），
                               因此控制台调通 = 外部调通，不会出现「本地能调、
                               线上 404」的假阳性；
  - `POST /api/dev/smoke`      一键冒烟：跑一组只读接口，返回通过率与逐项耗时。

另外提供 `GET /console` 页面本身（见文件末尾），以及按模块过滤的
`GET /api/dev/modules`。生产环境可用 `DEV_API_ENABLED=0` 关闭整组接口。
"""
from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from muti_llm_agent.api.deps import STATIC_DIR
from muti_llm_agent.config import settings
from muti_llm_agent.schemas import DevInvokeRequest

MODULE_NAME = "dev"
MODULE_TITLE = "接口调试"
MODULE_DESC = "接口清单、通用调用器、一键冒烟、接口控制台页面"

router = APIRouter()

# 冒烟测试覆盖的只读接口（不消耗模型额度、不改变运行状态）
SMOKE_TARGETS: list[tuple[str, str, dict]] = [
    ("默认配置", "GET", {}),
    ("模型花名册", "GET", {}),
    ("协同配方", "GET", {}),
    ("运行状态", "GET", {}),
    ("运行摘要", "GET", {}),
    ("知识库统计", "GET", {}),
    ("结构化日志", "GET", {}),
    ("历史讨论", "GET", {}),
    ("运行索引", "GET", {}),
    ("工作区概览", "GET", {}),
]

# 对应路径（与上面的名称一一对应，避免元组里塞路径后难以阅读）
SMOKE_PATHS = [
    "/api/defaults", "/api/models", "/api/recipes", "/api/status", "/api/summary",
    "/api/kb/stats", "/api/logs", "/api/history", "/api/runs", "/api/workspace",
]

# 会改变状态或消耗额度的接口：控制台里要高亮提示
_DANGEROUS = {
    ("POST", "/api/start"), ("POST", "/api/end"), ("POST", "/api/configure"),
    ("POST", "/api/followup"), ("POST", "/api/upload"), ("POST", "/api/ingest"),
    ("POST", "/api/steer"), ("DELETE", "/api/steer/{directive_id}"),
    ("POST", "/api/save_markdown"), ("POST", "/api/review/save"),
    ("POST", "/api/dev/invoke"), ("POST", "/api/integrations/feishu/push"),
    ("POST", "/api/integrations/dsh/invoke"),
    ("POST", "/api/integrations/webhooks/dispatch"),
    ("POST", "/api/integrations/webhooks/test"),
}


def _guard():
    """开发接口开关（关闭时统一返回 403）。"""
    if not settings.DEV_API_ENABLED:
        return JSONResponse(
            {"ok": False, "error": "开发调试接口已关闭（DEV_API_ENABLED=0）"},
            status_code=403)
    return None


def _example_from_schema(schema: dict | None) -> dict:
    """按 JSON Schema 生成一份可用的示例请求体（只取一层，够用即可）。"""
    if not schema or not isinstance(schema, dict):
        return {}
    props = schema.get("properties") or {}
    out = {}
    for key, spec in props.items():
        if not isinstance(spec, dict):
            continue
        if "default" in spec:
            out[key] = spec["default"]
        elif spec.get("enum"):
            out[key] = spec["enum"][0]
        else:
            t = spec.get("type")
            if t == "string":
                out[key] = ""
            elif t == "integer":
                out[key] = 0
            elif t == "number":
                out[key] = 0.0
            elif t == "boolean":
                out[key] = False
            elif t == "array":
                out[key] = []
            elif t == "object":
                out[key] = {}
    return out


def _catalog(app) -> list[dict]:
    """把 app 的接口按本包模块分组（不依赖 openapi 的排序）。"""
    # 延迟导入：api/__init__.py 在装配阶段会 import 本模块，此处再回导会形成环。
    from muti_llm_agent.api import ALL_MODULES

    try:
        spec = app.openapi()
    except Exception:
        spec = {"paths": {}, "components": {"schemas": {}}}
    schemas = (spec.get("components") or {}).get("schemas") or {}
    paths = spec.get("paths") or {}
    out: list[dict] = []
    for mod_name, mod_router, mod in ALL_MODULES:
        endpoints = []
        for route in getattr(mod_router, "routes", []):
            path = getattr(route, "path", "")
            raw_methods = [m.upper() for m in (getattr(route, "methods", None) or [])]
            if not raw_methods:
                continue
            raw_methods.sort()
            method = raw_methods[0]
            op = (paths.get(path) or {}).get(method.lower()) or {}
            request_model, request_schema = "", None
            rb = op.get("requestBody") or {}
            content = ((rb.get("content") or {}).get("application/json") or {})
            sch = content.get("schema") or {}
            ref = str(sch.get("$ref") or "")
            if ref:
                request_model = ref.rsplit("/", 1)[-1]
                request_schema = schemas.get(request_model)
            elif sch:
                request_schema = sch
            params = []
            for p in op.get("parameters") or []:
                params.append({
                    "name": p.get("name"), "in": p.get("in"),
                    "required": bool(p.get("required")),
                    "schema": p.get("schema") or {},
                })
            example = _example_from_schema(request_schema)
            if request_model == "SteerRequest":
                example.update({"text": "例：请把方案收敛到 3 个可落地的选项",
                                "kind": "correct"})
            endpoints.append({
                "method": method,
                "methods": raw_methods,
                "path": path,
                "summary": getattr(route, "summary", "") or op.get("summary", "") or "",
                "description": (getattr(route, "description", "") or "")[:400],
                "session_scoped": any(p.get("name") == "session_id" for p in params),
                "request_model": request_model,
                "request_schema": request_schema,
                "example_body": example,
                "parameters": params,
                "is_stream": path in ("/api/stream", "/ws"),
                "dangerous": any((m, path) in _DANGEROUS for m in raw_methods),
            })
        out.append({
            "name": mod_name,
            "title": getattr(mod, "MODULE_TITLE", mod_name),
            "description": getattr(mod, "MODULE_DESC", ""),
            "endpoints": endpoints,
        })
    return out


@router.get("/api/dev/endpoints", summary="接口清单（按模块分组，含请求 schema）")
async def dev_endpoints(request: Request, module: str = ""):
    blocked = _guard()
    if blocked is not None:
        return blocked
    modules = _catalog(request.app)
    if module:
        modules = [m for m in modules if m["name"] == module]
    count = sum(len(m["endpoints"]) for m in modules)
    return {
        "ok": True,
        "count": count,
        "modules": modules,
        "titles": {m["name"]: m["title"] for m in modules},
        "session_default": "default",
        "auth_required": bool(settings.AUTH_TOKEN),
        "generated_at": int(time.time() * 1000),
    }


@router.get("/api/dev/modules", summary="模块清单（模块化调试导航）")
async def dev_modules(request: Request):
    blocked = _guard()
    if blocked is not None:
        return blocked
    mods = _catalog(request.app)
    return {
        "ok": True,
        "items": [
            {"name": m["name"], "title": m["title"], "description": m["description"],
             "endpoints": len(m["endpoints"]),
             "paths": sorted({e["path"] for e in m["endpoints"]})}
            for m in mods
        ],
    }


@router.post("/api/dev/invoke", summary="通用接口调用器（进程内真实 ASGI 链路）")
async def dev_invoke(payload: DevInvokeRequest, request: Request):
    """按 method/path/body 调用任意接口，返回状态码、耗时与响应体。

    转发时保留调用方自己的 Authorization / Cookie 头，因此鉴权行为与外部调用一致。
    """
    blocked = _guard()
    if blocked is not None:
        return blocked
    method = (payload.method or "GET").upper()
    path = (payload.path or "").strip()
    if not path.startswith("/"):
        return JSONResponse({"ok": False, "error": "path 必须以 / 开头"}, status_code=400)
    # 防递归：控制台调用自己会造成无限嵌套
    if path.startswith("/api/dev/invoke"):
        return JSONResponse({"ok": False, "error": "不允许调用调用器自身"}, status_code=400)
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        return JSONResponse({"ok": False, "error": f"不支持的 method：{method}"},
                            status_code=400)
    timeout_ms = int(payload.timeout_ms or settings.DEV_API_TIMEOUT_MS)
    headers = {"accept": "application/json, text/plain, */*"}
    for name in ("authorization", "cookie"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    headers.update({str(k): str(v) for k, v in (payload.headers or {}).items()})

    kwargs = {"params": payload.query or {}, "headers": headers}
    if payload.body is not None and method not in ("GET", "HEAD"):
        kwargs["json"] = payload.body

    started = time.perf_counter()
    transport = httpx.ASGITransport(app=request.app)
    try:
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://dev-invoke.local") as client:
            resp = await asyncio.wait_for(
                client.request(method, path, **kwargs), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False, "error": f"调用超时（>{timeout_ms}ms）",
                             "method": method, "path": path},
                            status_code=504)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"调用失败：{e}",
                             "method": method, "path": path},
                            status_code=502)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    return {
        "ok": 200 <= resp.status_code < 400,
        "status_code": resp.status_code,
        "elapsed_ms": elapsed_ms,
        "method": method,
        "path": path,
        "response": body,
        "trace_id": resp.headers.get("x-trace-id", ""),
    }


@router.post("/api/dev/smoke", summary="一键冒烟（只读接口）")
async def dev_smoke(request: Request):
    """顺序跑一组只读接口，返回逐项结果与汇总。

    只读接口的选择标准：不消耗模型额度、不改变运行状态、不需要先配置席位。
    因此这个冒烟可以在任何时候跑，包括一次真实运行进行中。
    """
    blocked = _guard()
    if blocked is not None:
        return blocked
    results = []
    passed = failed = 0
    transport = httpx.ASGITransport(app=request.app)
    auth = request.headers.get("authorization", "")
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://dev-invoke.local") as client:
        for (title, method, _), path in zip(SMOKE_TARGETS, SMOKE_PATHS, strict=False):
            started = time.perf_counter()
            item = {"name": title, "method": method, "path": path,
                    "ok": False, "status_code": 0, "elapsed_ms": 0.0, "error": ""}
            try:
                headers = {"authorization": auth} if auth else {}
                resp = await asyncio.wait_for(
                    client.request(method, path, params={"session_id": "default"},
                                   headers=headers), timeout=15)
                item["status_code"] = resp.status_code
                item["ok"] = 200 <= resp.status_code < 400
                if not item["ok"]:
                    item["error"] = str(resp.text)[:200]
            except Exception as e:
                item["error"] = str(e)[:200]
            item["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            passed += 1 if item["ok"] else 0
            failed += 0 if item["ok"] else 1
            results.append(item)
    return {"ok": failed == 0, "passed": passed, "failed": failed,
            "total": len(results), "results": results,
            "generated_at": int(time.time() * 1000)}


# ---------------- 接口控制台页面 ----------------
@router.get("/console", include_in_schema=False)
async def console_page():
    """接口控制台（纯静态页，逻辑在 static/console.js）。"""
    page = STATIC_DIR / "console.html"
    if not page.is_file():
        return JSONResponse(
            {"ok": False, "error": "控制台页面缺失：static/console.html 不存在"},
            status_code=404)
    return FileResponse(str(page))
