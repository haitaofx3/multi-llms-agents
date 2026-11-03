"""
integrations/__init__.py — 对外集成层的统一出口

为什么单独拆一个包：
  - 外部集成（IM 通知、其它 Agent 的工具调用）与核心编排是两种生命周期：前者是对
    外契约、变更慢；后者迭代快。混在 orchestrator / main 里会让每次对外接口调整
    都牵动核心链路的 diff。
  - 子模块职责单一、依赖单向（feishu → dispatch），便于单独测试与替换：dispatch
    只管「把 JSON 发出去」，feishu 只管「飞书协议适配」，dsh 只管「工具清单 + 参数
    校验」。
  - 本包只依赖标准库，不 import FastAPI / requests 等运行时依赖，任何进程
    （脚本、CI、测试）都能安全地 import 本层而不拉起 Web 服务。
"""
from __future__ import annotations

from muti_llm_agent.integrations.dispatch import (
    dispatch_run_summary,
    post_json_webhook,
    summary_sentence,
)
from muti_llm_agent.integrations.dsh import (
    TOOL_SPECS,
    DshToolError,
    dsh_manifest,
    validate_arguments,
)
from muti_llm_agent.integrations.feishu import (
    FEISHU_TEXT_LIMIT,
    build_feishu_card,
    build_feishu_text,
    parse_feishu_event,
    push_feishu_webhook,
)

__all__ = [
    "FEISHU_TEXT_LIMIT",
    "TOOL_SPECS",
    "DshToolError",
    "build_feishu_card",
    "build_feishu_text",
    "dispatch_run_summary",
    "dsh_manifest",
    "parse_feishu_event",
    "post_json_webhook",
    "push_feishu_webhook",
    "summary_sentence",
    "validate_arguments",
]
