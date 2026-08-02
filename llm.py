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
from config import settings

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
