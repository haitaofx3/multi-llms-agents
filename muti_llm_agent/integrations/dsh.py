"""
dsh.py — 面向 DSH 等外部 Agent 的「工具清单 + 参数校验」层

为什么这样拆分：
  - 外部 Agent（DSH / 其它编排框架）通过 HTTP 把本系统当成一组工具来调用，需要
    两样东西：机器可读的工具描述（manifest）和**服务端**的参数校验。把两者放在
    同一文件、共用同一份 `TOOL_SPECS`，可以保证「文档描述」与「真正校验」不会漂移。
  - 刻意不引入 jsonschema：只需要 type / required / enum / default 四种能力，
    手写最小校验（约百行）比多一个运行时依赖更划算，错误信息也能写成
    「哪个参数、期望什么、实际收到什么」这种对模型友好的中文。
  - 本模块不依赖 FastAPI：invoke 端点只需 `try: validate_arguments(...) except
    DshToolError as exc: return JSONResponse(exc.to_dict(), status_code=400)` 即可，
    校验逻辑因此可以脱离 Web 框架单独测试。
"""
from __future__ import annotations

import copy
import re

# manifest 的默认版本号 / 名称 / 调用路径（与 main.py 约定一致）
MANIFEST_NAME = "muti_llm_agent"
DEFAULT_VERSION = "1.0.0"
INVOKE_PATH = "/api/integrations/dsh/invoke"

# 工具清单：唯一事实来源。parameters 是 JSON Schema 的子集（本模块只实现
# type / required / enum / default，其余关键字仅作说明用途，不参与校验）。
TOOL_SPECS: list[dict] = [
    {
        "name": "collab_start",
        "description": "开始一次多模型协同 / 辩论：提交议题后立即返回 session_id，"
                       "后续用 collab_status 轮询进度、collab_summary 取结果。",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "default": "",
                    "description": "会话标识；留空表示复用默认会话。传固定值可在同一会话上"
                                   "连续做「开始 → 纠偏 → 续轮」，承接上下文与迭代链。",
                },
                "topic": {
                    "type": "string",
                    "description": "讨论议题（必填，建议 10~200 字）。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["collaboration", "debate"],
                    "default": "collaboration",
                    "description": "协同（collaboration）或对抗辩论（debate）。",
                },
                "max_rounds": {
                    "type": "integer",
                    "default": 4,
                    "description": "最大轮次，建议 1~20。",
                },
                "recipe": {
                    "type": "string",
                    "default": "",
                    "description": "预设配方名；留空表示沿用会话已配置的 Agent 组合。",
                },
            },
            "required": ["topic"],
        },
    },
    {
        "name": "collab_status",
        "description": "查询当前运行状态（阶段、轮次、token、预估费用）；"
                       "不传 session_id 时使用默认会话。",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "default": "",
                    "description": "会话标识，留空表示默认会话。",
                },
            },
            "required": [],
        },
    },
    {
        "name": "collab_steer",
        "description": "运行中途注入人工纠偏（纠错 / 约束 / 补充事实 / 改变方向 / 禁止方向），"
                       "在下一个 Agent 回合生效，无需中断运行。",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "default": "",
                    "description": "会话标识，留空表示默认会话。",
                },
                "text": {
                    "type": "string",
                    "description": "纠偏内容（必填）。",
                },
                "kind": {
                    "type": "string",
                    "enum": ["correct", "constrain", "fact", "redirect", "stop"],
                    "default": "correct",
                    "description": "纠偏类型：纠错 / 约束 / 补充事实 / 改变方向 / 禁止方向。",
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "main", "agent"],
                    "default": "all",
                    "description": "作用域：全员 / 仅主持人 / 指定席位（配合 target）。",
                },
                "target": {
                    "type": "string",
                    "default": "",
                    "description": "目标 Agent 名；scope=agent 时生效，留空退化为全员。",
                },
                "priority": {
                    "type": "string",
                    "enum": ["normal", "high"],
                    "default": "normal",
                    "description": "normal=一次性注入；high=本轮内所有席位都注入。",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "collab_followup",
        "description": "补充材料后继续讨论若干轮：材料写入共享上下文，并指定本轮关注点。",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "default": "",
                    "description": "要续轮的会话标识（留空表示默认会话）。",
                },
                "materials": {
                    "type": "string",
                    "description": "补充材料正文（必填）。",
                },
                "focus": {
                    "type": "string",
                    "default": "",
                    "description": "希望本轮聚焦的问题；留空则由主持人自行判断。",
                },
                "max_rounds": {
                    "type": "integer",
                    "default": 2,
                    "description": "续轮轮数，建议 1~10。",
                },
            },
            "required": ["materials"],
        },
    },
    {
        "name": "collab_summary",
        "description": "取结构化运行摘要（主题、模式、轮次、token、费用、参与模型、裁判裁决）。",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "default": "",
                    "description": "会话标识，留空表示默认会话。",
                },
            },
            "required": [],
        },
    },
    {
        "name": "collab_export",
        "description": "导出讨论记录（Markdown / JSON / HTML），返回下载地址或原文。",
        "parameters": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["md", "json", "html"],
                    "default": "md",
                    "description": "导出格式，默认 md。",
                },
                "session_id": {
                    "type": "string",
                    "default": "",
                    "description": "会话标识，留空表示默认会话。",
                },
            },
            "required": [],
        },
    },
    {
        "name": "collab_history",
        "description": "列出历史讨论记录，可按题目 / trace_id 关键词过滤。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "default": "",
                    "description": "关键词；留空返回全部历史。",
                },
                "session_id": {
                    "type": "string",
                    "default": "",
                    "description": "会话标识（历史记录跨会话共享，此参数仅作回显与日志串联）。",
                },
            },
            "required": [],
        },
    },
]


