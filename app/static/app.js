// app.js — 圆桌协作前端逻辑
const $ = (id) => document.getElementById(id);

const state = {
  defaults: null,
  agentConfigs: [],   // 配置阶段
  agents: [],         // 运行阶段（含角色/是否主）
  tasks: [],          // {id,to,content,expected,var,completed,value}
  ctx: {},            // var -> {by,value}
  ws: null,
  seats: {},          // name -> element
  streamBoxes: {},    // speaker -> el（并行流式：多个 Agent 可同时流式发言）
};

const STATUS_TEXT = {
  idle: "空闲", running: "讨论中", paused: "已暂停", done: "已完成", stopped: "已结束",
};

const PHASE_TEXT = {
  idle: "空闲", preparing: "准备中", debating: "辩论中", judging: "裁决中", finished: "已完成",
};

const STANCE_TEXT = { HOST: "主持人", PRO: "正方", CON: "反方", NEUTRAL: "中立" };

// ---------------- 初始化 ----------------
async function init() {
  const res = await fetch("/api/defaults").then(r => r.json());
  state.defaults = res;
  $("backend-hint").textContent = `向量库后端：${res.vector_backend}`;
  generateAgentCards(parseInt($("agent-count").value, 10));
  bindEvents();
  connectWS();
}

function bindEvents() {
  $("agent-count").addEventListener("change", () => {
    const n = clamp(parseInt($("agent-count").value, 10), 2, 6);
    $("agent-count").value = n;
    generateAgentCards(n);
  });
  $("btn-gen-agents").addEventListener("click", () =>
    generateAgentCards(parseInt($("agent-count").value, 10)));
  $("btn-ingest").addEventListener("click", ingest);
  $("btn-start").addEventListener("click", startDiscussion);
  $("btn-pause").addEventListener("click", () => ctrl("pause"));
  $("btn-resume").addEventListener("click", () => ctrl("resume"));
  $("btn-end").addEventListener("click", () => ctrl("end"));
  $("btn-back").addEventListener("click", backToSetup);
  $("btn-export").addEventListener("click", exportMarkdown);
  $("btn-download").addEventListener("click", downloadMarkdown);
  $("btn-md-close").addEventListener("click", () => $("md-modal").classList.add("hidden"));
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

// ---------------- Agent 配置卡片 ----------------
function generateAgentCards(n) {
  const wrap = $("agent-cards");
  wrap.innerHTML = "";
  state.agentConfigs = [];
  for (let i = 0; i < n; i++) {
    const isMain = i === 0;
    const cfg = {
      name: `Agent_${i + 1}`,
      role: isMain ? "主持人 / 主 Agent" : "",
      personality: "",
      provider: "default",
      base_url: "", api_key: "", model_id: "",
      stance: isMain ? "HOST" : (i % 2 === 1 ? "PRO" : "CON"),
    };
    state.agentConfigs.push(cfg);

    // 立场下拉：主 Agent 固定主持人，其余按 PRO/CON 交替
    const stanceOpts = ["HOST", "PRO", "CON", "NEUTRAL"]
      .map(s => `<option value="${s}" ${s === cfg.stance ? "selected" : ""}>${STANCE_TEXT[s]}</option>`)
      .join("");

    const card = document.createElement("div");
    card.className = "acard" + (isMain ? " main" : "");
    card.innerHTML = `
      ${isMain ? '<span class="tag">主 Agent</span>' : ''}
      <h3>${isMain ? "席位 1（主）" : "席位 " + (i + 1)}</h3>
      <div class="field"><label>名称</label><input data-k="name" value="${cfg.name}"></div>
      <div class="field"><label>角色（必填，留空则按机器人）</label><input data-k="role" value="${cfg.role}" placeholder="如：产品经理"></div>
      <div class="field"><label>性格特征（必填，留空则按机器人）</label><input data-k="personality" value="${cfg.personality}" placeholder="如：严谨、富有创意"></div>
      <div class="field"><label>立场（辩论席位）</label>
        <select data-k="stance">${stanceOpts}</select>
      </div>
      <div class="field"><label>基础模型</label>
        <select data-k="provider">
          <option value="default">默认（.env）</option>
          <option value="qwen">Qwen</option>
          <option value="deepseek">DeepSeek</option>
          <option value="custom">自定义</option>
        </select>
      </div>
      <div class="custom-fields hidden">
        <div class="field"><label>Base URL</label><input data-k="base_url" placeholder="https://..."></div>
        <div class="field"><label>API Key</label><input data-k="api_key" type="password" placeholder="sk-..."></div>
        <div class="field"><label>Model ID</label><input data-k="model_id" placeholder="model-name"></div>
      </div>
    `;
    // 绑定输入
    card.querySelectorAll("[data-k]").forEach(el => {
      el.addEventListener("input", () => {
        cfg[el.dataset.k] = el.value;
        if (el.dataset.k === "provider") onProviderChange(card, cfg);
      });
    });
    wrap.appendChild(card);
  }
}

function onProviderChange(card, cfg) {
  const cf = card.querySelector(".custom-fields");
  const presets = state.defaults?.providers || {};
  if (cfg.provider === "custom") {
    cf.classList.remove("hidden");
  } else {
    cf.classList.add("hidden");
    const p = presets[cfg.provider] || presets["default"] || {};
    cfg.base_url = p.base_url || "";
    cfg.api_key = p.api_key || "";
    cfg.model_id = p.model_id || "";
    card.querySelector('[data-k="base_url"]').value = cfg.base_url;
    card.querySelector('[data-k="api_key"]').value = cfg.api_key;
    card.querySelector('[data-k="model_id"]').value = cfg.model_id;
  }
}

// ---------------- 注入知识 ----------------
async function ingest() {
  const text = $("kb-text").value.trim();
  if (!text) { $("ingest-hint").textContent = "请输入资料"; return; }
  const r = await fetch("/api/ingest", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ text, source: "用户注入" }),
  }).then(r => r.json());
  $("ingest-hint").textContent = r.ok ? `已注入 ${r.added} 段（${r.backend}）` : ("失败：" + (r.error || ""));
}

