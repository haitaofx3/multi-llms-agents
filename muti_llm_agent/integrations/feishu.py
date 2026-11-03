"""
feishu.py — 飞书（Lark）协议适配：卡片 / 纯文本构建、webhook 推送、事件回调解析

为什么这样拆分：
  - 「飞书消息长什么样」和「怎么把 JSON 发出去」是两件事：传输统一交给
    `dispatch.py`，本模块只做协议适配，测试时完全可以不碰网络。
  - 卡片构建是纯函数（summary → dict），便于快照测试与长度校验。飞书对文本长度、
    控制字符都很敏感，集中在渲染层裁剪比散落在各调用方可靠。
  - 事件回调是「不可信输入」的入口：字段缺失、类型不符、content 不是合法 JSON，
    都必须兜底成固定结构，绝不能让一个畸形回调把请求处理打挂。
  - 依赖方向单向：feishu → dispatch（dispatch 不反向依赖 feishu），避免循环导入。
"""
from __future__ import annotations

import json
import math
import re

from muti_llm_agent.integrations.dispatch import post_json_webhook, summary_sentence

# 飞书文本上限附近的安全值（留出 JSON 结构与转义开销）
FEISHU_TEXT_LIMIT = 28000
# 单个 lark_md 块的字符上限（飞书编辑器/卡片对单块也有长度约束，留余量）
BLOCK_TEXT_LIMIT = 4000
# 卡片标题 / 主题 / 单行字段的展示上限
TITLE_MAX_CHARS = 100
TOPIC_MAX_CHARS = 200
# 卡片里最多列出的模型行数与纯文本里最多列出的模型数
CARD_AGENT_LINES = 30
TEXT_AGENT_LIMIT = 10
# trace_id 在 note 里的展示长度
TRACE_MAX_CHARS = 64
# 飞书消息事件类型
MESSAGE_EVENT_TYPE = "im.message.receive_v1"
# 控制字符（保留 \n 用于多行正文；\t 先转空格）
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# @提及（飞书把被 @ 的机器人渲染成 @_user_1，@所有人 为 @_all）
_MENTION_RE = re.compile(r"@_user_\d+|@_all\b")
_STATUS_LABELS = {
    "done": "完成",
    "stopped": "已停止",
    "running": "进行中",
    "paused": "已暂停",
    "pending": "待开始",
    "error": "失败",
    "failed": "失败",
}
_MODE_LABELS = {"collaboration": "协同", "debate": "辩论"}
# header 配色：只有「正常结束」和「人工停止」有专属色，其余一律蓝色（中性）
_TEMPLATE_BY_STATUS = {"done": "green", "stopped": "grey"}
_DEFAULT_TEMPLATE = "blue"


