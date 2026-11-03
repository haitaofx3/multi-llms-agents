"""
dispatch.py — 出站 webhook 分发（只用标准库 urllib，不引入 requests / httpx）

为什么这样拆分：
  - 运行结束摘要需要同时通知多个外部系统（飞书群机器人、内部告警、CI 回调…），
    但「发一个 JSON 请求」这件事不该散落在 orchestrator / main 里，否则每加一个
    下游都要改核心链路。这里把它收敛成两个纯函数：单地址 `post_json_webhook`
    与多地址并发 `dispatch_run_summary`。
  - 出站集成是 best-effort 旁路：网络抖动、对端 5xx、URL 配错都不允许影响主流程，
    因此所有失败都转成返回值里的 ok=False / error，**绝不向上抛异常**。
  - 只用标准库：webhook 是低频旁路能力，不值得为它把 requests / httpx 变成运行时
    硬依赖（部署体积更小、依赖面更可控）。
  - 摘要文案（`summary_sentence`）也放在这里：它是通知载荷的一部分，飞书卡片与其它
    下游都要复用同一句话，集中一处可避免两套口径。
"""
from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# 单次推送默认超时（秒）：webhook 是旁路，宁可超时放弃也不拖慢主流程
DEFAULT_TIMEOUT = 8.0
# 并发推送上限：外部机器人普遍有频率限制，4 路足够且不会把对端打挂
MAX_WORKERS = 4
# 响应体只保留头部若干字符（足够定位问题，又不会被超大响应占内存）
MAX_BODY_CHARS = 4000
# 一句话摘要中主题 / trace_id 的展示长度
TOPIC_DISPLAY_CHARS = 60
TRACE_DISPLAY_CHARS = 12
# 全部控制字符（保留 \n 以便多行展示；\t 在单行化时先转空格）
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 状态中文口径（feishu 卡片标题色沿用同一套 status 取值）
_STATUS_LABELS = {
    "done": "完成",
    "stopped": "已停止",
    "running": "进行中",
    "paused": "已暂停",
    "pending": "待开始",
    "error": "失败",
    "failed": "失败",
}


def _result(ok: bool, status: int, body: str, error: str) -> dict:
    """统一结果结构，保证四个字段类型稳定（调用方无需再判空）。"""
    return {
        "ok": bool(ok),
        "status": int(status or 0),
        "body": str(body or ""),
        "error": str(error or ""),
    }


def _validate_url(url: Any) -> tuple[str, str]:
    """校验出站地址，返回 (规整后的 url, 错误信息)；错误信息为空表示通过。

    只做「形态」校验（http/https + 有主机名），不做 DNS / 内网拦截 ——
    webhook 地址由运维在 .env 里配置，属受信输入；若将来允许终端用户自带地址，
    应在此叠加 `netguard.check_outbound_url` 那套 SSRF 防护。
    """
    if not isinstance(url, str):
        return "", f"url must be a string, got {type(url).__name__}"
    raw = url.strip()
    if not raw:
        return "", "empty url"
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw, "invalid url"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return raw, f"unsupported scheme: {scheme or '(empty)'}（只允许 http / https）"
    if not parsed.netloc:
        return raw, "missing host in url"
    return raw, ""


def _safe_timeout(timeout: Any) -> float:
    """把超时规整成正浮点数，非法值回落到默认超时。"""
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_TIMEOUT
    return value


def _read_body(resp: Any) -> str:
    """读取响应体（按字节限长后再解码），解码失败用替换字符兜底。"""
    data = resp.read(MAX_BODY_CHARS * 4) or b""
    if not isinstance(data, bytes):
        data = str(data).encode("utf-8", errors="replace")
    return data.decode("utf-8", errors="replace")[:MAX_BODY_CHARS]


