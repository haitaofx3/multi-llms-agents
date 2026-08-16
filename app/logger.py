"""
logger.py — 结构化日志治理（优化 A4）

统一输出 JSON 行结构化日志（含 trace_id / level / event / msg / 附加字段），
替代散落在各模块的 print 与手工拼串，便于检索、采集与问题定位。

用法：
    from app.logger import log, set_trace_id

    set_trace_id(trace_id)            # 写入当前线程上下文
    log("info", "embeddings.load", "模型加载完成", dim=384)
    log("warning", "orchestrator.budget", "预算熔断", remaining=0)
    log("error", "orchestrator.error", "编排器异常", exc=str(e))

日志级别：debug < info < warning < error。
"""
import json
import logging
import threading
import time

# 线程级上下文：trace_id 随运行线程传递（FastAPI 单线程处理请求/事件循环场景足够）
_ctx = threading.local()

# 后端：Python 标准 logging，输出到 stdout（保持与既有控制台输出一致）
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
_LOGGER = logging.getLogger("debate")
_LOGGER.setLevel(logging.DEBUG)
_LOGGER.addHandler(_handler)
_LOGGER.propagate = False

_LEVEL_NUM = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def set_trace_id(trace_id: str):
    """把 trace_id 写入当前线程上下文，供后续所有日志携带。"""
    _ctx.trace_id = trace_id


def get_trace_id() -> str:
    return getattr(_ctx, "trace_id", "")


def log(level: str = "info", event: str = "", message: str = "", **fields):
    """输出一条结构化日志。

    Args:
        level: debug / info / warning / error
        event: 事件名（如 embeddings.load、orchestrator.configure）
        message: 人类可读描述
        **fields: 附加字段（trace_id 默认取线程上下文，也可显式覆盖）
    """
    level = (level or "info").lower()
    if level not in _LEVEL_NUM:
        level = "info"
    rec = {
        "ts": int(time.time() * 1000),
        "level": level,
        "event": event or "log",
        "msg": message,
        "trace_id": fields.pop("trace_id", get_trace_id()),
    }
    for k, v in fields.items():
        if v is not None:
            rec[k] = v
    line = json.dumps(rec, ensure_ascii=False)
    num = _LEVEL_NUM[level]
    if num == logging.ERROR:
        _LOGGER.error(line)
    elif num == logging.WARNING:
        _LOGGER.warning(line)
    elif num == logging.DEBUG:
        _LOGGER.debug(line)
    else:
        _LOGGER.info(line)
