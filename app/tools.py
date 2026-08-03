"""
tools.py — 每个 Agent 的 MCP 工具对象

每个 agent 拥有自己的 MCP 注册表（MCPRegistry），用于：
  - search(query):   按描述检索"应该用哪个工具"来完成任务
  - schemas():       导出 OpenAI function-calling 工具定义
  - call(name, args):执行工具

内置工具：
  - knowledge_search: 检索共享向量知识库（所有 agent 共用）
  - web_search:       联网搜索（SerpApi，可选）
  - memory_recall:    回忆本 agent 的记忆（短期/长期/情境）
任务分派/回报由 orchestrator 通过专用工具(intercept)处理。
"""
import json
import urllib.request
import urllib.parse
from app.config import settings
from app.vector_store import shared_vector_store


class Tool:
    """MCP 工具定义。"""

    def __init__(self, name, description, parameters, run_fn):
        self.name = name
        self.description = description
        self.parameters = parameters  # JSON schema
        self.run_fn = run_fn

    def run(self, **kwargs) -> str:
        try:
            return str(self.run_fn(**kwargs))
        except Exception as e:  # noqa
            return f"[工具执行出错] {e}"

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class MCPRegistry:
    """某个 Agent 的 MCP 工具注册表。"""

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.tools = {}

    def register(self, tool: Tool):
        self.tools[tool.name] = tool

    def schemas(self):
        return [t.schema() for t in self.tools.values()]

    def search(self, query: str, top_k: int = 3):
        """按描述关键词检索可用工具（用于"搜索对应工具"）。"""
        q = set(query.lower().split())
        scored = []
        for t in self.tools.values():
            desc = (t.name + " " + t.description).lower()
            s = sum(1 for w in q if w and w in desc)
            scored.append((s, t.name, t.description))
        scored.sort(key=lambda x: -x[0])
        return [{"name": n, "description": d} for _, n, d in scored[:top_k]]

    def call(self, name: str, arguments: dict) -> str:
        tool = self.tools.get(name)
        if not tool:
            return f"[未知工具] {name}"
        return tool.run(**arguments)

    def names(self):
        return list(self.tools.keys())


# ---------------- 内置工具实现 ----------------
def _knowledge_search(query: str, top_k: int = 3) -> str:
    hits = shared_vector_store.search(query, top_k=top_k)
    if not hits:
        return "（共享知识库中未检索到相关内容）"
    lines = []
    for i, h in enumerate(hits, 1):
        src = h.get("source", "知识库")
        lines.append(f"[{i}] 来源:{src} | {h.get('text', '')}")
    return "共享知识库检索结果：\n" + "\n".join(lines)


def _web_search(query: str, top_k: int = 5) -> str:
    if not settings.SERPAPI_API_KEY:
        return "（未配置 SERPAPI_API_KEY，无法联网搜索）"
    params = {"q": query, "api_key": settings.SERPAPI_API_KEY, "num": top_k}
    url = "https://serpapi.com/search?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("organic_results", [])[:top_k]
        if not results:
            return "（联网搜索无结果）"
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.get('title','')} | {r.get('snippet','')} | {r.get('link','')}")
        return "联网搜索结果：\n" + "\n".join(lines)
    except Exception as e:  # noqa
        return f"（联网搜索失败：{e}）"


def _memory_recall(memory, query: str = "", top_k: int = 4) -> str:
    txt = memory.context_text(query, top_k)
    return "本 Agent 记忆：\n" + txt


def build_default_tools(agent_name: str, memory=None):
    """为某个 agent 构建默认 MCP 工具集。"""
    reg = MCPRegistry(agent_name)
    reg.register(Tool(
        "knowledge_search",
        "检索团队共享的向量知识库，获取背景资料与事实。当需要查证信息或获取领域知识时使用。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词/问题"},
                "top_k": {"type": "integer", "description": "返回条数，默认3"},
            },
            "required": ["query"],
        },
        lambda query, top_k=3: _knowledge_search(query, top_k),
    ))
    reg.register(Tool(
        "web_search",
        "联网搜索实时信息。当共享知识库不足、需要最新外部资料时使用。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "top_k": {"type": "integer", "description": "返回条数，默认5"},
            },
            "required": ["query"],
        },
        lambda query, top_k=5: _web_search(query, top_k),
    ))
    if memory is not None:
        reg.register(Tool(
            "memory_recall",
            "回忆本 Agent 的短期/长期/情境记忆，用于保持角色一致性与延续对话。",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "回忆关键词，可空"},
                    "top_k": {"type": "integer", "description": "返回条数，默认4"},
                },
                "required": [],
            },
            lambda query="", top_k=4: _memory_recall(memory, query, top_k),
        ))
    return reg
