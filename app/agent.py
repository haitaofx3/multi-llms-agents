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

from app.config import PROJECT_ROOT, settings
from app.memory import MemorySystem
from app.tools import build_default_tools

MEMORY_DIR = PROJECT_ROOT / "data" / "memories"


class Agent:
    def __init__(self, agent_id, name, role, personality, model_config,
                 is_main=False, forget_config=None, stance="NEUTRAL"):
        self.agent_id = agent_id
        self.name = name
        self.role = role or "机器人"
        self.personality = personality or "理性、严谨、乐于协作的机器人"
        self.model_config = model_config
        self.is_main = is_main
        # 立场枚举：HOST(主持人) / PRO(正方) / CON(反方) / NEUTRAL(中立)
        self.stance = (stance or "NEUTRAL").upper()
        self.memory = MemorySystem(agent_id, MEMORY_DIR, forget_config)
        self.mcp = build_default_tools(name, self.memory)
        self.history = []  # [{role, content}] 不含 system
        self.max_history = 24
        self.compressed_summary = ""  # 长对话压缩摘要（优化 A9）

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
        lines.append("【讨论形式】本场为辩论式多轮协作讨论：由主持人引导与追问，各 Agent 基于自身立场立论、"
                     "辩护与交锋，最终由裁判评分并汇总。请始终维持你的人设与立场。")
        lines.append("")
        # 立场约束（架构文档 §2.2/2.3：Pro/Con 严格立场，禁止倒戈）
        stance_line = self._stance_declaration()
        if stance_line:
            lines.append(stance_line)
            lines.append("")
        # 记忆
        mem = self.memory.context_text("", top_k=4)
        lines.append("【你的记忆】\n" + mem)
        lines.append("")
        # 长对话压缩摘要（优化 A9）
        if self.compressed_summary:
            lines.append("【历史对话摘要】\n" + self.compressed_summary)
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
            lines.append("【你的职责（主持人 / Supervisor 视角）】你主持一场辩论："
                         "第1轮为协作 Agent 分派调研 / 立论任务并陈述你的开场立场；之后的轮次针对各方观点抛出质疑、"
                         "要求论据、指出分歧，推动多轮交锋；最终综合各方观点与裁判裁决进行总结。"
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
                lines.append("【你的职责（协作 Agent / 辩论参与者）】你正在参与一场辩论。"
                             "针对其他 Agent 的观点进行辩护、补充或反驳，可调用 knowledge_search 检索论据支撑。"
                             "请观点鲜明、有理有据，并始终围绕你的立场展开。")
        return "\n".join(lines)

    def _stance_declaration(self) -> str:
        """立场声明：PRO/CON 强制绑定立场，禁止倒戈。"""
        if self.stance == "PRO":
            return ("【你的立场】你持「正方(PRO)」立场，核心主张是维护讨论主题的正面立场。"
                    "你必须坚定维护正方观点，禁止倒戈、禁止保持中立或折中表述。"
                    "反驳对方时必须指出其逻辑谬误（如滑坡谬误、稻草人），而非单纯否认。")
        if self.stance == "CON":
            return ("【你的立场】你持「反方(CON)」立场，核心主张是质疑/反对讨论主题的正面立场。"
                    "你必须坚定维护反方观点，禁止倒戈、禁止保持中立或折中表述。"
                    "必须直接回应对方核心论据，优先使用逻辑归谬法，其次使用事实反证。")
        if self.stance == "HOST":
            return ("【你的立场】你是辩论主持人(HOST)，保持中立公正，不偏向任何一方。"
                    "你的职责是引导辩论、追问与归纳，不亲自立论或倒向某一方。")
        return ""  # NEUTRAL：不作强制声明

    def append_history(self, role, content):
        self.history.append({"role": role, "content": content})
        # 条数裁剪（原有：滚动窗口）
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
        # 字符硬上限（优化 A9）：超出 CONTEXT_MAX_CHARS 时丢弃最旧的轮次，
        # 保证单 Agent 上下文不超过硬性预算，防止长对话 token 膨胀。
        while self._history_chars() > settings.CONTEXT_MAX_CHARS and len(self.history) > 2:
            self.history.pop(0)

    def _history_chars(self) -> int:
        return sum(len(str(m.get("content", ""))) for m in self.history)

    # ---------------- 长对话压缩（优化 A9） ----------------
    async def compress_history(self, force: bool = False):
        """
        把最旧的轮次压缩为一条摘要，保留最近 CONTEXT_COMPRESS_KEEP_RECENT 条原文。

        触发条件：历史字符数 ≥ CONTEXT_COMPRESS_THRESHOLD（或 force=True），
        且历史条数多于「保留条数 + 2」。摘要存放在 self.compressed_summary，
        由 system_prompt 注入；history 截断为最近保留条。
        摘要优先用自身 model_config 调用 LLM 生成（经 to_thread 避免阻塞事件循环），
        失败时回退为确定性截断拼接。返回本次调用的 usage dict（用于预算统计），
        未触发压缩时返回 None。
        """
        keep = max(2, settings.CONTEXT_COMPRESS_KEEP_RECENT)
        if len(self.history) <= keep + 2:
            return None
        if not force and self._history_chars() < settings.CONTEXT_COMPRESS_THRESHOLD:
            return None
        old = self.history[:-keep]
        texts = [f"{m['role']}: {m['content']}" for m in old]
        summary, usage = await self._summarize(texts)
        self.compressed_summary = summary
        self.history = self.history[-keep:]
        return usage

    async def _summarize(self, texts):
        """生成摘要文本，返回 (summary, usage|None)。"""
        try:
            import asyncio

            from app.llm import chat
            joined = "\n".join(texts)
            messages = [
                {"role": "system",
                 "content": "你是对话历史摘要器。把给定的对话历史压缩为核心要点，"
                            "保留关键事实、结论与分歧点，150 字以内，不要输出其他内容。"},
                {"role": "user", "content": joined},
            ]
            result = await asyncio.to_thread(
                lambda: chat(messages, self.model_config, temperature=0.3))
            content = (result.get("content") or "").strip()
            if content:
                return content, result.get("usage") or {}
        except Exception:
            pass
        return self._naive_summary(texts), None

    def _naive_summary(self, texts) -> str:
        """非 LLM 兜底摘要：保留每条文本头部片段。"""
        parts = []
        for t in texts:
            s = t.split(": ", 1)[-1].strip()
            parts.append(s[:120])
        head = "；".join(parts[:6])
        if len(parts) > 6:
            head += f" 等 {len(parts)} 条"
        return f"此前对话要点：{head}"

    def reset_history(self):
        self.history = []
        self.compressed_summary = ""
