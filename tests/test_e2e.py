"""
test_e2e.py — 端到端测试：配置 -> 注入知识 -> 开始 -> WebSocket 收事件 -> 导出 Markdown

运行方式（需先启动服务器，在项目根目录执行）：
    python tests/test_e2e.py
使用 agent_env 解释器运行。
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# 避免 localhost 走代理
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)

BASE = "http://127.0.0.1:8000"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPORT_PATH = PROJECT_ROOT / "data" / "discussions" / "e2e_export.md"
# pytest 采集时 sys.stdout 已被换成捕获对象，不一定带 reconfigure，直接调会抛 AttributeError。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    import websockets  # 必须在代理清理之后再导入，否则会走系统代理
except ModuleNotFoundError:  # pragma: no cover - 仅精简环境 / CI 会走到
    # 本文件是「手动跑的端到端脚本」，没有任何 test_ 函数；正常 pytest 采集会被
    # tests/conftest.py 的 collect_ignore 直接跳过。但万一被直接导入
    # （pytest tests/test_e2e.py），缺 websockets 也不能炸掉导入——否则会变成
    # "Interrupted: 1 error during collection"，连带中断整个测试会话。
    websockets = None

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
                    print(f"  [{ev.get('round')}] {ev.get('speaker')}: {ev.get('text')[:120]}")
                elif t == "speech_start":
                    print(f"  [STREAM] {ev.get('speaker')} 开始流式发言")
                elif t == "speech_delta":
                    print(f"  [STREAM] {ev.get('speaker')} += {ev.get('delta')[:80]!r}")
                elif t == "speech_end":
                    print(f"  [STREAM] {ev.get('speaker')} 收尾 len={len(ev.get('text',''))}")
                elif t == "task_assigned":
                    print(f"  [TASK] -> {ev.get('to')}: {ev.get('content')[:80]}")
                elif t == "task_result":
                    print(f"  [REPORT] {ev.get('from')} done={ev.get('completed')}: {ev.get('message')[:80]}")
                elif t == "status":
                    print(f"  [STATUS] {ev.get('status')} round={ev.get('round')} phase={ev.get('phase')}")
                elif t == "status_sync":
                    print(f"  [SYNC] phase={ev.get('phase')} round={ev.get('current_round')} "
                          f"output={ev.get('output_tokens')} spent={ev.get('spent_tokens')}")
                elif t == "judgment":
                    print(f"  [JUDGE] judge={ev.get('judge')} winner={ev.get('winner')} "
                          f"scores={ev.get('scores')} agent_scores={ev.get('agent_scores')} "
                          f"model_performance={ev.get('model_performance')} "
                          f"composite_scores={ev.get('composite_scores')} "
                          f"converged={ev.get('convergence_flag')}")
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

    # 2. configure: 1 main + 2 collaborators, max_rounds=2（立场：主持人/正方/反方）
    #    2 轮即可跑完整状态机（preparing→debating→judging→finished）
    cfg = {
        "topic": "城市是否应该全面推广自动驾驶出租车？",
        "max_rounds": 2,
        "judge": {"name": "独立裁判", "provider": "qwen"},
        "agents": [
            {"name": "主持人", "role": "辩论主持人", "personality": "理性、善于追问与归纳",
             "provider": "qwen", "stance": "HOST"},
            {"name": "技术派", "role": "自动驾驶技术专家", "personality": "务实、看重安全与工程可行性",
             "provider": "qwen", "stance": "PRO"},
            {"name": "人文派", "role": "城市社会学家", "personality": "关注就业、公平与公共治理",
             "provider": "qwen", "stance": "CON"},
        ],
    }
    st, r = rest("POST", "/api/configure", cfg)
    print("configure:", st, r.get("ok"), "agents=", [a["name"] for a in r.get("agents", [])])
    print("configure stances:", [a["stance"] for a in r.get("agents", [])],
          "budget=", r.get("budget"), "trace_id=", r.get("trace_id"))
    print("configure judge:", r.get("judge"))

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

    # 6. wait for done (up to 30 min；MiMo 单轮辩论需数分钟)
    ok = ws_done.wait(timeout=1800)
    print("ws_done:", ok, "events:", len(events))

    # 7. status + export
    st, s = rest("GET", "/api/status")
    print("status:", s.get("status"), "round=", s.get("round"), "max=", s.get("max_rounds"))
    print("status detail: phase=", s.get("phase"), "trace_id=", s.get("trace_id"),
          "spent=", s.get("spent_tokens"), "output=", s.get("output_tokens"),
          "judgment=", bool(s.get("judgment")))

    st, ex = rest("GET", "/api/export")
    md = ex.get("markdown", "")
    print("export len:", len(md))
    # 打印结构化信息用于校验
    rounds_seen = sorted({e.get("round") for e in events if e.get("round")})
    print("rounds observed:", rounds_seen)
    print("has summary event:", any(e.get("type") == "summary" for e in events))
    print("has task_assigned event:", any(e.get("type") == "task_assigned" for e in events))
    print("has task_result event:", any(e.get("type") == "task_result" for e in events))
    # ---- 新增架构特性校验（状态机 / 预算 / 裁判） ----
    phases = [e.get("phase") for e in events
              if e.get("type") == "status_sync" and e.get("phase")]
    print("phases observed:", sorted(set(phases)))
    has_judgment = any(e.get("type") == "judgment" for e in events)
    has_status_sync = any(e.get("type") == "status_sync" for e in events)
    spent = s.get("spent_tokens") or 0
    output = s.get("output_tokens") or 0
    print(f"token telemetry: spent={spent}, output={output}")
    print(f"judgment event received: {has_judgment}")
    assert has_status_sync, "未收到 status_sync 事件（状态机/遥测同步缺失）"
    assert has_judgment, "未收到 judgment 事件（裁判裁决缺失）"
    assert spent >= 0 and output >= 0, "token 遥测统计异常"
    if s.get("judgment"):
        assert "winner" in s["judgment"], "judgment 缺少 winner 字段"
        assert "scores" in s["judgment"], "judgment 缺少 scores 字段"
        assert "agent_scores" in s["judgment"], "judgment 缺少 agent_scores 字段（独立裁判按辩手评分）"
        assert "model_performance" in s["judgment"], "judgment 缺少 model_performance 字段（模型质量评分）"
        assert "composite_scores" in s["judgment"], "judgment 缺少 composite_scores 字段（综合评分）"
        # 综合评分非空：至少一名辩手有复合分，且胜方合法
        assert s["judgment"]["composite_scores"], "composite_scores 为空"
        assert s["judgment"]["winner"] in ("PRO", "CON", "DRAW"), \
            f"胜方非法：{s['judgment']['winner']}"
    print("ARCHITECTURE CHECKS PASS (state machine / budget / judge)")
    # ---- 流式输出校验 ----
    n_start = sum(1 for e in events if e.get("type") == "speech_start")
    n_delta = sum(1 for e in events if e.get("type") == "speech_delta")
    n_end = sum(1 for e in events if e.get("type") == "speech_end")
    print(f"streaming events: speech_start={n_start}, speech_delta={n_delta}, speech_end={n_end}")
    deltas_by = {}
    for e in events:
        if e.get("type") == "speech_delta":
            deltas_by.setdefault(e.get("speaker"), []).append(e.get("delta", ""))
    for sp, ds in deltas_by.items():
        print(f"  stream [{sp}] joined={len(''.join(ds))} chars")
    # 断言：只要发生过任何内容发言，就应出现流式事件
    assert n_start == n_end, "speech_start/end 数量应一致"
    if n_start > 0:
        assert n_delta > 0, "存在流式发言但无 delta"
        total = sum(len("".join(ds)) for ds in deltas_by.values())
        assert total > 0, "流式 delta 内容为空"
    print("STREAMING CHECKS PASS")
    # 落盘（写入 data/discussions/）
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_PATH.write_text(md, encoding="utf-8")
    print(f"saved {EXPORT_PATH}")


if __name__ == "__main__":
    if websockets is None:
        raise SystemExit(
            "缺少 websockets：先 pip install websockets，再运行 python tests/test_e2e.py")
    main()