def _as_str(value: object) -> str:
    """安全转字符串（None → 空串，永不抛）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def _as_int(value: object) -> int | None:
    """安全取整数（bool 不算整数；失败返回 None）。"""
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


def _as_float(value: object) -> float | None:
    """安全取浮点数（NaN / inf 视为无效）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _clean(value: object, limit: int = 0) -> str:
    """对用户可控文本做最小清洗：`\\r\\n` 归一为 `\\n`，移除控制字符，按需截断。

    `limit <= 0` 表示不截断。不做 Markdown 转义（飞书 lark_md 需要保留 `**` 等
    排版标记），但单行字段请配合 `_one_line` 折叠换行，避免伪造卡片字段。
    """
    text = value if isinstance(value, str) else _as_str(value)
    text = text.replace("\t", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    if limit > 0 and len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text


def _one_line(value: object, limit: int) -> str:
    """把文本压成单行（折叠换行与连续空白）后裁剪。"""
    text = " ".join(_clean(value).split())
    if limit > 0 and len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text


def _status_label(data: dict) -> str:
    """状态中文名；未知状态原样展示，空状态用 "?"。"""
    raw = _as_str(data.get("status")).strip()
    return _STATUS_LABELS.get(raw.lower(), raw or "?")


def _mode_label(data: dict) -> str:
    """模式中文名；未知模式原样展示，缺失返回空串（调用方跳过该行）。"""
    raw = _as_str(data.get("mode")).strip()
    if not raw:
        return ""
    return _MODE_LABELS.get(raw, _one_line(raw, 40))


def _template_for(data: dict) -> str:
    """header 配色：done→green、stopped→grey、其它→blue。"""
    raw = _as_str(data.get("status")).strip().lower()
    return _TEMPLATE_BY_STATUS.get(raw, _DEFAULT_TEMPLATE)


def _steering_count(steering: dict) -> int | None:
    """纠偏次数：兼容 count / added / injected 等统计口径，最后退回指令条数。

    不同版本的中途指令统计字段名不一致（`summary.build_summary` 用的是 added），
    这里按优先级取第一个可用值，取不到就返回 None（调用方跳过该行）。
    """
    for key in ("count", "added", "injected", "injected_count", "total"):
        value = _as_int(steering.get(key))
        if value is not None:
            return value
    items = steering.get("items")
    return len(items) if isinstance(items, list) else None


def _summary_pairs(data: dict, *, with_basics: bool) -> list[tuple[str, str]]:
    """把摘要渲染成 (标签, 值) 列表，卡片与纯文本共用。

    - `with_basics=True`：附带「主题 / 状态」（卡片正文需要；纯文本正文已由
      `summary_sentence` 覆盖，不再重复）；
    - 模式 / 轮次 / Token / 费用 / 裁判 / 纠偏按字段存在与否决定是否出现，
      缺失即跳过，绝不为 None 渲染出 "None"。
    """
    pairs: list[tuple[str, str]] = []
    if with_basics:
        topic = _one_line(_as_str(data.get("topic")), TOPIC_MAX_CHARS) or "?"
        pairs.append(("主题", topic))
    mode = _mode_label(data)
    if mode:
        pairs.append(("模式", mode))
    if with_basics:
        pairs.append(("状态", _status_label(data)))

    rounds = _as_int(data.get("rounds_executed"))
    max_rounds = _as_int(data.get("max_rounds"))
    if rounds is not None and max_rounds is not None:
        pairs.append(("轮次", f"{rounds} / {max_rounds}"))
    elif rounds is not None:
        pairs.append(("轮次", f"{rounds}"))
    elif max_rounds is not None:
        pairs.append(("轮次", f"最多 {max_rounds}"))

    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        output = _as_int(tokens.get("output"))
        total = _as_int(tokens.get("total"))
        bits: list[str] = []
        if output is not None:
            bits.append(f"输出 {output}")
        if total is not None:
            bits.append(f"总计 {total}")
        if bits:
            pairs.append(("Token 用量", " / ".join(bits)))

    cost = _as_float(data.get("cost_rmb"))
    if cost is not None:
        pairs.append(("预估费用", f"≈ ¥{cost:.4f}"))

    judge = data.get("judge")
    if isinstance(judge, dict):
        winner = _one_line(_as_str(judge.get("winner")), 40)
        if winner:
            display = "平局" if winner.upper() == "DRAW" else f"胜方 {winner}"
            pairs.append(("裁判", display))

    steering = data.get("steering")
    if isinstance(steering, dict):
        count = _steering_count(steering)
        if count is not None:
            pairs.append(("人工纠偏", f"{count} 次"))
    return pairs


def _detail_block(data: dict) -> str:
    """卡片正文块（lark_md）：**标签**：值，逐行渲染。"""
    lines = [f"**{label}**：{value}" for label, value in _summary_pairs(data, with_basics=True)]
    return _clean("\n".join(lines), BLOCK_TEXT_LIMIT)


def _agents_block(data: dict) -> str:
    """参与模型块（lark_md）：每行 `- 名称（model_id）`，超出上限折叠成一行提示。"""
    agents = data.get("agents")
    rows: list[str] = []
    total = 0
    if isinstance(agents, (list, tuple)):
        for item in agents:
            total += 1
            if len(rows) >= CARD_AGENT_LINES:
                continue
            if isinstance(item, dict):
                name = _one_line(_as_str(item.get("name")), 60) or "?"
                model = _one_line(_as_str(item.get("model_id")), 60)
            else:
                name = _one_line(_as_str(item), 60) or "?"
                model = ""
            rows.append(f"- {name}（{model}）" if model else f"- {name}")
    if not rows:
        body = "（暂无参与模型）"
    else:
        body = "\n".join(rows)
        if total > len(rows):
            body += f"\n- …共 {total} 个"
    return _clean("**参与模型**\n" + body, BLOCK_TEXT_LIMIT)


def _text_blocks(card: dict) -> list[dict]:
    """收集卡片里所有可裁剪的文本容器（div.text 与 note.elements）。"""
    blocks: list[dict] = []
    for element in card.get("elements") or []:
        if not isinstance(element, dict):
            continue
        text = element.get("text")
        if isinstance(text, dict) and isinstance(text.get("content"), str):
            blocks.append(text)
        subs = element.get("elements")
        if isinstance(subs, list):
            for sub in subs:
                if isinstance(sub, dict) and isinstance(sub.get("content"), str):
                    blocks.append(sub)
    return blocks


def _json_size(value: dict) -> int:
    """卡片序列化长度（用于总长度门禁）。"""
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return FEISHU_TEXT_LIMIT + 1


def _cap_card_size(card: dict) -> dict:
    """保证整张卡片序列化后不超过 FEISHU_TEXT_LIMIT：优先压缩最长的文本块。

    正常路径不会触发（单块已限 4000，三块合计远小于 28000），这里是为
    「未来新增块 / 调大块上限」留的兜底门禁，保证契约不被悄悄破坏。
    """
    for _ in range(32):
        size = _json_size(card)
        if size <= FEISHU_TEXT_LIMIT:
            return card
        blocks = [block for block in _text_blocks(card)
                  if len(str(block.get("content") or "")) > 1]
        if not blocks:
            return card
        longest = max(blocks, key=lambda block: len(str(block.get("content") or "")))
        content = str(longest.get("content") or "")
        cut = min(len(content) - 1, max(16, size - FEISHU_TEXT_LIMIT + 32))
        longest["content"] = content[: len(content) - cut].rstrip() + "…"
    return card


def build_feishu_card(summary: dict, title: str = "多模型协同 · 运行摘要") -> dict:
    """构造飞书「消息卡片」body（不含 msg_type，由调用方组装 payload）。

    结构：header（配色 + 标题）→ 摘要正文 → 分隔线 → 参与模型 → note（trace_id）。
    所有文本都会清洗并按块 / 整体长度裁剪，保证能稳定通过飞书接口。
    """
    data = summary if isinstance(summary, dict) else {}
    trace = _one_line(_as_str(data.get("trace_id")), TRACE_MAX_CHARS) or "-"
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": _template_for(data),
            "title": {
                "tag": "plain_text",
                "content": _one_line(title, TITLE_MAX_CHARS) or "运行摘要",
            },
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": _detail_block(data)}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": _agents_block(data)}},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": _clean(f"trace_id: {trace}", 200)},
                ],
            },
        ],
    }
    return _cap_card_size(card)