def post_json_webhook(url: str, payload: dict, timeout: float = 8.0) -> dict:
    """把一个 JSON 体 POST 到 url，返回 `{ok, status, body, error}`。

    - 空 / 非字符串 / 非 http(s) 地址直接返回 ok=False（error 说明原因），不发请求；
    - 4xx / 5xx 不算异常，返回 ok=False + 真实 status + 响应体片段；
    - 网络错误、超时、序列化失败等全部被捕获，**本函数不会抛出任何异常**。
    """
    raw, error = _validate_url(url)
    if error:
        return _result(False, 0, "", error)
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except Exception as exc:
        return _result(False, 0, "", f"payload not serializable: {type(exc).__name__}: {exc}")
    # scheme 已在 _validate_url 中限制为 http/https，故这两处放行 S310
    request = urllib.request.Request(raw, data=body, method="POST")  # noqa: S310
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("User-Agent", "muti_llm_agent-integrations/1.0")
    try:
        with urllib.request.urlopen(request, timeout=_safe_timeout(timeout)) as resp:  # noqa: S310
            status = int(getattr(resp, "status", 0) or 0)
            text = _read_body(resp)
    except urllib.error.HTTPError as exc:
        # 注意：HTTPError 是 URLError 的子类，必须排在前面捕获
        try:
            text = _read_body(exc)
        except Exception:
            text = ""
        return _result(False, int(exc.code or 0), text, f"http error: {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        return _result(False, 0, "", f"url error: {exc.reason}")
    except TimeoutError:
        return _result(False, 0, "", f"timeout after {timeout}s")
    except Exception as exc:
        return _result(False, 0, "", f"{type(exc).__name__}: {exc}")
    ok = 200 <= status < 300
    return _result(ok, status, text, "" if ok else f"http status {status}")


def _as_str(value: Any) -> str:
    """安全转字符串（None → 空串，永不抛）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def _as_int(value: Any) -> int | None:
    """安全取整数（bool 不算整数；"12" / 12.0 视为可转换；失败返回 None）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        if isinstance(value, float):
            return int(value) if value.is_integer() else None
        text = str(value).strip()
        return int(text) if re.fullmatch(r"[+-]?\d+", text) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _as_float(value: Any) -> float | None:
    """安全取浮点数（NaN / inf 视为无效，返回 None）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _one_line(value: Any, limit: int) -> str:
    """把任意文本压成单行并裁剪：换行归一 → 移除控制字符 → 折叠空白 → 截断。"""
    text = value if isinstance(value, str) else _as_str(value)
    text = text.replace("\t", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = " ".join(text.split())
    if limit > 0 and len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text


def summary_sentence(summary: dict) -> str:
    """把运行摘要压缩成一句中文（用于 IM 通知首行 / webhook text 字段）。

    示例：`【完成】主题《X》｜3 个模型协同｜6 轮｜输出 12345 tokens｜预估 ¥0.0321｜trace abc123`。
    字段缺失时跳过对应片段而不是填 "?"，保证短摘要依然可读；任何取值都不会抛异常。
    """
    data = summary if isinstance(summary, dict) else {}
    try:
        status_raw = _as_str(data.get("status")).strip()
        status_word = _STATUS_LABELS.get(status_raw.lower(), status_raw or "?")
        topic = _one_line(_as_str(data.get("topic")), TOPIC_DISPLAY_CHARS) or "?"
        parts = [f"【{status_word}】主题《{topic}》"]

        agents = data.get("agents")
        agent_count = len(agents) if isinstance(agents, (list, tuple)) else 0
        if agent_count:
            parts.append(f"{agent_count} 个模型协同")

        rounds = _as_int(data.get("rounds_executed"))
        max_rounds = _as_int(data.get("max_rounds"))
        if rounds is not None:
            parts.append(f"{rounds} 轮")
        elif max_rounds is not None:
            parts.append(f"最多 {max_rounds} 轮")

        tokens = data.get("tokens")
        output = _as_int(tokens.get("output")) if isinstance(tokens, dict) else None
        if output is not None:
            parts.append(f"输出 {output} tokens")

        cost = _as_float(data.get("cost_rmb"))
        if cost is not None:
            parts.append(f"预估 ¥{cost:.4f}")

        trace = _one_line(_as_str(data.get("trace_id")), TRACE_DISPLAY_CHARS)
        if trace:
            parts.append(f"trace {trace}")
        return "｜".join(parts)
    except Exception:
        # 理论上不可达（取值都走安全辅助函数），保留兜底以满足「永不抛」契约
        return "【?】主题《?》"


def dispatch_run_summary(urls: list[str], summary: dict, timeout: float = 8.0) -> list[dict]:
    """把同一个 `run.finished` 事件并发推送到多个 webhook，结果按入参顺序返回。

    - payload 固定为 `{"event": "run.finished", "summary": ..., "text": 一句话中文摘要}`；
    - 线程数取 `min(4, len(urls))`，单个地址失败不影响其它地址；
    - 每项结果在 `post_json_webhook` 的四个字段之外附加 `url`，便于逐地址排障；
    - 本函数同样不抛异常：空列表返回空列表，内部异常降级为逐地址失败结果。
    """
    if isinstance(urls, str):
        targets: list[Any] = [urls]
    elif isinstance(urls, (list, tuple)):
        targets = list(urls)
    else:
        targets = []
    payload = {
        "event": "run.finished",
        "summary": summary if isinstance(summary, dict) else {},
        "text": summary_sentence(summary),
    }
    if not targets:
        return []
    try:
        slots: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(targets))) as pool:
            futures = {
                pool.submit(post_json_webhook, target, payload, timeout): index
                for index, target in enumerate(targets)
            }
            slots = [_result(False, 0, "", "not dispatched") for _ in targets]
            for future in as_completed(futures):
                index = futures[future]
                try:
                    res = future.result()
                except Exception as exc:
                    res = _result(False, 0, "", f"{type(exc).__name__}: {exc}")
                if not isinstance(res, dict):
                    res = _result(False, 0, "", "unexpected dispatch result")
                slots[index] = {**res, "url": str(targets[index])}
        return slots
    except Exception as exc:
        # 线程池创建失败等极端情况：逐地址给出失败结果，保持返回值形状稳定
        return [
            {**_result(False, 0, "", f"{type(exc).__name__}: {exc}"), "url": str(target)}
            for target in targets
        ]
