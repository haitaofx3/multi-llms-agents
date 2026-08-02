"""
e2e_test.py — 端到端测试：配置 -> 注入知识 -> 开始 -> WebSocket 收事件 -> 导出 Markdown
使用 agent_env 解释器运行。
"""
import sys, os, json, time, threading, urllib.request, urllib.error

# 避免 localhost 走代理
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)

BASE = "http://127.0.0.1:8000"
sys.stdout.reconfigure(encoding="utf-8")

import websockets  # 必须在代理清理后导入

events = []
ws_done = threading.Event()


def rest(method, path, data=None):
    url = BASE + path
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def ws_listener():
    async def run():
        async with websockets.connect("ws://127.0.0.1:8000/ws",
                                      open_timeout=10, close_timeout=5) as ws:
            while not ws_done.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                ev = json.loads(msg)
                events.append(ev)
                t = ev.get("type")
                if t == "speech":
                    print(f"  [{ev.get('round')}] {ev.get('agent')}: {ev.get('text')[:120]}")
                elif t == "task_assigned":
                    print(f"  [TASK] -> {ev.get('to')}: {ev.get('content')[:80]}")
                elif t == "task_result":
                    print(f"  [REPORT] {ev.get('from')} done={ev.get('completed')}: {ev.get('message')[:80]}")
                elif t == "status":
                    print(f"  [STATUS] {ev.get('status')} round={ev.get('round')}")
                elif t == "summary":
                    print(f"  [SUMMARY] {ev.get('text')[:200]}")
                if ev.get("type") == "status" and ev.get("status") == "done":
                    ws_done.set()
                    break
    import asyncio
    asyncio.run(run())


def main():
    # 1. defaults
    st, d = rest("GET", "/api/defaults")
    print("defaults:", st, "providers=", list(d.get("providers", {}).keys()))

    # 2. configure: 1 main + 2 collaborators, max_rounds=3
    cfg = {
        "topic": "城市是否应该全面推广自动驾驶出租车？",
        "max_rounds": 3,
        "agents": [
            {"name": "主持人", "role": "辩论主持人", "personality": "理性、善于追问与归纳",
             "provider": "default"},
            {"name": "技术派", "role": "自动驾驶技术专家", "personality": "务实、看重安全与工程可行性",
             "provider": "default"},
            {"name": "人文派", "role": "城市社会学家", "personality": "关注就业、公平与公共治理",
             "provider": "default"},
        ],
    }
    st, r = rest("POST", "/api/configure", cfg)
    print("configure:", st, r.get("ok"), "agents=", [a["name"] for a in r.get("agents", [])])

    # 3. ingest knowledge
    st, r = rest("POST", "/api/ingest", {"text": "研究表明：自动驾驶在封闭园区事故率低于人类驾驶员约 40%\n"
                                                 "但复杂城市路口的长尾场景仍是安全落地的主要难点。"})
    print("ingest:", st, r)

    # 4. start WS listener in background
    t = threading.Thread(target=ws_listener, daemon=True)
    t.start()
    time.sleep(1.0)

    # 5. start discussion
    st, r = rest("POST", "/api/start", {})
    print("start:", st, r)

    # 6. wait for done (up to 5 min)
    ok = ws_done.wait(timeout=300)
    print("ws_done:", ok, "events:", len(events))

    # 7. status + export
    st, s = rest("GET", "/api/status")
    print("status:", s.get("status"), "round=", s.get("round"), "max=", s.get("max_rounds"))

    st, ex = rest("GET", "/api/export")
    md = ex.get("markdown", "")
    print("export len:", len(md))
    # 打印结构化信息用于校验
    rounds_seen = sorted({e.get("round") for e in events if e.get("round")})
    print("rounds observed:", rounds_seen)
    print("has summary event:", any(e.get("type") == "summary" for e in events))
    print("has task_assigned event:", any(e.get("type") == "task_assigned" for e in events))
    print("has task_result event:", any(e.get("type") == "task_result" for e in events))
    # 落盘
    open("e2e_export.md", "w", encoding="utf-8").write(md)
    print("saved e2e_export.md")


if __name__ == "__main__":
    main()
