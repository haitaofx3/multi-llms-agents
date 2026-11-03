"""
start.py — 启动入口

在 agent_env 虚拟环境中运行：
    python start.py
然后浏览器打开 http://localhost:8000

三个入口页面：
    /         圆桌 UI（WebSocket 实时流式界面 + 纠偏 / 摘要 / 续轮）
    /console  接口控制台（接口级调用与模块化调试）
    /docs     自动生成的 OpenAPI 文档

默认仅绑定 127.0.0.1（个人级试点：不对外暴露）。如需局域网访问：
    HOST=0.0.0.0 PORT=8000 python start.py
⚠ 一旦绑定非回环地址，请在 .env 里设置 AUTH_TOKEN，否则任何人都能改写配置、
发起（消耗真实模型额度的）讨论。DSH 集成另外支持 DSH_SHARED_SECRET 共享密钥。
"""
import os

import uvicorn

from muti_llm_agent.main import app

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    # 关闭 uvicorn 自带的访问日志：本项目已有结构化 JSON 日志（含 trace_id），
    # 两套日志并存只会让排障时更难对齐。
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
