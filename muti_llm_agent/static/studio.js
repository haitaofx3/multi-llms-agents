/* studio.js — 多模型协同工作台的增量前端逻辑
 *
 * 设计原则：**不改动 app.js**。
 * app.js 是 1100 行的既有圆桌逻辑（席位渲染 / 流式气泡 / 历史 / 导出），
 * 直接进去改会牵动大量 DOM 契约。这里改成「外挂式」扩展：
 *
 *   1. 用一个 IIFE 包住全部新代码，不向全局泄漏任何名字；
 *   2. 通过包装全局 `handleEvent` 拦截 WebSocket 事件，追加处理
 *      directive / run_summary / iteration 三种新事件；
 *   3. 复用 app.js 已有的 `getSessionId()` / `generateAgentCards()` /
 *      `addMsg()` / `onModeChange()` 等全局函数，保证会话与视觉一致；
 *   4. 新增四个模块：协同配方、中途纠偏、运行摘要、补充材料续轮。
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // 运行期状态（仅本模块使用）
  const ui = {
    recipes: [],
    roster: null,
    directives: [],
    summary: null,
    lastTraceId: "",
    followupBusy: false,
  };

  // ---------------- 基础工具 ----------------
  function sid() {
    // app.js 的全局函数：稳定绑定本浏览器的会话
    try {
      return getSessionId();
    } catch (e) {
      return "default";
    }
  }

  function withSid(path) {
    return path + (path.indexOf("?") === -1 ? "?" : "&") +
      "session_id=" + encodeURIComponent(sid());
  }

  async function api(path, options) {
    const res = await fetch(path, options);
    let body = null;
    try {
      body = await res.json();
    } catch (e) {
      body = { ok: false, error: "响应不是 JSON（HTTP " + res.status + "）" };
    }
    if (!res.ok && body && body.error === undefined) {
      body.error = "HTTP " + res.status;
    }
    return body || {};
  }

  function hint(el, text, isError) {
    if (!el) return;
    el.textContent = text || "";
    el.classList.toggle("error", !!isError);
  }

  function fmtTokens(n) {
    const v = Number(n || 0);
    if (v >= 1000000) return (v / 1000000).toFixed(2) + "M";
    if (v >= 1000) return (v / 1000).toFixed(1) + "k";
    return String(v);
  }

  function say(text, label, cls) {
    // 复用 app.js 的气泡渲染，保证纠偏/续轮记录出现在同一条讨论流里
    try {
      addMsg("系统", text, cls || "task", null, label || "系统");
    } catch (e) {
      console.log("[studio]", text);
    }
  }

  // ---------------- 协同配方 ----------------
  async function loadRoster() {
    const data = await api("/api/models");
    if (!data.ok) {
      hint($("recipe-hint"), data.error || "模型花名册加载失败", true);
      return;
    }
    ui.roster = data;
    ui.recipes = data.recipes || [];
    const sel = $("recipe-select");
    if (sel) {
      sel.innerHTML = ui.recipes.map(r =>
        '<option value="' + r.name + '"' + (r.is_default ? " selected" : "") + ">" +
        r.title + "（" + r.agents.length + " 席 · " + r.max_rounds + " 轮）" +
        "</option>").join("");
    }
    renderRoster(data);
    const cfgCount = data.configured_count || 0;
    const bad = (data.providers || []).filter(p => p.configured && p.healthy === false);
    let msg;
    if (bad.length) {
      msg = "有 " + bad.length + " 个模型最近调用失败（" +
        bad.map(p => p.label + "：" + (p.last_error || "")).join("；") +
        "），配方已自动避开；修正 .env 后点「刷新」";
    } else if (cfgCount >= 2) {
      msg = "已配置 " + cfgCount + " 个模型，可做真正的多模型协同";
    } else {
      msg = "仅配置 " + cfgCount + " 个模型：配方会自动改派，但建议在 .env 里补第 2 个供应商的 Key";
    }
    hint($("recipe-hint"), msg, bad.length > 0 || cfgCount < 2);
  }

  function renderRoster(data) {
    const box = $("roster-box");
    if (!box) return;
    const items = (data.providers || []).filter(p => p.provider !== "custom");
    box.innerHTML = items.map(p => {
      // 三态：未配置（off）/ 配置且最近调用正常（on）/ 配置但调用报错（warn）
      const cls = !p.configured ? "off" : (p.healthy === false ? "warn" : "on");
      const title = !p.configured
        ? "未配置 API Key"
        : (p.healthy === false
          ? ("最近调用失败 " + (p.failure_count || 0) + " 次：" + (p.last_error || ""))
          : (p.strength || ""));
      return '<span class="roster-chip ' + cls + '" title="' + escapeText(title) + '">' +
        '<span class="dot"></span>' + escapeText(p.label) +
        (p.model_id ? " · " + escapeText(p.model_id) : "") +
        (p.healthy === false ? " ⚠" : "") +
        "</span>";
    }).join("");
  }

  async function applyRecipe() {
    const name = ($("recipe-select").value || "").trim();
    if (!name) return;
    hint($("recipe-hint"), "正在生成席位…");
    const cfg = await api("/api/recipes/" + encodeURIComponent(name));
    if (!cfg.ok) {
      hint($("recipe-hint"), cfg.error || "配方加载失败", true);
      return;
    }
    const agents = cfg.agents || [];
    if (!agents.length) {
      hint($("recipe-hint"), "该配方没有席位定义", true);
      return;
    }
    $("agent-count").value = agents.length;
    // 用配方数据重建席位卡片（复用 app.js 的渲染逻辑，保证样式与校验一致）
    generateAgentCards(agents.length, agents);
    const cards = document.querySelectorAll("#agent-cards .acard");
    cards.forEach((card, i) => {
      const a = agents[i] || {};
      // generateAgentCards 不负责回填 provider 下拉的选中态，这里补齐并触发变更
      const pSel = card.querySelector('[data-k="provider"]');
      if (pSel && a.provider) {
        pSel.value = a.provider;
        if (pSel.value !== a.provider) {
          // 供应商不在下拉里（例如自定义），退化为 custom 并填字段
          pSel.value = "custom";
          const b = card.querySelector('[data-k="base_url"]');
          if (b) b.value = a.base_url || "";
          const m = card.querySelector('[data-k="model_id"]');
          if (m) m.value = a.model_id || "";
        }
        pSel.dispatchEvent(new Event("input"));
      }
      const st = card.querySelector('[data-k="stance"]');
      if (st && a.stance) {
        st.value = a.stance;
        st.dispatchEvent(new Event("input"));
      }
      ["name", "role", "personality"].forEach(k => {
        const el = card.querySelector('[data-k="' + k + '"]');
        if (el && a[k]) {
          el.value = a[k];
          el.dispatchEvent(new Event("input"));
        }
      });
    });
    // 模式与轮次
    if (cfg.mode && $("discussion-mode")) {
      $("discussion-mode").value = cfg.mode;
      try { onModeChange(); } catch (e) { /* 忽略 */ }
    }
    if (cfg.max_rounds && $("max-rounds")) $("max-rounds").value = cfg.max_rounds;
    // 独立裁判
    if (cfg.judge && $("judge-provider")) {
      $("judge-provider").value = cfg.judge.provider || "default";
      try { onJudgeProviderChange(); } catch (e) { /* 忽略 */ }
      if ($("judge-name")) $("judge-name").value = cfg.judge.name || "独立裁判";
    }
    const notes = (cfg.notes || []).join("；");
    hint($("recipe-hint"),
      "已套用「" + cfg.title + "」，实际使用模型：" +
      (cfg.providers_used || []).join(" / ") + (notes ? "（" + notes + "）" : ""));
  }

  // ---------------- 中途注入纠偏 ----------------
  function renderSteerList(items) {
    ui.directives = items || [];
    const box = $("steer-list");
    const badge = $("steer-badge");
    const pending = ui.directives.filter(d =>
      d.status === "pending" || d.status === "active").length;
    if (badge) {
      badge.textContent = String(pending);
      badge.classList.toggle("hidden", pending === 0);
    }
    if (!box) return;
    if (!ui.directives.length) {
      box.innerHTML = '<div class="muted" style="font-size:12px">尚未下发纠偏指令</div>';
      return;
    }
    box.innerHTML = ui.directives.slice().reverse().map(d => {
      const cls = d.status === "applied" ? "applied"
        : (d.status === "cancelled" ? "cancelled" : "pending");
      const scope = d.scope === "agent" ? ("指定:" + (d.target || "?"))
        : (d.scope === "main" ? "仅主持人" : "全员");
      const round = d.applied_round || d.at_round || "-";
      return '<div class="steer-item ' + cls + '">' +
        '<span class="kind">' + (d.kind_label || d.kind) + "</span>" +
        '<span class="meta">[' + scope + " · 第" + round + "轮 · " +
        (d.status === "applied" ? "已生效" :
          d.status === "cancelled" ? "已撤销" : "待生效") + "]</span>" +
        '<div>' + escapeText(d.text) + "</div></div>";
    }).join("");
  }

  function escapeText(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  async function refreshSteer() {
    const data = await api(withSid("/api/steer"));
    if (data.ok) renderSteerList(data.items || []);
  }

  function syncSteerTargets() {
    const scope = $("steer-scope").value;
    const sel = $("steer-target");
    if (!sel) return;
    if (scope !== "agent") {
      sel.classList.add("hidden");
      return;
    }
    sel.classList.remove("hidden");
    // app.js 的 state 是顶层 const（挂在全局词法环境，不在 window 上），
    // 因此只能用 try/catch 探测，不能写成 window.state
    let names = [];
    try {
      names = (state.agents || []).map(a => a.name).filter(Boolean);
    } catch (e) {
      names = [];
    }
    if (!names.length) {
      sel.innerHTML = '<option value="">（尚无席位）</option>';
      return;
    }
    sel.innerHTML = names.map(n => '<option value="' + escapeText(n) + '">' +
      escapeText(n) + "</option>").join("");
  }

  async function sendSteer() {
    const text = ($("steer-text").value || "").trim();
    if (!text) {
      hint($("steer-hint"), "请先填写指令内容", true);
      return;
    }
    const scope = $("steer-scope").value;
    const payload = {
      session_id: sid(),
      text: text,
      kind: $("steer-kind").value,
      scope: scope,
      target: scope === "agent" ? ($("steer-target").value || "") : "",
      priority: $("steer-high").checked ? "high" : "normal",
    };
    hint($("steer-hint"), "下发中…");
    const data = await api("/api/steer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!data.ok) {
      hint($("steer-hint"), data.error || "下发失败", true);
      return;
    }
    $("steer-text").value = "";
    hint($("steer-hint"), data.will_apply ? "已入队，下个回合生效" : (data.hint || "已入队"));
    await refreshSteer();
  }

  // ---------------- 运行摘要 ----------------
  function renderSummary(sum) {
    const box = $("summary-panel");
    if (!box) return;
    if (!sum || (!sum.trace_id && !sum.status)) {
      box.innerHTML = '<div class="muted">暂无可展示的运行摘要</div>';
      return;
    }
    ui.summary = sum;
    ui.lastTraceId = sum.trace_id || "";
    const tokens = sum.tokens || {};
    const steering = sum.steering || {};
    const rows = [
      ["状态", sum.status + (sum.phase ? " / " + sum.phase : "")],
      ["轮次", (sum.rounds_executed || 0) + " / " + (sum.max_rounds || 0)],
      ["迭代", "第 " + (sum.iteration || 1) + " 轮"],
      ["参与模型", (sum.models_used || []).join(" / ") || "-"],
      ["输出 Token", fmtTokens(tokens.output) + "（总 " + fmtTokens(tokens.total) + "）"],
      ["预估费用", "¥" + Number(sum.cost_rmb || 0).toFixed(4)],
      ["纠偏指令", (steering.added || 0) + " 条（已生效 " + (steering.injected || 0) + " 次）"],
      ["trace", sum.trace_id || "-"],
    ];
    const html = ['<div class="sum">' + rows.map(r =>
      '<div class="sum-row"><span class="sum-k">' + r[0] +
      '</span><span class="sum-v">' + escapeText(r[1]) + "</span></div>").join("")];

    const agents = sum.agents || [];
    if (agents.length) {
      html.push('<div style="margin-top:8px">' + agents.map(a =>
        '<div class="sum-model"><span>' + escapeText(a.name) + "</span><span>" +
        escapeText(a.model_id || "-") + " · " + (a.speeches || 0) + " 次 / " +
        fmtTokens(a.chars) + " 字</span></div>").join("") + "</div>");
    }
    const hl = sum.highlights || [];
    if (hl.length) {
      html.push('<ul class="sum-hl">' + hl.map(h => "<li>" + escapeText(h) + "</li>").join("") +
        "</ul>");
    }
    if (sum.error_count) {
      html.push('<div class="hint" style="color:#c0392b">异常 ' + sum.error_count +
        " 条，详见复盘</div>");
    }
    html.push("</div>");
    box.innerHTML = html.join("");
  }

  async function refreshSummary() {
    const data = await api(withSid("/api/summary"));
    if (data.ok && data.summary) renderSummary(data.summary);
  }

  // ---------------- 导出复盘 ----------------
  function exportReview(fmt) {
    const url = withSid("/api/review/export") + "&format=" + encodeURIComponent(fmt);
    // 用链接导航触发下载（服务端带 Content-Disposition），避免 blob 内存拷贝
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    say("已开始导出复盘（" + fmt + "）", "导出");
  }

  // ---------------- 补充材料续轮 ----------------
  function openFollowup() {
    $("followup-panel").classList.remove("hidden");
    hint($("followup-hint"), "");
    $("followup-materials").focus();
  }

  function closeFollowup() {
    $("followup-panel").classList.add("hidden");
  }

  async function uploadFollowupFile() {
    const input = $("followup-file");
    if (!input.files || !input.files.length) {
      hint($("followup-upload-hint"), "请选择文件", true);
      return;
    }
    const fd = new FormData();
    fd.append("file", input.files[0]);
    hint($("followup-upload-hint"), "解析中…");
    try {
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await res.json();
      if (!data.ok) {
        hint($("followup-upload-hint"), data.error || "上传失败", true);
        return;
      }
      // 上传同时已把内容注入共享知识库；这里只需要把来源信息填进材料框
      const box = $("followup-materials");
      const note = "【文件材料】" + (data.source || input.files[0].name) +
        "（" + (data.chars || 0) + " 字，已注入共享知识库，可在讨论中检索）\n";
      box.value = (box.value ? box.value + "\n" : "") + note;
      hint($("followup-upload-hint"), "已解析并注入知识库：" + (data.added || 0) + " 段");
    } catch (e) {
      hint($("followup-upload-hint"), "上传失败：" + e, true);
    }
  }

  async function startFollowup() {
    if (ui.followupBusy) return;
    const materials = ($("followup-materials").value || "").trim();
    const focus = ($("followup-focus").value || "").trim();
    if (!materials && !focus) {
      hint($("followup-hint"), "补充材料与聚焦点至少填一项", true);
      return;
    }
    ui.followupBusy = true;
    hint($("followup-hint"), "正在归档上一轮并准备承接包…");
    try {
      const data = await api("/api/followup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sid(),
          materials: materials,
          focus: focus,
          max_rounds: parseInt($("followup-rounds").value, 10) || 3,
          auto_start: true,
        }),
      });
      if (!data.ok) {
        hint($("followup-hint"), data.error || "续轮失败", true);
        return;
      }
      hint($("followup-hint"),
        "已进入第 " + data.iteration + " 轮迭代（承接 " + (data.carryover_chars || 0) +
        " 字 / 新材料 " + (data.materials_chars || 0) + " 字），新一轮已开始");
      say("补充材料续轮：第 " + data.previous_iteration + " 轮迭代已归档，" +
        "进入第 " + data.iteration + " 轮。承接包 " + (data.carryover_chars || 0) +
        " 字，补充材料 " + (data.materials_chars || 0) + " 字。", "续轮", "summary");
      $("done-bar").classList.add("hidden");
      $("followup-panel").classList.add("hidden");
      $("followup-materials").value = "";
      $("followup-focus").value = "";
    } finally {
      ui.followupBusy = false;
    }
  }

  // ---------------- WebSocket 事件扩展 ----------------
  function onEvent(ev) {
    if (!ev || !ev.type) return;
    switch (ev.type) {
      case "directive": {
        if (ev.action === "applied") {
          const names = (ev.directives || [])
            .map(d => "[" + (d.kind_label || d.kind) + "] " + d.text).join("\n");
          say("已向 " + ev.agent + " 注入中途指令：\n" + names, "纠偏生效", "main");
        } else if (ev.action === "added") {
          const d = ev.directive || {};
          say("收到纠偏指令（" + (d.kind_label || d.kind) + "）：" + (d.text || ""),
            "纠偏", "task");
        }
        refreshSteer();
        break;
      }
      case "iteration": {
        if (ev.action === "prepared" && ev.info) {
          const it = ev.info;
          say("进入第 " + it.iteration + " 轮迭代（上一轮 trace " +
            (it.previous_trace_id || "-") + " 已归档）", "迭代", "summary");
          renderSteerList([]);
        }
        break;
      }
      case "run_summary": {
        if (ev.summary) renderSummary(ev.summary);
        break;
      }
      case "status_sync": {
        if (ev.steering) {
          const badge = $("steer-badge");
          const pending = ev.steering.pending || 0;
          if (badge) {
            badge.textContent = String(pending);
            badge.classList.toggle("hidden", pending === 0);
          }
        }
        break;
      }
      case "status": {
        if (ev.status === "done" || ev.status === "stopped") {
          // 终态：拉一次摘要（事件可能先于 run_summary 到达）
          setTimeout(refreshSummary, 600);
          setTimeout(refreshSteer, 300);
        }
        break;
      }
      default:
        break;
    }
  }

  function hookEvent() {
    if (typeof window.handleEvent !== "function") return;
    if (window.handleEvent.__studioWrapped) return;
    const orig = window.handleEvent;
    const wrapped = function (ev) {
      try {
        orig(ev);
      } catch (e) {
        console.error("[studio] 原始事件处理失败", e);
      }
      try {
        onEvent(ev);
      } catch (e) {
        console.error("[studio] 扩展事件处理失败", e);
      }
    };
    wrapped.__studioWrapped = true;
    window.handleEvent = wrapped;
  }

  // ---------------- 事件绑定 ----------------
  function bind() {
    const on = (id, ev, fn) => {
      const el = $(id);
      if (el) el.addEventListener(ev, fn);
    };
    on("btn-recipe-apply", "click", applyRecipe);
    on("recipe-select", "change", () => hint($("recipe-hint"), ""));
    on("btn-steer", "click", sendSteer);
    on("steer-scope", "change", syncSteerTargets);
    on("steer-text", "keydown", (e) => {
      // Ctrl/Cmd + Enter 快速下发，长指令不必去点按钮
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") sendSteer();
    });
    on("btn-refresh-summary", "click", refreshSummary);
    on("btn-review-html", "click", () => exportReview("html"));
    on("btn-review-json", "click", () => exportReview("json"));
    on("btn-followup-open", "click", openFollowup);
    on("btn-followup-close", "click", closeFollowup);
    on("btn-followup-upload", "click", uploadFollowupFile);
    on("btn-followup-start", "click", startFollowup);
    // 进入讨论界面后同步一次席位下拉（用于「指定席位」纠偏）
    const back = $("btn-back");
    if (back) back.addEventListener("click", () => setTimeout(syncSteerTargets, 100));
  }

  async function init() {
    bind();
    hookEvent();
    await loadRoster();
    await refreshSteer();
    await refreshSummary();
    syncSteerTargets();
    // 每 15 秒兜底刷新一次摘要（WebSocket 断线时也不至于看到空白面板）
    setInterval(() => {
      const panel = $("summary-panel");
      if (!panel) return;
      if (panel.querySelector(".muted")) refreshSummary();
    }, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
