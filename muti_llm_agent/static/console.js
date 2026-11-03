/* ============================================================
   console.js — 接口级调试控制台（原生 JS · 无构建 · 无第三方依赖）

   职责：
     1) 读取 GET /api/dev/endpoints，渲染模块导航与接口清单；
     2) 通过 POST /api/dev/invoke 做接口级调用（Method / Path / Query / Body / 超时）；
     3) is_stream=true 的接口改用 EventSource 直连 SSE；
     4) 一键冒烟（POST /api/dev/smoke）、服务端结构化日志（GET /api/logs）、
        集成状态（GET /api/integrations）等辅助能力。

   约定：
     - 所有逻辑都在 IIFE 内，不向 window 暴露任何变量 / 函数；
     - 所有 fetch 均 try/catch，失败写入底部日志面板，避免白屏；
     - 接口与字段全部按「可能缺省」处理，后端未就绪时给出优雅提示。
   ============================================================ */

(function () {
  "use strict";

  // ---------------- 常量 ----------------
  var LS_SESSION = "debate.session_id";
  var LS_TOKEN = "debate.auth_token";
  var LS_HISTORY = "console.invoke_history";
  var LS_LOGS_OPEN = "console.logs_open";

  var MAX_LOGS = 200;
  var MAX_HISTORY = 50;
  var LOG_LIMIT = 50;
  var INVOKE_TIMEOUT_MS = 30000;

  var METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"];
  // /api/stream 用「事件类型」作为 SSE 事件名（见 api/router_stream.py + orchestrator 事件总线）
  var SSE_EVENT_NAMES = [
    "status", "stream_open", "replay", "log", "error",
    "speech_start", "speech_delta", "speech_end", "speech",
    "tool_call_start", "tool_call_end", "agent_error",
    "task_assigned", "task_claimed", "task_result", "task_revised",
    "directive", "judgment", "run_summary", "iteration", "round",
    "conclusion", "summary", "status_sync",
    "delta", "token", "done", "event", "ping", "heartbeat"
  ];
  // 日志来源的中文显示
  var SOURCE_LABELS = { console: "本地", sse: "SSE", server: "服务端" };

  // ---------------- 页面状态（IIFE 内私有） ----------------
  var S = {
    modules: [],
    endpoints: [],
    total: 0,
    catalogLoaded: false,
    loading: false,
    catalogError: "",
    activeModule: "",
    query: "",
    active: null,
    activeIndex: -1,
    pendingPreload: null,
    openapi: null,
    history: [],
    logs: [],
    logPaused: false,
    logsOpen: true,
    sse: null,
    sseErrorLogged: false,
    busy: false,
    busyEp: null,
    smoking: false,
    lastTrace: "",
    backendOnline: null,
    statusTimer: null,
    searchTimer: null
  };

  // ================= 基础工具 =================

  function byId(id) {
    return document.getElementById(id);
  }

  /** HTML 转义（用于所有拼接进 innerHTML 的动态文本） */
  function escHtml(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function truncate(text, max) {
    var s = String(text === undefined || text === null ? "" : text);
    if (!max || s.length <= max) return s;
    return s.slice(0, max) + "…（已截断）";
  }

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  /** 兼容秒 / 毫秒时间戳 */
  function normalizeTs(ts) {
    var n = typeof ts === "number" ? ts : parseInt(ts, 10);
    if (!isFinite(n) || n <= 0) return Date.now();
    return n < 100000000000 ? n * 1000 : n;
  }

  function timeStr(ts) {
    var d = new Date(normalizeTs(ts));
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  function msStr(ms) {
    var n = typeof ms === "number" ? ms : parseFloat(ms);
    if (!isFinite(n)) return "—";
    return n < 10 ? n.toFixed(1) + " ms" : Math.round(n) + " ms";
  }

  function toInt(value, fallback) {
    var n = parseInt(value, 10);
    return isFinite(n) ? n : fallback;
  }

  function methodClass(method) {
    var m = String(method || "").toUpperCase();
    if (METHODS.indexOf(m) !== -1) return m.toLowerCase();
    return "other";
  }

  function normLevel(level) {
    var s = String(level || "info").toLowerCase();
    if (s === "err" || s === "error" || s === "critical" || s === "fatal") return "error";
    if (s === "warn" || s === "warning") return "warning";
    return "info";
  }

  function errText(err) {
    if (!err) return "未知错误";
    if (err.name === "AbortError") return "请求超时或被中断";
    var msg = err.message || String(err);
    if (/Failed to fetch|NetworkError|Load failed|network error|fetch failed/i.test(msg)) {
      return "网络请求失败";
    }
    return msg;
  }

  function httpHint(code) {
    if (code === 404) return "HTTP 404（接口尚未实现或路径不对）";
    if (code === 401) return "HTTP 401（鉴权失败，请检查 token）";
    if (code === 403) return "HTTP 403（无权访问，请检查 token）";
    return "HTTP " + code;
  }

  /** 优先展示后端返回的 error 文案（例如 DEV_API_ENABLED=0 的 403 原因） */
  function httpError(reply) {
    var data = reply && reply.data;
    if (data && typeof data === "object" && typeof data.error === "string" && data.error) {
      return data.error + "（HTTP " + reply.res.status + "）";
    }
    return httpHint(reply.res.status);
  }

  function parseJSONText(text) {
    try {
      return { ok: true, value: JSON.parse(text) };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  }

  function prettyJSON(value) {
    try {
      var text = JSON.stringify(value, null, 2);
      return text === undefined ? String(value) : text;
    } catch (err) {
      return String(value);
    }
  }

  function isEmptyPlainObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).length === 0;
  }

  function compactJSON(value) {
    try {
      var text = JSON.stringify(value);
      return text === undefined ? String(value) : text;
    } catch (err) {
      return String(value);
    }
  }

  // ---------------- localStorage（禁用时静默降级） ----------------
  function lsGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null;
    }
  }

  function lsSet(key, value) {
    try {
      window.localStorage.setItem(key, value);
      return true;
    } catch (err) {
      return false;
    }
  }

  // ---------------- 环境（session / token） ----------------
  function randomSessionId() {
    return "console-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  }

  function initEnv() {
    var sessionInput = byId("c-session");
    var sid = lsGet(LS_SESSION);
    if (!sid || !String(sid).trim()) {
      sid = randomSessionId();
      lsSet(LS_SESSION, sid);
    }
    if (sessionInput) sessionInput.value = String(sid);

    // 兼容 `?token=xxx` 一次性进入（开启 AUTH_TOKEN 时 /console 本身也需要令牌）
    var fromUrl = "";
    try {
      fromUrl = new URLSearchParams(location.search).get("token") || "";
    } catch (err) {
      fromUrl = "";
    }
    if (fromUrl) lsSet(LS_TOKEN, fromUrl);

    var tokenInput = byId("c-token");
    if (tokenInput) tokenInput.value = lsGet(LS_TOKEN) || "";

    // 日志面板展开状态持久化：首次访问桌面默认展开、窄屏默认折叠
    var pref = lsGet(LS_LOGS_OPEN);
    if (pref === "1") S.logsOpen = true;
    else if (pref === "0") S.logsOpen = false;
    else S.logsOpen = window.innerWidth > 860;
    setLogsOpen(S.logsOpen, false);
  }

  function currentSession() {
    var input = byId("c-session");
    var value = input ? String(input.value || "").trim() : "";
    if (value) return value;
    var stored = lsGet(LS_SESSION);
    if (stored && String(stored).trim()) return String(stored).trim();
    return "default";
  }

  function currentToken() {
    var input = byId("c-token");
    if (input) return String(input.value || "").trim();
    return String(lsGet(LS_TOKEN) || "").trim();
  }

  /** 统一的 JSON 请求封装：自动带 Authorization，返回 { res, data, text }；网络错误会抛出 */
  async function apiFetch(path, opts) {
    opts = opts || {};
    var headers = { "Accept": "application/json" };
    var hasBody = opts.body !== undefined && opts.body !== null;
    if (hasBody) headers["Content-Type"] = "application/json";
    var token = currentToken();
    if (token) headers["Authorization"] = "Bearer " + token;
    if (opts.headers) {
      Object.keys(opts.headers).forEach(function (k) {
        headers[k] = opts.headers[k];
      });
    }
    var init = {
      method: opts.method || "GET",
      headers: headers,
      cache: "no-store"
    };
    if (hasBody) {
      init.body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
    }
    if (opts.signal) init.signal = opts.signal;

    var res = await fetch(path, init);
    var text = await res.text();
    var data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (err) {
        data = text;
      }
    }
    return { res: res, data: data, text: text };
  }

  // ================= 后端状态点 =================

  async function refreshStatus() {
    var dot = byId("c-status-dot");
    var label = byId("c-status-text");
    if (S.backendOnline === null && dot) dot.className = "dot pending";
    if (S.backendOnline === null && label) label.textContent = "检测中…";

    var started = Date.now();
    var url = "/api/status?session_id=" + encodeURIComponent(currentSession());
    try {
      var reply = await apiFetch(url);
      var elapsed = Date.now() - started;
      if (reply.res.ok) {
        if (S.backendOnline === false) appendLog("info", "后端已恢复连接", "console");
        S.backendOnline = true;
        if (dot) dot.className = "dot ok";
        if (label) label.textContent = "后端在线 · " + elapsed + " ms";
        return;
      }
      if (dot) dot.className = "dot warn";
      if (label) {
        label.textContent = (reply.res.status === 401 || reply.res.status === 403)
          ? "鉴权失败 · " + reply.res.status
          : "后端异常 · HTTP " + reply.res.status;
      }
      if (S.backendOnline !== false) {
        appendLog(
          reply.res.status === 401 || reply.res.status === 403 ? "warning" : "error",
          "/api/status " + httpHint(reply.res.status) + "，请检查 token 或后端实现",
          "console"
        );
      }
      S.backendOnline = false;
    } catch (err) {
      if (S.backendOnline !== false) {
        appendLog("error", "/api/status 请求失败：" + errText(err) + "。请确认后端已启动（" + location.origin + "）。", "console");
      }
      S.backendOnline = false;
      if (dot) dot.className = "dot bad";
      if (label) label.textContent = "后端离线";
    }
  }

  // ================= 接口清单 =================

  async function loadEndpoints() {
    if (S.loading) return;
    S.loading = true;
    var btn = byId("c-refresh");
    if (btn) btn.disabled = true;
    renderModules();
    renderEndpoints();

    try {
      var reply = await apiFetch("/api/dev/endpoints");
      if (!reply.res.ok) throw new Error(httpError(reply));
      var data = reply.data;
      if (!data || typeof data !== "object") throw new Error("返回体不是 JSON 对象");
      if (!Array.isArray(data) && data.ok === false) {
        throw new Error(data.error ? String(data.error) : "后端返回 ok=false");
      }

      var catalog = normalizeCatalog(data);
      S.modules = catalog.modules;
      S.endpoints = catalog.endpoints;
      S.total = catalog.count;
      S.catalogLoaded = true;
      S.catalogError = "";
      setAlert("");

      // 刷新后尽量保持当前选中项（不重渲染面板，避免清掉用户已填内容）
      if (S.active) {
        var found = -1;
        for (var i = 0; i < S.endpoints.length; i++) {
          if (S.endpoints[i].path === S.active.path && S.endpoints[i].method === S.active.method) {
            found = i;
            break;
          }
        }
        S.activeIndex = found;
        if (found >= 0) S.active = S.endpoints[found];
      }
      if (S.activeModule && !hasModule(S.activeModule)) S.activeModule = "";

      appendLog(
        "info",
        "接口清单已加载：模块 " + S.modules.length + " 个 / 接口 " + S.endpoints.length + " 个" +
          (catalog.skipped ? "（跳过 " + catalog.skipped + " 条缺少 path 的条目）" : ""),
        "console"
      );
    } catch (err) {
      S.catalogError = errText(err);
      if (!S.endpoints.length) {
        S.modules = [];
        S.total = 0;
        S.catalogLoaded = false;
        S.activeIndex = -1;
      }
      setAlert(
        "接口清单不可用：" + S.catalogError + "。请确认后端已启动且已实现 GET /api/dev/endpoints；" +
          "也可先「载入 openapi」后手动调用。",
        "warn"
      );
      appendLog("error", "加载 /api/dev/endpoints 失败：" + S.catalogError +
        "（后端可能未启动、未实现该接口，或 DEV_API_ENABLED=0）", "console");
    } finally {
      S.loading = false;
      if (btn) btn.disabled = false;
      renderModules();
      renderEndpoints();
    }
    try {
      await refreshStatus();
    } catch (err) {
      // 状态探测自身已记录错误，这里只兜底，避免未处理的 Promise 拒绝
      appendLog("error", "刷新后端状态失败：" + errText(err), "console");
    }
  }

  function hasModule(name) {
    for (var i = 0; i < S.modules.length; i++) {
      if (S.modules[i].name === name) return true;
    }
    return false;
  }

  /** 把后端返回的 modules 结构归一化为前端使用的扁平接口数组（字段全部可缺省） */
  function normalizeCatalog(data) {
    var rawModules = [];
    if (Array.isArray(data)) {
      rawModules = data;
    } else if (data && Array.isArray(data.modules) && data.modules.length) {
      rawModules = data.modules;
    } else if (data && Array.isArray(data.endpoints)) {
      // 兼容 /api/dev/endpoints 直接返回扁平接口数组
      rawModules = [{ name: "all", title: "全部接口", description: "", endpoints: data.endpoints }];
    } else if (data && Array.isArray(data.modules)) {
      rawModules = data.modules;
    }

    var modules = [];
    var endpoints = [];
    var skipped = 0;

    rawModules.forEach(function (rawModule, mi) {
      if (!rawModule || typeof rawModule !== "object") return;
      var name = typeof rawModule.name === "string" && rawModule.name ? rawModule.name : "module-" + (mi + 1);
      var title = typeof rawModule.title === "string" && rawModule.title ? rawModule.title : name;
      var description = typeof rawModule.description === "string" ? rawModule.description : "";
      var rawEndpoints = Array.isArray(rawModule.endpoints) ? rawModule.endpoints : [];
      var mod = { name: name, title: title, description: description, endpoints: [] };

      rawEndpoints.forEach(function (raw) {
        if (!raw || typeof raw !== "object") {
          skipped += 1;
          return;
        }
        var epPath = typeof raw.path === "string" ? raw.path.trim() : "";
        if (!epPath) {
          skipped += 1;
          return;
        }
        var method = typeof raw.method === "string" && raw.method ? raw.method.toUpperCase() : "GET";
        var ep = {
          id: endpoints.length,
          method: method,
          path: epPath,
          summary: typeof raw.summary === "string" ? raw.summary : "",
          tags: Array.isArray(raw.tags)
            ? raw.tags.filter(function (t) { return typeof t === "string" && t; })
            : [],
          session_scoped: raw.session_scoped === true,
          request_model: typeof raw.request_model === "string" ? raw.request_model : "",
          request_schema: raw.request_schema && typeof raw.request_schema === "object" ? raw.request_schema : null,
          parameters: Array.isArray(raw.parameters) ? raw.parameters : [],
          example_body: raw.example_body,
          is_stream: raw.is_stream === true,
          dangerous: raw.dangerous === true,
          moduleName: name,
          moduleTitle: title
        };
        endpoints.push(ep);
        mod.endpoints.push(ep);
      });

      modules.push(mod);
    });

    var rawCount = data && data.count !== undefined ? Number(data.count) : NaN;
    var count = isFinite(rawCount) ? rawCount : endpoints.length;
    if (count <= 0 && endpoints.length > 0) count = endpoints.length;

    return { modules: modules, endpoints: endpoints, count: count, skipped: skipped };
  }

  // ================= 左侧模块导航 =================

  function renderModules() {
    var host = byId("c-modules");
    if (!host) return;
    host.innerHTML = "";

    if (!S.modules.length) {
      var ph = document.createElement("div");
      ph.className = "muted pad";
      ph.textContent = S.loading ? "加载中…" : (S.catalogError ? "接口清单不可用" : "暂无模块");
      host.appendChild(ph);
      updateCounts();
      return;
    }

    host.appendChild(makeModuleButton("", "全部接口", S.endpoints.length, ""));
    S.modules.forEach(function (mod) {
      host.appendChild(makeModuleButton(mod.name, mod.title, mod.endpoints.length, mod.description));
    });
    updateCounts();
  }

  function makeModuleButton(name, title, count, description) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mod-item" + (S.activeModule === name ? " active" : "");
    btn.setAttribute("data-module", name);
    if (description) btn.title = description;

    var titleSpan = document.createElement("span");
    titleSpan.className = "mod-title";
    titleSpan.textContent = title;

    var countSpan = document.createElement("span");
    countSpan.className = "count";
    countSpan.textContent = String(count);

    btn.appendChild(titleSpan);
    btn.appendChild(countSpan);
    return btn;
  }

  // ================= 中间接口清单 =================

  function filteredEndpoints() {
    var q = S.query.trim().toLowerCase();
    var out = [];
    for (var i = 0; i < S.endpoints.length; i++) {
      var ep = S.endpoints[i];
      if (S.activeModule && ep.moduleName !== S.activeModule) continue;
      if (q) {
        var epPath = String(ep.path || "").toLowerCase();
        var summary = String(ep.summary || "").toLowerCase();
        if (epPath.indexOf(q) === -1 && summary.indexOf(q) === -1) continue;
      }
      out.push(ep);
    }
    return out;
  }

  function renderEndpoints() {
    var host = byId("c-endpoints");
    if (!host) return;
    host.innerHTML = "";

    if (S.loading && !S.endpoints.length) {
      var loading = document.createElement("li");
      loading.className = "loading-state";
      loading.textContent = "正在加载接口清单…";
      host.appendChild(loading);
      updateCounts();
      return;
    }
    if (S.catalogError && !S.endpoints.length) {
      host.appendChild(emptyListItem(
        "接口清单不可用",
        "请确认后端已启动并实现 GET /api/dev/endpoints；也可先「载入 openapi」，再用「＋ 手动调用」。",
        true,
        false
      ));
      updateCounts();
      return;
    }
    if (!S.endpoints.length) {
      host.appendChild(emptyListItem("暂无接口", "后端返回的接口清单为空。可手动调用任意路径。", false, false));
      updateCounts();
      return;
    }

    var list = filteredEndpoints();
    if (!list.length) {
      host.appendChild(emptyListItem("没有匹配的接口", "试试清空搜索关键词，或切换左侧模块。", false, true));
      updateCounts();
      return;
    }

    list.forEach(function (ep) {
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ep" + (S.activeIndex === ep.id ? " active" : "");
      btn.setAttribute("data-index", String(ep.id));

      var line = document.createElement("div");
      line.className = "ep-line";
      var methodSpan = document.createElement("span");
      methodSpan.className = "method " + methodClass(ep.method);
      methodSpan.textContent = ep.method;
      var pathSpan = document.createElement("span");
      pathSpan.className = "ep-path";
      pathSpan.textContent = ep.path;
      pathSpan.title = ep.path;
      line.appendChild(methodSpan);
      line.appendChild(pathSpan);
      btn.appendChild(line);

      if (ep.summary) {
        var summary = document.createElement("div");
        summary.className = "ep-summary";
        summary.textContent = ep.summary;
        summary.title = ep.summary;
        btn.appendChild(summary);
      }

      var tags = tagsHtml(ep, 4);
      if (tags) {
        var tagWrap = document.createElement("div");
        tagWrap.className = "ep-tags";
        tagWrap.innerHTML = tags;
        btn.appendChild(tagWrap);
      }

      li.appendChild(btn);
      host.appendChild(li);
    });
    updateCounts();
  }

  function emptyListItem(title, hint, showRetry, showClear) {
    var li = document.createElement("li");
    var wrap = document.createElement("div");
    wrap.className = "empty-state";

    var titleEl = document.createElement("div");
    titleEl.className = "empty-title";
    titleEl.textContent = title;
    wrap.appendChild(titleEl);

    var hintEl = document.createElement("p");
    hintEl.className = "hint";
    hintEl.textContent = hint;
    wrap.appendChild(hintEl);

    var actions = document.createElement("div");
    actions.className = "empty-actions";

    if (showRetry) {
      var retry = document.createElement("button");
      retry.type = "button";
      retry.id = "c-empty-retry";
      retry.className = "small";
      retry.textContent = "重试加载";
      actions.appendChild(retry);
    }
    if (showClear) {
      var clear = document.createElement("button");
      clear.type = "button";
      clear.id = "c-clear-filter";
      clear.className = "ghost-btn small";
      clear.textContent = "清空筛选";
      actions.appendChild(clear);
    }
    var manual = document.createElement("button");
    manual.type = "button";
    manual.id = "c-empty-manual";
    manual.className = "ghost-btn small";
    manual.textContent = "＋ 手动调用";
    actions.appendChild(manual);

    wrap.appendChild(actions);
    li.appendChild(wrap);
    return li;
  }

  function tagsHtml(ep, limit) {
    var out = [];
    if (ep.is_stream) out.push('<span class="tag tag-stream">流式</span>');
    if (ep.session_scoped) out.push('<span class="tag tag-session">会话</span>');
    if (ep.dangerous) out.push('<span class="tag tag-danger">危险</span>');
    var seen = {};
    (ep.tags || []).forEach(function (tag) {
      if (typeof tag !== "string" || !tag || seen[tag]) return;
      if (tag === "stream" || tag === "session" || tag === "dangerous") return;
      seen[tag] = true;
      out.push('<span class="tag">' + escHtml(tag) + "</span>");
    });
    if (limit && out.length > limit) out = out.slice(0, limit);
    return out.join("");
  }

  function updateCounts() {
    var epCount = byId("c-ep-count");
    if (epCount) {
      var total = S.catalogLoaded ? (S.total > 0 ? S.total : S.endpoints.length) : null;
      epCount.textContent = total === null ? "—" : String(total);
    }

    var listCount = byId("c-list-count");
    if (listCount) {
      listCount.textContent = S.endpoints.length
        ? "显示 " + filteredEndpoints().length + " / 共 " + S.endpoints.length
        : "";
    }

    var modCount = byId("c-mod-count");
    if (modCount) modCount.textContent = S.modules.length ? S.modules.length + " 组" : "";
  }

  function setAlert(message, kind) {
    var el = byId("c-alert");
    if (!el) return;
    if (!message) {
      el.className = "alert hidden";
      el.textContent = "";
      return;
    }
    el.className = "alert" + (kind === "err" ? " err" : "");
    el.textContent = message;
  }

  // ================= 选中接口 / 手动调用 =================

  function selectEndpoint(index) {
    var ep = S.endpoints[index];
    if (!ep) return;
    if (S.activeIndex === index && S.active === ep) return; // 重复点击不重置已填内容
    if (S.sse) closeSse(true);
    S.activeIndex = index;
    S.active = ep;
    S.pendingPreload = null;
    renderEndpoints();
    renderPanel();
    scrollPanelOnMobile();
  }

  function openManualPanel(cfg) {
    cfg = cfg || {};
    if (S.sse) closeSse(true);
    S.pendingPreload = cfg.preload || null;
    S.active = {
      id: -1,
      method: String(cfg.method || "GET").toUpperCase(),
      path: cfg.path || "/api/status",
      summary: cfg.summary || "手动调用",
      tags: cfg.tags || ["手动"],
      session_scoped: cfg.session_scoped !== false,
      request_model: "",
      request_schema: null,
      example_body: undefined,
      is_stream: false,
      dangerous: false,
      moduleName: "manual",
      moduleTitle: "手动调用"
    };
    S.activeIndex = -1;
    renderEndpoints();
    renderPanel();
    scrollPanelOnMobile();
  }

  function scrollPanelOnMobile() {
    var panel = byId("c-panel");
    if (!panel || window.innerWidth > 1180 || !panel.scrollIntoView) return;
    // 顶部环境栏是 sticky 的，滚动时预留其高度，避免面板标题被盖住
    var bar = document.querySelector(".envbar");
    var offset = bar ? bar.offsetHeight + 12 : 12;
    panel.style.scrollMarginTop = offset + "px";
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ================= 右侧调用面板 =================

  function renderPanel() {
    var host = byId("c-panel");
    if (!host) return;
    var ep = S.active;

    if (!ep) {
      host.innerHTML = defaultPanelHtml();
      return;
    }

    var method = String(ep.method || "GET").toUpperCase();
    var isStream = !!ep.is_stream;
    var schema = effectiveRequestSchema(ep);
    var bodyText = "";
    if (ep.example_body !== undefined && ep.example_body !== null) {
      // 后端对无请求体的接口也会给出空对象示例，此时保持 Body 为空更清晰
      if (!(isEmptyPlainObject(ep.example_body) && !schema)) {
        bodyText = prettyJSON(ep.example_body);
      }
    }

    host.innerHTML =
      panelHeadHtml(ep, method) +
      '<div class="panel-body">' +
        '<div class="form-grid">' +
          '<label for="c-method">Method</label>' +
          '<select id="c-method">' + methodOptions(method) + "</select>" +
          '<label for="c-path">Path</label>' +
          '<input id="c-path" type="text" spellcheck="false" autocomplete="off" value="' + escHtml(ep.path) + '" />' +
        "</div>" +
        querySectionHtml(ep) +
        (isStream ? streamSectionHtml() : bodySectionHtml(bodyText, schema, method, ep)) +
      "</div>" +
      (isStream ? "" : panelFootHtml());

    bindPanel(ep);
  }

  /** 发送区固定在面板底部：面板内部滚动时主操作始终可见 */
  function panelFootHtml() {
    return '<div class="send-row panel-foot">' +
      '<button type="button" id="c-send" class="primary">发送请求</button>' +
      '<span class="hint">POST /api/dev/invoke · 超时 30s · Ctrl+Enter 发送</span>' +
    "</div>";
  }

  function defaultPanelHtml() {
    return '<div class="empty-state">' +
      '<div class="empty-title">选择一个接口开始调试</div>' +
      "<p>从中间列表点击任意接口，或使用「＋ 手动调用」手填 Path 调用后端。</p>" +
      '<p class="hint">接口清单来自 <code>GET /api/dev/endpoints</code>，请求经由 <code>POST /api/dev/invoke</code> 转发。</p>' +
    "</div>";
  }

  function panelHeadHtml(ep, method) {
    var sub = [];
    if (ep.summary) sub.push(escHtml(ep.summary));
    if (ep.request_model) sub.push('<span class="mono">' + escHtml(ep.request_model) + "</span>");
    if (ep.moduleTitle) sub.push('<span class="muted">' + escHtml(ep.moduleTitle) + "</span>");

    return '<div class="panel-head">' +
      '<div class="panel-title">' +
        '<span class="method ' + methodClass(method) + '">' + escHtml(method) + "</span>" +
        '<code title="' + escHtml(ep.path) + '">' + escHtml(ep.path) + "</code>" +
      "</div>" +
      (sub.length ? '<div class="panel-sub">' + sub.join(" · ") + "</div>" : "") +
      '<div class="panel-tags">' + tagsHtml(ep, 0) + "</div>" +
    "</div>";
  }

  function methodOptions(selected) {
    var list = METHODS.slice();
    if (selected && list.indexOf(selected) === -1) list.unshift(selected);
    return list.map(function (m) {
      return '<option value="' + m + '"' + (m === selected ? " selected" : "") + ">" + m + "</option>";
    }).join("");
  }

  function querySectionHtml(ep) {
    var inferred = inferQueryParams(ep);
    var hint = inferred.length
      ? "已从 openapi / schema 推断 " + inferred.length + " 个，可增删"
      : "未推断到参数，可手动添加";

    return '<section class="ps">' +
      '<div class="ps-head"><span>Query 参数</span><span class="hint" id="c-query-hint">' + escHtml(hint) + "</span></div>" +
      '<label class="check" for="c-q-session">' +
        '<input type="checkbox" id="c-q-session"' + (ep.session_scoped ? " checked" : "") + " />" +
        ' 附加 session_id = <code id="c-q-session-val">' + escHtml(currentSession()) + "</code>" +
      "</label>" +
      '<div id="c-query-rows" class="query-rows"></div>' +
      '<button type="button" id="c-add-query" class="ghost-btn small">＋ 添加参数</button>' +
    "</section>";
  }

  function bodySectionHtml(bodyText, schema, method, ep) {
    var hintText;
    if (bodyText) {
      hintText = "已预填 example_body，可直接发送或修改。";
    } else if (schema) {
      hintText = "未预填示例，可点「用 schema 生成示例」快速填充。";
    } else {
      hintText = "该接口未提供 request_schema，可直接手写 JSON。";
    }
    if (method === "GET" || method === "HEAD") {
      hintText = "GET/HEAD 通常不需要 Body；留空时不会发送 body。" + (bodyText ? "（当前已预填 example_body）" : "");
    }
    var model = ep.request_model
      ? ' <span class="mono">(' + escHtml(ep.request_model) + ")</span>"
      : "";

    return '<section class="ps">' +
      '<div class="ps-head"><span>Body（JSON）' + model + "</span>" +
        '<span class="ps-actions">' +
          '<button type="button" id="c-body-format" class="ghost-btn small">格式化</button>' +
          '<button type="button" id="c-body-clear" class="ghost-btn small">清空</button>' +
          '<button type="button" id="c-body-sample" class="ghost-btn small">用 schema 生成示例</button>' +
        "</span></div>" +
      '<textarea id="c-body" spellcheck="false" placeholder=\'{"key": "value"}\'>' + escHtml(bodyText) + "</textarea>" +
      '<div class="hint body-hint" id="c-body-hint">' + escHtml(hintText) + "</div>" +
    "</section>" +
    '<div id="c-response" class="response hidden"></div>';
  }

  function streamSectionHtml() {
    return '<section class="ps stream-box">' +
      '<div class="ps-head"><span>SSE 调试</span><span id="c-sse-state" class="sse-state">未连接</span></div>' +
      '<div class="hint">该接口标记 is_stream=true，不走 /api/dev/invoke，直接用 EventSource 连接。</div>' +
      '<div class="stream-row">' +
        '<button type="button" id="c-sse-open" class="primary">打开 SSE</button>' +
        '<button type="button" id="c-sse-close" class="ghost-btn" disabled>断开</button>' +
      "</div>" +
      '<div class="sse-url" id="c-sse-url">URL: —</div>' +
      '<div class="hint">EventSource 不能自定义请求头；已填 token 时会以 ?token= 附带（后端支持查询参数鉴权）。</div>' +
    "</section>";
  }

  function bindPanel(ep) {
    var rows = byId("c-query-rows");
    var inferred = inferQueryParams(ep);
    if (rows) {
      inferred.forEach(function (p) {
        rows.appendChild(makeQueryRow(p.name, p.value, false, p.placeholder || "值"));
      });
    }

    var cb = byId("c-q-session");
    if (cb) {
      cb.addEventListener("change", toggleSessionParam);
      if (cb.checked) toggleSessionParam();
    }

    var addBtn = byId("c-add-query");
    if (addBtn && rows) {
      addBtn.addEventListener("click", function () {
        var row = makeQueryRow("", "", false, "值");
        rows.appendChild(row);
        var key = row.querySelector(".q-key");
        if (key) key.focus();
        updateSseUrlPreview();
      });
    }

    if (rows) {
      rows.addEventListener("click", function (ev) {
        var target = ev.target;
        if (!target || !target.closest) return;
        var del = target.closest(".q-del");
        if (!del) return;
        var row = del.closest(".query-row");
        if (!row) return;
        if (row.getAttribute("data-session") === "1" && cb) cb.checked = false;
        if (row.parentNode) row.parentNode.removeChild(row);
        updateSseUrlPreview();
      });
      rows.addEventListener("input", updateSseUrlPreview);
    }

    var pathEl = byId("c-path");
    if (pathEl) {
      pathEl.addEventListener("input", updateSseUrlPreview);
      pathEl.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          sendInvoke();
        }
      });
    }
    var methodEl = byId("c-method");
    if (methodEl) methodEl.addEventListener("change", updateSseUrlPreview);

    var sendBtn = byId("c-send");
    if (sendBtn) sendBtn.addEventListener("click", sendInvoke);

    var formatBtn = byId("c-body-format");
    if (formatBtn) formatBtn.addEventListener("click", formatBody);
    var clearBtn = byId("c-body-clear");
    if (clearBtn) clearBtn.addEventListener("click", clearBody);
    var sampleBtn = byId("c-body-sample");
    if (sampleBtn) sampleBtn.addEventListener("click", useSchemaSample);

    var bodyEl = byId("c-body");
    if (bodyEl) {
      bodyEl.addEventListener("keydown", function (ev) {
        if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
          ev.preventDefault();
          sendInvoke();
        }
      });
    }

    var sseOpen = byId("c-sse-open");
    if (sseOpen) sseOpen.addEventListener("click", openSse);
    var sseClose = byId("c-sse-close");
    if (sseClose) sseClose.addEventListener("click", function () { closeSse(false); });

    applyPreload();
    updateSseUrlPreview();
  }

  // ---------------- Query 参数 ----------------

  function makeQueryRow(name, value, isSession, placeholder) {
    var row = document.createElement("div");
    row.className = "query-row";
    row.setAttribute("data-session", isSession ? "1" : "0");

    var key = document.createElement("input");
    key.type = "text";
    key.className = "q-key";
    key.placeholder = "key";
    key.value = name || "";
    key.setAttribute("aria-label", "参数名");
    key.setAttribute("spellcheck", "false");

    var val = document.createElement("input");
    val.type = "text";
    val.className = "q-val";
    val.placeholder = placeholder || "值";
    val.value = value === undefined || value === null ? "" : String(value);
    val.setAttribute("aria-label", "参数值");
    val.setAttribute("spellcheck", "false");

    if (isSession) {
      key.readOnly = true;
      val.readOnly = true;
      key.title = "由顶部 session_id 决定";
      val.title = "由顶部 session_id 决定";
    }

    var del = document.createElement("button");
    del.type = "button";
    del.className = "q-del";
    del.textContent = "✕";
    del.title = "删除该参数";
    del.setAttribute("aria-label", "删除参数 " + (name || ""));

    row.appendChild(key);
    row.appendChild(val);
    row.appendChild(del);
    return row;
  }

  function makeSessionRow() {
    return makeQueryRow("session_id", currentSession(), true, "session_id");
  }

  function toggleSessionParam() {
    var cb = byId("c-q-session");
    var rows = byId("c-query-rows");
    if (!cb || !rows) return;
    var existing = rows.querySelector('.query-row[data-session="1"]');
    if (cb.checked) {
      if (!existing) rows.insertBefore(makeSessionRow(), rows.firstChild);
    } else if (existing && existing.parentNode) {
      existing.parentNode.removeChild(existing);
    }
    updateSseUrlPreview();
  }

  function updateSessionRowValue() {
    var session = currentSession();
    var label = byId("c-q-session-val");
    if (label) label.textContent = session;
    var input = document.querySelector('#c-query-rows .query-row[data-session="1"] .q-val');
    if (input) input.value = session;
    updateSseUrlPreview();
  }

  function collectQuery() {
    var rows = byId("c-query-rows");
    var query = {};
    if (!rows) return query;
    var list = rows.querySelectorAll(".query-row");
    for (var i = 0; i < list.length; i++) {
      var row = list[i];
      var keyEl = row.querySelector(".q-key");
      var valEl = row.querySelector(".q-val");
      if (!keyEl || !valEl) continue;
      var key = String(keyEl.value || "").trim();
      if (!key) continue;
      var value = valEl.value;
      if (row.getAttribute("data-session") === "1") value = currentSession();
      query[key] = value;
    }
    return query;
  }

  function applyPreload() {
    var pre = S.pendingPreload;
    if (!pre) return;
    S.pendingPreload = null;

    var rows = byId("c-query-rows");
    var cb = byId("c-q-session");
    if (!rows) return;
    if (cb) cb.checked = false;
    rows.innerHTML = "";

    var query = pre.query && typeof pre.query === "object" ? pre.query : {};
    Object.keys(query).forEach(function (key) {
      var value = query[key];
      if (value === undefined || value === null) value = "";
      if (typeof value === "object") value = compactJSON(value);
      rows.appendChild(makeQueryRow(key, String(value), false, "值"));
    });

    var bodyEl = byId("c-body");
    if (bodyEl) {
      if (typeof pre.body === "string") bodyEl.value = pre.body;
      setBodyHint("已载入调用历史，可直接发送复现。", false);
    }
    updateSseUrlPreview();
  }

  function queryPreview(query) {
    var keys = Object.keys(query || {});
    var parts = [];
    keys.slice(0, 6).forEach(function (key) {
      var value = query[key];
      if (/token|secret|password|key/i.test(key)) value = "***";
      parts.push(key + "=" + String(value === undefined || value === null ? "" : value));
    });
    var text = parts.join("&");
    if (keys.length > 6) text += "&…";
    return truncate(text, 90);
  }

  // ---------------- openapi / schema 推断 ----------------

  async function loadOpenapi() {
    var btn = byId("c-openapi");
    if (btn) btn.disabled = true;
    try {
      var reply = await apiFetch("/openapi.json");
      if (!reply.res.ok) throw new Error(httpError(reply));
      var doc = reply.data;
      if (!doc || typeof doc !== "object" || !doc.paths || typeof doc.paths !== "object") {
        throw new Error("不是有效的 OpenAPI 文档（缺少 paths）");
      }
      S.openapi = doc;
      var count = Object.keys(doc.paths).length;
      var title = doc.info && doc.info.title ? " · " + doc.info.title : "";
      appendLog("info", "openapi 已载入：paths " + count + " 条" + title + "（重新选择接口即可使用参数推断 / schema 示例）", "console");
    } catch (err) {
      appendLog("error", "载入 /openapi.json 失败：" + errText(err) + "（该路径需要可访问且携带 token）", "console");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function openapiOp(path, method) {
    var paths = S.openapi && S.openapi.paths;
    if (!paths || typeof paths !== "object") return null;
    var cleanPath = String(path || "").split("?")[0];
    var item = paths[cleanPath];
    if (!item) item = matchTemplatePath(paths, cleanPath);
    if (!item || typeof item !== "object") return null;
    var op = item[String(method || "get").toLowerCase()];
    return op && typeof op === "object" ? op : null;
  }

  function matchTemplatePath(paths, path) {
    var target = String(path || "").split("?")[0].split("/");
    var keys = Object.keys(paths);
    for (var i = 0; i < keys.length; i++) {
      var tpl = keys[i].split("/");
      if (tpl.length !== target.length) continue;
      var matched = true;
      for (var j = 0; j < tpl.length; j++) {
        var seg = tpl[j];
        if (seg.charAt(0) === "{" && seg.charAt(seg.length - 1) === "}") continue;
        if (seg !== target[j]) {
          matched = false;
          break;
        }
      }
      if (matched) return paths[keys[i]];
    }
    return null;
  }

  /** 优先用接口自带的 request_schema，其次用 openapi 的 requestBody schema */
  function effectiveRequestSchema(ep) {
    var raw = ep.request_schema;
    if (raw && typeof raw === "object" &&
        (raw.type || raw.properties || raw.$ref || raw.allOf || raw.oneOf || raw.anyOf)) {
      return raw;
    }
    var op = openapiOp(ep.path, ep.method);
    var content = op && op.requestBody && op.requestBody.content;
    if (content && typeof content === "object") {
      var keys = Object.keys(content);
      for (var i = 0; i < keys.length; i++) {
        var media = content[keys[i]];
        if (media && media.schema && typeof media.schema === "object") return media.schema;
      }
    }
    return raw && typeof raw === "object" ? raw : null;
  }

  function inferQueryParams(ep) {
    var out = [];
    var seen = {};
    var op = openapiOp(ep.path, ep.method);

    function push(name, schema, required) {
      var lower = String(name || "").toLowerCase();
      if (!name || lower === "session_id" || lower === "token" || seen[name]) return;
      seen[name] = true;
      out.push({
        name: name,
        value: defaultValue(schema),
        placeholder: required ? "必填" : "值"
      });
    }

    // 后端 /api/dev/endpoints 直接给出的 parameters（最权威）
    var rawParams = Array.isArray(ep.parameters) ? ep.parameters : [];
    rawParams.forEach(function (p) {
      if (!p || typeof p !== "object" || p.in !== "query") return;
      push(typeof p.name === "string" ? p.name : "", p.schema || {}, p.required === true);
    });

    if (op && Array.isArray(op.parameters)) {
      op.parameters.forEach(function (p) {
        if (!p || typeof p !== "object" || p.in !== "query") return;
        var name = typeof p.name === "string" ? p.name : "";
        push(name, p.schema && typeof p.schema === "object" ? p.schema : {}, p.required === true);
      });
    }

    var schema = ep.request_schema;
    if (schema && Array.isArray(schema.parameters)) {
      schema.parameters.forEach(function (p) {
        if (!p || p.in !== "query") return;
        push(typeof p.name === "string" ? p.name : "", p.schema || {}, p.required === true);
      });
    }

    // 尽力推断：GET/HEAD/DELETE 的 request_schema 也可能是查询参数模型
    if (schema && schema.properties && typeof schema.properties === "object" &&
        (ep.method === "GET" || ep.method === "HEAD" || ep.method === "DELETE")) {
      Object.keys(schema.properties).forEach(function (name) {
        var prop = schema.properties[name];
        if (!isPrimitiveSchema(prop)) return;
        push(name, prop, false);
      });
    }

    return out;
  }

  function isPrimitiveSchema(schema) {
    if (!schema || typeof schema !== "object") return false;
    if (Array.isArray(schema.enum) && schema.enum.length) return true;
    var type = schema.type;
    if (Array.isArray(type)) type = type[0];
    return type === "string" || type === "number" || type === "integer" || type === "boolean";
  }

  function defaultValue(schema) {
    if (!schema || typeof schema !== "object") return "";
    if (schema.default !== undefined && schema.default !== null) return String(schema.default);
    if (Array.isArray(schema.enum) && schema.enum.length) return String(schema.enum[0]);
    if (schema.example !== undefined && schema.example !== null) return String(schema.example);
    return "";
  }

  function resolveRef(ref) {
    if (!ref || ref.charAt(0) !== "#") return null;
    var parts = ref.replace(/^#\//, "").split("/");
    var node = S.openapi;
    for (var i = 0; i < parts.length; i++) {
      if (!node) return null;
      var key = parts[i].replace(/~1/g, "/").replace(/~0/g, "~");
      node = node[key];
    }
    return node || null;
  }

  /** 按 JSON Schema 生成示例值（尽力而为，兼容 $ref / oneOf / allOf） */
  function sampleFromSchema(schema, depth) {
    depth = typeof depth === "number" ? depth : 0;
    if (!schema || typeof schema !== "object" || depth > 8) return null;

    if (typeof schema.$ref === "string") {
      var target = resolveRef(schema.$ref);
      return target ? sampleFromSchema(target, depth + 1) : null;
    }
    if (schema.example !== undefined) return schema.example;
    if (schema.default !== undefined) return schema.default;
    if (Array.isArray(schema.enum) && schema.enum.length) return schema.enum[0];

    var combo = Array.isArray(schema.oneOf) ? schema.oneOf : (Array.isArray(schema.anyOf) ? schema.anyOf : null);
    if (combo && combo.length) return sampleFromSchema(combo[0], depth + 1);

    if (Array.isArray(schema.allOf) && schema.allOf.length) {
      var merged = { type: "object", properties: {} };
      schema.allOf.forEach(function (part) {
        var resolved = part && typeof part.$ref === "string" ? resolveRef(part.$ref) : part;
        if (resolved && resolved.properties && typeof resolved.properties === "object") {
          Object.keys(resolved.properties).forEach(function (key) {
            merged.properties[key] = resolved.properties[key];
          });
        }
      });
      return sampleFromSchema(merged, depth + 1);
    }

    var type = schema.type;
    if (Array.isArray(type)) type = type[0];
    if (!type && schema.properties) type = "object";

    if (type === "object" || schema.properties) {
      var obj = {};
      var props = schema.properties && typeof schema.properties === "object" ? schema.properties : {};
      Object.keys(props).forEach(function (key) {
        obj[key] = sampleFromSchema(props[key], depth + 1);
      });
      return obj;
    }
    if (type === "array") {
      var item = sampleFromSchema(schema.items || {}, depth + 1);
      return item === null ? [] : [item];
    }
    if (type === "integer" || type === "number") return 0;
    if (type === "boolean") return false;
    if (type === "string") {
      if (schema.format === "date-time") return new Date().toISOString();
      if (schema.format === "date") return new Date().toISOString().slice(0, 10);
      return "";
    }
    return null;
  }

  // ---------------- Body 编辑 ----------------

  function setBodyHint(text, isBad) {
    var el = byId("c-body-hint");
    if (!el) return;
    el.textContent = text || "";
    el.className = "hint body-hint" + (isBad ? " bad" : "");
  }

  function formatBody() {
    var textarea = byId("c-body");
    if (!textarea) return;
    var raw = String(textarea.value || "").trim();
    if (!raw) {
      setBodyHint("Body 为空，无需格式化。", false);
      return;
    }
    var parsed = parseJSONText(raw);
    if (!parsed.ok) {
      setBodyHint("JSON 解析失败：" + parsed.error, true);
      appendLog("warning", "Body 格式化失败：" + parsed.error, "console");
      return;
    }
    textarea.value = prettyJSON(parsed.value);
    setBodyHint("已格式化。", false);
  }

  function clearBody() {
    var textarea = byId("c-body");
    if (!textarea) return;
    textarea.value = "";
    setBodyHint("Body 已清空（发送时不带 body）。", false);
  }

  function useSchemaSample() {
    var ep = S.active;
    if (!ep) return;
    var textarea = byId("c-body");
    if (!textarea) return;

    var schema = effectiveRequestSchema(ep);
    if (!schema) {
      if (ep.example_body !== undefined && ep.example_body !== null) {
        textarea.value = prettyJSON(ep.example_body);
        setBodyHint("无 schema，已填入 example_body。", false);
      } else {
        setBodyHint("该接口没有 request_schema，也未从 openapi 找到 requestBody。", true);
        appendLog("warning", "生成示例失败：接口未提供 request_schema，且 openapi 中无 requestBody", "console");
      }
      return;
    }

    var sample = sampleFromSchema(schema);
    if (sample === null && schema.type !== "null") {
      setBodyHint("schema 中没有可生成示例的字段。", true);
      return;
    }
    textarea.value = prettyJSON(sample);
    setBodyHint("已按 schema 生成示例，请按需修改后发送。", false);
    appendLog("info", "已用 schema 生成示例 Body（" + (ep.request_model || "匿名 schema") + "）", "console");
  }

  // ---------------- 发送 invoke ----------------

  async function sendInvoke() {
    var ep = S.active;
    if (!ep) return;
    if (ep.is_stream) {
      appendLog("warning", "该接口是流式接口（is_stream=true），请使用「打开 SSE」，不经过 /api/dev/invoke", "console");
      return;
    }
    if (S.busy && S.busyEp === ep) return;

    var methodEl = byId("c-method");
    var pathEl = byId("c-path");
    var method = String(methodEl ? methodEl.value : ep.method || "GET").toUpperCase();
    var path = String(pathEl ? pathEl.value : ep.path || "").trim();

    if (!path) {
      appendLog("error", "发送失败：Path 不能为空", "console");
      setBodyHint("Path 不能为空", true);
      return;
    }
    if (path.charAt(0) !== "/") {
      appendLog("error", "发送失败：Path 需以 / 开头（当前：" + path + "）", "console");
      setBodyHint("Path 需以 / 开头", true);
      return;
    }

    var bodyEl = byId("c-body");
    var bodyText = bodyEl ? String(bodyEl.value || "").trim() : "";
    var bodyValue;
    if (bodyText) {
      var parsed = parseJSONText(bodyText);
      if (!parsed.ok) {
        var parseMsg = "Body 不是合法 JSON：" + parsed.error;
        setBodyHint(parseMsg, true);
        appendLog("error", "发送失败：" + parseMsg, "console");
        return;
      }
      bodyValue = parsed.value;
    }

    var query = collectQuery();
    var payload = {
      method: method,
      path: path,
      query: query,
      timeout_ms: INVOKE_TIMEOUT_MS
    };
    if (bodyText) payload.body = bodyValue;

    S.busy = true;
    S.busyEp = ep;
    setBodyHint("", false);

    var sendBtn = byId("c-send");
    var originalLabel = sendBtn ? sendBtn.textContent : "发送请求";
    if (sendBtn) {
      sendBtn.disabled = true;
      sendBtn.textContent = "发送中…";
    }

    var started = Date.now();
    var controller = null;
    var timer = null;
    if (typeof window.AbortController === "function") {
      controller = new AbortController();
      timer = window.setTimeout(function () {
        try { controller.abort(); } catch (err) { /* 忽略 */ }
      }, INVOKE_TIMEOUT_MS + 5000);
    }

    var logPrefix = "→ " + method + " " + path +
      (Object.keys(query).length ? "?" + queryPreview(query) : "");

    try {
      var reply = await apiFetch("/api/dev/invoke", {
        method: "POST",
        body: payload,
        signal: controller ? controller.signal : undefined
      });
      var data = reply.data;
      if (!data || typeof data !== "object" || Array.isArray(data)) {
        throw new Error("返回体不是 JSON 对象（HTTP " + reply.res.status + "）");
      }

      var statusCode = toInt(data.status_code, reply.res.status);
      if (statusCode <= 0) statusCode = reply.res.status;
      var elapsed = typeof data.elapsed_ms === "number" ? data.elapsed_ms : (Date.now() - started);
      var traceId = typeof data.trace_id === "string" ? data.trace_id : "";
      if (traceId) S.lastTrace = traceId;

      var hasResponse = Object.prototype.hasOwnProperty.call(data, "response");
      var respValue = hasResponse ? data.response : data;
      var errorMsg = typeof data.error === "string"
        ? data.error
        : (data.error ? compactJSON(data.error) : "");
      var httpOk = statusCode >= 200 && statusCode < 300;
      var bizOk = data.ok !== false;
      if (!errorMsg && !bizOk) errorMsg = "后端返回 ok=false";
      if (!errorMsg && !httpOk) errorMsg = "HTTP " + statusCode;

      if (S.active === ep && S.busyEp === ep) {
        renderResponse({
          status_code: statusCode,
          elapsed_ms: elapsed,
          trace_id: traceId,
          error: errorMsg,
          text: responseText(respValue)
        });
      }

      var level = (statusCode >= 500 || !bizOk) ? (statusCode >= 500 ? "error" : "warning") : "info";
      appendLog(
        level,
        logPrefix + " · " + statusCode + " · " + msStr(elapsed) + (errorMsg ? " · " + errorMsg : ""),
        "console"
      );

      pushHistory({
        ts: Date.now(),
        method: method,
        path: path,
        query: query,
        body: bodyText,
        status_code: statusCode,
        elapsed_ms: elapsed,
        trace_id: traceId,
        ok: httpOk && bizOk
      });
    } catch (err) {
      var elapsedMs = Date.now() - started;
      var isAbort = err && err.name === "AbortError";
      var msg = isAbort
        ? "请求超时（" + Math.round((INVOKE_TIMEOUT_MS + 5000) / 1000) + "s）"
        : errText(err);

      if (S.active === ep && S.busyEp === ep) {
        renderResponse({
          status_code: 0,
          elapsed_ms: elapsedMs,
          trace_id: "",
          error: msg,
          text: "(无响应)",
          networkError: true
        });
      }
      appendLog("error", logPrefix + " · 调用失败：" + msg, "console");

      pushHistory({
        ts: Date.now(),
        method: method,
        path: path,
        query: query,
        body: bodyText,
        status_code: 0,
        elapsed_ms: elapsedMs,
        trace_id: "",
        ok: false
      });
    } finally {
      if (timer) window.clearTimeout(timer);
      if (S.busyEp === ep) {
        S.busy = false;
        S.busyEp = null;
      }
      if (sendBtn) {
        sendBtn.disabled = false;
        sendBtn.textContent = originalLabel || "发送请求";
      }
    }
  }

  function responseText(value) {
    if (value === undefined) return "(无响应体)";
    if (value === null) return "null";
    if (typeof value === "string") return value || "(空字符串)";
    return prettyJSON(value);
  }

  function statusBadge(code) {
    var n = typeof code === "number" ? code : parseInt(code, 10);
    if (!isFinite(n) || n <= 0) return { cls: "err", label: "网络错误" };
    var label = String(n);
    if (n === 200) label += " OK";
    else if (n === 201) label += " Created";
    else if (n === 204) label += " No Content";
    if (n >= 200 && n < 300) return { cls: "ok", label: label };
    if (n >= 400 && n < 500) return { cls: "warn", label: label };
    if (n >= 500) return { cls: "err", label: label };
    return { cls: "na", label: label };
  }

  function renderResponse(info) {
    var box = byId("c-response");
    if (!box) return;
    box.classList.remove("hidden");

    var badge = statusBadge(info.status_code);
    var traceId = info.trace_id || "";
    var bodyHtml;

    if (info.networkError) {
      bodyHtml = '<div class="resp-error">' + escHtml(info.error || "请求失败") + "</div>";
    } else {
      bodyHtml = (info.error ? '<div class="resp-error">' + escHtml(info.error) + "</div>" : "") +
        '<details class="resp-body" open>' +
          "<summary>响应体</summary>" +
          '<div class="resp-tools"><button type="button" class="ghost-btn small" id="c-resp-copy">复制</button></div>' +
          '<pre class="code-block" id="c-resp-text">' + escHtml(info.text) + "</pre>" +
        "</details>";
    }

    box.innerHTML =
      '<div class="resp-head">' +
        '<span class="status-badge ' + badge.cls + '">' + escHtml(badge.label) + "</span>" +
        '<span class="kv">耗时 <b>' + escHtml(msStr(info.elapsed_ms)) + "</b></span>" +
        '<span class="kv">trace <code>' + (traceId ? escHtml(traceId) : "—") + "</code></span>" +
        (traceId ? '<button type="button" class="ghost-btn small" id="c-resp-log">查看 trace 日志</button>' : "") +
      "</div>" +
      bodyHtml;

    var copyBtn = byId("c-resp-copy");
    if (copyBtn) {
      copyBtn.addEventListener("click", function () {
        copyText(info.text, copyBtn);
      });
    }
    var traceBtn = byId("c-resp-log");
    if (traceBtn) {
      traceBtn.addEventListener("click", function () {
        var traceInput = byId("c-log-trace");
        if (traceInput) traceInput.value = traceId;
        setLogsOpen(true, true);
        pullLogs();
      });
    }

    // 面板内部滚动时，把响应区带入视野
    if (box.scrollIntoView) {
      try {
        box.scrollIntoView({ behavior: "smooth", block: "nearest" });
      } catch (err) {
        /* 旧浏览器忽略即可 */
      }
    }
  }

  function copyText(text, button) {
    function finish(ok) {
      if (!button) return;
      var original = "复制";
      button.textContent = ok ? "已复制" : "复制失败";
      window.setTimeout(function () {
        button.textContent = original;
      }, 1200);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(String(text)).then(function () { finish(true); }, function () { finish(false); });
      return;
    }
    try {
      var ta = document.createElement("textarea");
      ta.value = String(text);
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      finish(ok);
    } catch (err) {
      finish(false);
    }
  }

  // ================= SSE =================

  function setSseState(text, cls) {
    var el = byId("c-sse-state");
    if (!el) return;
    el.textContent = text;
    el.className = "sse-state" + (cls ? " " + cls : "");
  }

  function setSseButtons(connected) {
    var openBtn = byId("c-sse-open");
    var closeBtn = byId("c-sse-close");
    if (openBtn) openBtn.disabled = !!connected;
    if (closeBtn) closeBtn.disabled = !connected;
  }

  function maskToken(url) {
    return String(url).replace(/([?&]token=)[^&]*/gi, "$1***");
  }

  function sseQueryString() {
    var query = collectQuery();
    var token = currentToken();
    // token 统一由顶部环境栏注入，避免把空 token 行 / 历史里的旧值带上
    if (token) delete query.token;
    var parts = [];
    var hasSession = false;
    Object.keys(query).forEach(function (key) {
      if (key === "session_id") hasSession = true;
      var value = query[key];
      parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(value === undefined || value === null ? "" : value));
    });
    if (!hasSession) parts.push("session_id=" + encodeURIComponent(currentSession()));
    if (token) parts.push("token=" + encodeURIComponent(token));
    return parts.join("&");
  }

  function updateSseUrlPreview() {
    var box = byId("c-sse-url");
    if (!box) return;
    var ep = S.active;
    var pathEl = byId("c-path");
    var path = String(pathEl && pathEl.value ? pathEl.value : (ep && ep.path) || "").trim();
    if (!path) {
      box.textContent = "URL: —";
      return;
    }
    var sep = path.indexOf("?") === -1 ? "?" : "&";
    box.textContent = "URL: " + maskToken(path + sep + sseQueryString());
  }

  function openSse() {
    var ep = S.active;
    if (!ep || !ep.is_stream) return;
    var pathEl = byId("c-path");
    var path = String(pathEl && pathEl.value ? pathEl.value : ep.path || "").trim();
    if (!path || path.charAt(0) !== "/") {
      appendLog("error", "打开 SSE 失败：Path 需以 / 开头", "sse");
      return;
    }
    if (S.sse) closeSse(true);

    if (typeof window.EventSource !== "function") {
      setSseState("不支持", "bad");
      appendLog("error", "当前浏览器不支持 EventSource，无法调试流式接口", "sse");
      return;
    }

    var sep = path.indexOf("?") === -1 ? "?" : "&";
    var url = path + sep + sseQueryString();

    setSseState("连接中…", "warn");
    setSseButtons(true);
    S.sseErrorLogged = false;

    var es;
    try {
      es = new EventSource(url);
    } catch (err) {
      setSseState("连接失败", "bad");
      setSseButtons(false);
      appendLog("error", "SSE 连接创建失败：" + errText(err), "sse");
      return;
    }
    S.sse = es;

    es.onopen = function () {
      if (S.sse !== es) return;
      S.sseErrorLogged = false;
      setSseState("已连接", "open");
      setSseButtons(true);
      appendLog("info", "SSE 已连接：" + maskToken(url), "sse");
    };

    es.onmessage = function (ev) {
      if (S.sse !== es) return;
      addSseEvent("message", ev.data);
    };

    SSE_EVENT_NAMES.forEach(function (name) {
      es.addEventListener(name, function (ev) {
        if (S.sse !== es) return;
        if (!ev || ev.data === undefined || ev.data === null) return; // 网络 error 由 onerror 处理
        addSseEvent(name, ev.data);
      });
    });

    es.onerror = function () {
      if (S.sse !== es) return;
      if (es.readyState === 2) {
        setSseState("已断开", "bad");
        setSseButtons(false);
        S.sse = null;
        if (!S.sseErrorLogged) {
          S.sseErrorLogged = true;
          appendLog("warning", "SSE 连接已断开：" + maskToken(url) + "（可点击「打开 SSE」重连）", "sse");
        }
      } else {
        setSseState("重连中…", "warn");
      }
    };
  }

  function closeSse(quiet) {
    var had = !!S.sse;
    if (S.sse) {
      try { S.sse.close(); } catch (err) { /* 忽略 */ }
      S.sse = null;
    }
    setSseButtons(false);
    setSseState(had ? "已断开" : "未连接", had ? "bad" : "");
    if (had && !quiet) appendLog("info", "SSE 已手动断开", "sse");
  }

  function addSseEvent(name, data) {
    if (data === undefined || data === null || data === "") return;
    var level = name === "error" ? "error" : "info";
    var text = truncate(formatSseData(data), 4000);
    if (typeof data === "string") {
      var parsed = parseJSONText(data);
      if (parsed.ok && parsed.value && typeof parsed.value === "object" && typeof parsed.value.level === "string") {
        level = normLevel(parsed.value.level);
      }
    }
    appendLog(level, "[" + name + "] " + text, "sse");
  }

  function formatSseData(raw) {
    var text = typeof raw === "string" ? raw : compactJSON(raw);
    var trimmed = text.trim();
    if (!trimmed) return "(空事件)";
    if (trimmed.charAt(0) === "{" || trimmed.charAt(0) === "[") {
      var parsed = parseJSONText(trimmed);
      if (parsed.ok && parsed.value && typeof parsed.value === "object" && !Array.isArray(parsed.value)) {
        var event = typeof parsed.value.event === "string" ? parsed.value.event : "";
        var speaker = typeof parsed.value.speaker === "string" && parsed.value.speaker
          ? "[" + parsed.value.speaker + "] "
          : "";
        var msg = "";
        if (typeof parsed.value.msg === "string") msg = parsed.value.msg;
        else if (typeof parsed.value.message === "string") msg = parsed.value.message;
        else if (typeof parsed.value.text === "string") msg = parsed.value.text;
        else if (typeof parsed.value.delta === "string") msg = parsed.value.delta;
        if (msg) return (event ? event + " · " : "") + speaker + msg;
        return compactJSON(parsed.value);
      }
    }
    return text;
  }

  // ================= 调用历史 =================

  function loadHistoryStore() {
    S.history = [];
    var raw = lsGet(LS_HISTORY);
    if (!raw) return;
    var arr = parseJSONText(raw);
    if (!arr.ok || !Array.isArray(arr.value)) return;
    S.history = arr.value.filter(function (item) {
      return item && typeof item === "object" && typeof item.path === "string";
    }).slice(0, MAX_HISTORY);
  }

  function saveHistoryStore() {
    try {
      var text = JSON.stringify(S.history);
      if (lsSet(LS_HISTORY, text)) return;
      // localStorage 配额不足：只保留最近 10 条再试一次
      S.history = S.history.slice(0, 10);
      lsSet(LS_HISTORY, JSON.stringify(S.history));
    } catch (err) {
      // 记录不可序列化 / 存储不可用时放弃持久化，不影响本次调用
    }
  }

  function pushHistory(record) {
    S.history.unshift(record);
    if (S.history.length > MAX_HISTORY) S.history.length = MAX_HISTORY;
    saveHistoryStore();
    renderHistory();
  }

  function renderHistory() {
    var host = byId("c-history");
    var countEl = byId("c-history-count");
    if (countEl) countEl.textContent = String(S.history.length);
    if (!host) return;
    host.innerHTML = "";

    if (!S.history.length) {
      var empty = document.createElement("div");
      empty.className = "muted pad";
      empty.textContent = "暂无记录";
      host.appendChild(empty);
      return;
    }

    S.history.forEach(function (record, index) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "hist-item";
      btn.setAttribute("data-hid", String(index));
      btn.title = "点击载入该次调用（" + record.method + " " + record.path + "）";

      var line = document.createElement("div");
      line.className = "hist-line";
      var method = document.createElement("span");
      method.className = "method " + methodClass(record.method);
      method.textContent = record.method || "?";
      var path = document.createElement("span");
      path.className = "hist-path";
      path.textContent = record.path || "—";
      line.appendChild(method);
      line.appendChild(path);

      var meta = document.createElement("span");
      meta.className = "hist-meta";
      meta.textContent = timeStr(record.ts) + " · " +
        (record.status_code ? record.status_code : "网络错误") + " · " + msStr(record.elapsed_ms);

      btn.appendChild(line);
      btn.appendChild(meta);
      host.appendChild(btn);
    });
  }

  function loadHistoryItem(index) {
    var record = S.history[index];
    if (!record) return;
    if (S.sse) closeSse(true);
    S.active = {
      id: -1,
      method: String(record.method || "GET").toUpperCase(),
      path: record.path || "/",
      summary: "来自调用历史（" + timeStr(record.ts) + "）",
      tags: ["历史"],
      session_scoped: false,
      request_model: "",
      request_schema: null,
      example_body: undefined,
      is_stream: false,
      dangerous: false,
      moduleName: "manual",
      moduleTitle: ""
    };
    S.activeIndex = -1;
    S.pendingPreload = {
      query: record.query && typeof record.query === "object" ? record.query : {},
      body: typeof record.body === "string" ? record.body : ""
    };
    renderEndpoints();
    renderPanel();
    appendLog("info", "已载入调用历史：" + S.active.method + " " + S.active.path, "console");
    scrollPanelOnMobile();
  }

  // ================= 一键冒烟 =================

  async function runSmoke() {
    if (S.smoking) return;
    S.smoking = true;
    var btn = byId("c-smoke");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "冒烟中…";
    }
    var host = byId("c-smoke-panel");
    var integrationsHost = byId("c-integrations-panel");
    if (integrationsHost) integrationsHost.classList.add("hidden");
    if (host) {
      host.classList.remove("hidden");
      host.innerHTML = '<div class="card-head"><h2>一键冒烟</h2><span class="muted">运行中…</span></div>' +
        '<div class="muted pad">正在逐个调用接口，请稍候…</div>';
    }

    try {
      var reply = await apiFetch("/api/dev/smoke", { method: "POST" });
      if (!reply.res.ok) throw new Error(httpError(reply));
      var data = reply.data;
      if (!data || typeof data !== "object") throw new Error("返回体不是 JSON 对象");
      if (data.ok === false && !Array.isArray(data.results)) {
        throw new Error(data.error ? String(data.error) : "后端返回 ok=false");
      }
      renderSmoke(data);
      var stats = smokeStats(data);
      appendLog(
        stats.failed > 0 ? "warning" : "info",
        "一键冒烟完成：通过 " + stats.passed + " / 共 " + stats.total +
          (stats.failed ? "，失败 " + stats.failed : ""),
        "console"
      );
    } catch (err) {
      var msg = errText(err);
      showSmokeError(msg);
      appendLog("error", "一键冒烟失败：" + msg, "console");
    } finally {
      S.smoking = false;
      if (btn) {
        btn.disabled = false;
        btn.textContent = "一键冒烟";
      }
    }
  }

  function smokeStats(data) {
    var results = Array.isArray(data.results) ? data.results : [];
    var total = toInt(data.total, results.length);
    if (total < results.length) total = results.length;
    var passed = toInt(data.passed, results.filter(resultOk).length);
    var failed = toInt(data.failed, total - passed);
    if (failed < 0) failed = 0;
    return { results: results, total: total, passed: passed, failed: failed };
  }

  function resultOk(item) {
    if (!item || typeof item !== "object") return false;
    if (item.ok === true) return true;
    var code = toInt(item.status_code, 0);
    return code >= 200 && code < 300;
  }

  function renderSmoke(data) {
    var host = byId("c-smoke-panel");
    if (!host) return;
    var stats = smokeStats(data);
    var rows = stats.results.map(function (item) {
      item = item || {};
      var ok = resultOk(item);
      var badge = statusBadge(item.status_code);
      return '<tr class="' + (ok ? "" : "row-fail") + '">' +
        '<td class="cell-name">' + escHtml(item.name || "未命名") + "</td>" +
        '<td><span class="method ' + methodClass(item.method) + '">' + escHtml(item.method || "?") + "</span></td>" +
        '<td class="cell-path">' + escHtml(item.path || "—") + "</td>" +
        '<td><span class="status-badge ' + badge.cls + '">' + escHtml(badge.label) + "</span></td>" +
        "<td>" + escHtml(msStr(item.elapsed_ms)) + "</td>" +
        '<td class="cell-err">' + escHtml(item.error || "") + "</td>" +
      "</tr>";
    }).join("");

    host.classList.remove("hidden");
    host.innerHTML = '<div class="card-head">' +
        '<h2>一键冒烟</h2>' +
        '<span class="smoke-summary">通过 <b>' + stats.passed + "</b> / 共 " + stats.total +
          '<span class="smoke-fail">失败 ' + stats.failed + "</span></span>" +
        '<button type="button" class="panel-close" id="c-smoke-close" aria-label="关闭冒烟结果">✕</button>' +
      "</div>" +
      (stats.results.length
        ? '<div class="table-wrap"><table class="smoke-table">' +
            "<thead><tr><th>名称</th><th>方法</th><th>路径</th><th>状态</th><th>耗时</th><th>错误</th></tr></thead>" +
            "<tbody>" + rows + "</tbody></table></div>"
        : '<div class="muted pad">冒烟返回了空结果。</div>');
    bindSmokeClose();
  }

  function showSmokeError(message) {
    var host = byId("c-smoke-panel");
    if (!host) return;
    host.classList.remove("hidden");
    host.innerHTML = '<div class="card-head"><h2>一键冒烟</h2>' +
        '<button type="button" class="panel-close" id="c-smoke-close" aria-label="关闭冒烟结果">✕</button></div>' +
      '<div class="alert err">' + escHtml(message) + "</div>" +
      '<div class="muted pad">提示：确认后端已实现 <code>POST /api/dev/smoke</code> 且服务已启动。</div>';
    bindSmokeClose();
  }

  function bindSmokeClose() {
    var btn = byId("c-smoke-close");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var host = byId("c-smoke-panel");
      if (host) host.classList.add("hidden");
    });
  }

  // ================= 集成状态 =================

  async function loadIntegrations() {
    var host = byId("c-integrations-panel");
    if (!host) return;
    var smokeHost = byId("c-smoke-panel");
    if (smokeHost) smokeHost.classList.add("hidden");
    host.classList.remove("hidden");
    host.innerHTML = '<div class="card-head"><h2>集成状态</h2>' +
        '<span class="muted">GET /api/integrations</span>' +
        '<button type="button" class="panel-close" id="c-integrations-close" aria-label="关闭集成状态">✕</button></div>' +
      '<div class="muted pad">正在拉取…</div>';
    bindIntegrationsClose();

    try {
      var reply = await apiFetch("/api/integrations");
      if (!reply.res.ok) throw new Error(httpError(reply));
      host.innerHTML = '<div class="card-head"><h2>集成状态</h2>' +
          '<span class="muted">GET /api/integrations</span>' +
          '<button type="button" class="panel-close" id="c-integrations-close" aria-label="关闭集成状态">✕</button></div>' +
        '<pre class="mini-json">' + escHtml(prettyJSON(reply.data)) + "</pre>";
      bindIntegrationsClose();
    } catch (err) {
      host.innerHTML = '<div class="card-head"><h2>集成状态</h2>' +
          '<button type="button" class="panel-close" id="c-integrations-close" aria-label="关闭集成状态">✕</button></div>' +
        '<div class="alert err">拉取 /api/integrations 失败：' + escHtml(errText(err)) + "</div>" +
        '<div class="muted pad">该接口可能尚未实现；本面板只做原始 JSON 展示，不依赖具体字段。</div>';
      bindIntegrationsClose();
      appendLog("error", "拉取 /api/integrations 失败：" + errText(err), "console");
    }
  }

  function bindIntegrationsClose() {
    var btn = byId("c-integrations-close");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var host = byId("c-integrations-panel");
      if (host) host.classList.add("hidden");
    });
  }

  // ================= 日志面板 =================

  function appendLog(level, message, source, ts) {
    var entry = {
      ts: typeof ts === "number" ? normalizeTs(ts) : Date.now(),
      level: normLevel(level),
      msg: truncate(String(message === undefined || message === null ? "" : message), 6000),
      source: source || "console"
    };
    S.logs.push(entry);

    var body = byId("c-logbody");
    if (body) {
      var placeholder = body.querySelector(".log-empty");
      if (placeholder) body.innerHTML = "";
      body.appendChild(buildLogRow(entry));
    }
    while (S.logs.length > MAX_LOGS) {
      S.logs.shift();
      if (body && body.firstElementChild) body.removeChild(body.firstElementChild);
    }
    updateLogCount();
    if (body && !S.logPaused) body.scrollTop = body.scrollHeight;
  }

  function buildLogRow(entry) {
    var row = document.createElement("div");
    row.className = "log-row lv-" + entry.level;

    var level = document.createElement("span");
    level.className = "lv-tag";
    level.textContent = entry.level;

    var time = document.createElement("span");
    time.className = "log-time";
    time.textContent = timeStr(entry.ts);

    var source = document.createElement("span");
    source.className = "log-src";
    source.textContent = SOURCE_LABELS[entry.source] || entry.source;

    var msg = document.createElement("span");
    msg.className = "log-msg";
    msg.textContent = entry.msg;

    row.appendChild(level);
    row.appendChild(time);
    row.appendChild(source);
    row.appendChild(msg);
    return row;
  }

  function updateLogCount() {
    var count = byId("c-log-count");
    if (count) count.textContent = S.logs.length + " 条";

    var errors = 0;
    for (var i = 0; i < S.logs.length; i++) {
      if (S.logs[i].level === "error") errors += 1;
    }
    var hint = byId("c-log-hint");
    if (hint) {
      hint.textContent = S.logPaused
        ? "已暂停自动滚动"
        : (errors ? "错误 " + errors + " 条" : "自动滚动");
      hint.className = "log-live muted" + (errors ? " bad" : "");
    }
  }

  function toggleLogPause() {
    S.logPaused = !S.logPaused;
    var btn = byId("c-log-pause");
    if (btn) {
      btn.textContent = S.logPaused ? "继续" : "暂停";
      btn.setAttribute("aria-pressed", S.logPaused ? "true" : "false");
    }
    appendLog("info", S.logPaused ? "日志自动滚动已暂停" : "日志自动滚动已恢复", "console");
  }

  function clearLogs() {
    S.logs = [];
    var body = byId("c-logbody");
    if (body) body.innerHTML = '<div class="muted pad log-empty">暂无日志</div>';
    updateLogCount();
  }

  function setLogsOpen(open, persist) {
    S.logsOpen = !!open;
    if (document.body) {
      document.body.classList.toggle("logs-open", S.logsOpen);
      document.body.classList.toggle("logs-collapsed", !S.logsOpen);
    }
    var btn = byId("c-log-toggle");
    if (btn) btn.setAttribute("aria-expanded", S.logsOpen ? "true" : "false");
    if (persist) lsSet(LS_LOGS_OPEN, S.logsOpen ? "1" : "0");
    if (S.logsOpen) {
      var body = byId("c-logbody");
      if (body && !S.logPaused) body.scrollTop = body.scrollHeight;
    }
  }

  async function pullLogs() {
    var traceEl = byId("c-log-trace");
    var levelEl = byId("c-log-level");
    var trace = traceEl ? String(traceEl.value || "").trim() : "";
    var level = levelEl ? String(levelEl.value || "").trim() : "";

    var params = ["limit=" + LOG_LIMIT];
    if (trace) params.push("trace_id=" + encodeURIComponent(trace));
    if (level) params.push("level=" + encodeURIComponent(level));

    var btn = byId("c-log-pull");
    if (btn) btn.disabled = true;
    try {
      var reply = await apiFetch("/api/logs?" + params.join("&"));
      if (!reply.res.ok) throw new Error(httpError(reply));
      var data = reply.data && typeof reply.data === "object" ? reply.data : {};
      var items = Array.isArray(data.items) ? data.items : (Array.isArray(data.logs) ? data.logs : []);

      appendLog("info", "拉取服务端日志：trace=" + (trace || "全部") + "，level=" + (level || "全部") +
        "，返回 " + items.length + " 条", "server");
      items.forEach(function (item) {
        if (!item || typeof item !== "object") return;
        appendLog(item.level || "info", serverLogText(item), "server", item.ts);
      });
      if (!items.length) appendLog("info", "服务端暂无匹配日志", "server");
    } catch (err) {
      appendLog("error", "拉取 /api/logs 失败：" + errText(err), "server");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function serverLogText(item) {
    var event = typeof item.event === "string" ? item.event : "";
    var msg = typeof item.msg === "string"
      ? item.msg
      : (typeof item.message === "string" ? item.message : "");
    var text = (event ? "[" + event + "] " : "") + (msg || compactJSON(item));
    if (item.trace_id) text += "  · trace=" + item.trace_id;
    return text;
  }

  // ================= 事件绑定 =================

  function bindEnv() {
    var sessionInput = byId("c-session");
    if (sessionInput) {
      sessionInput.addEventListener("input", function () {
        var value = String(sessionInput.value || "").trim();
        if (value) lsSet(LS_SESSION, value);
        updateSessionRowValue();
      });
      sessionInput.addEventListener("change", function () {
        if (!String(sessionInput.value || "").trim()) {
          var generated = randomSessionId();
          sessionInput.value = generated;
          lsSet(LS_SESSION, generated);
          updateSessionRowValue();
          appendLog("warning", "session_id 不能为空，已自动生成：" + generated, "console");
        }
      });
    }

    var tokenInput = byId("c-token");
    if (tokenInput) {
      tokenInput.addEventListener("input", function () {
        lsSet(LS_TOKEN, String(tokenInput.value || ""));
        updateSseUrlPreview();
      });
    }

    var refreshBtn = byId("c-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", function () { loadEndpoints(); });
    var smokeBtn = byId("c-smoke");
    if (smokeBtn) smokeBtn.addEventListener("click", runSmoke);
    var openapiBtn = byId("c-openapi");
    if (openapiBtn) openapiBtn.addEventListener("click", loadOpenapi);
  }

  function bindNav() {
    var search = byId("c-search");
    if (search) {
      search.addEventListener("input", function () {
        var value = search.value;
        if (S.searchTimer) window.clearTimeout(S.searchTimer);
        S.searchTimer = window.setTimeout(function () {
          S.query = String(value || "");
          renderEndpoints();
        }, 120);
      });
    }

    var modules = byId("c-modules");
    if (modules) {
      modules.addEventListener("click", function (ev) {
        var target = ev.target;
        if (!target || !target.closest) return;
        var btn = target.closest(".mod-item");
        if (!btn) return;
        S.activeModule = btn.getAttribute("data-module") || "";
        renderModules();
        renderEndpoints();
        var list = byId("c-endpoints");
        if (list) list.scrollTop = 0;
      });
    }
  }

  function bindList() {
    var list = byId("c-endpoints");
    if (!list) return;
    list.addEventListener("click", function (ev) {
      var target = ev.target;
      if (!target || !target.closest) return;

      if (target.closest("#c-empty-retry")) {
        loadEndpoints();
        return;
      }
      if (target.closest("#c-empty-manual")) {
        openManualPanel({});
        return;
      }
      if (target.closest("#c-clear-filter")) {
        S.query = "";
        S.activeModule = "";
        var search = byId("c-search");
        if (search) search.value = "";
        renderModules();
        renderEndpoints();
        return;
      }
      var epBtn = target.closest(".ep");
      if (epBtn) {
        var index = parseInt(epBtn.getAttribute("data-index"), 10);
        if (!isNaN(index)) selectEndpoint(index);
      }
    });
  }

  function bindLogDock() {
    var toggle = byId("c-log-toggle");
    if (toggle) toggle.addEventListener("click", function () { setLogsOpen(!S.logsOpen, true); });

    var pause = byId("c-log-pause");
    if (pause) pause.addEventListener("click", toggleLogPause);

    var clear = byId("c-log-clear");
    if (clear) clear.addEventListener("click", clearLogs);

    var pull = byId("c-log-pull");
    if (pull) pull.addEventListener("click", pullLogs);

    var trace = byId("c-log-trace");
    if (trace) {
      trace.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          pullLogs();
        }
      });
    }

    var history = byId("c-history");
    if (history) {
      history.addEventListener("click", function (ev) {
        var target = ev.target;
        if (!target || !target.closest) return;
        var item = target.closest(".hist-item");
        if (!item) return;
        var index = parseInt(item.getAttribute("data-hid"), 10);
        if (!isNaN(index)) loadHistoryItem(index);
      });
    }
  }

  function bindMisc() {
    var manual = byId("c-manual");
    if (manual) manual.addEventListener("click", function () { openManualPanel({}); });
    var integrations = byId("c-integrations-btn");
    if (integrations) integrations.addEventListener("click", loadIntegrations);
  }

  // ================= 启动 =================

  function boot() {
    initEnv();
    bindEnv();
    bindNav();
    bindList();
    bindLogDock();
    bindMisc();

    loadHistoryStore();
    renderModules();
    renderEndpoints();
    renderHistory();
    updateCounts();

    appendLog("info", "接口控制台已就绪 · " + location.origin, "console");
    appendLog(
      "info",
      "session_id = " + currentSession() + "；token " + (currentToken() ? "已设置（不显示）" : "未设置"),
      "console"
    );

    loadEndpoints();
    refreshStatus();
    S.statusTimer = window.setInterval(function () { refreshStatus(); }, 30000);
    window.addEventListener("beforeunload", function () {
      if (S.sse) closeSse(true);
    });
  }

  function reportFatal(err) {
    try {
      appendLog("error", "控制台初始化失败：" + errText(err), "console");
      setAlert("控制台初始化失败：" + errText(err) + "，请打开浏览器控制台查看详情。", "err");
    } catch (inner) {
      var body = document.body;
      if (body) {
        var box = document.createElement("div");
        box.className = "alert err";
        box.style.margin = "16px";
        box.textContent = "控制台初始化失败，请打开浏览器控制台查看详情。";
        body.insertBefore(box, body.firstChild);
      }
    }
  }

  function start() {
    try {
      boot();
    } catch (err) {
      reportFatal(err);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
