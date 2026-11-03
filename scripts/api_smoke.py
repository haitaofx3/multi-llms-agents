"""
scripts/api_smoke.py — 对「已运行的服务」做接口级冒烟

与 `tests/` 的区别：
  - `tests/` 用 ASGITransport 在进程内跑，验证的是**代码逻辑**（不需要服务器、不触网）；
  - 本脚本连**真实端口**，验证的是**部署形态**：静态页面能否打开、SSE 是否真的在流、
    中间件是否把响应头带出来、鉴权是否生效 —— 这些在 ASGITransport 里测不出来
    （无限流式响应会被缓冲住，这正是它不适合做 SSE 单测的原因）。

用法：
    # 先启动服务：python start.py
    python scripts/api_smoke.py
    python scripts/api_smoke.py --base-url http://127.0.0.1:8000 --token xxx
    # 额外跑一场真实协同（会消耗模型额度，默认不跑）
    python scripts/api_smoke.py --live-run --rounds 1 --recipe dual-2

退出码：0 = 全部通过；1 = 有失败项（便于接到 CI 或部署脚本里）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import httpx

# 终端着色标签（不是凭据；ruff S105 是对 PASS 这个变量名的误报）
PASS_TAG = "\033[32mPASS\033[0m"  # noqa: S105
FAIL_TAG = "\033[31mFAIL\033[0m"
SKIP_TAG = "\033[33mSKIP\033[0m"


class Smoke:
    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures: list[str] = []
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            follow_redirects=True,
        )

    # ---------------- 断言工具 ----------------
    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  {PASS_TAG}  {name}" + (f"  ({detail})" if detail else ""))
        else:
            self.failed += 1
            self.failures.append(f"{name}: {detail}")
            print(f"  {FAIL_TAG}  {name}  {detail}")
        return ok

    def skip(self, name: str, why: str) -> None:
        self.skipped += 1
        print(f"  {SKIP_TAG}  {name}  ({why})")

    def get(self, path: str, **kw) -> httpx.Response:
        return self.client.get(path, **kw)

    def post(self, path: str, **kw) -> httpx.Response:
        return self.client.post(path, **kw)

    def section(self, title: str) -> None:
        print(f"\n[{title}]")

    # ---------------- 冒烟主体 ----------------
    def run(self) -> int:
        print(f"接口冒烟目标：{self.base_url}")
        started = time.perf_counter()
        self._basic()
        self._observability()
        self._dev_console()
        self._run_control()
        self._integration()
        self._sse()
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        print(f"\n汇总：通过 {self.passed} / 失败 {self.failed} / 跳过 {self.skipped}"
              f"（{elapsed} ms）")
        if self.failures:
            print("失败明细：")
            for item in self.failures:
                print(f"  - {item}")
        # 注意：这里不关闭 client —— --live-run 还要继续用它发请求，
        # 关闭动作统一放在 main() 末尾。
        return 0 if self.failed == 0 else 1

    def _basic(self) -> None:
        self.section("基础页面与配置")
        r = self.get("/healthz")
        self.check("GET /healthz", r.status_code == 200, f"http {r.status_code}")
        self.check("响应头带 trace_id", bool(r.headers.get("x-trace-id")),
                   r.headers.get("x-trace-id", ""))
        for page in ("/", "/console", "/docs"):
            p = self.get(page)
            self.check(f"GET {page}", p.status_code == 200, f"http {p.status_code}")
        d = self.get("/api/defaults").json()
        self.check("GET /api/defaults", bool(d.get("providers")),
                   f"{len(d.get('providers', {}))} 供应商")
        self.check("默认配置含协同配方", bool(d.get("recipes")),
                   f"{len(d.get('recipes', []))} 个")

        m = self.get("/api/models").json()
        self.check("GET /api/models", bool(m.get("ok")), f"已配置 {m.get('configured_count')} 个")
        self.check("花名册不含密钥", "api_key" not in json.dumps(m))
        self.check("可用模型 ≥ 1", len(m.get("available", [])) >= 1,
                   ", ".join(m.get("available", [])))

        rec = self.get("/api/recipes").json()
        names = [x["name"] for x in rec.get("items", [])]
        self.check("GET /api/recipes", len(names) >= 3, ", ".join(names))
        if names:
            one = self.get(f"/api/recipes/{names[0]}").json()
            self.check(f"GET /api/recipes/{names[0]}",
                       bool(one.get("agents")), f"{len(one.get('agents', []))} 席")
        self.check("未知配方返回 404",
                   self.get("/api/recipes/not-exist").status_code == 404)

        w = self.get("/api/workspace").json()
        self.check("GET /api/workspace", bool(w.get("paths")))

    def _observability(self) -> None:
        self.section("可观测性")
        logs = self.get("/api/logs", params={"limit": 5}).json()
        self.check("GET /api/logs", bool(logs.get("ok")),
                   f"环形缓冲 {logs.get('ring_size')} 条")
        items = logs.get("items") or []
        self.check("日志字段完整",
                   all(k in items[0] for k in ("ts", "level", "event")) if items else True)
        runs = self.get("/api/runs").json()
        self.check("GET /api/runs", bool(runs.get("ok")),
                   f"索引 {len(runs.get('items', []))} 条")
        tl = self.get("/api/timeline").json()
        self.check("GET /api/timeline", bool(tl.get("ok")))
        tr = self.get("/api/trace/nonexistent-trace").json()
        self.check("GET /api/trace/{未知}", tr.get("ok") is False, "正确返回未找到")

    def _dev_console(self) -> None:
        self.section("接口级调试")
        cat = self.get("/api/dev/endpoints").json()
        mods = [m["name"] for m in cat.get("modules", [])]
        self.check("GET /api/dev/endpoints", cat.get("count", 0) >= 30,
                   f"{cat.get('count')} 个接口 / {len(mods)} 个模块")
        self.check("模块划分齐全",
                   set(mods) >= {"setup", "kb", "run", "stream", "export", "dev",
                                 "integration"}, ", ".join(mods))
        smoke = self.post("/api/dev/smoke").json()
        self.check("POST /api/dev/smoke", smoke.get("failed", 1) == 0,
                   f"{smoke.get('passed')}/{smoke.get('total')} 通过")
        inv = self.post("/api/dev/invoke", json={
            "method": "GET", "path": "/api/status"}).json()
        self.check("POST /api/dev/invoke", inv.get("ok") is True,
                   f"http {inv.get('status_code')} / {inv.get('elapsed_ms')}ms")

    def _run_control(self) -> None:
        self.section("运行控制 / 纠偏 / 续轮（不消耗模型额度）")
        sid = f"smoke_{uuid.uuid4().hex[:8]}"
        st = self.get("/api/status", params={"session_id": sid}).json()
        self.check("GET /api/status（未知会话）", st.get("status") == "idle")

        cfg = self.get("/api/recipes/collab-3").json()
        r = self.post("/api/configure", json={
            "session_id": sid, "agents": cfg["agents"],
            "topic": "冒烟测试：用三模型拆解一个学习计划",
            "max_rounds": cfg["max_rounds"], "mode": cfg["mode"],
            "judge": cfg.get("judge")})
        self.check("POST /api/configure（按配方）", r.status_code == 200, r.text[:160])

        s = self.post("/api/steer", json={
            "session_id": sid, "text": "只给三步以内的行动计划", "kind": "constrain"})
        self.check("POST /api/steer", s.status_code == 200, s.text[:160])
        ok_steer = s.status_code == 200
        pending = s.json().get("steering", {}).get("pending", 0) if ok_steer else 0
        self.check("纠偏指令已入队", pending >= 1, f"pending={pending}")
        directive = (s.json().get("directive") or {}) if ok_steer else {}
        if directive.get("directive_id"):
            d = self.client.delete(f"/api/steer/{directive['directive_id']}",
                                   params={"session_id": sid})
            self.check("DELETE /api/steer/{id}", d.status_code == 200, d.text[:120])

        f = self.post("/api/followup", json={
            "session_id": sid, "materials": "补充材料", "auto_start": False})
        self.check("POST /api/followup（无内容时应 400）", f.status_code == 400, f.text[:160])

        sm = self.get("/api/summary", params={"session_id": sid}).json()
        self.check("GET /api/summary", bool(sm.get("summary")),
                   f"{len((sm.get('summary') or {}).get('agents', []))} 席")
        it = self.get("/api/iterations", params={"session_id": sid}).json()
        self.check("GET /api/iterations", it.get("current") == 1)

        for fmt in ("md", "html", "json"):
            e = self.get("/api/review/export",
                         params={"session_id": sid, "format": fmt, "download": 0})
            self.check(f"GET /api/review/export?format={fmt}",
                       e.status_code == 200 and len(e.text) > 50, f"{len(e.text)} 字符")
        self.check("非法 format 返回 400",
                   self.get("/api/review/export",
                            params={"session_id": sid, "format": "pdf"}).status_code == 400)
        self.check("GET /api/export（旧契约）",
                   "markdown" in self.get("/api/export",
                                          params={"session_id": sid}).json())

    def _integration(self) -> None:
        self.section("对外集成")
        i = self.get("/api/integrations").json()
        self.check("GET /api/integrations", bool(i.get("ok")))
        self.check("集成状态含 dsh/feishu/webhooks/stream",
                   all(k in i for k in ("dsh", "feishu", "webhooks", "stream")))
        man = self.get("/api/integrations/dsh/manifest").json()
        tools = [t["name"] for t in man.get("tools", [])]
        self.check("GET /api/integrations/dsh/manifest", len(tools) >= 7, ", ".join(tools))
        inv = self.post("/api/integrations/dsh/invoke", json={
            "tool": "collab_status", "arguments": {}}).json()
        self.check("POST /api/integrations/dsh/invoke", inv.get("ok") is True)
        bad = self.post("/api/integrations/dsh/invoke",
                        json={"tool": "nope", "arguments": {}})
        self.check("未知 DSH 工具返回 400", bad.status_code == 400)
        ch = self.post("/api/integrations/feishu/webhook", json={
            "type": "url_verification", "challenge": "smoke-challenge"}).json()
        self.check("飞书 url_verification 握手",
                   ch.get("challenge") == "smoke-challenge", str(ch))

    def _sse(self) -> None:
        self.section("SSE 流式（真实长连接）")
        try:
            frames: list[str] = []
            with self.client.stream("GET", "/api/stream",
                                    params={"session_id": "smoke_sse", "replay": 0},
                                    timeout=httpx.Timeout(10.0, read=10.0)) as resp:
                ok = resp.status_code == 200 and "text/event-stream" in \
                    resp.headers.get("content-type", "")
                self.check("GET /api/stream 建立连接", ok,
                           resp.headers.get("content-type", ""))
                for line in resp.iter_lines():
                    if line.strip():
                        frames.append(line.strip())
                    if len(frames) >= 4:
                        break
            joined = "\n".join(frames)
            self.check("SSE 首帧为 status 快照",
                       "event: status" in joined or '"type": "status"' in joined,
                       joined[:120].replace("\n", " "))
            self.check("SSE 含 stream_open 帧", "stream_open" in joined)
        except Exception as e:  # 长连接异常不该让整份冒烟崩掉
            self.check("GET /api/stream", False, f"{type(e).__name__}: {e}")

    # ---------------- 可选的实跑 ----------------
    def live_run(self, recipe: str, rounds: int, session: str = "smoke_live") -> int:
        """真正跑一场协同（消耗模型额度），并验证纠偏 / 摘要 / 复盘链路。"""
        self.section(f"真实运行（recipe={recipe}, rounds={rounds}）")
        cfg = self.get(f"/api/recipes/{recipe}").json()
        if not cfg.get("agents"):
            self.check(f"配方 {recipe}", False, "配方不存在")
            return 1
        before = self.failed
        r = self.post("/api/configure", json={
            "session_id": session, "agents": cfg["agents"],
            "topic": "用一句话说明多模型协同比单模型好的两个理由",
            "max_rounds": rounds, "mode": cfg["mode"], "judge": cfg.get("judge")})
        self.check("配置席位", r.status_code == 200, r.text[:160])
        r = self.post("/api/start", json={"session_id": session})
        self.check("开始运行", r.status_code == 200, r.text[:160])
        start_trace = ""
        if r.status_code == 200:
            start_trace = r.json().get("trace_id", "")
        self.post("/api/steer", json={
            "session_id": session, "text": "答案控制在 100 字以内", "kind": "constrain"})

        deadline = time.time() + max(120, rounds * 180)
        status = "running"
        while time.time() < deadline:
            time.sleep(3)
            st = self.get("/api/status", params={"session_id": session}).json()
            status = st.get("status", "")
            print(f"    … 状态 {status} / 轮次 {st.get('round')}/{st.get('max_rounds')}"
                  f" / 输出 {st.get('output_tokens')} tokens")
            if status in ("done", "stopped"):
                break
        self.check("运行到达终态", status in ("done", "stopped"), f"status={status}")

        sm = self.get("/api/summary",
                      params={"session_id": session}).json().get("summary", {})
        self.check("摘要含模型指标", bool(sm.get("agents")),
                   f"{len(sm.get('agents', []))} 席")
        self.check("摘要含 token 与费用", sm.get("tokens", {}).get("output", 0) > 0,
                   f"输出 {sm.get('tokens', {}).get('output')} tokens")
        self.check("纠偏指令被统计", sm.get("steering", {}).get("added", 0) >= 1)
        self.check("/api/start 返回的 trace_id 与实际运行一致",
                   bool(start_trace) and start_trace == sm.get("trace_id"),
                   f"start={start_trace} actual={sm.get('trace_id')}")
        calls = sum(a.get("llm_calls", 0) for a in sm.get("agents", []))
        self.check("统计到 LLM 调用次数", calls > 0, f"{calls} 次")
        rv = self.get("/api/review/export",
                      params={"session_id": session, "format": "md", "download": 0})
        self.check("复盘 Markdown 含发言", "复盘摘要" in rv.text and len(rv.text) > 500,
                   f"{len(rv.text)} 字符")
        tr = self.get("/api/trace/" + str(sm.get("trace_id", ""))).json()
        self.check("trace 可查事件", tr.get("event_count", 0) > 0,
                   f"{tr.get('event_count')} 条事件")
        return 0 if self.failed == before else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="muti_llm_agent 接口冒烟")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--token", default="")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--live-run", action="store_true",
                    help="额外跑一场真实协同（消耗模型额度）")
    ap.add_argument("--recipe", default="collab-3")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--session", default="smoke_live",
                    help="实跑使用的会话标识（会覆盖该会话已有配置）")
    args = ap.parse_args(argv)

    smoke = Smoke(args.base_url, args.token, args.timeout)
    code = smoke.run()
    if args.live_run:
        live = smoke.live_run(args.recipe, args.rounds, args.session)
        code = code or live
        print(f"\n最终：通过 {smoke.passed} / 失败 {smoke.failed}")
    smoke.client.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
