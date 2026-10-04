"use strict";

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const state = { csrf: "", settings: {}, notifications: null, notificationBusy: false, lastBootstrap: 0, runs: [], selected: null, detail: null, loading: false, generation: 0, tab: "chat", signatures: {}, toastTimer: null, defaultWorkspace: "" };
  const peerIds = Array.from({ length: 10 }, (_, i) => `peer-${String(i + 1).padStart(2, "0")}`);
  const terminal = new Set(["completed", "complete", "stopped", "failed", "error", "cancelled", "canceled", "budget_exhausted"]);
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const pretty = value => typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 2);
  const human = value => String(value ?? "").replace(/[_-]/g, " ");
  const number = value => Number(value || 0).toLocaleString();
  const compact = value => new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(Number(value || 0));
  const money = value => {
    const amount = Number(value ?? 0);
    if (!Number.isFinite(amount)) return "—";
    if (amount > 0 && amount < 0.000001) return "<$0.000001";
    return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(amount);
  };
  const peerName = id => id === "all" ? "Entire swarm" : id === "user" ? "You" : id === "system" ? "System" : /^peer-\d+$/.test(id || "") ? `Peer ${id.slice(-2)}` : String(id || "Swarm");
  const time = value => {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  };
  const shortDate = value => {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString([], { month: "short", day: "numeric" });
  };
  const empty = (title, body, symbol = "↗") => `<div class="empty-state"><span class="empty-state-icon" aria-hidden="true">${symbol}</span><strong>${escape(title)}</strong><p>${escape(body)}</p></div>`;

  async function api(path, options = {}) {
    const headers = { Accept: "application/json" };
    if (options.method && options.method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-Swarm-Token"] = state.csrf;
    }
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options, headers: { ...headers, ...options.headers }, body: options.body === undefined ? undefined : JSON.stringify(options.body) });
    let payload;
    try { payload = await response.json(); } catch { payload = null; }
    if (!response.ok) {
      const detail = payload?.detail ?? payload?.error ?? payload?.message;
      throw new Error(typeof detail === "string" ? detail : detail ? pretty(detail) : `Request failed (${response.status}).`);
    }
    return payload;
  }

  function setConnection(connected, message = "") {
    $("#connection-dot").className = `connection-dot ${connected ? "online" : "offline"}`;
    $("#connection-status").textContent = connected ? "Local server connected" : "Connection interrupted";
    $("#connection-error").hidden = connected;
    $("#connection-error").textContent = connected ? "" : `${message || "Cannot reach the local server."} The dashboard will reconnect automatically.`;
  }

  function toast(message, error = false) {
    clearTimeout(state.toastTimer);
    const target = $("#toast");
    target.textContent = message;
    target.className = `toast${error ? " error" : ""}`;
    target.hidden = false;
    state.toastTimer = setTimeout(() => { target.hidden = true; }, error ? 7000 : 4000);
  }

  function formError(id, error) {
    const target = $(id);
    target.textContent = error?.message ?? error ?? "";
    target.hidden = !error;
  }

  async function withButton(button, label, action) {
    if (button.disabled) return;
    const original = button.innerHTML;
    button.dataset.busy = "true";
    button.disabled = true;
    button.textContent = label;
    try { return await action(); }
    finally {
      delete button.dataset.busy;
      button.innerHTML = original;
      button.disabled = false;
      if (state.detail) renderRun(state.detail);
    }
  }

  function renderSidebar() {
    $("#run-count").textContent = state.runs.length;
    $("#run-list").innerHTML = state.runs.length ? state.runs.map(run => `<button class="run-list-entry${run.id === state.selected ? " selected" : ""}" data-run-id="${escape(run.id)}" ${run.id === state.selected ? 'aria-current="page"' : ""} title="${escape(run.task)}"><span class="status-dot" aria-hidden="true"></span><span class="run-list-text"><span class="run-list-title">${escape(run.task || "Untitled run")}</span><span class="run-list-meta">${escape(human(run.status))} · ${escape(shortDate(run.created_at))}</span></span></button>`).join("") : '<div class="muted small sidebar-empty">Your runs will appear here.</div>';
    const configured = (state.settings.configured_keys || []).filter(Boolean).length;
    $("#key-summary").textContent = `${configured} of 10 API keys configured`;
    $("#model-label").textContent = state.settings.model || "DEEPSEEK";
    renderNotifications();
  }

  function renderNotifications() {
    const notification = state.notifications;
    const enabled = notification?.enabled !== false;
    $("#sound-toggle").setAttribute("aria-checked", String(enabled));
    $("#sound-toggle").disabled = !notification || state.notificationBusy;
    $("#sound-state").textContent = enabled ? "ON" : "OFF";
    $("#sound-test").disabled = !notification?.available || Boolean($("#sound-test").dataset.busy);
    $("#sound-status").textContent = !notification ? "Sound service unavailable" : notification.last_error ? `Sound unavailable: ${notification.last_error}` : !notification.available ? "Sound playback is unavailable on this system." : enabled ? "Plays on this PC, even with the browser closed." : "Sound alerts are turned off.";
    $("#sound-status").classList.toggle("sound-error", Boolean(notification?.last_error));
  }

  async function bootstrap(initial = false) {
    const result = await api("/api/bootstrap");
    state.csrf = result.csrf_token;
    state.settings = result.settings || {};
    state.notifications = result.notifications || null;
    state.lastBootstrap = Date.now();
    state.runs = result.runs || [];
    state.defaultWorkspace = result.default_workspace || "";
    renderSidebar();
    setConnection(true);
    if (initial) {
      const requested = new URL(location.href).searchParams.get("run");
      if (requested && state.runs.some(run => run.id === requested)) await selectRun(requested);
      else if (state.runs.length) await selectRun(state.runs[0].id);
    }
  }

  function updateRunList(run) {
    const index = state.runs.findIndex(existing => existing.id === run.id);
    if (index < 0) state.runs.unshift(run);
    else state.runs[index] = run;
    renderSidebar();
  }

  async function selectRun(id) {
    if (state.selected === id && state.detail) return;
    state.selected = id;
    state.detail = null;
    state.signatures = {};
    $("#chat-input").value = "";
    state.generation += 1;
    state.loading = false;
    const url = new URL(location.href);
    url.searchParams.set("run", id);
    history.replaceState(null, "", url);
    renderSidebar();
    $("#welcome").hidden = true;
    $("#run-view").hidden = false;
    $("#run-title").textContent = state.runs.find(run => run.id === id)?.task || "Loading run…";
    $("#breadcrumb-name").textContent = "Loading run";
    $("#message-list").innerHTML = empty("Loading conversation", "Retrieving the latest peer activity.", "·");
    $("#agent-grid").innerHTML = "";
    $("#approval-section").hidden = true;
    $("#billing-panel").hidden = true;
    $("#run-attention").hidden = true;
    for (const selector of ["#pause-run", "#stop-run", "#edit-permissions", "#edit-limits", "#send-message"]) $(selector).disabled = true;
    await refreshRun();
  }

  async function refreshRun() {
    if (!state.selected || state.loading) return;
    const generation = state.generation;
    const id = state.selected;
    state.loading = true;
    try {
      const result = await api(`/api/runs/${encodeURIComponent(id)}`);
      if (generation !== state.generation) return;
      state.detail = result;
      updateRunList(result.run);
      renderRun(result);
      setConnection(true);
    } catch (error) {
      if (generation === state.generation) setConnection(false, error.message);
    } finally {
      if (generation === state.generation) state.loading = false;
    }
  }

  function changed(key, value) {
    const signature = JSON.stringify(value);
    if (state.signatures[key] === signature) return false;
    state.signatures[key] = signature;
    return true;
  }

  function renderRun(detail) {
    const run = detail.run;
    const agents = detail.agents || [];
    const messages = detail.messages || [];
    const items = detail.items || [];
    const events = detail.events || [];
    const usage = detail.usage || {};
    const finished = terminal.has(run.status);
    const paused = run.status === "paused" || run.status === "pausing";
    $("#breadcrumb-name").textContent = "Swarm run";
    $("#run-title").textContent = run.task;
    $("#run-status").className = `status-badge ${String(run.status).replace(/[^a-zA-Z_]/g, "")}`;
    $("#run-status").textContent = human(run.status);
    $("#run-workspace").textContent = run.workspace || "Default workspace";
    const attention = run.attention || run.pause_reason || "";
    $("#run-attention").hidden = !attention;
    $("#run-attention-text").textContent = attention;
    renderBilling(detail.billing);
    $("#metric-peers").innerHTML = `${agents.length}<span> / 10</span>`;
    const done = items.filter(item => ["completed", "complete", "done"].includes(item.status)).length;
    $("#metric-items").innerHTML = `${done}<span> / ${items.length} completed</span>`;
    $("#metric-tokens").textContent = compact(usage.total_tokens);
    $("#metric-tokens").title = `${number(usage.total_tokens)} tokens`;
    $("#metric-requests").textContent = number(usage.requests);
    $("#access-summary").innerHTML = renderPermissions(run.permissions || {});
    if (!$("#pause-run").dataset.busy) $("#pause-run").textContent = paused ? "Resume" : "Pause";
    $("#pause-run").disabled = finished || Boolean($("#pause-run").dataset.busy) || run.status === "pausing";
    $("#stop-run").disabled = finished || Boolean($("#stop-run").dataset.busy) || run.status === "pausing";
    $("#edit-permissions").disabled = finished;
    $("#edit-limits").disabled = finished;
    $("#send-message").disabled = finished || Boolean($("#send-message").dataset.busy);
    $("#chat-input").disabled = finished;
    $("#chat-input").placeholder = finished ? "This run has ended. Start a new task to continue working." : "Add context, ask a question, or steer the task…";
    $("#message-recipient").disabled = finished;
    $("#live-indicator").className = `live-indicator${finished || paused ? " inactive" : ""}`;
    $("#live-indicator").innerHTML = `<span class="status-dot"></span> ${finished ? "ENDED" : paused ? "PAUSED" : "LIVE"}`;
    $("#chat-count").textContent = messages.length;
    $("#board-count").textContent = items.length;
    $("#activity-count").textContent = events.length;
    $("#run-budget").textContent = `Limits: ${number(run.max_rounds)} rounds per peer · ${number(run.max_tokens)} tokens · ${number(run.max_minutes)} minutes`;
    $("#last-updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    if (changed("agents", agents)) renderAgents(agents);
    if (changed("messages", messages)) renderMessages(messages);
    if (changed("items", items)) renderBoard(items);
    if (changed("events", events)) renderEvents(events);
    const pending = (detail.approvals || []).filter(approval => !approval.status || ["pending", "requested", "waiting"].includes(approval.status));
    if (changed("approvals", pending)) renderApprovals(pending);
  }

  function renderBilling(billing) {
    $("#billing-panel").hidden = !billing;
    if (!billing) return;
    const amounts = {
      spent: billing.spent_usd,
      held: Number(billing.reserved_usd || 0) + Number(billing.uncertain_usd || 0),
      remaining: billing.remaining_usd,
      budget: billing.budget_usd
    };
    for (const [key, value] of Object.entries(amounts)) {
      const target = $(`#billing-${key}`);
      target.textContent = money(value);
      target.title = `${value ?? 0} USD`;
    }
    const cap = Math.max(Number(billing.budget_usd || 0), 0.01);
    const committed = Math.max(0, Number(billing.spent_usd || 0) + amounts.held);
    $("#billing-progress").max = cap;
    $("#billing-progress").value = Math.min(committed, cap);
    $("#billing-progress").setAttribute("aria-valuetext", `${money(committed)} accounted or held out of ${money(cap)}`);
    $("#billing-panel").classList.toggle("budget-low", Number(billing.remaining_usd) <= 0);
    $("#billing-held-detail").textContent = `Active requests: ${money(billing.reserved_usd)} held. Unconfirmed usage: ${money(billing.uncertain_usd)} held.`;
    const pricing = billing.pricing;
    $("#billing-pricing").textContent = pricing ? `Per million tokens: cache-hit input ${money(pricing.cached_input_per_million_usd)} · cache-miss input ${money(pricing.input_per_million_usd)} · output ${money(pricing.output_per_million_usd)}.${pricing.verified_at ? ` Pricing verified ${pricing.verified_at}.` : ""}` : "Pricing information is unavailable for this run.";
    $("#billing-legacy").hidden = !billing.legacy_unpriced;
    const source = $("#billing-source");
    source.hidden = true;
    source.removeAttribute("href");
    if (pricing?.source) {
      try {
        const url = new URL(pricing.source);
        if (url.protocol === "https:") {
          source.href = url.href;
          source.hidden = false;
        }
      } catch { /* The pricing source may be a label rather than a URL. */ }
    }
  }

  function renderPermissions(permissions) {
    const entries = [["Files", permissions.read_files], ["Write", permissions.write_files], ["Web", permissions.internet], ["Commands", permissions.commands], ["Deploy", permissions.deploy]];
    return entries.map(([label, value]) => `<span class="access-chip${value === "ask" ? " ask" : value === false || value === "deny" || value == null ? " denied" : ""}" title="${escape(label)}: ${value === true ? "allowed" : value === false || value == null ? "denied" : escape(value)}">${escape(label)}${value === "ask" ? " ?" : value === false || value === "deny" || value == null ? " −" : " ✓"}</span>`).join("");
  }

  function renderAgents(agents) {
    const all = peerIds.map(id => agents.find(agent => agent.id === id) || { id, status: "idle", turns: 0, tokens: 0 });
    $("#agent-grid").innerHTML = all.map((agent, index) => `<article class="agent-card ${String(agent.status || "idle").replace(/[^a-zA-Z_]/g, "")}"><div class="agent-top"><span class="agent-avatar" aria-hidden="true">${String(index + 1).padStart(2, "0")}</span><span class="agent-name">${escape(agent.name || peerName(agent.id))}</span><span class="status-dot" aria-hidden="true"></span></div><div class="agent-status">${escape(human(agent.status || "idle"))}</div><div class="agent-message" title="${escape(agent.last_message || "")}">${escape(agent.last_message || "Ready to collaborate")}</div><div class="agent-stats"><span>${number(agent.turns)} turns</span><span>${compact(agent.tokens)} tokens</span></div></article>`).join("");
  }

  function renderMessages(messages) {
    const target = $("#message-list");
    const stickToBottom = !target.children.length || target.querySelector(".empty-state") || target.scrollHeight - target.scrollTop - target.clientHeight < 90;
    const previousPosition = target.scrollTop;
    target.innerHTML = messages.length ? messages.map(message => {
      const from = message.from || message.agent_id || "system";
      const isUser = ["user", "human"].includes(from);
      const isSystem = from === "system";
      const avatar = isUser ? "YOU" : isSystem ? "S" : from.slice(-2);
      return `<article class="message"><span class="message-avatar${isUser ? " user" : isSystem ? " system" : ""}" aria-hidden="true">${escape(avatar)}</span><div class="message-content"><div class="message-header"><span class="message-author">${escape(isUser ? "You" : peerName(from))}</span><span class="message-route">→ ${escape(peerName(message.to || "all"))}</span><time class="message-time" datetime="${escape(message.created_at)}" title="${escape(message.created_at)}">${escape(time(message.created_at))}</time></div><div class="message-text">${escape(message.text)}</div></div></article>`;
    }).join("") : empty("The conversation starts here", "Peer messages and your guidance appear together.");
    target.scrollTop = stickToBottom ? target.scrollHeight : previousPosition;
  }

  function renderBoard(items) {
    const groups = [
      { title: "Available", items: items.filter(item => ["pending", "open", "todo", "available", "proposed", "queued"].includes(item.status) || !item.status) },
      { title: "In progress", items: items.filter(item => !["pending", "open", "todo", "available", "proposed", "queued", "completed", "complete", "done"].includes(item.status) && item.status) },
      { title: "Completed", items: items.filter(item => ["completed", "complete", "done"].includes(item.status)) }
    ];
    $("#work-board").innerHTML = groups.map(group => `<section class="board-column"><h2 class="board-column-title">${group.title}<span>${group.items.length}</span></h2>${group.items.length ? group.items.map(item => `<article class="work-card"><span class="work-card-id">${escape(item.id)}</span><h3>${escape(item.title)}</h3>${item.description ? `<p>${escape(item.description)}</p>` : ""}<div class="work-card-footer"><span>${item.owner ? escape(peerName(item.owner)) : "Unclaimed"}</span><span class="status-badge ${String(item.status).replace(/[^a-zA-Z_]/g, "")}">${escape(human(item.status))}</span></div>${item.reviewer ? `<div class="reviewer">Reviewed by ${escape(peerName(item.reviewer))}</div>` : ""}${item.result ? `<details><summary>View result</summary><pre>${escape(pretty(item.result))}</pre></details>` : ""}</article>`).join("") : '<div class="board-empty">No work items here yet.</div>'}</section>`).join("");
  }

  function renderEvents(events) {
    const target = $("#activity-list");
    const openIds = new Set($$("details[open]", target).map(item => item.dataset.eventId));
    target.innerHTML = events.length ? [...events].reverse().map(event => `<details class="activity-event" data-event-id="${escape(event.id)}"${openIds.has(String(event.id)) ? " open" : ""}><summary><span class="event-kind" aria-hidden="true"></span><span class="event-name">${escape(human(event.kind || "event"))}</span>${event.agent_id ? `<span class="event-agent">${escape(peerName(event.agent_id))}</span>` : ""}<time class="event-time" datetime="${escape(event.created_at)}">${escape(time(event.created_at))}</time><span class="event-chevron" aria-hidden="true">›</span></summary><pre class="event-payload">${escape(pretty(event.data))}</pre></details>`).join("") : empty("A clear record of the work", "Actions and tool results will appear as the swarm gets started.", "≡");
  }

  function renderApprovals(approvals) {
    $("#approval-section").hidden = approvals.length === 0;
    $("#approval-count").textContent = approvals.length;
    $("#approval-list").innerHTML = approvals.map(approval => `<article class="approval-card"><div class="approval-card-top"><strong>${escape(peerName(approval.agent_id))} requests access</strong><span class="approval-category">${escape(human(approval.category))}</span></div><pre class="approval-payload">${escape(pretty(approval.payload))}</pre><div class="approval-actions"><button class="button button-small button-outline" data-approval="${escape(approval.id)}" data-approved="false">Deny</button><button class="button button-small button-primary" data-approval="${escape(approval.id)}" data-approved="true">Approve this action</button></div></article>`).join("");
  }

  function openTask() {
    formError("#task-error", null);
    $("#task-workspace").placeholder = "Leave blank for a new workspace";
    $("#task-workspace").title = state.defaultWorkspace ? `Default workspaces: ${state.defaultWorkspace}` : "";
    $("#task-dialog").showModal();
    $("#task-input").focus();
  }

  function readPermissions(container) {
    return {
      read_files: $('[name="read_files"]', container).checked,
      write_files: $('[name="write_files"]', container).checked,
      internet: $('[name="internet"]', container).checked,
      commands: $('[name="commands"]', container).value,
      deploy: $('[name="deploy"]', container).value
    };
  }

  function fillPermissions(container, permissions) {
    for (const key of ["read_files", "write_files", "internet"]) $(`[name="${key}"]`, container).checked = Boolean(permissions[key]);
    for (const key of ["commands", "deploy"]) $(`[name="${key}"]`, container).value = permissions[key] || "ask";
  }

  function openSettings() {
    const settings = state.settings;
    $("#settings-model").value = settings.model || "deepseek-flash";
    $("#settings-thinking").value = String(settings.thinking !== false);
    $("#settings-effort").value = settings.reasoning_effort || "high";
    $("#settings-base-url").value = settings.base_url || "";
    $("#settings-output-tokens").value = settings.max_output_tokens || 8192;
    $("#settings-prompt").value = settings.system_prompt || "";
    $("#api-key-fields").innerHTML = peerIds.map((id, index) => {
      const configured = Boolean(settings.configured_keys?.[index]);
      return `<div><label for="key-${id}">${escape(peerName(id))}<span class="key-state${configured ? " configured" : ""}">${configured ? "● CONFIGURED" : "NOT CONFIGURED"}</span></label><input id="key-${id}" data-key-index="${index}" type="password" autocomplete="new-password" spellcheck="false" autocapitalize="off" placeholder="${configured ? "Saved · leave blank to keep" : "Paste DeepSeek API key"}" aria-label="API key for ${escape(peerName(id))}"></div>`;
    }).join("");
    formError("#settings-error", null);
    $("#settings-dialog").showModal();
  }

  function selectTab(name) {
    state.tab = name;
    for (const button of $$("[data-tab]")) {
      const selected = button.dataset.tab === name;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      $(`#panel-${button.dataset.tab}`).hidden = !selected;
    }
  }

  document.addEventListener("click", event => {
    const newRun = event.target.closest('[data-action="new-run"]');
    if (newRun) openTask();
    const close = event.target.closest("[data-close]");
    if (close) document.getElementById(close.dataset.close)?.close();
    const run = event.target.closest("[data-run-id]");
    if (run) void selectRun(run.dataset.runId);
    const tab = event.target.closest("[data-tab]");
    if (tab) selectTab(tab.dataset.tab);
  });

  $$("dialog").forEach(dialog => {
    dialog.addEventListener("click", event => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
  });

  $("#settings-dialog").addEventListener("close", () => {
    for (const input of $$("[data-key-index]")) input.value = "";
  });
  $("#open-settings").addEventListener("click", openSettings);
  $("#sound-toggle").addEventListener("click", async () => {
    if (!state.notifications || state.notificationBusy) return;
    const enabled = !state.notifications.enabled;
    state.notificationBusy = true;
    renderNotifications();
    try {
      state.notifications = await api("/api/notifications", { method: "POST", body: { enabled } });
      toast(`Sound alerts ${enabled ? "enabled" : "disabled"}.`);
    } catch (error) { toast(error.message, true); }
    finally { state.notificationBusy = false; renderNotifications(); }
  });
  $("#sound-test").addEventListener("click", async event => {
    try {
      await withButton(event.currentTarget, "…", async () => {
        state.notifications = await api("/api/notifications/test", { method: "POST", body: {} });
        const error = state.notifications.last_error;
        toast(error || (state.notifications.available ? "Test sound sent to Windows." : "Sound playback is unavailable on this system."), Boolean(error) || !state.notifications.available);
      });
    } catch (error) { toast(error.message, true); }
    finally { renderNotifications(); }
  });
  $("#message-recipient").insertAdjacentHTML("beforeend", peerIds.map(id => `<option value="${id}">${peerName(id)}</option>`).join(""));

  document.addEventListener("keydown", event => {
    if (event.key.toLowerCase() === "n" && !event.ctrlKey && !event.metaKey && !event.altKey && !document.querySelector("dialog[open]") && !event.target.closest("input, textarea, select, [contenteditable]")) {
      event.preventDefault();
      openTask();
    }
    const tab = event.target.closest("[data-tab]");
    if (tab && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const tabs = $$("[data-tab]");
      const index = tabs.indexOf(tab);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      selectTab(tabs[next].dataset.tab);
      tabs[next].focus();
    }
  });

  $("#task-form").addEventListener("submit", async event => {
    event.preventDefault();
    formError("#task-error", null);
    const body = {
      task: $("#task-input").value.trim(),
      workspace: $("#task-workspace").value.trim(),
      budget_usd: Number($("#task-budget-usd").value),
      permissions: readPermissions($("#task-permissions")),
      max_rounds: Number($("#task-rounds").value),
      max_tokens: Number($("#task-tokens").value),
      max_minutes: Number($("#task-minutes").value),
      ...($("#task-stall-minutes").value ? { stall_minutes: Number($("#task-stall-minutes").value) } : {})
    };
    if (!body.task) { formError("#task-error", "Describe the shared objective first."); return; }
    if ((state.settings.configured_keys || []).filter(Boolean).length !== 10) {
      formError("#task-error", "Configure all 10 DeepSeek API keys in Connection & instructions before starting a task.");
      return;
    }
    try {
      await withButton($("#create-run"), "Starting…", async () => {
        const result = await api("/api/runs", { method: "POST", body });
        const run = result.run || result;
        updateRunList(run);
        $("#task-dialog").close();
        $("#task-input").value = "";
        await selectRun(run.id);
        toast("The swarm is starting.");
      });
    } catch (error) { formError("#task-error", error); }
  });

  $("#settings-form").addEventListener("submit", async event => {
    event.preventDefault();
    formError("#settings-error", null);
    const body = {
      model: $("#settings-model").value.trim(),
      thinking: $("#settings-thinking").value === "true",
      reasoning_effort: $("#settings-effort").value,
      max_output_tokens: Number($("#settings-output-tokens").value),
      system_prompt: $("#settings-prompt").value,
      keys: $$("[data-key-index]").map(input => input.value.trim())
    };
    try {
      await withButton($("#save-settings"), "Saving…", async () => {
        await api("/api/settings", { method: "POST", body });
        body.keys.fill("");
        $("#settings-dialog").close();
        await bootstrap();
        toast("Configuration saved.");
      });
    } catch (error) { formError("#settings-error", error); }
    finally { body.keys.fill(""); }
  });

  $("#chat-input").addEventListener("keydown", event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      $("#chat-form").requestSubmit();
    }
  });
  $("#chat-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!state.selected || !state.detail || terminal.has(state.detail.run.status)) return;
    const text = $("#chat-input").value.trim();
    if (!text) return;
    const selected = state.selected;
    try {
      await withButton($("#send-message"), "Sending…", async () => {
        await api(`/api/runs/${encodeURIComponent(selected)}/messages`, { method: "POST", body: { text, to: $("#message-recipient").value } });
        if (selected !== state.selected) return;
        if ($("#chat-input").value.trim() === text) $("#chat-input").value = "";
        $("#message-list").scrollTop = $("#message-list").scrollHeight;
        await refreshRun();
        $("#chat-input").focus();
      });
    } catch (error) { toast(error.message, true); }
  });

  async function controlRun(action, button) {
    if (!state.selected) return;
    try {
      await withButton(button, "Updating…", async () => {
        await api(`/api/runs/${encodeURIComponent(state.selected)}/control`, { method: "POST", body: { action } });
        await refreshRun();
      });
      if (state.detail) renderRun(state.detail);
    } catch (error) { toast(error.message, true); }
  }
  $("#pause-run").addEventListener("click", event => {
    if (!state.detail) return;
    void controlRun(["paused", "pausing"].includes(state.detail.run.status) ? "resume" : "pause", event.currentTarget);
  });
  $("#stop-run").addEventListener("click", event => { void controlRun("stop", event.currentTarget); });
  $("#edit-permissions").addEventListener("click", () => {
    if (!state.detail) return;
    fillPermissions($("#run-permissions"), state.detail.run.permissions || {});
    formError("#permissions-error", null);
    $("#permissions-dialog").showModal();
  });
  $("#edit-limits").addEventListener("click", () => {
    if (!state.detail) return;
    const { run, usage } = state.detail;
    $("#run-rounds").value = run.max_rounds;
    $("#run-tokens").value = run.max_tokens;
    $("#run-minutes").value = run.max_minutes;
    $("#run-output-tokens").value = run.max_output_tokens || 8192;
    $("#run-stall-minutes").value = run.stall_minutes || 10;
    $("#run-budget-usd").value = Number(run.budget_usd ?? state.detail.billing?.budget_usd ?? 1).toFixed(2);
    $("#limits-usage").textContent = `Used so far: ${number(usage?.total_tokens)} tokens · ${Math.floor(Number(run.active_seconds || 0) / 60)} active minutes.${state.detail.billing ? ` Accounted cost: ${money(state.detail.billing.spent_usd)}.` : ""}`;
    formError("#limits-error", null);
    $("#limits-dialog").showModal();
  });
  $("#limits-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!state.selected) return;
    formError("#limits-error", null);
    try {
      await withButton($("#save-limits"), "Saving…", async () => {
        await api(`/api/runs/${encodeURIComponent(state.selected)}/limits`, { method: "POST", body: {
          max_rounds: Number($("#run-rounds").value),
          max_tokens: Number($("#run-tokens").value),
          max_minutes: Number($("#run-minutes").value),
          max_output_tokens: Number($("#run-output-tokens").value),
          budget_usd: Number($("#run-budget-usd").value),
          ...($("#run-stall-minutes").value ? { stall_minutes: Number($("#run-stall-minutes").value) } : {})
        } });
        $("#limits-dialog").close();
        await refreshRun();
        toast("Run limits updated. Existing usage is preserved.");
      });
    } catch (error) { formError("#limits-error", error); }
  });
  $("#permissions-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!state.selected) return;
    formError("#permissions-error", null);
    try {
      await withButton($("#save-permissions"), "Saving…", async () => {
        await api(`/api/runs/${encodeURIComponent(state.selected)}/permissions`, { method: "POST", body: readPermissions($("#run-permissions")) });
        $("#permissions-dialog").close();
        await refreshRun();
        toast("Run permissions updated.");
      });
    } catch (error) { formError("#permissions-error", error); }
  });

  $("#approval-list").addEventListener("click", async event => {
    const button = event.target.closest("[data-approval]");
    if (!button || !state.selected) return;
    const approved = button.dataset.approved === "true";
    const sibling = button.parentElement.querySelector(`[data-approved="${!approved}"]`);
    if (sibling) sibling.disabled = true;
    try {
      await withButton(button, "Saving…", async () => {
        await api(`/api/runs/${encodeURIComponent(state.selected)}/approvals/${encodeURIComponent(button.dataset.approval)}`, { method: "POST", body: { approved } });
        await refreshRun();
        toast(approved ? "Action approved." : "Action denied.");
      });
    } catch (error) { toast(error.message, true); }
    finally { if (sibling?.isConnected) sibling.disabled = false; }
  });

  async function start() {
    try { await bootstrap(true); }
    catch (error) { setConnection(false, error.message); }
    setInterval(async () => {
      if (document.hidden) return;
      if (!state.csrf) {
        try { await bootstrap(true); } catch (error) { setConnection(false, error.message); }
      } else {
        if (state.selected) await refreshRun();
        if (Date.now() - state.lastBootstrap > 15000) {
          try { await bootstrap(); } catch (error) { setConnection(false, error.message); }
        }
      }
    }, 2000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && state.selected) void refreshRun();
    });
  }
  void start();
})();