class DshToolError(Exception):
    """DSH 工具调用契约错误：`code` 供程序判断，`message` 面向人 / 模型可读。

    错误码：
      - `unknown_tool`      工具名不在 TOOL_SPECS 中；
      - `invalid_arguments` 缺少必填参数 / 类型不符 / 枚举越界。
    """

    def __init__(self, code: str = "invalid_arguments", message: str = "") -> None:
        self.code = code or "invalid_arguments"
        self.message = message or self.code
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """直接可用的 HTTP 错误响应体（invoke 端点 400 时原样返回）。"""
        return {"ok": False, "code": self.code, "error": self.message}


def _spec_for(tool: str) -> dict:
    """按工具名查 spec；未知工具抛 DshToolError("unknown_tool")。"""
    name = tool.strip() if isinstance(tool, str) else ""
    for spec in TOOL_SPECS:
        if spec.get("name") == name:
            return spec
    available = "、".join(str(spec.get("name")) for spec in TOOL_SPECS)
    raise DshToolError("unknown_tool", f"未知工具：{tool!r}；可用工具：{available}")


def _preview(value: object, limit: int = 60) -> str:
    """把任意值压成短 repr 用于错误信息（自身也不能抛）。"""
    try:
        text = repr(value)
    except Exception:
        return "<unprintable>"
    return text if len(text) <= limit else text[:limit] + "…"


def _type_error(name: str, expected: str, value: object) -> DshToolError:
    """构造类型错误（不直接 raise，便于在表达式里 return）。"""
    return DshToolError(
        "invalid_arguments",
        f"参数 {name} 期望 {expected}，收到 {type(value).__name__}：{_preview(value)}",
    )


def _to_string(name: str, value: object) -> str:
    """转字符串：标量做字面转换，容器视为类型不符。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    raise _type_error(name, "string", value)


def _to_int(name: str, value: object) -> int:
    """转整数：整数字符串与整数值浮点可接受，其余抛类型错误。"""
    if isinstance(value, bool):
        raise _type_error(name, "integer", value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise _type_error(name, "integer", value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            try:
                return int(text)
            except ValueError:
                raise _type_error(name, "integer", value) from None
    raise _type_error(name, "integer", value)


def _to_bool(name: str, value: object) -> bool:
    """转布尔：兼容 true/false、1/0、yes/no 等常见外部 Agent 写法。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "y", "on"):
            return True
        if text in ("false", "0", "no", "n", "off"):
            return False
    raise _type_error(name, "boolean", value)


