"""
llm.py — 大语言模型统一接入层

支持任意 OpenAI 兼容协议的服务（DeepSeek / Qwen / MiMo / 自定义）。
- Qwen:   来自 .env 默认 (dashscope compatible-mode)
- DeepSeek: https://api.deepseek.com
- MiMo:    https://api.xiaomimimo.com/v1 (mimo-v2.5)
- 自定义:   用户在前端填写 base_url / api_key / model_id

提供原生 function-calling（tools）。返回结构包含 content 与 tool_calls。
"""
import json
import re
from app.config import settings

from openai import OpenAI

# 模型供应商预设
PROVIDER_PRESETS = {
    "qwen": {
        "base_url": settings.QWEN_BASE_URL,
        "api_key": settings.QWEN_API_KEY,
        "model_id": settings.QWEN_MODEL_ID,
    },
    "deepseek": {
        "base_url": settings.DEEPSEEK_BASE_URL,
        # 注意：不能用 `or settings.LLM_API_KEY` 兜底——LLM_API_KEY 是默认模型（MiMo）的 key，
        # 拿它请求 api.deepseek.com 必然 401，且错误信息极具误导性。
        "api_key": settings.DEEPSEEK_API_KEY,
        "model_id": settings.DEEPSEEK_MODEL_ID,
    },
    "mimo": {
        "base_url": settings.MIMO_BASE_URL,
        "api_key": settings.MIMO_API_KEY,
        "model_id": settings.MIMO_MODEL_ID,
    },
    "default": {
        "base_url": settings.LLM_BASE_URL,
        "api_key": settings.LLM_API_KEY,
        "model_id": settings.LLM_MODEL_ID,
    },
}


def resolve_model_config(provider: str = "default", base_url: str = "", api_key: str = "",
                         model_id: str = ""):
    """解析出一个完整的模型配置字典。

    - provider == "custom"：不再静默回退默认预设。base_url / api_key / model_id
      任一为空都会抛出 ValueError，避免用错误的供应商端点/凭据请求导致 400/404。
    - 预置提供商（qwen / deepseek / default）：API Key 为空时同样提前报错，
      提示用户去 .env 补充，而不是等到请求时才暴露难以定位的认证错误。
    """
    if provider == "custom":
        missing = [name for name, val in
                   (("base_url", base_url), ("api_key", api_key), ("model_id", model_id))
                   if not val.strip()]
        if missing:
            raise ValueError(
                f"自定义模型缺少必填项: {', '.join(missing)}。"
                "请在 Agent 配置卡片中填写 Base URL / API Key / Model ID。"
            )
        return {
            "base_url": base_url.strip(),
            "api_key": api_key.strip(),
            "model_id": model_id.strip(),
        }

    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["default"])
    cfg = {
        "base_url": base_url or preset["base_url"],
        "api_key": api_key or preset["api_key"],
        "model_id": model_id or preset["model_id"],
    }
    if not cfg["api_key"].strip():
        raise ValueError(
            f"模型提供商「{provider}」未配置 API Key。"
            "请在 .env 中补充对应配置（如 LLM_API_KEY / DEEPSEEK_API_KEY）。"
        )
    return cfg


