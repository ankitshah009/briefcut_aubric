"use strict";

/*
 * The Meta calls panel: every Meta model call the open project makes, live,
 * from GET /api/campaigns/{id}/meta-calls. Paid calls go to the Meta Model API.
 * In private mode (src/vedit/open_weights.py) text-only calls run on this computer
 * with Meta's open-weight Muse Glimmer; those rows say so and cost $0.00.
 *
 * The server reads the project's call log (src/vedit/meta.py call_log). It holds
 * purposes, models, sizes, costs and response ids, never keys, prompts,
 * transcripts or footage. Text from the server always goes in as textContent.
 *
 * It polls quickly while the project works or a call runs, slowly while the tab
 * is visible so calls made from Find, a background, or an agent still appear,
 * and not at all while the tab is hidden.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const ACTIVE = new Set(["queued", "analyzing", "rendering"]);
  const FAST = 1500, SLOW = 4000, TIMEOUT = 8000;
  const PREF = "briefcut.metaCalls";
  const STATUS = { running: "Running", done: "Done", failed: "Failed", stopped: "Stopped" };
  const STATUS_HINT = {
    running: "Meta is working on this now.",
    done: "Meta answered and Briefcut checked the answer.",
    failed: "The call did not finish. Its cost stays counted in case Meta billed it.",
    stopped: "Briefcut stopped before Meta answered. Its cost stays counted in case Meta billed it.",
  };
  const LOCAL_HINT = {
    running: "Muse Glimmer is working on this, on this computer.",
    done: "Muse Glimmer answered on this computer and Briefcut checked the answer.",
    failed: "The call did not finish. It ran on this computer, so nothing was billed.",
    stopped: "Briefcut stopped before Muse Glimmer answered. It ran on this computer, so nothing was billed.",
  };
  /* A call Meta refused before running it (call_log's ``released``) was never billed. */
  const RELEASED = "Meta refused it before running it, so nothing was billed.";
  /* The person's own locale groups the digits. */
  const number = new Intl.NumberFormat();

  const view = { panel: null, toggle: null, count: null, list: null, empty: null, total: null, sub: null, where: null, close: null };
  const state = { jobId: "", jobStatus: "", report: null, key: "", timer: null, ticker: null, controller: null, failures: 0, seen: new Set(), loaded: false, primed: false };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function pref() { try { return localStorage.getItem(PREF) || ""; } catch (_) { return ""; } }
  function setPref(value) { try { localStorage.setItem(PREF, value); } catch (_) { /* A private window keeps no preference. */ } }
  function announce(message) { const node = $("live"); if (node) node.textContent = message; }
  const finite = (value) => typeof value === "number" && Number.isFinite(value);

  function usd(value, places) {
    const digits = places === undefined ? 4 : places;
    if (!finite(value) || value <= 0) return "$" + (0).toFixed(digits);
    if (value < 0.0001) return "under $0.0001";
    return "$" + value.toFixed(digits);
  }
  function seconds(value) {
    if (!finite(value)) return "";
    if (value < 60) return value.toFixed(value < 10 ? 1 : 0) + " s";
    return Math.floor(value / 60) + " min " + String(Math.round(value % 60)).padStart(2, "0") + " s";
  }
  function clockTime(epoch) {
    if (!finite(epoch)) return "";
    return new Date(epoch * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  }
  function sizeParts(size, running) {
    const parts = [];
    if (!size) return parts;
    /* SAM reports zero tokens; only a count that measured something is shown. */
    if (finite(size.input_tokens) && size.input_tokens > 0) parts.push(number.format(size.input_tokens) + " tokens in");
    if (finite(size.output_tokens) && size.output_tokens > 0) parts.push(number.format(size.output_tokens) + " out");
    if (finite(size.frames)) parts.push((running ? "up to " : "") + number.format(size.frames) + " frames");
    if (finite(size.audio_seconds)) parts.push(seconds(size.audio_seconds) + " of audio");
    if (finite(size.images)) parts.push(size.images === 1 ? "1 image" : number.format(size.images) + " images");
    return parts;
  }

  /* A local row names the open-weight model and the runtime, e.g. "Muse Glimmer 30B · on this computer (llama.cpp b11669)". */
  function localName(call) {
    const model = /muse-glimmer-30b/i.test(call.model || "") ? "Muse Glimmer 30B" : (call.model || "Muse Glimmer");
    const runtime = (call.runtime || "llama.cpp").replace(/-[0-9a-f]{7,}$/i, "");
    return model + " \u00b7 on this computer (" + runtime + ")";
  }

  /* ---------- Rendering ---------- */
  function row(call) {
    const status = STATUS[call.status] ? call.status : "failed";
    const local = call.runs_on === "local";
    const item = el("li", "mc-row");
    item.dataset.status = status;
    item.dataset.runs = local ? "local" : "meta-api";
    item.dataset.id = call.id;
    const time = el("time", "mc-time", clockTime(call.started_at));
    if (finite(call.started_at)) {
      const when = new Date(call.started_at * 1000);
      time.dateTime = when.toISOString();
      /* A call from another day says which day. */
      if (when.toDateString() !== new Date().toDateString()) time.append(el("span", "mc-day", when.toLocaleDateString([], { month: "short", day: "numeric" })));
    }
    const purpose = el("strong", "mc-purpose", call.purpose || "Ask Meta");
    const chip = el("span", "mc-status", STATUS[status]);
    const released = !local && call.released === true;
    const hint = released ? RELEASED : (local ? LOCAL_HINT : STATUS_HINT)[status];
    const reason = typeof call.reason === "string" ? call.reason.trim() : "";
    chip.title = reason || hint;
    const model = el("span", "mc-model", call.model || "Meta model");
    if (call.model) model.title = call.model + (local && call.weights ? " \u00b7 " + call.weights : "");
    const reserved = !local && !released && call.cost_kind !== "actual";
    const free = local || released;
    const cost = el("span", "mc-cost" + (reserved ? " is-reserved" : "") + (free ? " is-local" : ""), free ? usd(0, 2) : usd(call.cost_usd) + (reserved ? " reserved" : ""));
    cost.title = local ? "Ran on this computer. A local call costs nothing." : released ? RELEASED : reserved ? "Reserved against the budget. Meta reported no exact charge for this call." : "Cost Meta reported for this call.";
    const facts = el("p", "mc-facts");
    const duration = el("span", "mc-duration", status === "running" ? seconds(Math.max(0, Date.now() / 1000 - (call.started_at || Date.now() / 1000))) : seconds(call.seconds));
    if (status === "running") duration.dataset.since = String(call.started_at || "");
    const parts = [duration].concat(sizeParts(call.size, status === "running").map((text) => el("span", "", text)));
    parts.filter((node) => node.textContent).forEach((node) => facts.append(node));
    item.append(time, purpose, chip, model, cost, facts);
    if (status === "failed" || status === "stopped") {
      /* Why it failed, and whether it cost anything, in words the person can read without hovering. */
      const why = el("p", "mc-reason", [reason, released ? RELEASED : (local ? "" : STATUS_HINT[status])].filter(Boolean).join(" "));
      if (why.textContent) item.append(why);
    }
    if (local) {
      const where = el("p", "mc-local");
      where.append(el("span", "mc-local-tag", "Private"), el("span", "mc-local-name", localName(call)));
      where.title = "Meta's open-weight Muse Glimmer ran on this computer. Nothing was sent to the Meta Model API." + (call.weights ? " Weights: " + call.weights + "." : "");
      item.append(where);
    }
    if (call.request_id) {
      const request = el("p", "mc-request");
      request.append(el("span", "mc-label", "Response id"), el("code", "", call.request_id));
      request.title = "Meta's id for this response: " + call.request_id;
      item.append(request);
    }
    return item;
  }

  function render() {
    const report = state.report;
    const calls = report && Array.isArray(report.calls) ? report.calls : [];
    const totals = report && report.totals || {};
    view.count.textContent = String(totals.calls || calls.length || 0);
    const live = calls.some((call) => call.status === "running");
    view.toggle.classList.toggle("is-live", live);
    const runningCount = calls.filter((call) => call.status === "running").length;
    view.toggle.setAttribute("aria-label", "Meta calls: " + (totals.calls || 0) + (live ? ", " + runningCount + " running" : ""));
    view.empty.hidden = calls.length > 0 || !state.loaded;
    view.list.replaceChildren(...calls.map(row));
    const count = totals.calls || 0;
    view.sub.textContent = (count ? number.format(count) + (count === 1 ? " call" : " calls") : "Nothing sent yet") + (polling() ? ". Updating live." : ".");
    view.sub.classList.toggle("is-live", polling());
    const runningCost = calls.filter((call) => call.status === "running").reduce((sum, call) => sum + (call.cost_usd || 0), 0);
    const spent = Math.max(0, (totals.spent_usd || 0) - runningCost);
    let line = "Spent " + usd(spent) + (finite(totals.budget_usd) ? " of the " + usd(totals.budget_usd, 2) + " project budget." : ".");
    if (runningCost > 0) line += " " + usd(runningCost) + " reserved for the running call" + (calls.filter((call) => call.status === "running").length > 1 ? "s." : ".");
    if (finite(totals.remaining_usd)) line += " " + usd(totals.remaining_usd) + " left for new calls.";
    view.total.textContent = line;
    if (view.where) view.where.textContent = whereLine(totals, calls);
    tick();
  }

  /* Says where the calls ran, from the server's counts: the Meta Model API, this computer, or both. */
  function whereLine(totals, calls) {
    const localCount = finite(totals.local_calls) ? totals.local_calls : calls.filter((call) => call.runs_on === "local").length;
    const all = finite(totals.calls) ? totals.calls : calls.length;
    const apiCount = finite(totals.api_calls) ? totals.api_calls : Math.max(0, all - localCount);
    if (!localCount) return "All calls go to the Meta Model API from this computer.";
    if (!apiCount) return (localCount === 1 ? "This call ran" : "All " + number.format(localCount) + " calls ran") + " on this computer with Muse Glimmer. Nothing went to the Meta Model API.";
    return number.format(apiCount) + (apiCount === 1 ? " call" : " calls") + " on the Meta Model API, " + number.format(localCount) + " on this computer with Muse Glimmer.";
  }

  function tick() {
    clearInterval(state.ticker);
    state.ticker = null;
    const running = view.list.querySelectorAll(".mc-duration[data-since]");
    if (!running.length) return;
    const update = () => running.forEach((node) => { const since = Number(node.dataset.since); if (since) node.textContent = seconds(Math.max(0, Date.now() / 1000 - since)); });
    update();
    state.ticker = setInterval(update, 1000);
  }

  /* ---------- Opening and closing ---------- */
  function isOpen() { return !view.panel.hidden; }
  function open(remember, refresh) {
    if (!state.jobId) return;
    view.panel.hidden = false;
    view.toggle.setAttribute("aria-expanded", "true");
    if (remember) setPref("open");
    if (refresh !== false) load();
  }
  function close(remember) {
    view.panel.hidden = true;
    view.toggle.setAttribute("aria-expanded", "false");
    if (remember) setPref("closed");
  }

  /* ---------- Loading ---------- */
  function polling() {
    if (!state.jobId || document.hidden) return false;
    return true;
  }
  function delay() {
    const running = !!(state.report && state.report.totals && state.report.totals.running);
    if (state.failures) return Math.min(15000, FAST * 2 ** state.failures);
    return ACTIVE.has(state.jobStatus) || running ? FAST : SLOW;
  }
  function schedule() {
    clearTimeout(state.timer);
    state.timer = null;
    if (!polling()) return;
    state.timer = setTimeout(load, delay());
  }

  async function load() {
    clearTimeout(state.timer);
    state.timer = null;
    const ident = state.jobId;
    if (!ident) return;
    if (state.controller) state.controller.abort();
    const controller = new AbortController();
    state.controller = controller;
    const timer = setTimeout(() => controller.abort(), TIMEOUT);
    try {
      const response = await fetch("/api/campaigns/" + encodeURIComponent(ident) + "/meta-calls", { headers: { Accept: "application/json" }, cache: "no-store", signal: controller.signal });
      if (ident !== state.jobId) return;
      if (response.status === 404) { forget(); return; }
      const data = await response.json();
      if (!response.ok || !data || data.ok === false || !Array.isArray(data.calls)) throw new Error("unreadable");
      state.failures = 0;
      if (data.status) state.jobStatus = data.status;
      const fresh = data.calls.filter((call) => !state.seen.has(call.id));
      data.calls.forEach((call) => state.seen.add(call.id));
      const key = JSON.stringify([data.calls, data.totals]);
      state.loaded = true;
      if (key !== state.key) { state.key = key; state.report = data; render(); }
      else render();
      const started = fresh.filter((call) => call.status === "running");
      if (started.length && state.primed) {
        announce((started[0].runs_on === "local" ? "Muse Glimmer started on this computer: " : "Meta call started: ") + (started[0].purpose || "Ask Meta") + ".");
        if (pref() !== "closed") open(false, false);
      }
      state.primed = true;
      if (pref() === "open" && !isOpen()) open(false, false);
    } catch (error) {
      if (ident !== state.jobId) return;
      if (error && error.name === "AbortError" && controller !== state.controller) return;
      state.failures = Math.min(state.failures + 1, 4);
      view.sub.textContent = "Could not refresh the call log. Trying again.";
      view.sub.classList.remove("is-live");
    } finally {
      clearTimeout(timer);
      if (state.controller === controller) state.controller = null;
      if (ident === state.jobId) schedule();
    }
  }

  function forget() {
    clearTimeout(state.timer);
    clearInterval(state.ticker);
    if (state.controller) state.controller.abort();
    Object.assign(state, { jobId: "", jobStatus: "", report: null, key: "", timer: null, ticker: null, controller: null, failures: 0, seen: new Set(), loaded: false, primed: false });
    view.list.replaceChildren();
    view.empty.hidden = false;
    view.toggle.hidden = true;
    close(false);
  }

  function follow(job) {
    const ident = job && job.id ? String(job.id) : "";
    if (!ident) { forget(); return; }
    if (ident !== state.jobId) {
      forget();
      state.jobId = ident;
      view.toggle.hidden = false;
      view.count.textContent = "0";
      view.sub.textContent = "Reading the call log.";
      state.jobStatus = job.status || "";
      load();
      return;
    }
    const changed = (job.status || "") !== state.jobStatus;
    state.jobStatus = job.status || "";
    if (changed) load();
    else if (!state.timer && !state.controller) schedule();
  }

  function current() {
    const studio = window.BriefcutStudio;
    return studio && studio.currentJobId ? studio.currentJobId() : state.jobId;
  }

  function start() {
    Object.assign(view, { panel: $("meta-calls"), toggle: $("meta-calls-toggle"), count: $("meta-calls-count"), list: $("meta-calls-list"), empty: $("meta-calls-empty"), total: $("meta-calls-total"), sub: $("meta-calls-sub"), where: $("meta-calls-where"), close: $("meta-calls-close") });
    if (!view.panel || !view.toggle) return;
    view.toggle.addEventListener("click", () => (isOpen() ? close(true) : open(true)));
    view.close.addEventListener("click", () => { close(true); view.toggle.focus(); });
    view.panel.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.stopPropagation(); close(true); view.toggle.focus(); } });
    document.addEventListener("visibilitychange", () => { if (document.hidden) { clearTimeout(state.timer); state.timer = null; } else if (state.jobId) load(); });
    const editor = window.BriefcutEditor;
    if (editor && editor.on) {
      editor.on("job", follow);
      /* A new project resets the editor without a job event; the timeline event comes with it. */
      editor.on("timeline", () => { if (state.jobId && !current()) forget(); });
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