// ---------------- 开始讨论 ----------------
async function startDiscussion() {
  $("setup-error").textContent = "";
  // 校验：自定义 provider 的 base_url / api_key / model_id 必须填写
  const missing = state.agentConfigs.filter(c => c.provider === "custom")
    .map(c => ({ c, keys: ["base_url", "api_key", "model_id"].filter(k => !(c[k] || "").trim()) }))
    .filter(x => x.keys.length);
  if (missing.length) {
    const parts = missing.map(x => `${x.c.name}（缺少 ${x.keys.join(" / ")}）`);
    $("setup-error").textContent = "自定义模型配置不完整：" + parts.join("；");
    return;
  }
  const payload = {
    agents: state.agentConfigs.map((c, i) => ({
      agent_id: `agent_${i + 1}`,
      name: c.name || `Agent_${i + 1}`,
      role: c.role,
      personality: c.personality,
      provider: c.provider,
      base_url: c.base_url,
      api_key: c.api_key,
      model_id: c.model_id,
      stance: c.stance,
    })),
    topic: $("topic").value.trim(),
    max_rounds: parseInt($("max-rounds").value, 10) || 8,
  };
  const cfg = await fetch("/api/configure", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
  }).then(r => r.json());
  if (!cfg.ok) { $("setup-error").textContent = "配置失败：" + (cfg.error || ""); return; }

  state.agents = cfg.agents;
  state.tasks = [];
  state.ctx = {};
  renderSeats(state.agents);
  $("transcript").innerHTML = "";
  $("topic-display") && ($("topic-display").textContent = payload.topic);
  $("center-topic").textContent = payload.topic || "圆桌讨论";
  $("center-round").textContent = `第 0 / ${cfg.max_rounds || payload.max_rounds} 轮`;
  $("done-bar").classList.add("hidden");

  $("setup").classList.add("hidden");
  $("discussion").classList.remove("hidden");
  setPill("running");

  // 启动讨论
  const st = await fetch("/api/start", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      topic: payload.topic,
      max_rounds: payload.max_rounds,
      budget: parseInt($("budget").value, 10) || 100000,
    }),
  }).then(r => r.json());
  if (!st.ok) {
    alert("启动失败：" + (st.error || ""));
  }
}

// ---------------- 圆桌席位渲染（等分） ----------------
function renderSeats(agents) {
  const table = $("round-table");
  // 清除旧席位（保留 center）
  Object.values(state.seats).forEach(el => el.remove());
  state.seats = {};
  const n = agents.length;
  agents.forEach((a, i) => {
    const angle = (-90 + i * (360 / n)) * Math.PI / 180;
    const rPct = 38;
    const x = 50 + rPct * Math.cos(angle);
    const y = 50 + rPct * Math.sin(angle);
    const seat = document.createElement("div");
    seat.className = "seat" + (a.is_main ? " main" : "");
    seat.style.left = x + "%";
    seat.style.top = y + "%";
    seat.innerHTML = `
      <div class="avatar">${a.is_main ? "👑" : "🤖"}</div>
      <div class="sname">${a.name}</div>
      <div class="srole">${a.role || "机器人"}</div>
      <div class="srole stance">${STANCE_TEXT[a.stance] || a.stance || ""}</div>
      ${a.is_main ? '<div class="badge">主</div>' : ''}
    `;
    table.appendChild(seat);
    state.seats[a.name] = seat;
  });
}

