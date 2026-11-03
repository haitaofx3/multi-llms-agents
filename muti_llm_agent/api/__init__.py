"""
api/__init__.py — 路由模块注册表

每个 router 模块带三个模块级常量，供接口控制台（/api/dev/endpoints）自动分组：

    MODULE_NAME   模块短名（用于 URL 参数与过滤）
    MODULE_TITLE  中文标题
    MODULE_DESC   一句话说明

`ALL_MODULES` 是模块清单的唯一事实来源：新增路由模块只要在这里登记一次，
接口控制台、冒烟测试、模块化调试页都会自动看到它。
"""
from __future__ import annotations

from fastapi import APIRouter

from muti_llm_agent.api import (
    router_dev,
    router_export,
    router_integration,
    router_kb,
    router_run,
    router_setup,
    router_stream,
)

# (模块名, 路由对象, 模块文件) —— 顺序即接口控制台的分组顺序
ALL_MODULES: list[tuple[str, APIRouter, object]] = [
    (router_setup.MODULE_NAME, router_setup.router, router_setup),
    (router_kb.MODULE_NAME, router_kb.router, router_kb),
    (router_run.MODULE_NAME, router_run.router, router_run),
    (router_stream.MODULE_NAME, router_stream.router, router_stream),
    (router_export.MODULE_NAME, router_export.router, router_export),
    (router_dev.MODULE_NAME, router_dev.router, router_dev),
    (router_integration.MODULE_NAME, router_integration.router, router_integration),
]

__all__ = ["ALL_MODULES"]
