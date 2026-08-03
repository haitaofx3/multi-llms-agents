"""
llm.py — 大语言模型统一接入层

支持任意 OpenAI 兼容协议的服务（DeepSeek / Qwen / 自定义）。
- Qwen:   来自 .env 默认 (dashscope compatible-mode)
- DeepSeek: https://api.deepseek.com
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
        "base_url": settings.LLM_BASE_URL,
        "api_key": settings.LLM_API_KEY,
        "model_id": settings.LLM_MODEL_ID,
    },
    "deepseek": {
        "base_url": settings.DEEPSEEK_BASE_URL,
        "api_key": settings.DEEPSEEK_API_KEY or settings.LLM_API_KEY,
        "model_id": settings.DEEPSEEK_MODEL_ID,
    },
    "default": {
        "base_url": settings.LLM_BASE_URL,
        "api_key": settings.LLM_API_KEY,
        "model_id": settings.LLM_MODEL_ID,
    },
}


def resolve_model_config(provider: str = "default", base_url: str = "", api_key: str = "",
                         model_id: str = ""):
    """解析出一个完整的模型配置字典。"""
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["default"])
    return {
        "base_url": base_url or preset["base_url"],
        "api_key": api_key or preset["api_key"],
        "model_id": model_id or preset["model_id"],
    }


def _client(model_config: dict) -> OpenAI:
    return OpenAI(
        base_url=model_config["base_url"],
        api_key=model_config["api_key"] or "EMPTY",
        timeout=settings.LLM_TIMEOUT,
    )


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

    for chunk in resp:
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

    yield {"type": "final", "result": {
        "content": content,
        "tool_calls": tool_calls,
        "assistant_message": assistant_message,
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

    return {"content": content, "tool_calls": tool_calls, "assistant_message": assistant_message}
