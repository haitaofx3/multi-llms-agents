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
        self.history_rounds = []  # 与 history 平行：每条消息所属辩论轮次（预算强压缩按轮保留）
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
        # 双模式：合作模式（collaboration/collab）为合作式协作；其余为辩论式
        if mode in ("collaboration", "collab"):
            lines.append("【讨论形式】本场为合作式多轮协作：主 Agent 将整体任务拆分为子任务放入共享任务池，"
                         "各协作 Agent 主动认领并完成子任务，将结果回报给主 Agent，"
                         "最终由主 Agent 汇总整体成果。请始终维持你的人设。")
        else:
            lines.append("【讨论形式】本场为辩论式多轮协作讨论：由主持人引导与追问，各 Agent 基于自身立场立论、"
                         "辩护与交锋，最终由裁判评分并汇总。请始终维持你的人设与立场。")
        lines.append("")
        # 立场约束（架构文档 §2.2/2.3：Pro/Con 严格立场，禁止倒戈）
        # 合作模式弱化立场：无 PRO/CON 对抗，跳过立场强制声明
        if mode not in ("collaboration", "collab"):
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
            if mode in ("collaboration", "collab"):
                lines.append("【你的职责（主 Agent / 统筹者）】你负责把整体任务拆解为可独立完成的子任务，"
                             "用 delegate_task 将子任务放入共享任务池（recipient 留空），"
                             "说明任务内容(content)、期望效果(expected_outcome)与接收结果的变量名(var_name)；"
                             "统筹各协作 Agent 的认领与完成情况，最终汇总整体成果。"
                             "你只负责拆解与统筹，不亲自执行子任务。")
            else:
                lines.append("【你的职责（主持人 / Supervisor 视角）】你主持一场辩论："
                             "第1轮为协作 Agent 分派调研 / 立论任务并陈述你的开场立场；之后的轮次针对各方观点抛出质疑、"
                             "要求论据、指出分歧，推动多轮交锋；最终综合各方观点与裁判裁决进行总结。"
                             "请用 delegate_task 分派 / 追问任务，说明任务内容(content)、"
                             "期望效果(expected_outcome)与接收结果的变量名(var_name)。"
                             "你只做主持与分派，不亲自执行任务。")
            if collaborators:
                lines.append("可指派的协作 Agent：" + "、".join(c["name"] for c in collaborators))
        else:
            if mode in ("collaboration", "collab"):
                lines.append("【你的职责（协作 Agent）】你是一名协作成员。"
                             "可从任务池用 claim_task 认领未分配的子任务（认领后由你独占负责），"
                             "完成后用 report_result 回报【最终结果】与【是否完成(completed)】，"
                             "并将结果填入主 Agent 指定的变量(var_name)。"
                             "可调用 knowledge_search / web_search / memory_recall 等工具获取信息；"
                             "严禁返回中间思考或检索过程。")
            elif mode == "task":
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

    def append_history(self, role, content, round_no=None):
        """追加一条历史消息。round_no 缺省取当前最大轮次（第 1 轮起记 1）。

        history 与 history_rounds 平行维护：三处裁剪（条数上限、字符硬上限、
        压缩截断）都必须同步，否则预算强压缩的「按轮保留」会错位。
        """
        if round_no is None:
            round_no = max(self.history_rounds, default=1)
        self.history.append({"role": role, "content": content})
        self.history_rounds.append(int(round_no))
        # 条数裁剪（原有：滚动窗口）—— history_rounds 同步
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
            self.history_rounds = self.history_rounds[-self.max_history:]
        # 字符硬上限（优化 A9）：超出 CONTEXT_MAX_CHARS 时丢弃最旧的轮次，
        # 保证单 Agent 上下文不超过硬性预算，防止长对话 token 膨胀。
        while self._history_chars() > settings.CONTEXT_MAX_CHARS and len(self.history) > 2:
            self.history.pop(0)
            self.history_rounds.pop(0)

    def _history_chars(self) -> int:
        return sum(len(str(m.get("content", ""))) for m in self.history)

    # ---------------- 长对话压缩（优化 A9） ----------------
    async def compress_history(self, force: bool = False, keep_rounds: int | None = None):
        """
        压缩历史以缩小后续输入。

        keep_rounds=None（默认，优化 A9）：
          把最旧的轮次压缩为一条摘要，保留最近 CONTEXT_COMPRESS_KEEP_RECENT 条原文。
          触发条件：历史字符数 ≥ CONTEXT_COMPRESS_THRESHOLD（或 force=True），
          且历史条数多于「保留条数 + 2」。摘要存放在 self.compressed_summary，
          由 system_prompt 注入；history 截断为最近保留条。

        keep_rounds=N（需求 6：预算吃紧强压缩）：
          从尾部扫描最近 N 个不同辩论轮次，仅保留这些轮次的原文，更早轮次全部
          压入 compressed_summary（与既有摘要拼接）。历史不足 N 个不同轮次时
          无需压缩，返回 None。force 在按轮模式下恒生效（调用方均为预算触发）。

        摘要优先用 LLM 生成（经 to_thread 避免阻塞事件循环），失败回退为
        确定性截断。返回本次调用的 usage dict（用于预算统计），未触发时返回 None。
        """
        if keep_rounds is not None:
            return await self._compress_keep_rounds(int(keep_rounds))
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
        self.history_rounds = self.history_rounds[-keep:]
        return usage

    async def _compress_keep_rounds(self, keep_rounds: int):
        """按轮强压缩：只保留最近 keep_rounds 个不同轮次的原文，更早轮次摘要化。"""
        if not self.history_rounds:
            return None
        # 尾部扫描最近 keep_rounds 个不同轮次的起始下标 cut
        seen = set()
        cut = len(self.history_rounds)
        for i in range(len(self.history_rounds) - 1, -1, -1):
            r = self.history_rounds[i]
            if r not in seen:
                seen.add(r)
                if len(seen) > keep_rounds:
                    break
            cut = i
        # 不同轮次数 ≤ keep_rounds：历史本身已足够小，无需压缩
        if len(seen) <= keep_rounds:
            return None
        cut = min(cut, len(self.history))
        old = self.history[:cut]
        texts = [f"{m['role']}: {m['content']}" for m in old]
        summary, usage = await self._summarize(texts)
        if summary:
            self.compressed_summary = (
                f"{self.compressed_summary}\n{summary}" if self.compressed_summary else summary)
        self.history = self.history[cut:]
        self.history_rounds = self.history_rounds[cut:]
        return usage

    async def _summarize(self, texts):
        """生成摘要文本，返回 (summary, usage|None)。"""
        from app.llm import summarize_text
        return await summarize_text(self.model_config, "\n".join(texts), max_chars=150)

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
        self.history_rounds = []
        self.compressed_summary = ""
