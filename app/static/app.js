// app.js — 圆桌辩论前端逻辑
// 设计语言：聊天气泡 + 首字母文字头像（按立场配色）+ 流式光标 + 历史记忆检索
const $ = (id) => document.getElementById(id);

const state = {
  defaults: null,
  sessionId: null,      // 当前客户端会话（localStorage 持久化，稳定绑定多会话后端）
  agentConfigs: [],   // 配置阶段
  agents: [],         // 运行阶段（含角色/是否主/立场）
  tasks: [],          // {id,to,content,expected,var,completed,value}
  ctx: {},            // var -> {by,value}
  ws: null,
  seats: {},          // name -> element（圆桌席位）
  streamBoxes: {},    // speaker -> el（并行流式：多个 Agent 可同时流式发言）
  liveDiscussion: false,  // 存在可继续（暂停中）的讨论：重新进入时不重配置、不重开
  historyItems: [],   // 历史讨论索引（检索结果）
  currentHistory: null,   // 正在查看的历史记录 {filename, topic, ...}
  historyActiveFilename: null,
  elapsedTimer: null,   // 实时耗时轮询定时器（讨论中每秒刷新）
  budgetEvents: [],     // 公平性/告警事件（结构化展示，保留最近 6 条）
  telemetry: { spent_tokens: 0, output_tokens: 0 },   // 最近一次 token 遥测快照（speech_end 实时刷新用）
};

const STATUS_TEXT = {
  idle: "空闲", running: "讨论中", paused: "已暂停", done: "已完成",
  stopped: "已结束", interrupted: "已中断",
};

const PHASE_TEXT = {
  idle: "空闲", preparing: "准备中", debating: "辩论中", judging: "裁决中", finished: "已完成",
};

const STANCE_TEXT = { HOST: "主持人", PRO: "正方", CON: "反方", NEUTRAL: "中立" };

// 立场 → 头像配色类（CSS 定义于 style.css .av-*）
const STANCE_AV = { HOST: "av-host", PRO: "av-pro", CON: "av-con", NEUTRAL: "av-neutral" };

