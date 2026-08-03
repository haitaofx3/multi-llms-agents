"""
agent.py — Agent 定义

每个 Agent 拥有：
  - 角色(role) 与 性格(personality)：对话必须严格符合，除非用户修改
  - 基础模型(model_config)：支持 deepseek / qwen / 自定义
  - 记忆系统(MemorySystem)：短期/长期/情境 三类记忆
  - MCP 工具对象(MCPRegistry)：知识检索 / 联网搜索 / 记忆回忆
  - 上下文变量：主 agent 分派任务时提供"任务接收方变量"，协作 agent 回填

第一个被创建的 agent 为主 agent(main)，其余为协作 agent。
"""
from pathlib import Path

from app.config import settings, PROJECT_ROOT
from app.memory import MemorySystem
from app.tools import build_default_tools

MEMORY_DIR = PROJECT_ROOT / "data" / "memories"


class Agent:
    def __init__(self, agent_id, name, role, personality, model_config,
                 is_main=False, forget_config=None):
        self.agent_id = agent_id
        self.name = name
        self.role = role or "机器人"
        self.personality = personality or "理性、严谨、乐于协作的机器人"
        self.model_config = model_config
        self.is_main = is_main
        self.memory = MemorySystem(agent_id, MEMORY_DIR, forget_config)
        self.mcp = build_default_tools(name, self.memory)
        self.history = []  # [{role, content}] 不含 system
        self.max_history = 24

    # ---------------- 上下文构建 ----------------
    def system_prompt(self, collaborators, shared_context, task_status_text,
                      mode, topic=""):
        lines = []
        lines.append(f"你是「{self.name}」。")
        lines.append(f"你的角色：{self.role}")
        lines.append(f"你的性格：{self.personality}")
        lines.append("")
        lines.append("【角色与性格约束】你必须严格以上述角色和性格进行对话，"
                     "保持人设一致，不得脱离角色，也不得声明自己是大语言模型或 AI。"
                     "只有在用户明确修改了你的角色/性格描述时，才允许改变。")
        lines.append("")
        lines.append("【讨论形式】本场为圆桌辩论：由主持人引导与追问，各 Agent 基于自身角色立论、"
                     "辩护与交锋，最终由主持人汇总。请始终维持你的人设与立场。")
        lines.append("")
        # 记忆
        mem = self.memory.context_text("", top_k=4)
        lines.append("【你的记忆】\n" + mem)
        lines.append("")
        # 共享上下文（任务接收方变量）
        if shared_context:
            ctx_lines = ["【共享上下文 / 任务接收方变量】"]
            for k, v in shared_context.items():
                by = v.get("by", "")
                val = v.get("value", "")
                done = "✅已回填" if val else "⏳待回填"
                ctx_lines.append(f"- {k}（由 {by} 提供）: {val or '（空）'} {done}")
            lines.append("\n".join(ctx_lines))
            lines.append("")
        # 任务状态
        if task_status_text:
            lines.append("【当前任务状态】\n" + task_status_text)
            lines.append("")
        # 模式指令
        if self.is_main:
            lines.append("【你的职责（主 Agent / 辩论主持人）】你主持一场圆桌辩论："
                         "第1轮为协作 Agent 分派调研 / 立论任务；之后的轮次针对各方观点抛出质疑、"
                         "要求论据、指出分歧，推动多轮交锋；最终综合各方观点进行总结。"
                         "请用 delegate_task 分派 / 追问任务，说明任务内容(content)、"
                         "期望效果(expected_outcome)与接收结果的变量名(var_name)。"
                         "你只做主持与分派，不亲自执行任务。")
            if collaborators:
                lines.append("可指派的协作 Agent：" + "、".join(c["name"] for c in collaborators))
        else:
            if mode == "task":
                lines.append("【你的职责（协作 Agent）】你收到一项任务。可调用 knowledge_search / "
                             "web_search / memory_recall 等工具获取信息，但只需通过 report_result "
                             "工具返回【最终结果】与【是否完成(completed)】，"
                             "并将结果填入主 Agent 指定的变量(var_name)。严禁返回中间思考或检索过程。")
            else:
                lines.append("【你的职责（协作 Agent / 辩论参与者）】你正在参与一场圆桌辩论。"
                             "针对主持人的质疑或其他 Agent 的观点，基于你的角色与性格进行辩护、"
                             "补充或反驳，可调用 knowledge_search 检索论据支撑。请观点鲜明、有理有据。")
        return "\n".join(lines)

    def append_history(self, role, content):
        self.history.append({"role": role, "content": content})
        # 裁剪
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

    def reset_history(self):
        self.history = []
