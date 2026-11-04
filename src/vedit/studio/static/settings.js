"use strict";

/* First-run setup, Settings, the project library, and autosave status.
   Talks to the routes in studio/product.py and opens projects through
   window.BriefcutStudio. Text from the server always goes in as textContent. */
(function () {
  const $ = (id) => document.getElementById(id);
  const studio = () => window.BriefcutStudio || {};

  function h(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || (value === false && key !== "spellcheck")) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key in node) node[key] = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    node.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
    return node;
  }

  async function api(url, options) {
    const config = options || {};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), config.timeout || 20000);
    try {
      const headers = config.body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" };
      const response = await fetch(url, { method: config.method || "GET", headers, body: config.body === undefined ? undefined : JSON.stringify(config.body), signal: controller.signal, cache: "no-store" });
      const data = await response.json().catch(() => null);
      if (!data) throw new Error("Studio sent an unreadable answer. Restart it and try again.");
      if (!response.ok || data.ok === false) throw new Error(data.error || "Studio refused this request.");
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Studio took too long to answer. Try again.");
      if (error instanceof TypeError) throw new Error("Studio is not reachable. Keep it running, then try again.");
      throw error;
    } finally { clearTimeout(timer); }
  }

  function megabytes(bytes) {
    const value = Math.max(0, Number(bytes) || 0);
    if (value >= 1e9) return (value / 1e9).toFixed(1) + " GB";
    if (value >= 1e6) return Math.round(value / 1e6) + " MB";
    if (value >= 1e3) return Math.round(value / 1e3) + " KB";
    return value + " B";
  }
  function clock(seconds) {
    const total = Math.round(Number(seconds) || 0);
    return Math.floor(total / 60) + ":" + String(total % 60).padStart(2, "0");
  }
  function shortDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const options = { month: "short", day: "numeric" };
    if (date.getFullYear() !== new Date().getFullYear()) options.year = "numeric";
    return date.toLocaleDateString(undefined, options);
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* Blocked in some webviews. */ }
    // Inside the open dialog: everything outside a modal is inert and cannot take a selection.
    const area = h("textarea", { class: "sr", value: text, readOnly: true });
    (document.querySelector("dialog[open]") || document.body).append(area);
    area.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } catch (_) { copied = false; }
    area.remove();
    return copied;
  }

  function closeButton(dialog) {
    return h("button", { class: "bc-close", type: "button", "aria-label": "Close", text: "×", onclick: () => dialog.close() });
  }

  function lightDismiss(dialog) {
    dialog.setAttribute("closedby", "any");
    if ("closedBy" in HTMLDialogElement.prototype) return;
    // WebKit, and so the Mac app. Test the pointer, not the target: padding also targets the dialog.
    const outside = (event) => { const box = dialog.getBoundingClientRect(); return event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom; };
    let pressedOutside = false;
    dialog.addEventListener("pointerdown", (event) => { pressedOutside = outside(event); });
    dialog.addEventListener("pointerup", (event) => { if (pressedOutside && outside(event)) dialog.close(); });
  }

  function panel(id, title, lead) {
    const dialog = h("dialog", { id, class: "bc-dialog", "aria-labelledby": id + "-title" });
    const leadNode = h("p", { class: "bc-lead", text: lead || "" });
    const body = h("div", { class: "bc-body" });
    dialog.append(h("header", { class: "bc-head" }, h("div", {}, h("h2", { id: id + "-title", text: title }), leadNode), closeButton(dialog)), body);
    document.body.append(dialog);
    return { dialog, body, lead: leadNode };
  }

  function confirmAction({ title, body, action }) {
    return new Promise((resolve) => {
      const dialog = h("dialog", { class: "bc-dialog bc-confirm", "aria-labelledby": "bc-confirm-title" });
      dialog.append(h("div", { class: "bc-confirm-body" },
        h("h2", { id: "bc-confirm-title", text: title }),
        h("p", { text: body }),
        h("form", { method: "dialog", class: "bc-actions" },
          h("button", { class: "button button-secondary", value: "no", autofocus: true, text: "Keep it" }),
          h("button", { class: "button bc-danger", value: "yes", text: action }))));
      document.body.append(dialog);
      dialog.addEventListener("close", () => { const yes = dialog.returnValue === "yes"; dialog.remove(); resolve(yes); }, { once: true });
      dialog.showModal();
    });
  }

  /* ---------- First-run setup ---------- */

  const setup = { view: null, data: null, timer: null, ready: true };

  function installSteps(install) {
    const steps = install && install.steps || [];
    const list = h("ol", { class: "bc-steps" }, steps.map((step) => {
      const button = h("button", { type: "button", class: "button button-secondary bc-copy", text: "Copy" });
      button.addEventListener("click", async () => {
        const copied = await copyText(step.command);
        button.textContent = copied ? "Copied" : "Select the text to copy it";
        setTimeout(() => { button.textContent = "Copy"; }, 1800);
      });
      return h("li", {}, h("p", { text: step.label }), h("div", { class: "bc-command" }, h("code", { text: step.command }), button));
    }));
    return [steps.length ? list : null, install && install.note ? h("p", { class: "field-note", text: install.note }) : null];
  }

  function keyLink(url) {
    const href = url || "https://dev.meta.ai";
    return h("a", { href, target: "_blank", rel: "noopener noreferrer", text: href.replace(/^https:\/\//, "") });
  }

  function metaSteps(item) {
    const open = h("button", { type: "button", class: "button button-primary", id: "bc-setup-key", text: item.key_configured ? "Open Settings" : "Add Meta key", onclick: () => { setup.view.dialog.close(); openSettings(); } });
    if (item.key_configured) return h("div", { class: "bc-meta-steps" }, open);
    return h("div", { class: "bc-meta-steps" },
      h("ol", { class: "bc-steps" },
        h("li", {}, h("p", {}, "Create an API key at ", keyLink(item.key_url), ".")),
        h("li", {}, h("p", { text: "Paste it in Settings. Briefcut keeps it on this computer." }))),
      open);
  }

  function setupRow(item) {
    const state = item.ok ? ["Ready", "is-complete"] : item.required ? ["Required", "is-failed"] : ["Optional", ""];
    const row = h("section", { class: "bc-row" },
      h("div", { class: "bc-row-head" }, h("h3", { text: item.title }), h("span", { class: "status-pill " + state[1], text: state[0] })),
      h("p", { class: "bc-why", text: item.why }),
      item.summary ? h("p", { text: item.summary }) : null);
    if (item.id === "ffmpeg" && !item.ok) row.append(...installSteps(item.install).filter(Boolean));
    if (item.id === "meta" && !item.ok) row.append(metaSteps(item));
    return row;
  }

  function paintSetup(data) {
    const missing = data.items.filter((item) => item.required && !item.ok).length;
    setup.view.lead.textContent = data.ready ? "Briefcut has what it needs to make a video." : missing === 1 ? "One thing is needed before you can make a video." : "Two things are needed before you can make a video.";
    setup.view.body.replaceChildren(
      ...data.items.map(setupRow),
      h("details", { class: "bc-details" }, h("summary", { text: "All checks" }),
        h("ul", { class: "bc-checks" }, (data.checks || []).map((check) => h("li", { text: (check.ok ? "OK  " : "No  ") + check.name + ": " + check.detail })))),
      h("div", { class: "bc-actions" },
        h("button", { type: "button", class: "button button-secondary", id: "bc-check-again", text: "Check again", onclick: checkAgain }),
        h("button", { type: "button", class: "button button-primary", text: data.ready ? "Start editing" : "Close", onclick: () => setup.view.dialog.close() })));
  }

  function paintBadge(data) {
    const button = $("settings-button");
    if (button) button.classList.toggle("needs-setup", !data.ready);
  }

  async function loadSetup(refresh) {
    const data = await api("/api/setup" + (refresh ? "?refresh=1" : ""));
    const wasReady = setup.ready;
    setup.data = data; setup.ready = data.ready;
    paintBadge(data);
    if (setup.view.dialog.open) paintSetup(data);
    if (!wasReady && data.ready) {
      if (studio().loadCapabilities) await studio().loadCapabilities();
      const dismiss = $("dismiss-error"); if (dismiss) dismiss.click();
    }
    return data;
  }

  async function checkAgain() {
    const button = $("bc-check-again");
    if (button) { button.disabled = true; button.textContent = "Checking"; }
    try { await loadSetup(true); if (studio().loadCapabilities) await studio().loadCapabilities(); }
    catch (error) { setup.view.lead.textContent = error.message; }
    finally { if (button && button.isConnected) { button.disabled = false; button.textContent = "Check again"; } }
  }

  function openSetup() {
    if (!setup.view.dialog.open) setup.view.dialog.showModal();
    if (setup.data) paintSetup(setup.data);
    loadSetup(true).catch((error) => { setup.view.lead.textContent = error.message; });
  }

  /* ---------- Settings ---------- */

  const settings = { view: null, form: {}, current: null };

  function field(label, control, note) {
    return h("div", { class: "bc-field" }, h("label", { for: control.id, text: label }), control, note || null);
  }

  function check(id, text, note) {
    const input = h("input", { type: "checkbox", id });
    return { input, node: h("label", { class: "check-field bc-check", for: id }, input, h("span", {}, text, note ? h("small", { class: "fine-print", text: note }) : null)) };
  }

  function buildSettings() {
    const view = panel("settings", "Settings", "Stored on this computer, in ~/.vedit.");
    const form = settings.form;
    form.key = h("input", { type: "password", id: "bc-key", autocomplete: "off", spellcheck: false, maxLength: 512, placeholder: "Paste your API key" });
    form.keyNote = h("p", { class: "field-note", id: "bc-key-note" });
    form.removeKey = h("button", { type: "button", class: "text-button", id: "bc-key-remove", text: "Remove key", hidden: true, onclick: removeKey });
    const enabled = check("bc-enabled", "Allow Meta calls", "Turn this off to pause every Meta call. Footage is sent only when you create a story and confirm the send.");
    const contributor = check("bc-contributor", "Contributor mode", "Muse Spark costs less, and in exchange Meta may use what you send to it to train its future models: your video, transcript, and brief. Leave this off unless you are comfortable with that. Tracking, cutouts, generated images, and transcription have no contributor mode.");
    form.enabled = enabled.input; form.contributor = contributor.input;
    // Private mode: the steps that need only words run on Muse Glimmer 30B, Meta's open-weight model, here.
    form.private = h("input", { type: "checkbox", id: "bc-private", "aria-describedby": "bc-private-note" });
    form.privateNote = h("p", { class: "field-note", id: "bc-private-note", role: "status" });
    const privateMode = h("label", { class: "check-field bc-check", for: "bc-private" }, form.private,
      h("span", {}, "Private mode", h("small", { class: "fine-print" },
        "Run director\u2019s notes, caption translation and script scenes on Muse Glimmer 30B, Meta\u2019s open-weight model, on this computer. No Meta key needed for those steps. Needs the local Glimmer server (",
        h("code", { text: "uv run python -m vedit.open_weights serve" }), ").")));
    form.envNote = h("p", { class: "field-note", id: "bc-env-note", hidden: true });
    form.test = h("button", { type: "button", class: "button button-secondary", id: "bc-test", text: "Test connection", onclick: testConnection });
    form.testResult = h("p", { class: "field-note", id: "bc-test-result", role: "status" });
    form.budget = h("input", { type: "number", id: "bc-budget", min: "0.01", max: "5", step: "0.01", inputMode: "decimal" });
    form.language = h("select", { id: "bc-language" });
    form.workspace = h("input", { type: "text", id: "bc-workspace", spellcheck: false, autocomplete: "off" });
    form.workspaceNote = h("p", { class: "field-note", id: "bc-workspace-note" });
    form.status = h("p", { class: "bc-status", id: "bc-settings-status", role: "status" });
    form.diagnostics = h("pre", { class: "bc-diagnostics", id: "bc-diagnostics-text", tabIndex: 0 });
    form.diagnosticsWrap = h("details", { class: "bc-details", hidden: true }, h("summary", { text: "What was copied" }), form.diagnostics);
    form.diagnosticsStatus = h("span", { class: "fine-print", id: "bc-diagnostics-status", role: "status" });
    view.body.append(h("form", { class: "bc-form", id: "bc-settings-form", autocomplete: "off", onsubmit: saveSettings },
      h("section", { class: "bc-row" },
        h("h3", { text: "This computer" }),
        h("p", { class: "bc-why", id: "bc-setup-summary", text: "FFmpeg and your Meta API key." }),
        h("p", { class: "field-note", id: "bc-blender-note", hidden: true, text: "Needs Blender for 3D title cards. Install Blender 4.2 or later from blender.org, then reload this page. Everything else works without it." }),
        h("button", { type: "button", class: "button button-secondary", id: "bc-open-setup", text: "Open setup", onclick: () => { view.dialog.close(); openSetup(); } })),
      h("section", { class: "bc-row" },
        h("h3", { text: "Meta" }),
        h("p", { class: "bc-why", text: "Required. Briefcut runs on Meta models. Muse Spark plans and reviews the story, SAM 3.1 follows the subject, Meta Voice Transcribe writes the captions, and Muse Image draws covers." }),
        h("p", { class: "field-note" }, "No key yet? Create one at ", keyLink(), "."),
        form.envNote,
        field("API key", form.key, h("div", { class: "bc-key-line" }, form.keyNote, form.removeKey)),
        enabled.node, contributor.node,
        h("div", { class: "bc-inline" }, form.test, form.testResult)),
      h("section", { class: "bc-row" },
        h("h3", { text: "Defaults for new projects" }),
        h("div", { class: "bc-pair" },
          field("Project API limit, in US dollars", form.budget, h("p", { class: "field-note", text: "Meta calls stop at this limit. $0.01 to $5." })),
          field("Spoken language for captions", form.language, h("p", { class: "field-note", text: "This helps recognition. It does not translate." })))),
      h("section", { class: "bc-row" },
        h("h3", { text: "Project folder" }),
        field("Folder", form.workspace, form.workspaceNote)),
      h("section", { class: "bc-row" },
        h("h3", { text: "Privacy" }),
        h("p", { class: "bc-why", text: "Your footage is stored and rendered on this computer. It goes to Meta only when you create a story and confirm the send. Briefcut sends no analytics or usage data." }),
        privateMode, form.privateNote),
      h("section", { class: "bc-row" },
        h("h3", { text: "Help" }),
        h("p", { class: "bc-why", text: "Copies versions, tool checks, and the last error for a bug report. No home folder paths, keys, or transcript text." }),
        h("div", { class: "bc-inline" }, h("button", { type: "button", class: "button button-secondary", id: "bc-diagnostics", text: "Copy diagnostics", onclick: copyDiagnostics }), form.diagnosticsStatus),
        form.diagnosticsWrap),
      h("div", { class: "bc-actions bc-sticky" }, form.status, h("button", { type: "submit", class: "button button-primary", id: "bc-save", text: "Save" }))));
    settings.view = view;
  }

  function paintSettings(state) {
    const form = settings.form;
    settings.current = state;
    const key = state.meta_key;
    const environment = key.source === "environment";
    form.key.value = ""; form.key.disabled = environment;
    form.key.placeholder = key.configured ? "Paste a new key to replace it" : "Paste your API key";
    const place = key.source === "keychain" ? " Stored in the macOS Keychain." : key.source === "file" ? " Stored in " + state.config_path + ", readable only by you." : "";
    form.keyNote.textContent = environment ? "Set by MODEL_API_KEY in the environment, which takes priority." : key.configured ? "Configured" + (key.last4 ? ", ending in " + key.last4 : "") + "." + place : state.keychain_available ? "Your key is stored in the macOS Keychain." : "Your key is stored in " + state.config_path + ", readable only by you.";
    form.removeKey.hidden = !key.configured || environment;
    // A first key is pasted to be used: tick Allow Meta calls so one Save is enough. It stays visible and can be unticked.
    form.key.oninput = () => { if (!key.configured && form.key.value.trim() && state.meta_enabled_source !== "environment") form.enabled.checked = true; };
    form.enabled.checked = state.meta_enabled; form.enabled.disabled = state.meta_enabled_source === "environment";
    form.contributor.checked = state.meta_mode === "contributor"; form.contributor.disabled = state.meta_mode_source === "environment";
    form.private.checked = state.private_model === "glimmer"; form.private.disabled = state.private_model_source === "environment";
    paintPrivate(state);
    const fixed = [state.meta_enabled_source === "environment" ? "VEDIT_META_ENABLED" : "", state.meta_mode_source === "environment" ? "VEDIT_META_MODE" : ""].filter(Boolean);
    form.envNote.hidden = !fixed.length;
    form.envNote.textContent = fixed.length ? fixed.join(" and ") + " in the environment take" + (fixed.length === 1 ? "s" : "") + " priority over these switches." : "";
    form.budget.min = String(state.limits.budget_min); form.budget.max = String(state.limits.budget_max);
    form.budget.value = String(state.default_budget_usd);
    const page = $("language");
    /* The page's picker is built from the server's language list, so Settings offers exactly the same choices. */
    const options = page && page.options.length ? Array.from(page.options).map((option) => [option.value, option.textContent]) : [["auto", "Detect automatically"]];
    if (!options.some(([value]) => value === state.default_language)) options.push([state.default_language, state.default_language]);
    form.language.replaceChildren(...options.map(([value, text]) => new Option(text, value)));
    form.language.value = state.default_language;
    const workspace = state.workspace;
    form.workspace.value = workspace.saved || workspace.current;
    form.workspace.disabled = workspace.source === "environment";
    form.workspaceNote.textContent = workspace.source === "environment" ? "Set by VEDIT_WORKSPACE in the environment, which takes priority." : workspace.restart_required ? "Restart Briefcut to use this folder. Until then, projects stay in " + workspace.current + "." : "A new folder applies the next time Briefcut starts. Existing projects stay where they are.";
    const data = setup.data;
    if (data) $("bc-setup-summary").textContent = data.items.map((item) => item.title + (item.ok ? " ready" : " missing")).join(" · ");
    // Capabilities say motion_cards: false where Blender is missing; the Effects tab then hides the 3D title card.
    const caps = window.BriefcutKit && window.BriefcutKit.caps ? window.BriefcutKit.caps() : {};
    $("bc-blender-note").hidden = caps.motion_cards !== false;
  }

  /* What is running now comes from capabilities (meta.private); the box is the saved choice. */
  function paintPrivate(state) {
    const caps = window.BriefcutKit && window.BriefcutKit.caps ? window.BriefcutKit.caps() : {};
    const live = caps.meta && caps.meta.private && typeof caps.meta.private === "object" ? caps.meta.private : null;
    const note = settings.form.privateNote;
    if (!note) return;
    const where = live && live.runs_on ? live.runs_on : "this computer";
    note.textContent = (state && state.private_model_source === "environment" ? "Set by VEDIT_PRIVATE_MODEL in the environment, which takes priority. " : "")
      + (!live ? "Private mode status is not available in this build."
        : live.enabled ? "On now. Muse Glimmer 30B runs on " + where + "." : "Off now. These steps run on the Meta Model API.");
    note.title = live && live.model ? "Model: " + live.model : "";
  }

  async function openSettings() {
    settings.form.status.textContent = ""; settings.form.testResult.textContent = "";
    settings.view.dialog.showModal();
    try { paintSettings((await api("/api/settings")).settings); }
    catch (error) { settings.form.status.textContent = error.message; }
  }

  function settingsPayload() {
    const form = settings.form;
    const payload = { meta_enabled: form.enabled.checked, meta_mode: form.contributor.checked ? "contributor" : "standard", default_budget_usd: Number(form.budget.value), default_language: form.language.value, private_model: form.private.checked ? "glimmer" : "" };
    const state = settings.current;
    if (form.key.value.trim()) payload.meta_api_key = form.key.value.trim();
    if (state && state.meta_enabled_source === "environment") delete payload.meta_enabled;
    if (state && state.meta_mode_source === "environment") delete payload.meta_mode;
    if (state && state.private_model_source === "environment") delete payload.private_model;
    if (!form.workspace.disabled && (!state || form.workspace.value.trim() !== (state.workspace.saved || state.workspace.current))) payload.workspace = form.workspace.value.trim();
    return payload;
  }

  async function persist(payload) {
    const { settings: state } = await api("/api/settings", { method: "POST", body: payload });
    paintSettings(state);
    await loadSetup(false);
    if (studio().loadCapabilities) await studio().loadCapabilities();
    paintPrivate(state);
    applyDefaults(state);
    return state;
  }

  async function saveSettings(event) {
    event.preventDefault();
    const form = settings.form;
    const button = $("bc-save"); button.disabled = true; form.status.textContent = "Saving";
    try {
      const state = await persist(settingsPayload());
      form.status.textContent = state.workspace.restart_required ? "Saved. Restart Briefcut to use the new folder." : "Saved.";
    } catch (error) { form.status.textContent = error.message; }
    finally { button.disabled = false; }
  }

  async function removeKey() {
    settings.form.status.textContent = "Removing";
    try { await persist({ clear_meta_api_key: true }); settings.form.status.textContent = "Key removed."; }
    catch (error) { settings.form.status.textContent = error.message; }
  }

  async function testConnection() {
    const form = settings.form;
    form.test.disabled = true; form.testResult.textContent = "Checking"; form.testResult.className = "field-note";
    try {
      await persist(settingsPayload());
      const { access } = await api("/api/settings/test", { method: "POST", body: {}, timeout: 120000 });
      form.testResult.textContent = access.reason;
      form.testResult.className = "field-note " + (access.ok ? "bc-good" : "bc-bad");
    } catch (error) { form.testResult.textContent = error.message; form.testResult.className = "field-note bc-bad"; }
    finally { form.test.disabled = false; }
  }

  async function copyDiagnostics() {
    const form = settings.form;
    try {
      const { text } = await api("/api/diagnostics");
      form.diagnostics.textContent = text;
      form.diagnosticsWrap.hidden = false;
      form.diagnosticsStatus.textContent = (await copyText(text)) ? "Copied." : "Copying was blocked. Select the text below.";
      if (form.diagnosticsStatus.textContent !== "Copied.") form.diagnosticsWrap.open = true;
    } catch (error) { form.diagnosticsStatus.textContent = error.message; }
  }

  function applyDefaults(state) {
    if (studio().currentJobId && studio().currentJobId()) return;
    const budget = $("budget"); const language = $("language");
    if (budget) {
      const value = String(state.default_budget_usd);
      if (!Array.from(budget.options).some((option) => Number(option.value) === Number(value))) budget.append(new Option("$" + Number(value).toFixed(2), value));
      budget.value = Array.from(budget.options).find((option) => Number(option.value) === Number(value)).value;
    }
    if (language) {
      if (!Array.from(language.options).some((option) => option.value === state.default_language)) language.append(new Option(state.default_language, state.default_language));
      language.value = state.default_language;
    }
  }

  /* ---------- Project library ---------- */

  const library = { view: null, status: null, storage: null };
  const pills = { complete: "is-complete", failed: "is-failed", interrupted: "is-failed", queued: "is-working", analyzing: "is-working", rendering: "is-working" };

  function renameForm(project, title) {
    const input = h("input", { type: "text", value: project.title, maxLength: 120, required: true, "aria-label": "Project name" });
    const form = h("form", { class: "bc-rename", onsubmit: async (event) => {
      event.preventDefault();
      try {
        await api("/api/projects/" + project.id + "/rename", { method: "POST", body: { title: input.value } });
        if (studio().currentJobId && studio().currentJobId() === project.id && $("project-name")) $("project-name").textContent = input.value.trim();
        await loadLibrary("Renamed.");
      } catch (error) { library.status.textContent = error.message; }
    } },
      input,
      h("button", { type: "submit", class: "text-button", text: "Save" }),
      h("button", { type: "button", class: "text-button", text: "Cancel", onclick: () => form.replaceWith(title) }));
    title.replaceWith(form);
    input.focus(); input.select();
  }

  async function duplicateProject(project) {
    try { await api("/api/projects/" + project.id + "/duplicate", { method: "POST", body: {} }); await loadLibrary("Copy created. It starts at the review step."); }
    catch (error) { library.status.textContent = error.message; }
  }

  async function deleteProject(project) {
    const yes = await confirmAction({
      title: "Delete “" + project.title + "”?",
      body: "This removes the project and its renders from this computer" + (project.size_bytes ? ", about " + megabytes(project.size_bytes) : "") + ". Briefcut's stored copy of the clip goes too, unless another project uses it. Your original file is not touched. This cannot be undone.",
      action: "Delete project",
    });
    if (!yes) return;
    try {
      const result = await api("/api/campaigns/" + project.id, { method: "DELETE" });
      if (studio().currentJobId && studio().currentJobId() === project.id && studio().resetProject) studio().resetProject(true);
      await loadLibrary("Deleted. Freed " + megabytes(result.freed_bytes) + ".");
    } catch (error) { library.status.textContent = error.message; }
  }

  async function cleanOldVersions(bytes) {
    const yes = await confirmAction({
      title: "Clean up renders of old versions?",
      body: "This frees about " + megabytes(bytes) + ". Older versions leave their projects' history. The latest verified version of every project stays, along with your plans and clips.",
      action: "Clean up",
    });
    if (!yes) return;
    try {
      const result = await api("/api/projects/cleanup", { method: "POST", body: {}, timeout: 60000 });
      const current = studio().currentJobId && studio().currentJobId();
      if (current && result.removed.some((item) => item.id === current) && studio().openJob) await studio().openJob(current);
      await loadLibrary("Freed " + megabytes(result.freed_bytes) + ".");
    } catch (error) { library.status.textContent = error.message; }
  }

  function projectCard(project) {
    const title = h("h3", { text: project.title, title: project.title });
    const open = () => { library.view.dialog.close(); if (studio().openJob) studio().openJob(project.id); };
    const picture = h("img", { alt: "", loading: "lazy", src: project.thumbnail_url + "?t=" + encodeURIComponent(project.updated_at || "") });
    picture.addEventListener("error", () => picture.remove());
    const facts = [shortDate(project.updated_at), project.duration ? clock(project.duration) : "", megabytes(project.size_bytes)].filter(Boolean).join(" · ");
    return h("li", { class: "bc-card", "data-project": project.id },
      h("button", { type: "button", class: "bc-thumb", "aria-label": "Open " + project.title, onclick: open }, picture),
      h("div", { class: "bc-card-body" },
        h("div", { class: "bc-card-title" }, title),
        h("p", { class: "bc-facts" }, h("span", { class: "status-pill " + (pills[project.status] || ""), text: project.status_label }), h("span", { text: facts })),
        project.status === "interrupted" ? h("p", { class: "fine-print", text: "Briefcut stopped before this finished. Open it to retry." }) : null,
        h("div", { class: "bc-card-actions" },
          h("button", { type: "button", class: "text-button", text: "Open", onclick: open }),
          h("button", { type: "button", class: "text-button", text: "Rename", onclick: () => renameForm(project, title) }),
          h("button", { type: "button", class: "text-button", text: "Duplicate", disabled: project.active, title: project.active ? "Wait for this project to finish." : "", onclick: () => duplicateProject(project) }),
          h("button", { type: "button", class: "text-button bc-text-danger", text: "Delete", disabled: project.active, title: project.active ? "Cancel this project first." : "", onclick: () => deleteProject(project) }))));
  }

  function paintLibrary(data, message) {
    const { projects, storage } = data;
    library.storage.replaceChildren(...[
      h("span", { text: megabytes(storage.total_bytes) + " used by Briefcut" }),
      storage.reclaimable_bytes ? h("button", { type: "button", class: "text-button", id: "bc-cleanup", text: "Clean up renders of old versions (" + megabytes(storage.reclaimable_bytes) + ")", onclick: () => cleanOldVersions(storage.reclaimable_bytes) }) : null,
    ].filter(Boolean));
    library.status.textContent = message || "";
    library.view.body.replaceChildren(projects.length
      ? h("ul", { class: "bc-grid" }, projects.map(projectCard))
      : h("p", { class: "bc-empty", text: "Your projects will appear here. Choose your footage to start one." }));
    const button = $("recent-projects");
    if (button) button.classList.toggle("has-interrupted", data.interrupted > 0);
  }

  async function loadLibrary(message) {
    try { paintLibrary(await api("/api/projects", { timeout: 30000 }), message); }
    catch (error) { library.status.textContent = error.message; }
  }

  function openLibrary() {
    library.view.dialog.showModal();
    library.view.body.replaceChildren(h("p", { class: "bc-empty", text: "Loading your projects." }));
    loadLibrary();
  }

  function buildLibrary() {
    const view = panel("library", "Projects", "");
    lightDismiss(view.dialog);
    library.storage = h("div", { class: "bc-storage", id: "bc-storage" });
    library.status = h("p", { class: "bc-status", id: "bc-library-status", role: "status" });
    view.lead.replaceWith(library.storage);
    view.body.before(library.status);
    library.view = view;
  }

  async function noticeInterrupted() {
    try {
      const data = await api("/api/projects", { timeout: 30000 });
      const button = $("recent-projects");
      if (button) button.classList.toggle("has-interrupted", data.interrupted > 0);
      if (data.interrupted && studio().toast) studio().toast(data.interrupted === 1 ? "A project was interrupted when Briefcut stopped. Open Projects to retry it." : data.interrupted + " projects were interrupted when Briefcut stopped. Open Projects to retry them.");
    } catch (_) { /* The library shows the same state when it opens. */ }
  }

  /* ---------- Autosave status ---------- */

  function watchAutosave(actions) {
    const draft = $("draft-status");
    const saved = $("save-status");
    const pill = h("span", { id: "autosave-status", class: "autosave", role: "status", hidden: true, title: "Your edits are kept in this browser as you work. Rendering saves them to the project. In a private window they are not kept." });
    actions.prepend(pill);
    const stamp = () => new Date().toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    const show = (text, warning) => { pill.textContent = text; pill.hidden = false; pill.classList.toggle("is-warning", !!warning); };
    if (draft) new MutationObserver(() => {
      const text = draft.textContent.trim();
      if (text) show(text.startsWith("Unsaved") ? "Not saved" : "Saved " + stamp(), text.startsWith("Unsaved"));
    }).observe(draft, { childList: true, characterData: true, subtree: true });
    if (saved) new MutationObserver(() => {
      const text = saved.textContent.trim();
      if (!text) pill.hidden = true;
      else if (pill.hidden) show(text);
    }).observe(saved, { childList: true, characterData: true, subtree: true });
  }

  /* ---------- Start ---------- */

  async function boot() {
    const actions = document.querySelector(".top-actions");
    setup.view = panel("setup", "Set up Briefcut", "");
    buildSettings();
    buildLibrary();
    if (actions) {
      const button = h("button", { id: "settings-button", class: "button button-quiet", type: "button", "aria-label": "Settings", title: "Settings", onclick: openSettings });
      button.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M8.6 2h2.8l.4 2.3 1.5.6 1.9-1.3 2 2-1.3 1.9.6 1.5 2.3.4v2.8l-2.3.4-.6 1.5 1.3 1.9-2 2-1.9-1.3-1.5.6-.4 2.3H8.6l-.4-2.3-1.5-.6-1.9 1.3-2-2 1.3-1.9-.6-1.5-2.3-.4V9.2l2.3-.4.6-1.5-1.3-1.9 2-2 1.9 1.3 1.5-.6.4-2.3Z"/><circle cx="10" cy="10.8" r="2.6"/></svg>';
      const anchor = $("shortcuts-button");
      if (anchor && anchor.parentNode === actions) actions.insertBefore(button, anchor); else actions.append(button);
      watchAutosave(actions);
    }
    const recent = $("recent-projects");
    if (recent) {
      const summary = recent.querySelector("summary");
      summary.setAttribute("aria-haspopup", "dialog");
      summary.addEventListener("click", (event) => { event.preventDefault(); openLibrary(); });
    }
    const newProject = $("new-project");
    if (newProject) newProject.addEventListener("click", () => { if (settings.current) applyDefaults(settings.current); });
    try {
      const data = await loadSetup(false);
      if (!data.ready) openSetup();
    } catch (_) { /* The editor shows connection problems itself. */ }
    try { const { settings: state } = await api("/api/settings"); settings.current = state; applyDefaults(state); } catch (_) { /* Form defaults stay. */ }
    noticeInterrupted();
  }

  window.BriefcutSetup = { openSetup, openSettings, openLibrary };
  boot();
}());