def _coerce(name: str, value: object, prop: dict) -> object:
    """按 schema 的 type 做类型转换，再校验 enum（顺序：先转再看枚举）。"""
    expected = prop.get("type")
    if expected == "string":
        converted: object = _to_string(name, value)
    elif expected == "integer":
        converted = _to_int(name, value)
    elif expected == "boolean":
        converted = _to_bool(name, value)
    elif expected == "object":
        if not isinstance(value, dict):
            raise _type_error(name, expected, value)
        converted = dict(value)
    elif expected == "array":
        if not isinstance(value, (list, tuple)):
            raise _type_error(name, expected, value)
        converted = list(value)
    elif expected:
        raise DshToolError("invalid_arguments", f"参数 {name} 的 schema 类型不受支持：{expected}")
    else:
        converted = value
    enum = prop.get("enum")
    if isinstance(enum, (list, tuple)) and converted not in enum:
        raise DshToolError(
            "invalid_arguments",
            f"参数 {name} 取值必须是 {list(enum)} 之一，收到：{_preview(converted)}",
        )
    return converted


def _is_blank(value: object) -> bool:
    """必填参数的「空」判定：None / 空白字符串 / 空容器 都算缺失。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def validate_arguments(tool: str, arguments: dict) -> dict:
    """校验并规整工具参数，返回可直接执行的参数字典。

    - 未知工具 → `DshToolError("unknown_tool")`；
    - 必填缺失 / 类型不符 / 枚举越界 → `DshToolError("invalid_arguments")`；
    - 缺失的可选参数补 schema 里的 default（深拷贝，避免调用方改到 TOOL_SPECS）；
    - 标量做宽松类型转换（"5" → 5、"true" → True），容器不做猜测；
    - 未在 schema 中声明的字段既不透传也不报错（前向兼容：外部 Agent 多传字段
      不会因为本系统升级而失败，但也不会被下游误用）。
    """
    spec = _spec_for(tool)
    schema = spec.get("parameters")
    if not isinstance(schema, dict):
        schema = {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    required = [name for name in (schema.get("required") or []) if isinstance(name, str)]
    if arguments is None:
        raw: dict = {}
    elif isinstance(arguments, dict):
        raw = arguments
    else:
        raise DshToolError(
            "invalid_arguments",
            f"arguments 必须是 JSON 对象，收到 {type(arguments).__name__}：{_preview(arguments)}",
        )

    normalized: dict = {}
    for name, prop in properties.items():
        if not isinstance(name, str) or not isinstance(prop, dict):
            continue
        if name in raw and raw[name] is not None:
            normalized[name] = _coerce(name, raw[name], prop)
        elif "default" in prop:
            normalized[name] = copy.deepcopy(prop["default"])
    for name in required:
        if name not in normalized or _is_blank(normalized[name]):
            raise DshToolError("invalid_arguments", f"缺少必填参数：{name}（{tool}）")
    return normalized


def dsh_manifest(base_url: str = "", version: str = "1.0.0") -> dict:
    """返回 DSH 工具清单（tools 为 TOOL_SPECS 的深拷贝，调用方可安全修改）。"""
    return {
        "name": MANIFEST_NAME,
        "version": version or DEFAULT_VERSION,
        "description": "多模型协同 / 辩论系统：发起讨论、中途纠偏、追踪状态、"
                       "取结构化摘要、导出记录、查询历史。",
        "base_url": base_url or "",
        "transport": {"type": "http", "invoke_path": INVOKE_PATH},
        "tools": copy.deepcopy(TOOL_SPECS),
    }