function highlightSeat(name) {
  const el = state.seats[name];
  if (!el) return;
  el.classList.add("speaking");
  setTimeout(() => el.classList.remove("speaking"), 1600);
}

// ---------------- 控制 ----------------
async function ctrl(action) {
  await fetch("/api/" + action, { method: "POST" }).then(r => r.json());
}
function backToSetup() {
  $("discussion").classList.add("hidden");
  $("setup").classList.remove("hidden");
}

function setPill(status) {
  const pill = $("status-pill");
  pill.className = "pill " + status;
  pill.textContent = STATUS_TEXT[status] || status;
}

// ---------------- WebSocket ----------------
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    handleEvent(ev);
  };
  ws.onclose = () => {
    setTimeout(connectWS, 2000); // 断线重连
  };
}

function handleEvent(ev) {
  switch (ev.type) {
    case "status":
      setPill(ev.status);
      if (ev.agents && ev.agents.length) {
        state.agents = ev.agents;
        renderSeats(ev.agents);
      }
      if (ev.topic) $("center-topic").textContent = ev.topic;
      if (ev.max_rounds) $("center-round").textContent = `第 ${ev.round || 0} / ${ev.max_rounds} 轮`;
      renderRunStatus({
        phase: ev.phase, current_round: ev.round,
        remaining_budget: ev.remaining_budget != null ? ev.remaining_budget : (ev.budget != null ? ev.budget - (ev.spent || 0) : null),
        spent_budget: ev.spent,
        elapsed_time_ms: ev.elapsed_time_ms,
        trace_id: ev.trace_id,
      });
      if (ev.status === "done" || ev.status === "stopped") {
        $("done-bar").classList.remove("hidden");
        $("done-msg").textContent = ev.status === "done" ? "讨论完成，可导出结果。" : "讨论已结束。";
        $("btn-pause").classList.add("hidden");
        $("btn-resume").classList.add("hidden");
      } else if (ev.status === "paused") {
        $("btn-pause").classList.add("hidden");
        $("btn-resume").classList.remove("hidden");
      } else if (ev.status === "running") {
        $("btn-pause").classList.remove("hidden");
        $("btn-resume").classList.add("hidden");
      }
      break;
    case "round":
      $("center-round").textContent = `第 ${ev.round} / ${ev.max} 轮`;
      break;
    case "speech":
      addMsg(ev.speaker, ev.text, ev.is_main ? "main" : "", ev.round);
      highlightSeat(ev.speaker);
      break;
    // ---- 流式发言：start 建气泡 → delta 追加文本 → end 用完整文本收尾 ----
    // 第 1 轮辩手并行立论时多个 Agent 会同时流式发言，因此按 speaker 维护独立气泡，
    // 避免并发流式时互相覆盖（架构文档 §6.2 并行立论）。
    case "speech_start":
      state.streamBoxes[ev.speaker] = addMsgStart(ev.speaker, ev.is_main ? "main" : "", ev.round);
      highlightSeat(ev.speaker);
      break;
    case "speech_delta": {
      const box = state.streamBoxes[ev.speaker];
      if (box) {
        box.querySelector(".body").textContent += ev.delta;
        $("transcript").scrollTop = $("transcript").scrollHeight;
      }
      break;
    }
    case "speech_end": {
      const box = state.streamBoxes[ev.speaker];
      if (box) {
        // 以完整文本替换，避免与流式增量存在偏差
        box.querySelector(".body").textContent = ev.text;
        delete state.streamBoxes[ev.speaker];
      }
      break;
    }
    case "task_assigned": {
      addMsg(ev.from, `分派任务给 ${ev.to}：\n内容：${ev.content}\n期望：${ev.expected}\n变量：${ev.var}`, "task", null, ev.from);
      state.tasks.push({ id: Date.now() + Math.random(), to: ev.to, content: ev.content, expected: ev.expected, var: ev.var, completed: false, value: "" });
      state.ctx[ev.var] = { by: ev.from, value: "" };
      renderTasks(); renderCtx(); highlightSeat(ev.from);
      break;
    }
    case "task_result": {
      const txt = `${ev.message}\n（变量 ${ev.var} = ${ev.value}）`;
      addMsg(ev.from, txt, "task", null, ev.from + " 回报");
      const t = state.tasks.find(t => t.var === ev.var);
      if (t) { t.completed = ev.completed; t.value = ev.value; }
      if (state.ctx[ev.var]) state.ctx[ev.var].value = ev.value;
      renderTasks(); renderCtx(); highlightSeat(ev.from);
      break;
    }
    case "summary":
      addMsg(ev.speaker, ev.text, "summary", ev.round, "最终总结");
      break;
    case "status_sync":
      renderRunStatus(ev);
      break;
    case "judgment": {
      const scoreTxt = Object.entries(ev.scores || {})
        .map(([k, v]) => `${k}: ${v}`).join("，") || "（无评分）";
      const winnerTxt = ev.winner === "DRAW" ? "平局（DRAW）" : `胜方：${STANCE_TEXT[ev.winner] || ev.winner}`;
      const text = `【裁判裁决】\n- ${winnerTxt}\n- 得分：${scoreTxt}\n- 收敛终止：${ev.convergence_flag ? "是" : "否"}\n- 评语：${ev.critique || ""}`;
      addMsg("裁判", text, "summary", ev.round, "裁决");
      break;
    }
    case "log":
      addMsg("系统", ev.text, "log", null, "");
      break;
    case "agent_error":
      addMsg(ev.agent || "系统", ev.error, "error", null, "模型调用失败");
      if (ev.agent) highlightSeat(ev.agent);
      break;
  }
}

