"""
start.py — 启动入口

在 agent_env 虚拟环境中运行：
    python start.py
然后浏览器打开 http://localhost:8000
"""
import uvicorn
from app.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