def _agents_text(data: dict) -> str:
    """纯文本里的参与模型一行（超出上限用「等 N 个」收尾）。"""
    agents = data.get("agents")
    if not isinstance(agents, (list, tuple)) or not agents:
        return ""
    names: list[str] = []
    for item in agents[:TEXT_AGENT_LIMIT]:
        if isinstance(item, dict):
            name = _one_line(_as_str(item.get("name")), 40) or "?"
            model = _one_line(_as_str(item.get("model_id")), 40)
            names.append(f"{name}（{model}）" if model else name)
        else:
            names.append(_one_line(_as_str(item), 40) or "?")
    suffix = f" 等 {len(agents)} 个" if len(agents) > len(names) else ""
    return "参与模型：" + "、".join(names) + suffix


def build_feishu_text(summary: dict) -> dict:
    """构造纯文本消息体 `{"msg_type": "text", "content": {"text": ...}}`。

    首行是一句话摘要，其后按行补上模式 / 轮次 / Token / 费用 / 裁判 / 纠偏 /
    参与模型 / trace_id；整体截断到 FEISHU_TEXT_LIMIT。
    """
    data = summary if isinstance(summary, dict) else {}
    lines = [summary_sentence(data)]
    for label, value in _summary_pairs(data, with_basics=False):
        lines.append(f"{label}：{value}")
    agents_line = _agents_text(data)
    if agents_line:
        lines.append(agents_line)
    trace = _one_line(_as_str(data.get("trace_id")), TRACE_MAX_CHARS)
    if trace:
        lines.append(f"trace_id: {trace}")
    text = _clean("\n".join(lines), FEISHU_TEXT_LIMIT)
    return {"msg_type": "text", "content": {"text": text}}