// ---------------- 渲染 ----------------
function addMsgStart(who, cls, round) {
  // 创建发言气泡（不填充文本，供流式增量追加）
  const box = $("transcript");
  const el = document.createElement("div");
  el.className = "msg " + (cls || "");
  const tag = round ? `<span class="round-tag">第${round}轮</span>` : "";
  el.innerHTML = `<div class="who">${who}${tag}</div><div class="body"></div>`;
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
  return el;
}

function addMsg(who, text, cls, round, label) {
  const box = $("transcript");
  const el = document.createElement("div");
  el.className = "msg " + (cls || "");
  const tag = round ? `<span class="round-tag">第${round}轮</span>` : "";
  const lab = label ? `<span class="round-tag">${label}</span>` : "";
  el.innerHTML = `<div class="who">${who}${tag}${lab}</div><div class="body"></div>`;
  el.querySelector(".body").textContent = text;
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

function renderTasks() {
  const box = $("task-list");
  if (!state.tasks.length) { box.innerHTML = '<div class="muted">暂无任务</div>'; updateProgress(); return; }
  box.innerHTML = state.tasks.map(t => `
    <div class="task-item">
      <div class="t-head">
        <span>→ ${t.to} <span class="muted">(变量 ${t.var})</span></span>
        <span class="${t.completed ? "done" : "doing"}">${t.completed ? "✅完成" : "⏳进行中"}</span>
      </div>
      <div class="t-body">${t.content}</div>
    </div>`).join("");
  updateProgress();
}

function renderCtx() {
  const box = $("ctx-list");
  const keys = Object.keys(state.ctx);
  if (!keys.length) { box.innerHTML = '<div class="muted">暂无</div>'; return; }
  box.innerHTML = keys.map(k => `
    <div class="kv"><b>${k}</b> <span class="muted">(由 ${state.ctx[k].by})</span><br>${state.ctx[k].value || "（待回填）"}</div>
  `).join("");
}

function updateProgress() {
  const total = state.tasks.length;
  const done = state.tasks.filter(t => t.completed).length;
  $("center-progress").textContent = total ? `任务 ${done}/${total} 完成` : "";
}

// ---------------- 运行状态（阶段 / Token 预算） ----------------
function renderRunStatus(data) {
  const box = $("run-status");
  if (!box) return;
  const phase = PHASE_TEXT[data.phase] || data.phase || "-";
  const round = data.current_round != null ? `第 ${data.current_round} 轮` : "-";
  const budget = data.remaining_budget != null
    ? `剩余 ${data.remaining_budget.toLocaleString()} / 已用 ${(data.spent_budget || 0).toLocaleString()}`
    : "-";
  const elapsed = data.elapsed_time_ms != null
    ? `用时 ${(data.elapsed_time_ms / 1000).toFixed(1)}s` : "";
  box.innerHTML = `
    <div class="kv"><b>阶段</b>：${phase}</div>
    <div class="kv"><b>轮次</b>：${round}</div>
    <div class="kv"><b>预算</b>：${budget}</div>
    ${elapsed ? `<div class="kv"><b>${elapsed}</b></div>` : ""}
    ${data.trace_id ? `<div class="kv muted" style="font-size:11px">Trace: ${data.trace_id}</div>` : ""}
  `;
}

// ---------------- Markdown 导出 ----------------
async function exportMarkdown() {
  const md = await fetch("/api/export").then(r => r.json()).then(r => r.markdown);
  $("md-text").textContent = md;
  $("md-modal").classList.remove("hidden");
}
async function downloadMarkdown() {
  const md = await fetch("/api/export").then(r => r.json()).then(r => r.markdown);
  const blob = new Blob([md], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "discussion.md";
  a.click();
  URL.revokeObjectURL(a.href);
  // 同时保存到服务器
  fetch("/api/save_markdown", { method: "POST", headers: {"Content-Type": "application/json"} });
}

init();
