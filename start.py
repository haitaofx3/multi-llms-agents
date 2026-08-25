"""
start.py — 启动入口

在 agent_env 虚拟环境中运行：
    python start.py
然后浏览器打开 http://localhost:8000

默认仅绑定 127.0.0.1（本地访问，避免远端暴露 / 长期挂载）。
如需开放局域网访问，可用环境变量覆盖：
    HOST=0.0.0.0 PORT=8000 python start.py
"""
import os

import uvicorn
from app.main import app

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")