def push_feishu_webhook(
    url: str,
    summary: dict,
    as_card: bool = True,
    timeout: float = 8.0,
) -> dict:
    """推送到飞书群机器人 webhook，返回 dispatch 结果并附加 `kind`（card / text）。

    `as_card=True` 发交互卡片，否则发纯文本；网络失败只体现在返回值的 ok=False 里，
    不会抛出异常（调用方无需 try/except）。
    """
    if as_card:
        payload = {"msg_type": "interactive", "card": build_feishu_card(summary)}
        kind = "card"
    else:
        payload = build_feishu_text(summary)
        kind = "text"
    result = post_json_webhook(url, payload, timeout=timeout)
    if not isinstance(result, dict):
        result = {"ok": False, "status": 0, "body": "", "error": "unexpected dispatch result"}
    result["kind"] = kind
    return result


def _unknown_event() -> dict:
    """固定的兜底结构：任何畸形输入都返回它（字段齐全、类型稳定）。"""
    return {
        "type": "unknown",
        "challenge": "",
        "text": "",
        "user": "",
        "chat_id": "",
        "message_id": "",
    }


def _plain(value: object) -> str:
    """只接受字符串原值（数字等一律视为空，避免把结构体拼进文本）。"""
    return value if isinstance(value, str) else ""


def _is_challenge(body: dict) -> bool:
    """是否为 url_verification 回显请求。"""
    if _plain(body.get("type")) == "url_verification":
        return True
    # 部分网关只转发 challenge 字段：没有 event 时按验证请求处理
    return "challenge" in body and "event" not in body


def _sender_id(event: dict) -> str:
    """从 event.sender 里取发送者标识（优先 open_id，再 union_id / user_id）。"""
    sender = event.get("sender")
    if isinstance(sender, dict):
        sender_id = sender.get("sender_id")
        if isinstance(sender_id, dict):
            for key in ("open_id", "union_id", "user_id"):
                value = _plain(sender_id.get(key))
                if value:
                    return value
        for key in ("open_id", "union_id", "user_id"):
            value = _plain(sender.get(key))
            if value:
                return value
    return _plain(event.get("open_id"))


def _strip_mentions(text: str) -> str:
    """去掉 @提及（`@_user_1`、`@_all`）并把连续空格折叠为单个空格。"""
    cleaned = _MENTION_RE.sub(" ", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _extract_text(content: object) -> str:
    """从 message.content 提取正文：JSON 字符串 → `{"text": ...}`。

    兼容三种形态：已解析好的 dict、标准 JSON 字符串、非 JSON 的裸文本
    （自建网关偶尔直接转发纯文本）。解析失败返回空串，绝不抛异常。
    """
    text = ""
    if isinstance(content, dict):
        text = _plain(content.get("text"))
    elif isinstance(content, str):
        raw = content.strip()
        if not raw:
            return ""
        parsed: object = None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            text = _plain(parsed.get("text"))
        elif not raw.startswith(("{", "[")):
            text = raw
    else:
        return ""
    return _strip_mentions(_clean(text))


def _parse_event(body: object) -> dict:
    """`parse_feishu_event` 的实际实现（外层负责兜底异常）。"""
    out = _unknown_event()
    if not isinstance(body, dict):
        return out
    if _is_challenge(body):
        out["type"] = "challenge"
        out["challenge"] = _plain(body.get("challenge"))
        return out

    event = body.get("event")
    if not isinstance(event, dict):
        return out
    header = body.get("header")
    event_type = _plain(header.get("event_type")) if isinstance(header, dict) else ""
    if event_type and event_type != MESSAGE_EVENT_TYPE:
        # 明确的非消息事件（已读回执、入群通知等）：保持 unknown，避免误当用户消息
        return out
    message = event.get("message")
    if not isinstance(message, dict):
        return out

    out["type"] = "message"
    out["chat_id"] = _plain(message.get("chat_id")) or _plain(event.get("chat_id"))
    out["message_id"] = _plain(message.get("message_id")) or _plain(event.get("message_id"))
    out["user"] = _sender_id(event)
    out["text"] = _extract_text(message.get("content"))
    return out


def parse_feishu_event(body: dict) -> dict:
    """解析飞书事件回调，返回固定六键结构（type / challenge / text / user / chat_id / message_id）。

    - `url_verification` → `{"type": "challenge", "challenge": ...}`（原样回显）；
    - `im.message.receive_v1` → 提取正文（已去掉 @提及）与发送者信息；
    - 其它事件 / None / 缺字段 / 类型不符 → `{"type": "unknown", ...}`，**绝不抛异常**。
    """
    try:
        return _parse_event(body)
    except Exception:
        # 兜底：解析器自身的意外错误也必须降级成 unknown，不能影响 webhook 端点
        return _unknown_event()