// ---------------- 初始化 ----------------
// 会话 ID：每个浏览器客户端在 localStorage 中持久化一个稳定 session_id，
// 后端据此把配置/状态/事件隔离到独立 Orchestrator（多会话，P1-7）。
function getSessionId() {
  if (state.sessionId) return state.sessionId;
  let sid = localStorage.getItem("debate.session_id");
  if (!sid) {
    sid = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : ("sess_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8));
    localStorage.setItem("debate.session_id", sid);
  }
  state.sessionId = sid;
  return sid;
}

function sidQuery() {
  return "?session_id=" + encodeURIComponent(getSessionId());
}

async function init() {
  const res = await fetch("/api/defaults").then(r => r.json());
  state.defaults = res;
  $("backend-hint").textContent = `向量库后端：${res.vector_backend}`;
  generateAgentCards(parseInt($("agent-count").value, 10));
  refreshTemplateSelect();
  bindEvents();
  connectWS();
  // 检测服务器是否已有可继续的讨论（刷新页面 / 之前暂停未结束），此时"进入圆桌"应回到旧讨论
  try {
    const st = await fetch("/api/status" + sidQuery()).then(r => r.json());
    if (st.status === "interrupted") {
      // 上一进程遗留的中断会话：展示横幅，需重新配置后开启
      showInterruptedBanner(st);
    }
    if ((st.agents || []).length && (st.status === "paused" || st.status === "running")) {
      state.liveDiscussion = true;
      $("btn-start").textContent = "↩ 回到圆桌（继续讨论）";
    }
  } catch (e) { /* 状态查询失败时忽略 */ }
}

function showInterruptedBanner(st) {
  const box = $("interrupted-banner");
  if (!box) return;
  const topic = (st.topic || "").trim();
  box.classList.remove("hidden");
  box.textContent = topic
    ? `上一次讨论（“${topic}”）因服务重启而中断，请重新配置后开启新讨论。`
    : "上一次讨论因服务重启而中断，请重新配置后开启新讨论。";
}

function bindEvents() {
  $("agent-count").addEventListener("change", () => {
    const n = clamp(parseInt($("agent-count").value, 10), 2, 6);
    $("agent-count").value = n;
    generateAgentCards(n);
  });
  $("btn-gen-agents").addEventListener("click", () =>
    generateAgentCards(parseInt($("agent-count").value, 10)));
  $("judge-provider").addEventListener("change", onJudgeProviderChange);
  $("btn-template-save").addEventListener("click", saveTemplate);
  $("btn-template-load").addEventListener("click", () => {
    const name = $("template-select").value;
    if (!name) { $("template-hint").textContent = "请先选择要载入的模板"; return; }
    applyTemplate(name);
  });
  $("btn-template-delete").addEventListener("click", deleteTemplate);
  $("btn-ingest").addEventListener("click", ingest);
  $("btn-upload").addEventListener("click", uploadFile);
  $("btn-start").addEventListener("click", startDiscussion);
  $("btn-pause").addEventListener("click", () => ctrl("pause"));
  $("btn-resume").addEventListener("click", () => ctrl("resume"));
  $("btn-end").addEventListener("click", () => ctrl("end"));
  $("btn-back").addEventListener("click", backToSetup);
  $("btn-export").addEventListener("click", exportMarkdown);
  $("btn-download").addEventListener("click", downloadMarkdown);
  $("btn-md-close").addEventListener("click", () => $("md-modal").classList.add("hidden"));

  // ---- 历史讨论记忆检索 ----
  $("btn-history").addEventListener("click", openHistory);
  $("btn-history-close").addEventListener("click", () => $("history-modal").classList.add("hidden"));
  $("history-search").addEventListener("input", debounce(() => loadHistory($("history-search").value.trim()), 300));
  $("history-list").addEventListener("click", (e) => {
    const item = e.target.closest(".h-item");
    if (item && item.dataset.filename) viewHistory(item.dataset.filename);
  });
  $("btn-history-download").addEventListener("click", downloadHistoryMarkdown);
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ---------------- 头像（首字母文字头像，按立场配色） ----------------
function initialOf(name) {
  const s = (name || "?").trim();
  return s ? s[0].toUpperCase() : "?";
}

function stanceOf(name) {
  const a = (state.agents || []).find(x => x.name === name);
  return a ? a.stance : "";
}

// 立场 → 头像配色类；找不到立场时按席位序号兜底（av-seat0..5）
function avatarClass(stance, idx) {
  const s = (stance || "").toUpperCase();
  if (STANCE_AV[s]) return STANCE_AV[s];
  return "av-seat" + ((idx == null ? 0 : idx) % 6);
}

// ---------------- Agent 配置卡片 ----------------
function generateAgentCards(n, prefill) {
  const wrap = $("agent-cards");
  wrap.innerHTML = "";
  state.agentConfigs = [];
  for (let i = 0; i < n; i++) {
    const isMain = i === 0;
    const src = (prefill && prefill[i]) || {};
    const cfg = {
      name: src.name || `Agent_${i + 1}`,
      role: src.role ?? (isMain ? "主持人 / 主 Agent" : ""),
      personality: src.personality || "",
      provider: src.provider || "default",
      base_url: src.base_url || "", api_key: src.api_key || "", model_id: src.model_id || "",
      stance: src.stance || (isMain ? "HOST" : (i % 2 === 1 ? "PRO" : "CON")),
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
          <option value="mimo">MiMo</option>
          <option value="custom">自定义</option>
        </select>
      </div>
      <div class="custom-fields hidden">
        <div class="field"><label>Base URL</label><input data-k="base_url" value="${cfg.base_url}" placeholder="https://..."></div>
        <div class="field"><label>API Key</label><input data-k="api_key" type="password" value="${cfg.api_key}" placeholder="sk-..."></div>
        <div class="field"><label>Model ID</label><input data-k="model_id" value="${cfg.model_id}" placeholder="model-name"></div>
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
    // 按 provider 归一化：非 custom 时用预设回填 base_url / api_key / model_id
    onProviderChange(card, cfg);
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

// ---------------- 独立裁判模型切换（展示/隐藏自定义字段） ----------------
function onJudgeProviderChange() {
  const p = $("judge-provider").value;
  $("judge-custom").classList.toggle("hidden", p !== "custom");
  if (p !== "custom") {
    const presets = state.defaults?.providers || {};
    const conf = presets[p] || presets["default"] || {};
    $("judge-base-url").value = conf.base_url || "";
    $("judge-api-key").value = conf.api_key || "";
    $("judge-model-id").value = conf.model_id || "";
  }
}

function collectJudgeConfig() {
  const provider = $("judge-provider").value;
  return {
    name: $("judge-name").value.trim() || "独立裁判",
    provider,
    base_url: $("judge-base-url").value,
    api_key: $("judge-api-key").value,
    model_id: $("judge-model-id").value,
  };
}

// ---------------- 讨论模板（P1-5：localStorage 持久化） ----------------
const TEMPLATE_KEY = "debate.templates.v1";

function readTemplates() {
  try { return JSON.parse(localStorage.getItem(TEMPLATE_KEY)) || {}; }
  catch (e) { return {}; }
}

function writeTemplates(obj) {
  localStorage.setItem(TEMPLATE_KEY, JSON.stringify(obj));
}

function refreshTemplateSelect(selectedName) {
  const sel = $("template-select");
  const names = Object.keys(readTemplates()).sort();
  sel.innerHTML = '<option value="">选择模板…</option>' +
    names.map(n => `<option value="${n}" ${n === selectedName ? "selected" : ""}>${n}</option>`).join("");
}

// 直接从 DOM 收集当前席位配置（保证包含未触发事件的最新输入）
function collectAgentConfigsFromDOM() {
  return Array.from(document.querySelectorAll("#agent-cards .acard")).map(card => {
    const cfg = {};
    card.querySelectorAll("[data-k]").forEach(el => { cfg[el.dataset.k] = el.value; });
    return cfg;
  });
}

function collectSetupConfig() {
  return {
    topic: $("topic").value,
    max_rounds: parseInt($("max-rounds").value, 10) || 8,
    kb_text: $("kb-text").value,
    agents: collectAgentConfigsFromDOM(),
    judge: collectJudgeConfig(),
  };
}

function saveTemplate() {
  const name = ($("template-name").value || "").trim();
  if (!name) { $("template-hint").textContent = "请先填写模板名称"; return; }
  const tpl = collectSetupConfig();
  tpl.name = name;
  tpl.saved_at = new Date().toISOString();
  const all = readTemplates();
  all[name] = tpl;
  writeTemplates(all);
  refreshTemplateSelect(name);
  $("template-hint").textContent = `已保存模板「${name}」`;
}

function applyTemplate(name) {
  const tpl = readTemplates()[name];
  if (!tpl) return;
  const n = clamp(tpl.agents ? tpl.agents.length : 3, 2, 6);
  $("agent-count").value = n;
  $("topic").value = tpl.topic || "";
  $("max-rounds").value = clamp(tpl.max_rounds || 8, 1, 30);
  $("kb-text").value = tpl.kb_text || "";
  $("template-name").value = name;
  // 独立裁判
  const j = tpl.judge || {};
  if (j.name) $("judge-name").value = j.name;
  $("judge-provider").value = j.provider || "default";
  onJudgeProviderChange();
  if (j.base_url) $("judge-base-url").value = j.base_url;
  if (j.api_key) $("judge-api-key").value = j.api_key;
  if (j.model_id) $("judge-model-id").value = j.model_id;
  // 席位卡片：重建后由 generateAgentCards 用模板值回填
  generateAgentCards(n, tpl.agents || []);
  $("template-hint").textContent = `已载入模板「${name}」`;
}

function deleteTemplate() {
  const name = $("template-select").value;
  if (!name) return;
  if (!confirm(`删除模板「${name}」？`)) return;
  const all = readTemplates();
  delete all[name];
  writeTemplates(all);
  refreshTemplateSelect();
  $("template-hint").textContent = `已删除模板「${name}」`;
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

// ---------------- 上传文件解析并注入 ----------------
async function uploadFile() {
  const input = $("kb-file");
  const file = input.files && input.files[0];
  if (!file) { $("upload-hint").textContent = "请先选择文件"; return; }
  $("upload-hint").textContent = `解析中：${file.name}…`;
  $("btn-upload").disabled = true;
  try {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch("/api/upload", { method: "POST", body: fd }).then(r => r.json());
    $("upload-hint").textContent = r.ok
      ? `已注入 ${r.added} 段（${r.chars} 字，${r.backend}）`
      : ("失败：" + (r.error || ""));
  } catch (e) {
    $("upload-hint").textContent = "上传失败：" + e;
  } finally {
    $("btn-upload").disabled = false;
  }
}

// ---------------- 开始讨论 ----------------
async function startDiscussion() {
  // 已存在可继续的讨论：直接回到之前的讨论页，不重新配置 / 不重开新讨论
  if (state.liveDiscussion) {
    state.liveDiscussion = false;
    $("btn-start").textContent = "▶ 进入圆桌并开始讨论";
    $("setup").classList.add("hidden");
    $("discussion").classList.remove("hidden");
    return;
  }
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
  // 校验独立裁判：自定义 provider 时必须填齐三要素
  const judge = collectJudgeConfig();
  if (judge.provider === "custom") {
    const miss = ["base_url", "api_key", "model_id"].filter(k => !(judge[k] || "").trim());
    if (miss.length) {
      $("setup-error").textContent = "裁判自定义模型配置不完整（缺少 " + miss.join(" / ") + "）";
      return;
    }
  }
  const payload = {
    session_id: getSessionId(),
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
    judge,
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
  $("conclusion-panel").classList.add("hidden");  // 新讨论开始时隐藏旧结论
  $("topic-display") && ($("topic-display").textContent = payload.topic);
  $("center-topic").textContent = payload.topic || "圆桌讨论";
  $("center-round").textContent = `第 0 / ${cfg.max_rounds || payload.max_rounds} 轮`;
  $("done-bar").classList.add("hidden");

  $("setup").classList.add("hidden");
  $("discussion").classList.remove("hidden");
  setPill("running");

  // 配置后立即展示公平性治理基线（预启动预警）
  renderRunStatus({
    phase: "idle", current_round: 0,
    spent_tokens: 0, output_tokens: 0,
    fairness: cfg.fairness || null,
    elapsed_time_ms: 0, trace_id: cfg.trace_id,
  });
  renderBudgetPanel({
    spent_tokens: 0, output_tokens: 0,
    fairness: cfg.fairness || null,
  });

  // 启动讨论
  const st = await fetch("/api/start", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      session_id: getSessionId(),
      topic: payload.topic,
      max_rounds: payload.max_rounds,
    }),
  }).then(r => r.json());
  if (!st.ok) {
    alert("启动失败：" + (st.error || ""));
  }
  startElapsedTick();
}

// ---------------- 圆桌席位渲染（等分，首字母头像） ----------------
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
      <div class="avatar ${avatarClass(a.stance, i)}">${initialOf(a.name)}</div>
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
  return await fetch("/api/" + action + sidQuery(), { method: "POST" }).then(r => r.json());
}

async function backToSetup() {
  // 讨论进行中先暂停，确保返回配置后能继续；并按服务端实际状态决定能否"回到旧讨论"
  stopElapsedTick();
  let st = null;
  if (state.agents.length) {
    st = await ctrl("pause");
  }
  const s = st ? st.status : "idle";
  state.liveDiscussion = !!(
    state.agents.length && (s === "paused" || s === "running")
  );
  $("btn-start").textContent = state.liveDiscussion
    ? "↩ 回到圆桌（继续讨论）"
    : "▶ 进入圆桌并开始讨论";
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
  const ws = new WebSocket(`${proto}://${location.host}/ws${sidQuery()}`);
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
      // 中断 / 正常状态时收起中断横幅（重新配置并启动后会自动隐藏）
      if (ev.status !== "interrupted") {
        const banner = $("interrupted-banner");
        if (banner) banner.classList.add("hidden");
      }
      if (ev.agents && ev.agents.length) {
        state.agents = ev.agents;
        renderSeats(ev.agents);
      }
      if (ev.topic) $("center-topic").textContent = ev.topic;
      if (ev.max_rounds) $("center-round").textContent = `第 ${ev.round || 0} / ${ev.max_rounds} 轮`;
      renderRunStatus({
        phase: ev.phase, current_round: ev.round,
        spent_tokens: ev.spent_tokens != null ? ev.spent_tokens : 0,
        output_tokens: ev.output_tokens != null ? ev.output_tokens : 0,
        fairness: ev.fairness,
        elapsed_time_ms: ev.elapsed_time_ms,
        trace_id: ev.trace_id,
      });
      renderBudgetPanel(ev);
      if (ev.status === "done" || ev.status === "stopped") {
        stopElapsedTick();
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
    // 避免并发流式时互相覆盖（架构文档 §6.2 并行立论）。每次气泡只显示当前发言内容。
    case "speech_start":
      state.streamBoxes[ev.speaker] = addMsgStart(ev.speaker, ev.is_main ? "main" : "", ev.round);
      highlightSeat(ev.speaker);
      break;
    case "speech_delta": {
      const box = state.streamBoxes[ev.speaker];
      if (box) {
        const body = box.querySelector(".body");
        const caret = body.querySelector(".caret");
        // 追加到流式光标之前，保持光标在文本末尾
        body.insertBefore(document.createTextNode(ev.delta), caret || null);
        $("transcript").scrollTop = $("transcript").scrollHeight;
      }
      break;
    }
    case "speech_end": {
      const box = state.streamBoxes[ev.speaker];
      if (box) {
        // 以完整文本替换，避免与流式增量存在偏差（同时移除流式光标）
        box.querySelector(".body").textContent = ev.text;
        delete state.streamBoxes[ev.speaker];
      }
      // 实时输出 token 统计：发言结束时立即刷新（无需等待下一次秒级轮询）
      if (ev.output_tokens != null) {
        renderRunStatus(Object.assign({}, state.telemetry, {
          output_tokens: ev.output_tokens, cost_rmb: ev.cost_rmb,
        }));
        renderBudgetPanel(Object.assign({}, state.telemetry, {
          output_tokens: ev.output_tokens, cost_rmb: ev.cost_rmb,
        }));
      }
      break;
    }
    case "tool_call_start": {
      // P1-4 工具可视化：在发言气泡内追加「正在调用工具 …」指示条。
      // 纯工具回合可能先于任何文本增量到达，需在气泡不存在时自行创建。
      let box = state.streamBoxes[ev.speaker];
      if (!box) {
        box = addMsgStart(ev.speaker, ev.is_main ? "main" : "", ev.round);
        state.streamBoxes[ev.speaker] = box;
        highlightSeat(ev.speaker);
      }
      const chips = box.querySelector(".tool-calls");
      const chip = document.createElement("div");
      chip.className = "tool-chip live";
      chip.dataset.name = ev.name;
      chip.textContent = "🔧 正在调用 " + ev.name + " …";
      chips.appendChild(chip);
      $("transcript").scrollTop = $("transcript").scrollHeight;
      break;
    }
    case "tool_call_end": {
      // 工具执行完成：把「正在调用」条更新为「✅ 完成 + 摘要」；找不到则直接补一条
      const box = state.streamBoxes[ev.speaker];
      if (box) {
        const chips = box.querySelector(".tool-calls");
        const live = Array.from(chips.querySelectorAll(".tool-chip.live"))
          .find(c => c.dataset.name === ev.name)
          || chips.querySelector(".tool-chip.live");
        const label = ev.summary ? ev.name + "：" + ev.summary : ev.name + " 完成";
        if (live) {
          live.classList.remove("live");
          live.classList.add("done");
          live.textContent = "✅ " + label;
        } else {
          const chip = document.createElement("div");
          chip.className = "tool-chip done";
          chip.textContent = "✅ " + label;
          chips.appendChild(chip);
        }
        $("transcript").scrollTop = $("transcript").scrollHeight;
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
    case "conclusion": {
      // P0-1 结构化结论：讨论结束后展示「核心结论」面板
      $("conclusion-core").textContent = ev.core || "";
      const parts = [];
      if (ev.winner) parts.push("胜方：" + (STANCE_TEXT[ev.winner] || ev.winner));
      if (ev.positions) parts.push("【各方立场与论点摘要】\n" + ev.positions);
      if (ev.judge_ruling) parts.push("【裁判裁决】\n" + ev.judge_ruling);
      if (ev.pending) parts.push("【分歧与待办】\n" + ev.pending);
      $("conclusion-detail").textContent = parts.join("\n\n");
      $("conclusion-panel").classList.remove("hidden");
      break;
    }
    case "status_sync":
      renderRunStatus(ev);
      renderBudgetPanel(ev);
      break;
    case "judgment": {
      const scoreTxt = Object.entries(ev.scores || {})
        .map(([k, v]) => `${k}: ${v}`).join("，") || "（无评分）";
      const agentScoreTxt = (ev.agent_scores && Object.keys(ev.agent_scores).length)
        ? "\n- 辩手评分：\n  - " + Object.entries(ev.agent_scores)
            .map(([k, v]) => `${k}：${Object.entries(v).map(([d, s]) => `${d} ${s}`).join(" / ")}`)
            .join("\n  - ")
        : "";
      const perfTxt = (ev.model_performance && Object.keys(ev.model_performance).length)
        ? "\n- 模型质量：\n  - " + Object.entries(ev.model_performance)
            .map(([k, v]) => `${k}：TTFT ${v.avg_ttft_ms}ms / ${v.avg_gen_rate}字/s / 质量分 ${v.performance_score}`)
            .join("\n  - ")
        : "";
      const compTxt = (ev.composite_scores && Object.keys(ev.composite_scores).length)
        ? "\n- 综合评分：" + Object.entries(ev.composite_scores)
            .map(([k, v]) => `${k} ${v}`).join("，")
        : "";
      const winnerTxt = ev.winner === "DRAW" ? "平局（DRAW）" : `胜方：${STANCE_TEXT[ev.winner] || ev.winner}`;
      const text = `【裁判裁决】\n- ${winnerTxt}\n- 综合得分：${scoreTxt}${agentScoreTxt}${perfTxt}${compTxt}\n- 收敛终止：${ev.convergence_flag ? "是" : "否"}\n- 评语：${ev.critique || ""}`;
      addMsg(ev.judge || "裁判", text, "judge", ev.round, "裁决");
      break;
    }
    case "log":
      addMsg("系统", ev.text, "log", null, "");
      addBudgetEvent(ev);   // budget.* 告警事件额外进入结构化面板
      break;
    case "agent_error":
      addMsg(ev.agent || "系统", ev.error, "error", null, "模型调用失败");
      if (ev.agent) highlightSeat(ev.agent);
      break;
  }
}

// ---------------- 渲染（聊天气泡） ----------------
// 每个发言独立一个气泡（.bubble-row），只承载当前对话内容，不拼接历史。
function addMsgStart(who, cls, round) {
  // 创建发言气泡（不填充文本，供流式增量追加），并带一个流式光标
  const box = $("transcript");
  const el = document.createElement("div");
  el.className = "bubble-row " + (cls || "");
  const tag = round ? `<span class="bubble-tag">第${round}轮</span>` : "";
  el.innerHTML = `
    <div class="bubble-avatar ${avatarClass(stanceOf(who), (state.agents || []).findIndex(a => a.name === who))}">${initialOf(who)}</div>
    <div class="bubble-main">
      <div class="bubble-meta"><span class="bubble-name">${who}</span>${tag}</div>
      <div class="bubble"><span class="body"><span class="caret"></span></span></div>
      <div class="tool-calls"></div>
    </div>
  `;
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
  return el;
}

function addMsg(who, text, cls, round, label) {
  const box = $("transcript");
  const el = document.createElement("div");
  el.className = "bubble-row " + (cls || "");
  const tag = round ? `<span class="bubble-tag">第${round}轮</span>` : "";
  const lab = label ? `<span class="bubble-tag">${label}</span>` : "";
  // log / summary / judge 居中整行展示，不带头像
  const center = (cls === "log" || cls === "summary" || cls === "judge");
  if (center) {
    el.innerHTML = `
      <div class="bubble-main">
        <div class="bubble-meta"><span class="bubble-name">${who}</span>${tag}${lab}</div>
        <div class="bubble"></div>
      </div>
    `;
  } else {
    el.innerHTML = `
      <div class="bubble-avatar ${avatarClass(stanceOf(who), (state.agents || []).findIndex(a => a.name === who))}">${initialOf(who)}</div>
      <div class="bubble-main">
        <div class="bubble-meta"><span class="bubble-name">${who}</span>${tag}${lab}</div>
        <div class="bubble"></div>
      </div>
    `;
  }
  el.querySelector(".bubble").textContent = text;
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
// 实时耗时：讨论运行中每秒轮询 /api/status，刷新耗时（不依赖轮次边界事件）
function startElapsedTick() {
  stopElapsedTick();
  state.elapsedTimer = setInterval(async () => {
    try {
      const s = await fetch("/api/status" + sidQuery()).then(r => r.json());
      renderRunStatus({
        phase: s.phase,
        current_round: s.round,
        spent_tokens: s.spent_tokens != null ? s.spent_tokens : 0,
        output_tokens: s.output_tokens != null ? s.output_tokens : 0,
        fairness: s.fairness,
        elapsed_time_ms: s.elapsed_time_ms,
        trace_id: s.trace_id,
      });
      renderBudgetPanel(s);
    } catch (e) { /* 轮询失败时静默忽略，下次再试 */ }
  }, 1000);
}

function stopElapsedTick() {
  if (state.elapsedTimer) {
    clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
  }
}

function renderRunStatus(data) {
  // 缓存完整快照：供 speech_end 等事件按最新 output_tokens 立即刷新，不丢耗时/阶段
  state.telemetry = Object.assign({}, state.telemetry, data, {
    spent_tokens: data.spent_tokens != null ? data.spent_tokens : state.telemetry.spent_tokens,
    output_tokens: data.output_tokens != null ? data.output_tokens : state.telemetry.output_tokens,
    cost_rmb: data.cost_rmb != null ? data.cost_rmb : state.telemetry.cost_rmb,
  });
  const box = $("run-status");
  if (!box) return;
  const phase = PHASE_TEXT[data.phase] || data.phase || "-";
  const round = data.current_round != null ? `第 ${data.current_round} 轮` : "-";
  // 实时 token 统计（只读遥测）：累计输出 token + 累计总用量 + 预估费用（¥）
  const outT = data.output_tokens != null ? data.output_tokens : 0;
  const spentT = data.spent_tokens != null ? data.spent_tokens : 0;
  const cost = data.cost_rmb != null ? data.cost_rmb : 0;
  const tokens = `输出 ${outT.toLocaleString()} / 累计 ${spentT.toLocaleString()}`
    + ` / 费用 ≈ ¥${cost.toFixed(4)}`;
  const elapsed = data.elapsed_time_ms != null
    ? `用时 ${(data.elapsed_time_ms / 1000).toFixed(1)}s` : "";
  // 公平性治理摘要（L1-L6 各层状态；替代旧「预算可支撑轮数」）
  const fair = data.fairness && data.fairness.layers ? data.fairness.layers : null;
  const fairLine = fair
    ? `<div class="kv"><b>公平性</b>：${fair.map(l => `${l.layer}·${l.status}`).join(" / ")}</div>`
    : "";
  box.innerHTML = `
    <div class="kv"><b>阶段</b>：${phase}</div>
    <div class="kv"><b>轮次</b>：${round}</div>
    <div class="kv"><b>Token 输出 / 累计 / 费用（遥测）</b>：${tokens}</div>
    ${fairLine}
    ${elapsed ? `<div class="kv"><b>${elapsed}</b></div>` : ""}
    ${data.trace_id ? `<div class="kv muted" style="font-size:11px">Trace: ${data.trace_id}</div>` : ""}
  `;
}

// ---------------- Token 统计面板 ----------------
// 结构化展示实时输出 token / 累计用量 + 公平性各层状态，并把 fairness.* 告警事件渲染为彩色标签（防 XSS：全部 textContent）
const BUDGET_EVENT_TEXT = {
  // 公平性治理事件（替代旧的 budget.* 预算告警）
  "fairness.output_truncate": { text: "L1 输出截断", level: "warn" },
  "fairness.report":          { text: "公平性报告", level: "ok" },
};

function fmtTokens(n) {
  return n == null ? "-" : n.toLocaleString();
}

function renderBudgetPanel(data) {
  const box = $("budget-panel");
  if (!box) return;
  // 只读遥测：累计输出 token 与累计总用量（无预算总量概念）
  const outT = data.output_tokens != null ? data.output_tokens : null;
  const spent = data.spent_tokens != null ? data.spent_tokens : null;
  if (outT == null && spent == null) {
    box.innerHTML = '<div class="muted">未开始</div>';
    return;
  }
  const rows = [];
  if (outT != null) rows.push(`<div class="kv"><b>实时输出 Token（遥测）</b>：${fmtTokens(outT)}</div>`);
  if (spent != null) rows.push(`<div class="kv"><b>累计用量 Token（遥测）</b>：${fmtTokens(spent)}</div>`);
  if (data.cost_rmb != null) rows.push(`<div class="kv"><b>预估费用（遥测，¥）</b>：≈ ¥${data.cost_rmb.toFixed(4)}</div>`);
  // 公平性治理各层状态（替代旧「输出上限 / 可支撑轮数 / 校准因子」约束视图）
  if (data.fairness && data.fairness.layers) {
    data.fairness.layers.forEach(l => {
      const color = l.status === "ok" ? "" : (l.status === "violation" ? ' style="color:#d33"' : ' style="color:#b80"');
      rows.push(`<div class="kv"><b>${l.layer} ${l.name}</b>${color}：${l.status}</div>`);
      if (l.detail) rows.push(`<div class="kv muted" style="font-size:11px;padding-left:12px">${l.detail}</div>`);
    });
  }
  box.innerHTML = rows.join("");
}

function addBudgetEvent(ev) {
  const meta = BUDGET_EVENT_TEXT[ev.event];
  if (!meta) return;   // 非预算告警事件（含 EMA 频发事件 / 档案载入保存）不入面板
  state.budgetEvents.unshift({ tag: meta.text, level: meta.level, time: new Date().toLocaleTimeString() });
  if (state.budgetEvents.length > 6) state.budgetEvents.length = 6;
  const box = $("budget-events");
  if (!box) return;
  box.innerHTML = "";
  state.budgetEvents.forEach(e => {
    const row = document.createElement("div");
    row.className = "budget-event";
    const tag = document.createElement("span");
    tag.className = `ev-tag ev-${e.level}`;
    tag.textContent = e.tag;
    const t = document.createElement("span");
    t.className = "muted";
    t.textContent = e.time;
    row.appendChild(tag);
    row.appendChild(t);
    box.appendChild(row);
  });
}

// ---------------- 历史讨论（记忆检索 / 查看 / 导出） ----------------
async function openHistory() {
  $("history-modal").classList.remove("hidden");
  await loadHistory($("history-search").value.trim());
}

async function loadHistory(q) {
  const url = q ? `/api/history?q=${encodeURIComponent(q)}` : "/api/history";
  try {
    const res = await fetch(url).then(r => r.json());
    state.historyItems = res.items || [];
  } catch (e) {
    state.historyItems = [];
  }
  renderHistoryList();
}

function renderHistoryList() {
  const box = $("history-list");
  if (!state.historyItems.length) {
    box.innerHTML = '<div class="muted pad">暂无历史记录</div>';
    return;
  }
  box.innerHTML = state.historyItems.map(r => `
    <div class="h-item${r.filename === state.historyActiveFilename ? " active" : ""}" data-filename="${r.filename}">
      <div class="h-topic">${escapeHtml(r.topic || "（无题目）")}</div>
      <div class="h-meta">
        <span>${escapeHtml(r.timestamp || "")}</span>
        <span>${STATUS_TEXT[r.status] || r.status || ""}</span>
        <span>第 ${r.round || 0}/${r.max_rounds || 0} 轮</span>
        <span>${(r.agents || []).length} 位</span>
      </div>
    </div>
  `).join("");
}

async function viewHistory(filename) {
  try {
    const res = await fetch(`/api/history/${encodeURIComponent(filename)}`).then(r => r.json());
    if (!res.ok) throw new Error(res.error || "加载失败");
    const rec = state.historyItems.find(r => r.filename === filename) || {};
    state.currentHistory = { ...rec, markdown: res.markdown };
    state.historyActiveFilename = filename;
    $("history-view-title").textContent = `${rec.topic || filename}（${filename}）`;
    $("history-md").innerHTML = renderMarkdown(res.markdown);
    renderHistoryList();
  } catch (e) {
    $("history-md").textContent = "加载失败：" + e.message;
  }
}

function downloadHistoryMarkdown() {
  const md = state.currentHistory ? state.currentHistory.markdown : "";
  if (!md) { alert("请先选择一条历史记录"); return; }
  const blob = new Blob([md], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = state.currentHistory.filename || "discussion.md";
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---------------- Markdown 导出 ----------------
async function exportMarkdown() {
  const md = await fetch("/api/export" + sidQuery()).then(r => r.json()).then(r => r.markdown);
  $("md-text").innerHTML = renderMarkdown(md);
  $("md-modal").classList.remove("hidden");
}
async function downloadMarkdown() {
  const md = await fetch("/api/export" + sidQuery()).then(r => r.json()).then(r => r.markdown);
  const blob = new Blob([md], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "discussion.md";
  a.click();
  URL.revokeObjectURL(a.href);
  // 同时保存到服务器（服务端会同步写入历史记忆索引）
  fetch("/api/save_markdown" + sidQuery(), { method: "POST", headers: {"Content-Type": "application/json"} });
}

// ---------------- Markdown 渲染（marked 预览 + XSS 防护） ----------------
// 预览为阅读友好的 HTML；原始 markdown 仍由导出/下载功能完整保留。
let mdReady = false;
function initMarkdown() {
  if (mdReady || typeof marked === "undefined") return;
  const renderer = new marked.Renderer();
  // 转义 markdown 中的原始 HTML，杜绝注入；链接一律新窗口打开
  renderer.html = (html) => escapeHtml(html);
  renderer.link = (href, title, text) =>
    `<a href="${escapeHtml(href)}" target="_blank" rel="noopener">${text}</a>`;
  marked.setOptions({ breaks: true, gfm: true });
  marked.use({ renderer });
  mdReady = true;
}

function renderMarkdown(md) {
  if (typeof marked === "undefined" || !mdReady) {
    return `<pre class="md-fallback">${escapeHtml(md || "")}</pre>`;
  }
  return marked.parse(md || "");
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

initMarkdown();
init();