def _normalize_base_url(base_url: str) -> str:
    """OpenAI SDK 会自动在 base_url 后拼接 /chat/completions。

    若用户填的是完整端点（如 https://api.xiaomimimo.com/v1/chat/completions），
    直接使用会拼出双重路径。这里剥离尾部 /chat/completions 做兜底。
    """
    url = (base_url or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")]
    return url


def _client(model_config: dict) -> OpenAI:
    return OpenAI(
        base_url=_normalize_base_url(model_config["base_url"]),
        api_key=model_config["api_key"] or "EMPTY",
        timeout=settings.LLM_TIMEOUT,
    )


def _estimate_usage(messages, output_text: str = "") -> dict:
    """粗略估算 token 用量（流式或服务未返回 usage 时的兜底）。

    中文约 2 字符/token，英文约 4 字符/token，这里统一按 2 估算，
    足够用于预算控制（预算熔断场景无需精确到个位）。
    """
    def _tokens(s: str) -> int:
        return max(1, len(s) // 2)

    prompt = sum(_tokens(str(m.get("content", ""))) for m in messages)
    completion = _tokens(output_text or "")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _usage_from_response(resp) -> dict:
    """从 OpenAI 兼容响应中提取 usage；缺失则返回 None。"""
    try:
        u = resp.usage
        if u is not None:
            return {
                "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                "total_tokens": getattr(u, "total_tokens", 0) or 0,
            }
    except Exception:  # noqa
        pass
    return None


def _strip_think(text: str) -> str:
    """移除 Qwen3 等模型产生的 <think>...</think> 思考片段。"""
    if not text:
        return text
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


class ThinkFilter:
    """
    流式场景下跨 chunk 边界剥离 <think>...</think> 思考片段。

    由于 LLM 流式返回的 token 可能把 `<think>` / `</think>` 标签拆散到多个
    chunk 中，这里维护一个短缓冲区：检测到 `<think>` 前缀未闭合时先暂存尾部，
    等到确认不是标签（或遇到 `</think>`）再放行，保证过滤结果与非流式
    `_strip_think` 一致。
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"
    _MAX_KEEP = max(len(_OPEN), len(_CLOSE)) - 1  # 最多暂存一个标签长度减1

    def __init__(self):
        self._buf = ""
        self._in_think = False

    def feed(self, delta: str) -> str:
        """输入一段增量文本，返回可以安全展示的部分（思考片段已剥离）。"""
        if not delta:
            return ""
        self._buf += delta
        out = ""
        while self._buf:
            if self._in_think:
                idx = self._buf.find(self._CLOSE)
                if idx == -1:
                    # 只保留可能包含 `</think>` 起点的尾部，等待跨 chunk 闭合
                    if len(self._buf) <= self._MAX_KEEP:
                        break  # 已是最小缓冲，等下一个 chunk
                    self._buf = self._buf[-self._MAX_KEEP:]
                    continue
                self._buf = self._buf[idx + len(self._CLOSE):]
                self._in_think = False
                continue
            # 非思考态：找 `<think>` 开头
            idx = self._buf.find(self._OPEN)
            if idx == -1:
                # 尾部可能是被拆散的 `<thin` 等前缀，先暂存再判断
                keep = 0
                for k in range(1, min(len(self._buf), self._MAX_KEEP) + 1):
                    tail = self._buf[-k:]
                    if self._OPEN.startswith(tail):
                        keep = k
                if keep:
                    if keep == len(self._buf):
                        break  # 整个缓冲都是 `<think>` 前缀，等下一个 chunk
                    out += self._buf[:-keep]
                    self._buf = self._buf[-keep:]
                else:
                    out += self._buf
                    self._buf = ""
                continue
            out += self._buf[:idx]
            self._buf = self._buf[idx + len(self._OPEN):]
            self._in_think = True
        return out

    def flush(self) -> str:
        """流结束后调用，返回尚未放行的残留文本（思考中则丢弃）。"""
        return "" if self._in_think else self._buf


def chat_stream(messages, model_config: dict, tools=None, tool_choice="auto",
                temperature=0.7):
    """
    流式调用 chat completion（生成器）。

    yield 事件（dict）：
      {"type": "content",        "delta": str}   增量文本（已剥离 think 片段）
      {"type": "tool_call_args", "index": int, "delta": str}  工具参数增量
      {"type": "final", "result": {...}}           流结束，携带完整结果

    完整结果结构与 `chat()` 一致：{content, tool_calls, assistant_message}。
    """
    client = _client(model_config)
    kwargs = {
        "model": model_config["model_id"],
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    resp = client.chat.completions.create(**kwargs)

    filter_ = ThinkFilter()
    content_parts = []
    tool_acc = {}  # index -> {"id","name","arguments"}
    usage = None

    for chunk in resp:
        # 捕获流式 usage（部分提供商在最后一个 chunk 附带）
        u = _usage_from_response(chunk)
        if u:
            usage = u
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta

        # ---- 文本增量（过滤 think 片段） ----
        if getattr(delta, "content", None):
            out = filter_.feed(delta.content)
            if out:
                content_parts.append(out)
                yield {"type": "content", "delta": out}

        # ---- 工具调用增量（按 index 累积） ----
        for tc in (getattr(delta, "tool_calls", None) or []):
            idx = tc.index if tc.index is not None else 0
            acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                acc["id"] = tc.id
            if tc.function and tc.function.name:
                acc["name"] = tc.function.name
            if tc.function and tc.function.arguments:
                acc["arguments"] += tc.function.arguments
                yield {"type": "tool_call_args", "index": idx,
                       "delta": tc.function.arguments}

    # 流结束：放行残留文本
    tail = filter_.flush()
    if tail:
        content_parts.append(tail)
        yield {"type": "content", "delta": tail}

    # 组装完整结果（与非流式 chat() 返回结构一致）
    content = "".join(content_parts).strip()
    tool_calls = []
    assistant_tool_calls = []
    for idx in sorted(tool_acc):
        acc = tool_acc[idx]
        try:
            args = json.loads(acc["arguments"] or "{}")
        except Exception:
            args = {}
        tool_calls.append({"id": acc["id"], "name": acc["name"], "arguments": args})
        assistant_tool_calls.append({
            "id": acc["id"],
            "type": "function",
            "function": {"name": acc["name"], "arguments": acc["arguments"] or "{}"},
        })

    assistant_message = {"role": "assistant", "content": content}
    if assistant_tool_calls:
        assistant_message["tool_calls"] = assistant_tool_calls

    if usage is None:
        usage = _estimate_usage(messages, content)

    yield {"type": "final", "result": {
        "content": content,
        "tool_calls": tool_calls,
        "assistant_message": assistant_message,
        "usage": usage,
    }}


def chat(messages, model_config: dict, tools=None, tool_choice="auto", temperature=0.7):
    """
    调用 chat completion。

    返回 dict:
      {
        "content": str,            # 模型文本回复（已去掉 think 片段）
        "tool_calls": [             # 结构化工具调用
            {"name": str, "arguments": dict},
            ...
        ],
        "raw": 原始 message 对象
      }
    """
    client = _client(model_config)
    kwargs = {
        "model": model_config["model_id"],
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    resp = client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message

    content = _strip_think(msg.content or "")
    tool_calls = []
    assistant_tool_calls = None
    if getattr(msg, "tool_calls", None):
        assistant_tool_calls = []
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
            assistant_tool_calls.append({
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
            })

    # 构造可回传给 API 的 assistant 消息（维持多轮工具调用）
    assistant_message = {"role": "assistant", "content": content}
    if assistant_tool_calls:
        assistant_message["tool_calls"] = assistant_tool_calls

    usage = _usage_from_response(resp) or _estimate_usage(messages, content)

    return {"content": content, "tool_calls": tool_calls,
            "assistant_message": assistant_message, "usage": usage}
