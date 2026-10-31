"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const activeStatuses = new Set(["queued", "analyzing", "rendering"]);
  const labels = { queued: "Queued", analyzing: "Shaping the story", planned: "Ready to edit", rendering: "Making the video", complete: "Verified", failed: "Needs attention", cancelled: "Cancelled" };
  const state = { file: null, paths: [], sourceURL: "", objectURL: "", duration: 0, job: null, draft: null, capabilities: null, busy: false, epoch: 0, pollTimer: null, pollFailures: 0, selected: -1, rangeEnd: null, preview: "source", toastTimer: null, sourceName: "", pollController: null, jobStarted: 0, settingsSnapshot: "", settingsDirty: false, pendingCreate: null, effectPresets: [], effectTimingEdited: false, loop: false, drag: null, transcriptQuery: "", cueIndex: -1 };
  const uploadedClips = new WeakMap();
  const storageKey = "vedit.studio.current";
  const maxClipBytes = 200000000;

  function storageGet(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
  function storageSet(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* A private browser can still edit. */ } }
  function storageRemove(key) { try { localStorage.removeItem(key); } catch (_) { /* Storage is optional. */ } }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function finite(value, fallback) { const n = Number(value); return Number.isFinite(n) ? n : fallback; }
  function timestamp(value) { if (!value) return 0; if (typeof value === "number") return value > 1e11 ? value : value * 1000; const n = Date.parse(value); return Number.isFinite(n) ? n : 0; }
  function time(value) { const seconds = Math.max(0, Math.floor(finite(value, 0))); return String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0"); }
  function durationText(value) { const n = finite(value, 0); return n < 60 ? n.toFixed(n % 1 ? 1 : 0) + "s" : time(n); }
  function errorMessage(error) { if (typeof error === "string" && error) return error; if (error && typeof error.message === "string") return error.message; return "Something interrupted this step. Try again."; }
  function announce(message) { $("live").textContent = message; }
  function showError(message) { $("error-text").textContent = errorMessage(message); $("error").hidden = false; announce(errorMessage(message)); }
  function clearError() { $("error").hidden = true; $("error-text").textContent = ""; document.querySelectorAll('[aria-invalid="true"]').forEach((field) => { field.removeAttribute("aria-invalid"); field.removeAttribute("aria-describedby"); }); }
  function toast(message) { clearTimeout(state.toastTimer); $("toast").textContent = message; $("toast").hidden = false; state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3500); }
  function safeArtifact(value) { if (typeof value !== "string" || !value) return ""; try { const url = new URL(value, location.href); return url.origin === location.origin && url.pathname.startsWith("/api/campaigns/") ? url.href : ""; } catch (_) { return ""; } }
  function artifact(name, id) { return "/api/campaigns/" + encodeURIComponent(id || state.job.id) + "/artifact?name=" + encodeURIComponent(name); }
  function sourceBasename(path) { return String(path || "").split(/[\\/]/).pop() || "Source footage"; }
  function hasVerifiedResult(job) { return !!(job && job.result && job.result.proof && job.result.proof.has_video); }
  function verifiedRevision(job) { return job && job.result ? finite(job.result.revision, finite(job.verified_revision, finite(job.revision, 1))) : 0; }
  function previousResult(job) { return hasVerifiedResult(job) && (job.result.stale === true || verifiedRevision(job) < finite(job.revision, verifiedRevision(job))); }
  function finalLabel(job) { return previousResult(job) ? "Previous verified video" : "Finished video"; }
  function versionedArtifact(value) {
    const safe = safeArtifact(value);
    if (!safe) return "";
    const url = new URL(safe);
    if (!url.searchParams.has("v")) url.searchParams.set("v", String(verifiedRevision(state.job)));
    return url.href;
  }

  async function request(url, options) {
    const config = options || {};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), config.timeout || 20000);
    const externalSignal = config.signal;
    const abort = () => controller.abort();
    if (externalSignal) { if (externalSignal.aborted) controller.abort(); else externalSignal.addEventListener("abort", abort, { once: true }); }
    try {
      const response = await fetch(url, { method: config.method || "GET", headers: Object.assign({ Accept: "application/json" }, config.body === undefined ? {} : { "Content-Type": "application/json" }, config.headers || {}), body: config.body === undefined ? undefined : JSON.stringify(config.body), signal: controller.signal, cache: "no-store" });
      let data;
      try { data = await response.json(); } catch (_) { throw new Error("The editor returned an unreadable response (" + response.status + "). Restart the editor and try again."); }
      if (!response.ok || !data || data.ok === false) throw new Error(errorMessage(data && data.error) || "The editor refused this request.");
      return data;
    } catch (error) {
      if (error.name === "AbortError") { if (externalSignal && externalSignal.aborted) throw error; throw new Error("The editor took too long to respond. Your saved project is still available; reconnect to check it."); }
      if (error instanceof TypeError) throw new Error("The editor is not reachable. Keep it running on your Mac, then reconnect.");
      throw error;
    } finally { clearTimeout(timer); if (externalSignal) externalSignal.removeEventListener("abort", abort); }
  }

  function stopPolling() { clearTimeout(state.pollTimer); state.pollTimer = null; if (state.pollController) state.pollController.abort(); state.pollController = null; state.epoch += 1; }
  function provider() { return $("provider-meta").checked ? "meta" : "local"; }
  function readSettings() {
    const chosenProvider = provider();
    return { goal: $("goal").value.trim(), provider: chosenProvider, target_duration: Number($("target-duration").value), aspect: $("aspect").value, captions: $("captions").checked, language: $("language").value, subject: chosenProvider === "meta" ? $("subject").value.trim() : "", budget_usd: Number($("budget").value), generate_cover: chosenProvider === "meta" && $("cover-mode").value === "meta", caption_provider: captionProvider(), review: chosenProvider === "meta" && $("review").checked };
  }
  function metaTranscribeAvailable() { const caps = state.capabilities; const meta = caps && caps.meta || {}; return !!(meta.enabled && meta.configured && meta.features && meta.features.transcribe); }
  function captionProvider() { return $("captions").checked && metaTranscribeAvailable() && $("caption-provider").value === "meta" ? "meta" : "local"; }
  function updateCaptionProvider() {
    const show = $("captions").checked && metaTranscribeAvailable();
    $("caption-provider-field").hidden = !show;
    const meta = state.capabilities && state.capabilities.meta || {};
    const price = meta.prices && Number.isFinite(Number(meta.prices.transcribe_per_hour)) ? "$" + Number(meta.prices.transcribe_per_hour).toFixed(2) + " per hour of audio" : "billed per hour of audio";
    $("caption-provider-note").textContent = $("caption-provider").value === "meta" ? "Muse Voice Transcribe hears the audio on Meta\u2019s servers, " + price + ". The transcript still renders locally." : "Local transcription runs on this Mac at no cost.";
  }
  function readEffect() {
    if (!state.effectPresets.length || $("effect-preset").value === "none") return null;
    return { preset: $("effect-preset").value, intensity: Number($("effect-intensity").value), start: Number($("effect-start").value), duration: Number($("effect-duration").value) };
  }
  function markSettingsChanged() {
    state.settingsDirty = !!state.job && !!state.settingsSnapshot && JSON.stringify(readSettings()) !== state.settingsSnapshot;
    if (state.draft) $("render-note").textContent = state.settingsDirty ? "Settings changed. Create a story to apply them before rendering." : state.job && state.job.status === "complete" ? "Changes make a new render of this project." : "Your story. Your final say.";
    setControls();
  }
  function isActive() { return state.busy || !!(state.job && activeStatuses.has(state.job.status)); }
  function setControls() {
    const locked = isActive();
    for (const control of $("desk-form").querySelectorAll("input,textarea,select,button")) control.disabled = locked;
    const caps = state.capabilities;
    $("provider-meta").disabled = locked || !(caps && caps.meta && caps.meta.enabled && caps.meta.configured);
    $("captions").disabled = locked || !(caps && caps.local && caps.local.captions || metaTranscribeAvailable());
    $("effect-preset").disabled = locked || !state.effectPresets.length;
    ["effect-intensity", "effect-start", "effect-duration"].forEach((id) => { $(id).disabled = locked || !state.effectPresets.length || $("effect-preset").value === "none"; });
    $("plan").disabled = locked || !!(caps && caps.local && caps.local.ready === false);
    $("new-project").disabled = state.busy;
    $("make").disabled = locked || state.settingsDirty || !state.draft || !state.job || !["planned", "complete", "failed", "cancelled"].includes(state.job.status);
    $("make").textContent = state.job && ["complete", "failed", "cancelled"].includes(state.job.status) ? "Render this version" : "Make the video";
    $("cancel-job").disabled = state.busy || !!(state.job && state.job.cancel_requested);
    $("retry-job").disabled = state.busy;
    $("render-center-crop").disabled = state.busy || state.settingsDirty || !state.draft;
    for (const control of $("story-content").querySelectorAll("input,textarea,button")) {
      if (control.id === "make") continue;
      control.disabled = locked || !state.draft;
    }
    const segments = state.draft && state.draft.segments || [];
    $("add-segment").disabled = locked || !state.draft || segments.length >= 8;
    $("program-list").querySelectorAll(".move-up").forEach((button, index) => { button.disabled = locked || index === 0; });
    $("program-list").querySelectorAll(".move-down").forEach((button, index) => { button.disabled = locked || index === segments.length - 1; });
    $("program-list").querySelectorAll(".remove-segment").forEach((button) => { button.disabled = locked || segments.length <= 1; });
  }

  function updateMode() {
    const meta = provider() === "meta";
    $("meta-options").hidden = !meta;
    $("engine-note").textContent = meta ? "Meta reads the footage and proposes a story. You review every moment before rendering." : "Local mode creates an editable starting cut. It does not interpret the footage.";
    $("privacy-note").textContent = meta ? "Cloud understanding. Local rendering. Your final say." : "Rendered on your Mac. Ready when you are.";
  }

  function updateEffectControls() {
    const selected = state.effectPresets.find((item) => item.id === $("effect-preset").value);
    $("effect-settings").hidden = !selected;
    $("effect-description").textContent = selected ? selected.description : "Add a touch of 3D-style movement to your footage.";
    $("effect-strength").textContent = Math.round(finite($("effect-intensity").value, .55) * 100) + "%";
    $("effect-intensity").setAttribute("aria-valuetext", $("effect-strength").textContent);
    setControls();
  }

  function fillEffect(effect) {
    effect = effect || {};
    $("effect-preset").value = effect.preset || "none";
    $("effect-intensity").value = String(finite(effect.intensity, .55));
    $("effect-start").value = String(finite(effect.start, 0));
    $("effect-duration").value = String(finite(effect.duration, 3));
    state.effectTimingEdited = !!effect.preset;
    updateEffectControls();
  }

  function boundDefaultEffectDuration() {
    if (state.effectTimingEdited || state.job) return;
    const available = Math.min(state.duration || 3, Number($("target-duration").value));
    $("effect-duration").value = String(Math.max(.1, Math.floor(Math.min(3, available) * 10) / 10));
  }

  async function loadCapabilities() {
    try {
      const data = await request("/api/capabilities");
      state.capabilities = data.capabilities || data;
      const caps = state.capabilities;
      const meta = caps.meta || {};
      const local = caps.local || {};
      const effects = caps.effects;
      state.effectPresets = effects && effects.supported && Array.isArray(effects.presets) ? effects.presets.filter((item) => item && ["tilt", "float", "push"].includes(item.id)) : [];
      $("effects-section").hidden = !state.effectPresets.length;
      $("effect-preset").replaceChildren(new Option("No effect", "none"), ...state.effectPresets.map((item) => new Option(item.label, item.id)));
      updateEffectControls();
      const enabled = !!(meta.enabled && meta.configured);
      $("connection-status").textContent = local.ready === false ? "Setup needed" : "Editor ready";
      $("connection-status").classList.toggle("is-connected", local.ready !== false);
      $("meta-tag").textContent = enabled ? "Connected" : "Not connected";
      $("meta-setup").hidden = enabled;
      $("meta-reason").textContent = meta.reason || "Meta access has not been enabled on this editor. Local editing is ready to use.";
      $("caption-availability").textContent = local.captions ? "Local speech model ready" : "Local speech model not installed";
      if (!local.captions && !(enabled && meta.features && meta.features.transcribe)) $("captions").checked = false;
      if (!local.captions && enabled && meta.features && meta.features.transcribe) $("caption-availability").textContent = "Meta Voice Transcribe available";
      updateCaptionProvider();
      if (!enabled && $("provider-meta").checked) $("provider-local").checked = true;
      if (local.ready === false) showError(local.reason || "The local editor needs its required media tools before it can render.");
      updateMode(); setControls();
    } catch (error) { $("connection-status").textContent = "Not connected"; showError(error); }
  }

  function revokeSource() { if (state.objectURL) URL.revokeObjectURL(state.objectURL); state.objectURL = ""; }
  function selectFile(file) {
    if (!file || isActive()) return;
    if (!file.size) { showError("This file is empty. Choose a video with footage."); return; }
    if (file.size > maxClipBytes) { showError("Choose a clip under 200 MB. Larger footage can be edited with the local command-line editor."); return; }
    if (file.type && !file.type.startsWith("video/") && !/\.(mp4|mov|m4v|mkv|webm)$/i.test(file.name)) { showError("Choose a video file, such as MP4, MOV, or WebM."); return; }
    resetProject(false);
    state.file = file;
    state.sourceName = file.name;
    state.objectURL = URL.createObjectURL(file);
    state.sourceURL = state.objectURL;
    $("clip-name").textContent = file.name;
    $("drop-title").textContent = "Change footage";
    $("source-badge").textContent = (file.size / 1000000).toFixed(1) + " MB";
    $("project-name").textContent = file.name.replace(/\.[^.]+$/, "");
    $("say-clip").textContent = file.name;
    setPreview("source");
    clearError();
    announce(file.name + " selected. Write a direction, then create a story.");
  }

  async function ensureUploaded(file) {
    if (uploadedClips.has(file)) return uploadedClips.get(file);
    const pending = uploadClip(file).catch((error) => { uploadedClips.delete(file); throw error; });
    uploadedClips.set(file, pending);
    return pending;
  }

  function uploadClip(file) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload?name=" + encodeURIComponent(file.name));
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.timeout = 120000;
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) { const ratio = event.loaded / event.total; $("progress-bar").value = ratio; $("progress-detail").textContent = "Saving footage to your local workspace · " + Math.round(ratio * 100) + "%"; }
      });
      xhr.addEventListener("load", () => {
        let data;
        try { data = JSON.parse(xhr.responseText); } catch (_) { reject(new Error("The editor did not accept this clip. Try uploading it again.")); return; }
        if (xhr.status < 200 || xhr.status >= 300 || !data.ok) { reject(new Error(errorMessage(data.error))); return; }
        if (!Array.isArray(data.paths) || data.paths.length !== 1 || typeof data.paths[0] !== "string") { reject(new Error("The editor did not return a saved clip. Try again.")); return; }
        resolve(data.paths);
      });
      xhr.addEventListener("error", () => reject(new Error("The clip transfer was interrupted. Keep the editor running and try again.")));
      xhr.addEventListener("timeout", () => reject(new Error("Clip transfer timed out. Try a smaller file or upload it again.")));
      xhr.addEventListener("abort", () => reject(new Error("Clip transfer was cancelled. Choose the clip again to continue.")));
      xhr.send(file);
    });
  }

  async function createStory(event) {
    if (event) event.preventDefault();
    if (isActive()) return;
    stopPolling();
    clearError();
    const goal = $("goal").value.trim();
    if (!goal) { showError("Give the video a direction first."); $("goal").focus(); return; }
    if (!state.file && !state.paths.length) { showError("Choose your footage first."); $("clip").focus(); return; }
    const chosenProvider = provider();
    if (chosenProvider === "meta" && !$("cloud-consent").checked) { showError("Confirm that this footage can be sent to Meta before creating the story."); $("cloud-consent").focus(); return; }
    const payload = readSettings();
    const settingsSnapshot = JSON.stringify(payload);
    payload.effect = readEffect();
    const creationSignature = JSON.stringify(payload) + "|" + (state.file ? state.file.name + ":" + state.file.size + ":" + state.file.lastModified : state.paths.join("|"));
    if (!state.pendingCreate || state.pendingCreate.signature !== creationSignature) state.pendingCreate = { signature: creationSignature, key: typeof crypto.randomUUID === "function" ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2) };
    payload.idempotency_key = state.pendingCreate.key;
    state.busy = true; state.jobStarted = Date.now(); setControls();
    $("job-progress").hidden = false; $("busy").textContent = "Preparing your footage"; $("progress-stage").textContent = "Local transfer"; $("progress-bar").value = 0; $("cancel-job").hidden = true; $("reconnect-job").hidden = true;
    $("progress-detail").textContent = "Saving the clip once. Future edits reuse it.";
    try {
      payload.paths = state.paths.length ? state.paths : await ensureUploaded(state.file);
      state.paths = payload.paths;
      $("busy").textContent = "Creating your story";
      $("progress-detail").textContent = chosenProvider === "meta" ? "The footage will be read using your configured Meta access." : "Building an editable starting cut.";
      const planStarted = Date.now();
      const data = await request("/api/campaigns", { method: "POST", body: payload, timeout: 25000 });
      if (!data.job || !data.job.id) throw new Error("The editor did not create a project. Try again.");
      markTiming(data.job.id, "planStart", planStarted);
      state.settingsSnapshot = settingsSnapshot; state.settingsDirty = false; state.pendingCreate = null;
      state.draft = null;
      adoptJob(data.job, true);
      refreshRecent();
    } catch (error) { showError(error); $("job-progress").hidden = true; }
    finally { state.busy = false; setControls(); }
  }

  function draftKey(id) { return "vedit.studio.draft." + id; }
  function saveDraft() {
    if (!state.draft || !state.job) return;
    const saved = { serverPlan: JSON.stringify(state.job.plan), plan: state.draft, effect: readEffect() };
    try { localStorage.setItem(draftKey(state.job.id), JSON.stringify(saved)); $("draft-status").textContent = "Saved in this browser"; }
    catch (_) { $("draft-status").textContent = "Unsaved in private browsing"; }
    updateStoryDuration();
  }

  function restoreDraft(job) {
    if (!job.plan) return null;
    try { const cached = JSON.parse(storageGet(draftKey(job.id)) || "null"); if (cached && cached.serverPlan === JSON.stringify(job.plan) && validDraftShape(cached.plan)) { if (Object.prototype.hasOwnProperty.call(cached, "effect")) fillEffect(cached.effect); return cached.plan; } } catch (_) { /* Ignore an outdated browser draft. */ }
    return clone(job.plan);
  }

  function validDraftShape(plan) {
    return !!plan && typeof plan.title === "string" && typeof plan.caption === "string" && Array.isArray(plan.hashtags) && plan.hashtags.every((tag) => typeof tag === "string") && Array.isArray(plan.segments) && plan.segments.length > 0 && plan.segments.length <= 8 && plan.segments.every((segment) => segment && typeof segment.label === "string" && typeof segment.reason === "string" && (segment.start === null || typeof segment.start === "number") && (segment.end === null || typeof segment.end === "number"));
  }

  function adoptJob(job, fresh) {
    const previous = state.job;
    const idChanged = !previous || previous.id !== job.id;
    if (idChanged || fresh) stopPolling();
    state.job = job;
    storageSet(storageKey, job.id);
    state.paths = job.request && Array.isArray(job.request.paths) ? job.request.paths : state.paths;
    const source = job.source || {};
    state.duration = finite(source.duration, finite(job.plan && job.plan.source_duration, state.duration));
    state.sourceName = source.name || state.sourceName || sourceBasename(state.paths[0]);
    if (!state.file || idChanged && !fresh) { revokeSource(); state.file = null; state.sourceURL = source.has_video ? artifact("source", job.id) : ""; }
    if (job.plan && (!state.draft || idChanged || fresh && job.status === "planned")) { state.draft = restoreDraft(job); renderStory(); }
    if (!job.plan && idChanged) { state.draft = null; renderStory(); }
    if (idChanged || fresh) {
      state.pollFailures = 0;
      state.jobStarted = timestamp(job.updated_at) || Date.now();
      if (job.status === "complete" || hasVerifiedResult(job) && (idChanged || state.preview === "final")) setPreview("final");
      else if (!state.file) setPreview("source");
    }
    if (!state.file && source.has_video && !(previous && previous.source && previous.source.has_video) && state.preview === "source") setPreview("source");
    const changedStatus = !previous || previous.status !== job.status;
    noteTiming(job, previous);
    paintJob();
    renderScrubber();
    if (changedStatus) announce(job.message || labels[job.status] || job.status);
    if (activeStatuses.has(job.status)) schedulePoll();
    else { clearTimeout(state.pollTimer); state.pollTimer = null; }
    if (changedStatus && job.status === "complete") { setPreview("final"); refreshRecent(); }
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    const epoch = state.epoch;
    const id = state.job.id;
    state.pollTimer = setTimeout(async () => {
      if (epoch !== state.epoch) return;
      const controller = new AbortController(); state.pollController = controller;
      try {
        const data = await request("/api/campaigns/" + encodeURIComponent(id), { signal: controller.signal, timeout: 12000 });
        if (epoch !== state.epoch) return;
        if (!data.job) throw new Error("This saved project is not available.");
        state.pollFailures = 0;
        $("reconnect-job").hidden = true;
        adoptJob(data.job, false);
      } catch (error) {
        if (epoch !== state.epoch || error.name === "AbortError") return;
        state.pollFailures += 1;
        $("progress-detail").textContent = "Connection interrupted. The editor may still be working; your project is saved.";
        if (state.pollFailures < 4) schedulePoll();
        else { $("reconnect-job").hidden = false; showError(error); }
      } finally { if (state.pollController === controller) state.pollController = null; }
    }, state.pollFailures ? Math.min(8000, 1500 * state.pollFailures) : 1000);
  }

  function fillRequest(job) {
    const req = job.request || {};
    $("goal").value = req.goal || "";
    $("aspect").value = req.aspect || "9:16";
    if ([15, 30, 60, 90].includes(Number(req.target_duration))) $("target-duration").value = String(req.target_duration);
    $("provider-meta").checked = req.provider === "meta";
    $("provider-local").checked = req.provider !== "meta";
    $("captions").checked = !!req.captions;
    $("language").value = req.language || "en";
    $("language-field").hidden = !req.captions;
    $("caption-provider").value = req.caption_provider === "meta" ? "meta" : "local";
    updateCaptionProvider();
    $("subject").value = req.subject || "";
    if ([0.25, 0.5, 1, 2].includes(Number(req.budget_usd))) $("budget").value = String(req.budget_usd);
    $("cover-mode").value = req.generate_cover ? "meta" : "frame";
    $("review").checked = !!req.review;
    fillEffect(req.effect);
    $("cloud-consent").checked = false;
    updateMode();
  }

  async function openJob(id) {
    if (state.busy) return;
    clearError(); stopPolling();
    const epoch = state.epoch;
    const restoreFocus = $("recent-projects").open;
    try {
      const data = await request("/api/campaigns/" + encodeURIComponent(id));
      if (epoch !== state.epoch) return;
      if (!data.job) throw new Error("This project is no longer available.");
      revokeSource(); state.file = null; state.sourceURL = ""; state.duration = 0; state.draft = null; state.sourceName = ""; $("clip").value = "";
      fillRequest(data.job);
      state.settingsSnapshot = JSON.stringify(readSettings()); state.settingsDirty = false; state.pendingCreate = null;
      adoptJob(data.job, true);
      $("recent-projects").open = false;
      if (restoreFocus) $("project-name").focus();
    } catch (error) { if (epoch !== state.epoch) return; showError(error); if (state.job && activeStatuses.has(state.job.status)) schedulePoll(); else storageRemove(storageKey); }
  }

  function updateWorkflow(stage) {
    const ids = ["step-source", "step-story", "step-deliver"];
    ids.forEach((id, index) => { const item = $(id); if (index === stage) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current"); item.classList.toggle("is-done", index < stage); });
  }

  function paintJob() {
    const job = state.job;
    if (!job) return;
    const active = activeStatuses.has(job.status);
    const complete = job.status === "complete";
    const recoverable = ["failed", "cancelled"].includes(job.status);
    const verified = hasVerifiedResult(job);
    $("project-name").textContent = state.draft && state.draft.title || job.plan && job.plan.title || state.sourceName || "Untitled project";
    $("result").textContent = job.message || labels[job.status] || "Your project is saved.";
    $("project-status").textContent = labels[job.status] || job.status;
    $("project-status").className = "status-pill" + (active ? " is-working" : complete ? " is-complete" : recoverable ? " is-failed" : "");
    $("job-progress").hidden = !active;
    $("cancel-job").hidden = !active;
    $("cancel-job").textContent = job.cancel_requested ? "Stopping…" : "Cancel";
    $("busy").textContent = labels[job.status] || "Working on your project";
    $("progress-detail").textContent = job.message || "Your project is saved as it progresses.";
    $("progress-stage").textContent = String(job.stage || job.status).replace(/[_-]/g, " ");
    $("progress-bar").value = Math.max(0, Math.min(1, finite(job.progress, 0)));
    $("clip-name").textContent = state.sourceName;
    $("source-badge").textContent = state.duration ? durationText(state.duration) : "Saved locally";
    $("drop-title").textContent = "Change footage";
    $("save-status").textContent = "Project saved";
    $("budget-status").hidden = !(job.request && job.request.provider === "meta" && job.budget);
    if (job.budget) $("budget-status").textContent = budgetText(job.budget);
    $("recovery-panel").hidden = !recoverable;
    const centerCrop = recoverable && job.recovery && job.recovery.option === "center_crop";
    $("render-center-crop").hidden = !centerCrop;
    $("recovery-message").textContent = recoverable ? errorMessage(centerCrop && job.recovery.message || job.error || job.message || "This job was interrupted. Try again using the saved footage.") : "";
    $("delivery-panel").hidden = !verified;
    $("preview-final").disabled = !verified;
    $("preview-final").textContent = finalLabel(job);
    if (state.preview === "final" && verified) {
      $("preview-label").textContent = finalLabel(job) + " · Version " + verifiedRevision(job);
      updateMonitorMeta();
    }
    updateWorkflow(complete ? 2 : job.plan ? 1 : 0);
    if (job.plan && state.draft) {
      $("story-description").textContent = job.plan.summary || "Review and refine the moments before rendering.";
      $("say-pauses").textContent = state.draft.segments.length + (state.draft.segments.length === 1 ? " selected moment" : " selected moments");
      $("say-shape").textContent = (job.request && job.request.aspect || "9:16") + " output";
      $("say-captions").textContent = job.request && job.request.captions ? (job.request.caption_provider === "meta" ? "Captions by Meta Voice Transcribe" : "Local captions requested") : "Original audio";
      $("say-clip").textContent = (job.request && job.request.provider) === "meta" ? "Story proposed by Meta" : analysisOf(job) ? "Local analysis · Review the selection" : "Local starting cut · Review the selection";
      paintAnalysis(job);
      paintEffectSummary();
      $("render-note").textContent = state.settingsDirty ? "Settings changed. Create a story to apply them before rendering." : complete ? "Changes make a new render of this project." : "Your story. Your final say.";
      paintWarnings("story-warnings", [].concat(job.warnings || [], job.plan.warnings || []));
    }
    if (verified) paintDelivery(job.result);
    setControls();
  }

  function paintWarnings(id, warnings) {
    const box = $(id); box.replaceChildren();
    const messages = warnings.filter((value) => typeof value === "string" && value);
    box.hidden = !messages.length;
    messages.forEach((message) => { const p = document.createElement("p"); p.textContent = message; box.append(p); });
  }

  function budgetText(budget) {
    const labels = { story: "Story", tracking: "Tracking", cover: "Cover" };
    const operations = budget.operations || {};
    const parts = Object.keys(labels).concat(Object.keys(operations).filter((name) => !labels[name])).map((name) => {
      const entry = operations[name];
      if (!entry || !finite(entry.calls, 0)) return null;
      return (labels[name] || name) + " $" + finite(entry.reserved_usd, 0).toFixed(3) + (entry.model ? " (" + entry.model + ")" : "");
    }).filter(Boolean);
    const total = "$" + finite(budget.reserved_usd, 0).toFixed(3) + " reserved of $" + finite(budget.cap_usd, 0).toFixed(2) + " limit.";
    return total + (parts.length ? " By operation: " + parts.join(", ") + "." : " No Meta calls recorded yet.") + " Reservations include calls whose billing is not yet known.";
  }

  function paintDelivery(result) {
    const proof = result.proof || {};
    const previous = previousResult(state.job);
    const revision = verifiedRevision(state.job);
    $("delivery-panel").classList.toggle("is-previous", previous);
    $("delivery-heading").textContent = previous ? "Your previous version is safe." : "Ready for the world.";
    $("verified-version").textContent = "Version " + revision + " · Verified";
    const descriptions = {
      queued: "The new version is queued. You can still preview and download this verified version.",
      rendering: "A new version is rendering. These downloads are from the previous verified version.",
      failed: "The latest render did not finish. This earlier version passed verification and is still available.",
      cancelled: "The latest render was cancelled. This earlier verified version is still available.",
    };
    $("delivery-description").textContent = previous ? descriptions[state.job.status] || "These files belong to the previous verified version, before your latest changes." : "Your video is rendered and verified. Take the whole campaign with you.";
    const links = [["download-bundle", "bundle_url", "bundle"], ["download-video", "video_url", "video"], ["download-cover", "cover_url", "cover"], ["download-report", "report_url", "report"]];
    links.forEach(([id, field, name]) => { const link = $(id); const url = versionedArtifact(result[field] || artifact(name)); link.hidden = !url; if (url) link.href = url; else link.removeAttribute("href"); });
    const cover = versionedArtifact(result.cover_url || artifact("cover"));
    $("cover-image").hidden = !cover;
    if (cover && $("cover-image").src !== cover) $("cover-image").src = cover;
    const renderedEffect = result.effect && state.effectPresets.find((item) => item.id === result.effect.preset);
    $("delivery-summary").textContent = durationText(proof.duration) + " video · " + (proof.width || "—") + " × " + (proof.height || "—") + " · " + (proof.has_audio ? "Audio included" : "Silent video") + (renderedEffect && result.effect.intensity > 0 ? " · " + renderedEffect.label : "");
    $("proof-duration").textContent = finite(proof.duration, 0).toFixed(2) + " seconds";
    $("proof-width").textContent = proof.width || "—";
    $("proof-height").textContent = proof.height || "—";
    $("proof-has_audio").textContent = proof.has_audio ? "Present" : "No audio track";
    $("proof-has_video").textContent = proof.has_video ? "Present" : "No video track";
    $("proof-color").textContent = [proof.color_space || proof.color && proof.color.space, proof.color_range || proof.color && proof.color.range].filter(Boolean).join(" / ") || "Not tagged";
    const trackingOff = state.job && state.job.request && state.job.request.tracking_disabled && state.job.request.subject;
    $("proof-subject").textContent = result.tracked_subject ? result.tracked_subject : trackingOff ? "Off (center crop)" : "None requested";
    paintWarnings("delivery-warnings", result.warnings || []);
    paintRunFacts(state.job, result);
    paintDirectorNotes(result.review, state.job && state.job.request && state.job.request.review);
  }

  function paintDirectorNotes(review, requested) {
    const panel = $("director-notes");
    const show = !!(requested || review);
    panel.hidden = !show;
    if (!show) return;
    const error = review && typeof review.error === "string" ? review.error : (!review ? "The review did not run for this version." : "");
    $("director-error").hidden = !error;
    $("director-error").textContent = error;
    const ok = !!review && !error;
    $("director-verdict").textContent = ok ? review.verdict || "" : "";
    const score = ok ? Math.max(0, Math.min(5, Math.round(finite(review.hook_score, 0)))) : 0;
    $("hook-score").textContent = "";
    $("hook-score").hidden = !ok;
    if (ok) {
      for (let i = 1; i <= 5; i += 1) { const dot = element("i", i <= score ? "is-on" : ""); dot.setAttribute("aria-hidden", "true"); $("hook-score").appendChild(dot); }
      $("hook-score").appendChild(element("span", "", "Hook " + score + "/5"));
    }
    const strengths = $("director-strengths"); strengths.textContent = "";
    (ok && Array.isArray(review.strengths) ? review.strengths : []).forEach((text) => strengths.appendChild(element("li", "", String(text))));
    if (ok && !strengths.children.length) strengths.appendChild(element("li", "muted", "No specific strengths listed."));
    const fixes = $("director-fixes"); fixes.textContent = "";
    (ok && Array.isArray(review.fixes) ? review.fixes : []).forEach((fix) => {
      const item = element("li", "", "");
      const where = fix && Number.isFinite(Number(fix.where_seconds)) && fix.where_seconds !== null ? Number(fix.where_seconds) : null;
      if (where !== null) {
        const jump = element("button", "text-button fix-time", time(where) + "." + String(Math.round((where % 1) * 10)));
        jump.type = "button"; jump.title = "Jump to this moment in the finished video";
        jump.addEventListener("click", () => { if (state.preview !== "final") setPreview("final"); const player = $("player"); const seek = () => { player.currentTime = Math.max(0, where); player.play().catch(() => {}); }; if (player.readyState >= 1) seek(); else player.addEventListener("loadedmetadata", seek, { once: true }); });
        item.appendChild(jump); item.appendChild(document.createTextNode(" "));
      }
      item.appendChild(document.createTextNode(String(fix && fix.what || "")));
      fixes.appendChild(item);
    });
    if (ok && !fixes.children.length) fixes.appendChild(element("li", "muted", "Nothing flagged."));
    const caption = ok && typeof review.caption_suggestion === "string" ? review.caption_suggestion.trim() : "";
    $("director-caption-wrap").hidden = !caption;
    $("director-caption").textContent = caption;
  }

  function setPreview(version) {
    state.preview = version; state.rangeEnd = null;
    const result = state.job && state.job.result;
    const url = version === "final" && result ? safeArtifact(result.video_url || artifact("video")) : state.sourceURL;
    $("preview-source").setAttribute("aria-pressed", String(version === "source"));
    $("preview-final").setAttribute("aria-pressed", String(version === "final"));
    $("preview-label").textContent = version === "final" ? finalLabel(state.job) + " · Version " + verifiedRevision(state.job) : "Original footage";
    $("monitor-empty").hidden = !!url;
    $("player").hidden = !url;
    $("monitor-picture").classList.toggle("has-video", !!url);
    if (version !== "source") state.loop = false;
    $("scrubber").hidden = version !== "source" || !url || !state.draft;
    if (url) {
      const keyed = version === "final" ? versionedArtifact(url) : url;
      if ($("player").getAttribute("src") !== keyed) { $("player").pause(); $("player").src = keyed; $("player").load(); }
    } else { $("player").pause(); $("player").removeAttribute("src"); $("player").load(); }
    updateMonitorMeta();
  }

  function updateMonitorMeta() {
    const source = state.job && state.job.source || {};
    const proof = state.job && state.job.result && state.job.result.proof || {};
    const info = state.preview === "final" ? proof : source;
    const video = $("player");
    const w = finite(info.width, video.videoWidth || 0), h = finite(info.height, video.videoHeight || 0);
    $("monitor-meta").textContent = w && h ? w + " × " + h + (state.preview === "final" ? " · " + (previousResult(state.job) ? "Previous version" : "Finished") : " · Source") : state.sourceURL ? "Source preview" : "Your canvas awaits";
  }

  function seekSegment(index, options) {
    if (!state.draft || !state.draft.segments[index]) return;
    const segment = state.draft.segments[index];
    if (state.preview !== "source") setPreview("source");
    const again = state.selected === index;
    state.loop = options && options.keepLoop ? state.loop && again : again ? !state.loop : false;
    state.selected = index;
    const player = $("player");
    const seek = () => { player.currentTime = finite(segment.start, 0); state.rangeEnd = finite(segment.end, null); const playing = player.play(); if (playing && playing.catch) playing.catch(() => { toast("Press play to preview this moment."); }); };
    if (player.readyState >= 1) seek(); else player.addEventListener("loadedmetadata", seek, { once: true });
    $("program-list").querySelectorAll(".segment").forEach((row, i) => row.classList.toggle("is-selected", index === i));
    $("timeline").querySelectorAll("button").forEach((button, i) => { button.classList.toggle("is-selected", index === i); button.setAttribute("aria-pressed", String(index === i)); });
    $("preview-label").textContent = "Moment " + (index + 1) + " · " + (segment.label || "Selected footage") + (state.loop ? " · Looping" : "");
    paintScrubSelection();
    announce("Previewing " + (segment.label || "moment " + (index + 1)) + " from " + time(segment.start) + " to " + time(segment.end));
  }

  function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
  function iconButton(label, text, className, action) { const button = element("button", "icon-button " + className, text); button.type = "button"; button.title = label; button.setAttribute("aria-label", label); button.addEventListener("click", action); return button; }

  function renderStory() {
    const plan = state.draft;
    $("story-content").hidden = !plan;
    $("story-empty").hidden = !!plan;
    $("story-duration").hidden = !plan;
    $("program-list").replaceChildren(); $("timeline").replaceChildren();
    if (!plan) return;
    $("story-title").value = plan.title || "";
    $("post-caption").value = plan.caption || "";
    $("hashtags").value = (plan.hashtags || []).map((tag) => String(tag).startsWith("#") ? tag : "#" + tag).join(" ");
    plan.segments.forEach((segment, index) => {
      const row = element("li", "segment" + (state.selected === index ? " is-selected" : ""));
      const top = element("div", "segment-top");
      top.append(element("span", "segment-number", String(index + 1).padStart(2, "0")));
      const label = element("input", "segment-label"); label.type = "text"; label.value = segment.label || "Moment " + (index + 1); label.maxLength = 100; label.setAttribute("aria-label", "Title of moment " + (index + 1));
      label.addEventListener("input", () => { segment.label = label.value; saveDraft(); renderTimeline(); const block = $("scrub-moments").children[index]; if (block) block.firstChild.textContent = (index + 1) + " " + (segment.label || "Moment"); });
      top.append(label);
      const kind = kindLabel(segment); if (kind) top.append(element("span", "segment-kind is-" + (segment.kind || "scene"), kind));
      const actions = element("div", "segment-actions");
      actions.append(iconButton("Move moment " + (index + 1) + " earlier", "↑", "move-up", () => moveSegment(index, -1)));
      actions.append(iconButton("Move moment " + (index + 1) + " later", "↓", "move-down", () => moveSegment(index, 1)));
      actions.append(iconButton("Remove moment " + (index + 1), "×", "remove-segment", () => { if (plan.segments.length <= 1) return; plan.segments.splice(index, 1); state.selected = -1; saveDraft(); renderStory(); const survivor = $("program-list").children[Math.min(index, plan.segments.length - 1)]; if (survivor) survivor.querySelector(".segment-label").focus(); announce("Moment " + (index + 1) + " removed. " + plan.segments.length + " moments remain."); }));
      top.append(actions); row.append(top);
      const bottom = element("div", "segment-bottom"); const timing = element("div", "segment-time");
      for (const key of ["start", "end"]) {
        if (key === "end") timing.append(element("span", "to", "–"));
        const inputId = "segment-" + index + "-" + key;
        const timeLabel = element("label", "", key === "start" ? "In" : "Out"); timeLabel.htmlFor = inputId;
        const input = element("input", ""); input.id = inputId; input.type = "number"; input.min = "0"; input.step = "0.1"; input.value = String(Math.round(finite(segment[key], 0) * 100) / 100); if (state.duration) input.max = String(state.duration); input.setAttribute("aria-label", "Moment " + (index + 1) + " " + key + " in seconds");
        input.addEventListener("input", () => { segment[key] = input.value === "" ? null : Number(input.value); input.removeAttribute("aria-invalid"); input.removeAttribute("aria-describedby"); saveDraft(); renderTimeline(); const block = $("scrub-moments").children[index]; if (block && segment.start !== null && segment.end !== null) { block.style.left = pct(segment.start) + "%"; block.style.width = Math.max(.6, pct(segment.end) - pct(segment.start)) + "%"; } highlightTranscript(); });
        timing.append(timeLabel, input);
      }
      bottom.append(timing);
      bottom.append(element("p", "segment-reason", segment.reason || "Your selected source range."));
      const preview = element("button", "segment-preview", "Preview"); preview.type = "button"; preview.setAttribute("aria-label", "Preview moment " + (index + 1)); preview.addEventListener("click", () => seekSegment(index)); bottom.append(preview);
      row.append(bottom); $("program-list").append(row);
    });
    renderTimeline(); updateStoryDuration(); renderScrubber(); renderTranscript(); setControls();
  }

  function renderTimeline() {
    $("timeline").replaceChildren();
    if (!state.draft) return;
    state.draft.segments.forEach((segment, index) => {
      const button = element("button", state.selected === index ? "is-selected" : "", (index + 1) + "  " + (segment.label || "Moment"));
      button.type = "button"; button.style.flexGrow = String(Math.max(.1, finite(segment.end, 0) - finite(segment.start, 0))); button.title = (segment.label || "Moment") + " · " + time(segment.start) + " – " + time(segment.end); button.setAttribute("aria-label", "Preview moment " + (index + 1) + ": " + (segment.label || "Selected footage")); button.setAttribute("aria-pressed", String(state.selected === index)); button.addEventListener("click", () => seekSegment(index)); $("timeline").append(button);
    });
  }

  function updateStoryDuration() {
    if (!state.draft) return;
    const total = state.draft.segments.reduce((sum, segment) => sum + Math.max(0, finite(segment.end, 0) - finite(segment.start, 0)), 0);
    $("story-duration").textContent = durationText(total) + " selected";
    $("say-pauses").textContent = state.draft.segments.length + (state.draft.segments.length === 1 ? " selected moment" : " selected moments");
    paintEffectSummary();
  }

  function paintEffectSummary() {
    const effect = readEffect();
    const preset = effect && state.effectPresets.find((item) => item.id === effect.preset);
    $("say-effects").textContent = preset && effect.intensity > 0 ? preset.label + " · " + durationText(effect.duration) + " from " + time(effect.start) : "";
  }

  function moveSegment(index, offset) {
    if (isActive() || !state.draft) return;
    const dest = index + offset; const segments = state.draft.segments;
    if (dest < 0 || dest >= segments.length) return;
    [segments[index], segments[dest]] = [segments[dest], segments[index]];
    state.selected = dest; saveDraft(); renderStory();
    const focus = $("program-list").children[dest].querySelector(".segment-label"); if (focus) focus.focus();
  }

  function addSegment(range) {
    if (isActive() || !state.draft || state.draft.segments.length >= 8) { if (state.draft && state.draft.segments.length >= 8) toast("A story holds up to eight moments."); return; }
    const segments = state.draft.segments; const end = segments.length ? finite(segments[segments.length - 1].end, 0) : 0;
    const start = range ? range.start : end < state.duration - .1 ? end : 0;
    const stop = range ? range.end : Math.min(state.duration || start + 5, start + 5);
    segments.push({ start: Math.round(start * 100) / 100, end: Math.round(stop * 100) / 100, label: range && range.label || "New moment", reason: range && range.reason || "Added by you.", kind: range && range.kind || undefined });
    state.selected = segments.length - 1;
    saveDraft(); renderStory(); const last = $("program-list").lastElementChild; if (last && !(range && range.quiet)) last.querySelector(".segment-label").focus();
    if (range) announce("Added moment " + segments.length + " from " + time(start) + " to " + time(stop));
  }

  function validateDraft() {
    if (!state.draft || !state.draft.segments.length) throw new Error("Keep at least one moment in the story.");
    const invalid = (message, id) => { const error = new Error(message); error.fieldId = id; throw error; };
    state.draft.segments.forEach((segment, index) => {
      if (segment.start === null || !Number.isFinite(Number(segment.start)) || Number(segment.start) < 0) invalid("Moment " + (index + 1) + " needs an in time of zero or more seconds.", "segment-" + index + "-start");
      if (segment.end === null || !Number.isFinite(Number(segment.end)) || Number(segment.end) - Number(segment.start) < .099999) invalid("Moment " + (index + 1) + " must end at least 0.1 seconds after its in time.", "segment-" + index + "-end");
      if (state.duration && Number(segment.end) > state.duration + .001) invalid("Moment " + (index + 1) + " ends after the source footage. Choose an out time below " + state.duration.toFixed(2) + " seconds.", "segment-" + index + "-end");
    });
    if (state.draft.segments.reduce((total, segment) => total + segment.end - segment.start, 0) > 180.05) invalid("Keep the story under 180 seconds. Shorten or remove a moment.", "segment-" + (state.draft.segments.length - 1) + "-end");
    if (!state.draft.title.trim()) invalid("Give your campaign a title before rendering.", "story-title");
    if (state.draft.hashtags.length > 15 || state.draft.hashtags.some((tag) => tag.length > 80)) invalid("Use at most 15 hashtags, each under 80 characters.", "hashtags");
    const effect = readEffect();
    if (effect && effect.intensity > 0) {
      const length = state.draft.segments.reduce((total, segment) => total + segment.end - segment.start, 0);
      if (!Number.isFinite(effect.start) || effect.start < 0 || effect.start >= length) invalid("Start the effect inside your finished story, before " + durationText(length) + ".", "effect-start");
      if (!Number.isFinite(effect.duration) || effect.duration <= 0 || effect.start + effect.duration > length + .001) invalid("The effect must fit inside the " + durationText(length) + " story. Shorten its duration or move its start.", "effect-duration");
    }
    return clone(state.draft);
  }

  async function renderCampaign(options) {
    const overrides = options || {};
    if (isActive() || !state.job) return;
    if (state.settingsDirty) { showError("Create a story to apply your changed settings before rendering."); return; }
    clearError();
    let plan;
    try { plan = validateDraft(); } catch (error) { showError(error); const field = error.fieldId && $(error.fieldId); if (field) { field.setAttribute("aria-invalid", "true"); field.setAttribute("aria-describedby", "error-text"); if (field.closest("details")) field.closest("details").open = true; field.focus(); } return; }
    state.busy = true; setControls();
    const id = state.job.id;
    try {
      const body = { plan, effect: readEffect() };
      if (typeof overrides.tracking === "boolean") body.tracking = overrides.tracking;
      const data = await request("/api/campaigns/" + encodeURIComponent(id) + "/render", { method: "POST", body });
      if (!data.job) throw new Error("The render was not started. Your story is still saved.");
      storageRemove(draftKey(id));
      markTiming(id, "renderStart", null); markTiming(id, "renderEnd", null); markTiming(id, "renderStart", Date.now());
      if (data.job.plan) { state.draft = clone(data.job.plan); renderStory(); }
      state.jobStarted = Date.now(); adoptJob(data.job, true);
      $("job-progress").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
    } catch (error) { showError(error); }
    finally { state.busy = false; setControls(); }
  }

  async function jobAction(action) {
    if (state.busy || !state.job) return;
    clearError(); state.busy = true; setControls();
    if (action === "cancel") { $("cancel-job").textContent = "Stopping…"; $("progress-detail").textContent = hasVerifiedResult(state.job) ? "Stopping the current step. Your previous verified video stays available." : "Stopping the current processing step. Your footage and story are saved."; }
    try { const data = await request("/api/campaigns/" + encodeURIComponent(state.job.id) + "/" + action, { method: "POST", body: {} }); if (!data.job) throw new Error("The editor did not update this project."); adoptJob(data.job, true); }
    catch (error) { showError(error); }
    finally { state.busy = false; if (state.job) $("cancel-job").textContent = state.job.cancel_requested ? "Stopping…" : "Cancel"; setControls(); }
  }

  async function refreshRecent() {
    try {
      const data = await request("/api/campaigns");
      const jobs = Array.isArray(data.jobs) ? data.jobs : [];
      const list = $("recent-list"); list.replaceChildren();
      if (!jobs.length) list.append(element("li", "empty-recent", "Your projects will appear here."));
      jobs.slice(0, 20).forEach((job) => {
        const item = element("li"); const button = element("button", "recent-item"); button.type = "button";
        const title = job.plan && job.plan.title || job.source && job.source.name || job.request && job.request.goal || "Untitled project";
        button.append(element("strong", "", title));
        const date = timestamp(job.updated_at || job.created_at);
        button.append(element("span", "", (labels[job.status] || job.status) + (date ? " · " + new Date(date).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "")));
        button.addEventListener("click", () => openJob(job.id)); item.append(button); list.append(item);
      });
    } catch (_) { /* Current editing remains usable when history cannot load. */ }
  }

  function resetProject(clearForm) {
    stopPolling(); revokeSource();
    state.file = null; state.paths = []; state.sourceURL = ""; state.duration = 0; state.job = null; state.draft = null; state.selected = -1; state.sourceName = ""; state.rangeEnd = null; state.settingsSnapshot = ""; state.settingsDirty = false; state.pendingCreate = null; state.effectTimingEdited = false;
    storageRemove(storageKey);
    if (clearForm) { $("desk-form").reset(); $("clip").value = ""; $("language-field").hidden = true; updateMode(); updateEffectControls(); }
    $("project-name").textContent = "Untitled project"; $("result").textContent = "A little raw footage. A story worth sharing."; $("project-status").textContent = "Ready to start"; $("project-status").className = "status-pill";
    $("clip-name").textContent = "The original stays untouched."; $("drop-title").textContent = "Choose your footage"; $("source-badge").textContent = "No clip selected";
    $("job-progress").hidden = true; $("delivery-panel").hidden = true; $("delivery-panel").classList.remove("is-previous"); $("recovery-panel").hidden = true; $("preview-final").disabled = true; $("preview-final").textContent = "Finished video"; $("budget-status").hidden = true; $("save-status").textContent = ""; $("story-description").textContent = "Review the moments before you make the video.";
    state.loop = false; $("scrubber").hidden = true; $("transcript-panel").hidden = true; $("analysis-badge").hidden = true; $("analysis-notes").hidden = true; $("run-facts").hidden = true;
    clearError(); renderStory(); setPreview("source"); updateWorkflow(0); setControls();
  }

  async function copyPost() {
    if (!state.draft) return;
    const text = [state.draft.caption, (state.draft.hashtags || []).map((tag) => String(tag).startsWith("#") ? tag : "#" + tag).join(" ")].filter(Boolean).join("\n\n");
    try { await navigator.clipboard.writeText(text); toast("Post copy copied."); }
    catch (_) { $("post-caption").focus(); $("post-caption").select(); toast("Select and copy the post text from the field."); }
  }


  /* ---------- Analysis, scrubber, transcript ---------- */
  function analysisOf(job) {
    const plan = job && job.plan;
    const analysis = plan && plan.analysis;
    return analysis && typeof analysis === "object" ? analysis : null;
  }
  function kindLabel(segment) {
    const names = { hook: "Hook", speech: "Speech", scene: "Scene", opening: "Opening", closing: "Closing" };
    const kind = segment && typeof segment.kind === "string" ? names[segment.kind] : "";
    const score = segment && Number.isFinite(Number(segment.score)) ? Number(segment.score) : null;
    const strength = score === null ? "" : score >= .75 ? "strong" : score >= .45 ? "solid" : "soft";
    if (!kind && !strength) return "";
    return [kind, strength].filter(Boolean).join(" · ");
  }
  function paintAnalysis(job) {
    const analysis = analysisOf(job);
    const meta = (job.request && job.request.provider) === "meta" || (analysis && analysis.method === "meta");
    const badge = $("analysis-badge");
    badge.hidden = !(analysis || meta);
    badge.textContent = meta ? "Meta director" : "Local analysis";
    badge.classList.toggle("is-meta", !!meta);
    badge.title = meta ? "Moments proposed by Muse Spark from the footage. Review every range." : "Moments chosen from scene cuts, speech, and your brief’s words. Deterministic, not AI understanding.";
    const notes = analysis && Array.isArray(analysis.notes) ? analysis.notes.filter((note) => typeof note === "string" && note) : [];
    $("analysis-notes").hidden = !notes.length;
    $("analysis-notes").textContent = notes.join(" ");
  }
  function sourceLength() { return state.duration || (state.job && state.job.source && finite(state.job.source.duration, 0)) || 0; }
  function pct(seconds) { const total = sourceLength(); return total ? Math.max(0, Math.min(100, seconds / total * 100)) : 0; }

  function renderScrubber() {
    const total = sourceLength();
    const show = !!state.draft && total > 0 && state.preview === "source" && !!state.sourceURL;
    $("scrubber").hidden = !show;
    if (!show) return;
    const analysis = analysisOf(state.job) || {};
    const speech = $("scrub-speech"); speech.replaceChildren();
    const speechRanges = Array.isArray(analysis.speech) ? analysis.speech.slice(0, 400) : [];
    speechRanges.forEach((range) => { if (!range || !Number.isFinite(Number(range.start)) || !Number.isFinite(Number(range.end))) return; const band = element("span"); band.style.left = pct(range.start) + "%"; band.style.width = Math.max(.15, pct(range.end) - pct(range.start)) + "%"; speech.append(band); });
    $("key-speech").hidden = !speech.childElementCount;
    const scenes = $("scrub-scenes"); scenes.replaceChildren();
    (Array.isArray(analysis.scenes) ? analysis.scenes.slice(0, 400) : []).forEach((at) => { if (!Number.isFinite(Number(at))) return; const tick = element("span"); tick.style.left = pct(at) + "%"; scenes.append(tick); });
    $("key-scene").hidden = !scenes.childElementCount;
    const moments = $("scrub-moments"); moments.replaceChildren();
    state.draft.segments.forEach((segment, index) => {
      const start = finite(segment.start, 0), end = finite(segment.end, start);
      const block = element("div", "scrub-moment" + (state.selected === index ? " is-selected" : ""), (index + 1) + " " + (segment.label || "Moment"));
      block.style.left = pct(start) + "%"; block.style.width = Math.max(.6, pct(end) - pct(start)) + "%";
      block.tabIndex = 0; block.dataset.index = String(index);
      block.setAttribute("role", "button"); block.setAttribute("aria-label", "Moment " + (index + 1) + ", " + (segment.label || "Moment") + ", " + time(start) + " to " + time(end) + ". Press Enter to preview, arrows to nudge.");
      block.title = (segment.label || "Moment") + " · " + start.toFixed(1) + "s – " + end.toFixed(1) + "s";
      for (const edge of ["start", "end"]) {
        const handle = element("button", "scrub-handle is-" + edge); handle.type = "button"; handle.dataset.edge = edge; handle.dataset.index = String(index);
        handle.setAttribute("aria-label", (edge === "start" ? "In point" : "Out point") + " of moment " + (index + 1) + ", " + (edge === "start" ? start : end).toFixed(1) + " seconds. Arrows adjust.");
        handle.addEventListener("keydown", (event) => handleEdgeKey(event, index, edge));
        handle.addEventListener("click", (event) => event.stopPropagation());
        block.append(handle);
      }
      block.addEventListener("keydown", (event) => handleBlockKey(event, index));
      moments.append(block);
    });
    paintPlayhead();
  }
  function paintScrubSelection() { $("scrub-moments").querySelectorAll(".scrub-moment").forEach((block, i) => block.classList.toggle("is-selected", i === state.selected)); }
  function paintPlayhead() {
    if ($("scrubber").hidden) return;
    const video = $("player");
    $("scrub-playhead").style.left = pct(finite(video.currentTime, 0)) + "%";
  }
  function trackSeconds(clientX) {
    const rect = $("scrub-track").getBoundingClientRect();
    if (!rect.width) return 0;
    return Math.max(0, Math.min(sourceLength(), (clientX - rect.left) / rect.width * sourceLength()));
  }
  function seekTo(seconds, play) {
    const player = $("player");
    if (state.preview !== "source") setPreview("source");
    state.rangeEnd = null; state.loop = false;
    player.currentTime = Math.max(0, Math.min(sourceLength() || seconds, seconds));
    if (play) { const p = player.play(); if (p && p.catch) p.catch(() => {}); }
    paintPlayhead();
  }
  function scrubPointerDown(event) {
    if (event.button !== 0) return;
    const handle = event.target.closest(".scrub-handle");
    const block = event.target.closest(".scrub-moment");
    if (!state.draft) return;
    if (!block) { seekTo(trackSeconds(event.clientX), false); return; }
    const index = Number(block.dataset.index);
    const segment = state.draft.segments[index];
    if (!segment) return;
    if (isActive()) { seekSegment(index, { keepLoop: true }); return; }
    event.preventDefault();
    const origin = { x: event.clientX, start: finite(segment.start, 0), end: finite(segment.end, 0), mode: handle ? handle.dataset.edge : "move", index, moved: false, target: handle || block };
    state.drag = origin;
    origin.target.setPointerCapture(event.pointerId);
    block.classList.add("is-dragging");
    const onMove = (move) => {
      const total = sourceLength(); const rect = $("scrub-track").getBoundingClientRect();
      if (!rect.width || !total) return;
      const delta = (move.clientX - origin.x) / rect.width * total;
      if (Math.abs(move.clientX - origin.x) > 2) origin.moved = true;
      if (!origin.moved) return;
      let start = origin.start, end = origin.end;
      if (origin.mode === "move") { const length = end - start; start = Math.max(0, Math.min(total - length, start + delta)); end = start + length; }
      else if (origin.mode === "start") start = Math.max(0, Math.min(end - .1, start + delta));
      else end = Math.min(total, Math.max(start + .1, end + delta));
      applySegmentChange(index, start, end, false);
      const player = $("player"); player.currentTime = origin.mode === "end" ? end : start; paintPlayhead();
    };
    const onUp = () => {
      origin.target.removeEventListener("pointermove", onMove); origin.target.removeEventListener("pointerup", onUp); origin.target.removeEventListener("pointercancel", onUp);
      block.classList.remove("is-dragging"); state.drag = null;
      if (origin.moved) { applySegmentChange(index, state.draft.segments[index].start, state.draft.segments[index].end, true); announce("Moment " + (index + 1) + " now runs " + time(state.draft.segments[index].start) + " to " + time(state.draft.segments[index].end)); }
      else if (!handle) seekSegment(index);
    };
    origin.target.addEventListener("pointermove", onMove); origin.target.addEventListener("pointerup", onUp); origin.target.addEventListener("pointercancel", onUp);
  }
  function applySegmentChange(index, start, end, commit) {
    const segment = state.draft && state.draft.segments[index];
    if (!segment) return;
    segment.start = Math.round(start * 100) / 100; segment.end = Math.round(end * 100) / 100;
    const row = $("program-list").children[index];
    if (row) { const s = row.querySelector("#segment-" + index + "-start"), e = row.querySelector("#segment-" + index + "-end"); if (s) s.value = String(segment.start); if (e) e.value = String(segment.end); }
    const block = $("scrub-moments").children[index];
    if (block) { block.style.left = pct(segment.start) + "%"; block.style.width = Math.max(.6, pct(segment.end) - pct(segment.start)) + "%"; block.title = (segment.label || "Moment") + " · " + segment.start.toFixed(1) + "s – " + segment.end.toFixed(1) + "s"; }
    renderTimeline(); updateStoryDuration();
    if (commit) { saveDraft(); highlightTranscript(); }
  }
  function nudgeSegment(index, deltaStart, deltaEnd) {
    const segment = state.draft && state.draft.segments[index];
    if (!segment || isActive()) return;
    const total = sourceLength() || Infinity;
    let start = finite(segment.start, 0) + deltaStart, end = finite(segment.end, 0) + deltaEnd;
    start = Math.max(0, start); end = Math.min(total, end);
    if (end - start < .1) { if (deltaStart) start = end - .1; else end = start + .1; }
    applySegmentChange(index, start, end, true);
    seekTo(deltaEnd && !deltaStart ? end : start, false);
  }
  function handleBlockKey(event, index) {
    const step = event.shiftKey ? 1 : .1;
    if (event.key === "ArrowLeft") { event.preventDefault(); event.stopPropagation(); nudgeSegment(index, -step, -step); }
    else if (event.key === "ArrowRight") { event.preventDefault(); event.stopPropagation(); nudgeSegment(index, step, step); }
    else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); seekSegment(index); }
  }
  function handleEdgeKey(event, index, edge) {
    const step = event.shiftKey ? 1 : .1;
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault(); event.stopPropagation();
    const delta = event.key === "ArrowLeft" ? -step : step;
    nudgeSegment(index, edge === "start" ? delta : 0, edge === "end" ? delta : 0);
    const block = $("scrub-moments").children[index]; const handle = block && block.querySelector(".scrub-handle.is-" + edge); if (handle) handle.focus();
  }

  function transcriptCues() {
    const analysis = analysisOf(state.job);
    const cues = analysis && Array.isArray(analysis.transcript) ? analysis.transcript : [];
    return cues.filter((cue) => cue && typeof cue.text === "string" && cue.text.trim() && Number.isFinite(Number(cue.start)) && Number.isFinite(Number(cue.end))).slice(0, 600);
  }
  function renderTranscript() {
    const cues = transcriptCues();
    const panel = $("transcript-panel");
    panel.hidden = !cues.length || !state.draft;
    if (panel.hidden) return;
    $("transcript-count").textContent = cues.length + (cues.length === 1 ? " line" : " lines") + " · local speech recognition";
    const list = $("transcript-list"); list.replaceChildren();
    cues.forEach((cue, index) => {
      const item = element("li", "transcript-cue"); item.dataset.start = String(cue.start); item.dataset.end = String(cue.end); item.dataset.text = cue.text.toLowerCase();
      const at = element("button", "cue-time", time(cue.start)); at.type = "button"; at.setAttribute("aria-label", "Jump to " + time(cue.start)); at.addEventListener("click", () => seekTo(Number(cue.start), true));
      const text = element("span", "cue-text"); text.textContent = cue.text.trim();
      const add = element("button", "cue-add", "Add as moment"); add.type = "button"; add.setAttribute("aria-label", "Add line " + (index + 1) + " as a moment");
      add.addEventListener("click", () => addSegment({ start: Number(cue.start), end: Math.max(Number(cue.start) + .5, Number(cue.end)), label: cue.text.trim().split(/\s+/).slice(0, 5).join(" "), reason: "Spoken at " + time(cue.start) + ": “" + cue.text.trim() + "”", kind: "speech", quiet: true }));
      item.append(at, text, add); list.append(item);
    });
    highlightTranscript(); filterTranscript();
  }
  function highlightTranscript() {
    if ($("transcript-panel").hidden || !state.draft) return;
    const segments = state.draft.segments;
    $("transcript-list").querySelectorAll(".transcript-cue").forEach((item) => {
      const start = Number(item.dataset.start), end = Number(item.dataset.end);
      item.classList.toggle("is-inside", segments.some((segment) => Number(segment.start) <= start + .05 && Number(segment.end) >= end - .05));
    });
  }
  function filterTranscript() {
    const query = state.transcriptQuery;
    $("transcript-list").querySelectorAll(".transcript-cue").forEach((item) => {
      const text = item.querySelector(".cue-text"); const raw = text.textContent;
      const hit = !query || item.dataset.text.includes(query);
      item.hidden = !hit;
      if (query && hit) { const at = item.dataset.text.indexOf(query); text.replaceChildren(raw.slice(0, at), Object.assign(document.createElement("mark"), { textContent: raw.slice(at, at + query.length) }), raw.slice(at + query.length)); }
      else text.textContent = raw;
    });
  }
  function paintCurrentCue() {
    if ($("transcript-panel").hidden || state.preview !== "source") return;
    const now = $("player").currentTime;
    $("transcript-list").querySelectorAll(".transcript-cue").forEach((item) => item.classList.toggle("is-current", Number(item.dataset.start) <= now && Number(item.dataset.end) > now));
  }

  /* ---------- Run facts ---------- */
  function timingKey(id) { return "vedit.studio.timing." + id; }
  function readTiming(id) { try { return JSON.parse(storageGet(timingKey(id)) || "{}") || {}; } catch (_) { return {}; } }
  function markTiming(id, field, value) { const data = readTiming(id); if (value === null) delete data[field]; else if (!data[field]) data[field] = value; else return; storageSet(timingKey(id), JSON.stringify(data)); }
  function noteTiming(job, previous) {
    if (!job || !job.id) return;
    const was = previous && previous.id === job.id ? previous.status : "";
    if (job.status === "planned" && was !== "planned") markTiming(job.id, "planEnd", Date.now());
    if (job.status === "complete" && was !== "complete") markTiming(job.id, "renderEnd", Date.now());
    if (activeStatuses.has(job.status) && !was && job.revision > 0) markTiming(job.id, "renderStart", timestamp(job.updated_at) || Date.now());
  }
  function seconds(value) { return Number.isFinite(value) && value >= 0 ? (value < 10 ? value.toFixed(1) : Math.round(value)) + " s" : "—"; }
  function paintRunFacts(job, result) {
    const facts = $("run-facts");
    if (!job) { facts.hidden = true; return; }
    facts.hidden = false;
    const server = job.timings && typeof job.timings === "object" ? job.timings : {};
    const local = readTiming(job.id);
    const planSeconds = Number.isFinite(Number(server.plan_seconds)) ? Number(server.plan_seconds) : local.planStart && local.planEnd ? (local.planEnd - local.planStart) / 1000 : NaN;
    const renderSeconds = Number.isFinite(Number(server.render_seconds)) ? Number(server.render_seconds) : local.renderStart && local.renderEnd ? (local.renderEnd - local.renderStart) / 1000 : NaN;
    const measured = (value, where) => { const node = $(where); node.textContent = seconds(value); if (Number.isFinite(value)) node.append(Object.assign(document.createElement("small"), { textContent: server.plan_seconds !== undefined || server.render_seconds !== undefined ? "measured by the editor" : "measured in this browser" })); };
    measured(planSeconds, "fact-plan"); measured(renderSeconds, "fact-render");
    const source = job.source || {}; const proof = result && result.proof || {};
    $("fact-shape").textContent = (source.duration ? durationText(source.duration) : "—") + (source.width && source.height ? " · " + source.width + "×" + source.height : "") + " → " + (proof.duration ? durationText(proof.duration) : "—") + (proof.width && proof.height ? " · " + proof.width + "×" + proof.height : "");
    const provider = job.request && job.request.provider === "meta" ? "Meta director (Muse Spark)" : analysisOf(job) ? "Local analysis" : "Local starting cut";
    $("fact-provider").textContent = provider;
    const captionProviderUsed = result && result.caption_provider || job.request && job.request.caption_provider;
    $("fact-captions").textContent = job.request && job.request.captions ? (captionProviderUsed === "meta" ? "Meta Voice Transcribe" : "Local Whisper") : "None";
    const spend = job.budget && Number.isFinite(Number(job.budget.reserved_usd)) ? Number(job.budget.reserved_usd) : 0;
    $("fact-spend").textContent = job.request && job.request.provider === "meta" || captionProviderUsed === "meta" ? "$" + spend.toFixed(3) + " reserved" : "$0.00";
  }

  /* ---------- Shortcuts ---------- */
  function openShortcuts() { const dialog = $("shortcuts"); if (typeof dialog.showModal === "function") { if (!dialog.open) dialog.showModal(); } else dialog.setAttribute("open", ""); }
  function editingTarget(target) { return !!target && (["INPUT", "TEXTAREA", "SELECT", "BUTTON", "SUMMARY", "A"].includes(target.tagName) && !target.classList.contains("scrub-moment") || target.isContentEditable); }
  function handleShortcut(event) {
    if (event.key === "Escape") { if ($("recent-projects").open) { $("recent-projects").open = false; $("recent-projects").querySelector("summary").focus(); } return; }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "?" && !editingTarget(event.target)) { event.preventDefault(); openShortcuts(); return; }
    if ($("shortcuts").open || editingTarget(event.target) || event.target.classList.contains("scrub-handle")) return;
    const player = $("player"); const hasVideo = !player.hidden && player.getAttribute("src");
    const key = event.key.toLowerCase();
    if (key === " " || key === "k") { if (!hasVideo) return; event.preventDefault(); if (player.paused) { const p = player.play(); if (p && p.catch) p.catch(() => {}); } else player.pause(); return; }
    if (key === "j" || key === "l") { if (!hasVideo) return; event.preventDefault(); state.rangeEnd = null; player.currentTime = Math.max(0, player.currentTime + (key === "j" ? -5 : 5)); paintPlayhead(); return; }
    if (key === "c") { if (!$("preview-final").disabled) { event.preventDefault(); setPreview(state.preview === "final" ? "source" : "final"); toast(state.preview === "final" ? "Finished video" : "Source footage"); } return; }
    if (/^[1-8]$/.test(key)) { if (state.draft && state.draft.segments[Number(key) - 1]) { event.preventDefault(); seekSegment(Number(key) - 1, { keepLoop: true }); const block = $("scrub-moments").children[Number(key) - 1]; if (block) block.focus(); } return; }
    if (!state.draft || state.selected < 0 || !state.draft.segments[state.selected]) return;
    const step = event.shiftKey ? 1 : .1;
    if (key === "i" || key === "o") {
      if (!hasVideo || state.preview !== "source") return;
      event.preventDefault();
      const segment = state.draft.segments[state.selected]; const at = player.currentTime;
      if (key === "i") applySegmentChange(state.selected, Math.min(at, finite(segment.end, at) - .1), finite(segment.end, at), true);
      else applySegmentChange(state.selected, finite(segment.start, 0), Math.max(at, finite(segment.start, 0) + .1), true);
      toast((key === "i" ? "In" : "Out") + " point set at " + time(at));
      return;
    }
    if (event.key === "ArrowLeft" && !event.target.classList.contains("scrub-moment")) { event.preventDefault(); nudgeSegment(state.selected, -step, -step); }
    else if (event.key === "ArrowRight" && !event.target.classList.contains("scrub-moment")) { event.preventDefault(); nudgeSegment(state.selected, step, step); }
  }

  $("desk-form").addEventListener("submit", createStory);
  $("make").addEventListener("click", renderCampaign);
  $("clip").addEventListener("change", () => selectFile($("clip").files && $("clip").files[0]));
  $("drop").addEventListener("dragover", (event) => { event.preventDefault(); if (!isActive()) $("drop").classList.add("is-hot"); });
  $("drop").addEventListener("dragleave", () => $("drop").classList.remove("is-hot"));
  $("drop").addEventListener("drop", (event) => { event.preventDefault(); $("drop").classList.remove("is-hot"); const file = event.dataTransfer && event.dataTransfer.files[0]; if (file) selectFile(file); });
  document.querySelectorAll(".prompt-chip").forEach((button) => button.addEventListener("click", () => { $("goal").value = button.dataset.goal; markSettingsChanged(); $("goal").focus(); }));
  $("desk-form").addEventListener("input", (event) => { if (event.target.id !== "clip" && event.target.id !== "cloud-consent") markSettingsChanged(); });
  $("desk-form").addEventListener("change", (event) => { if (event.target.id !== "clip" && event.target.id !== "cloud-consent") markSettingsChanged(); });
  $("provider-local").addEventListener("change", updateMode); $("provider-meta").addEventListener("change", updateMode);
  $("captions").addEventListener("change", () => { $("language-field").hidden = !$("captions").checked; updateCaptionProvider(); });
  $("caption-provider").addEventListener("change", updateCaptionProvider);
  $("effect-preset").addEventListener("change", () => { updateEffectControls(); saveDraft(); paintEffectSummary(); });
  $("effect-intensity").addEventListener("input", () => { updateEffectControls(); saveDraft(); paintEffectSummary(); });
  ["effect-start", "effect-duration"].forEach((id) => { $(id).addEventListener("input", () => { state.effectTimingEdited = true; saveDraft(); paintEffectSummary(); }); });
  $("target-duration").addEventListener("change", boundDefaultEffectDuration);
  $("new-project").addEventListener("click", () => { const running = isActive(); resetProject(true); if (running) toast("Your previous job keeps running. Find it in Recent projects."); $("clip").focus(); });
  $("dismiss-error").addEventListener("click", clearError);
  $("preview-source").addEventListener("click", () => setPreview("source"));
  $("preview-final").addEventListener("click", () => setPreview("final"));
  $("add-segment").addEventListener("click", addSegment);
  $("cancel-job").addEventListener("click", () => jobAction("cancel"));
  $("retry-job").addEventListener("click", () => jobAction("retry"));
  $("render-center-crop").addEventListener("click", () => renderCampaign({ tracking: false }));
  $("reconnect-job").addEventListener("click", () => { clearError(); state.pollFailures = 0; $("reconnect-job").hidden = true; if (state.job) schedulePoll(); });
  $("copy-caption").addEventListener("click", copyPost);
    $("use-director-caption").addEventListener("click", () => {
      const caption = $("director-caption").textContent.trim();
      if (!caption || !state.draft) return;
      $("post-caption").value = caption;
      $("post-caption").dispatchEvent(new Event("input", { bubbles: true }));
      $("post-caption").focus();
      toast("Caption replaced with the director’s suggestion. Render this version to use it.");
    });
  $("story-title").addEventListener("input", () => { if (!state.draft) return; state.draft.title = $("story-title").value; $("project-name").textContent = state.draft.title || "Untitled project"; saveDraft(); });
  $("post-caption").addEventListener("input", () => { if (!state.draft) return; state.draft.caption = $("post-caption").value; saveDraft(); });
  $("hashtags").addEventListener("input", () => { if (!state.draft) return; state.draft.hashtags = $("hashtags").value.split(/[\s,]+/).map((tag) => tag.replace(/^#+/, "")).filter(Boolean); saveDraft(); });
  $("recent-projects").addEventListener("toggle", () => { if ($("recent-projects").open) refreshRecent(); });
  $("download-report").addEventListener("click", (event) => event.stopPropagation());
  $("cover-image").addEventListener("error", () => { $("cover-image").hidden = true; });
  $("player").addEventListener("loadedmetadata", () => {
    const video = $("player"); if (state.preview === "source" && Number.isFinite(video.duration)) { state.duration = video.duration; $("source-badge").textContent = durationText(state.duration); }
    boundDefaultEffectDuration();
    renderScrubber();
    updateMonitorMeta(); $("playback-time").textContent = time(video.currentTime) + " / " + time(video.duration);
  });
  $("player").addEventListener("timeupdate", () => {
    const video = $("player"); $("playback-time").textContent = time(video.currentTime) + " / " + time(video.duration);
    if (state.rangeEnd !== null && video.currentTime >= state.rangeEnd) {
      const segment = state.loop && state.draft && state.draft.segments[state.selected];
      if (segment) { video.currentTime = finite(segment.start, 0); } else { video.pause(); state.rangeEnd = null; }
    }
    paintPlayhead(); paintCurrentCue();
  });
  $("player").addEventListener("error", () => { if (!$("player").getAttribute("src")) return; if (state.job && activeStatuses.has(state.job.status)) return; showError("This video cannot be previewed in the browser. You can still edit supported footage and download the finished MP4."); });
  document.addEventListener("keydown", handleShortcut);
  $("shortcuts-button").addEventListener("click", () => openShortcuts());
  $("shortcuts-close").addEventListener("click", () => $("shortcuts").close());
  $("transcript-search").addEventListener("input", () => { state.transcriptQuery = $("transcript-search").value.trim().toLowerCase(); filterTranscript(); });
  $("scrub-track").addEventListener("pointerdown", scrubPointerDown);
  window.addEventListener("resize", () => { if (!$("scrubber").hidden) renderScrubber(); });
  window.addEventListener("beforeunload", () => { saveDraft(); });
  setInterval(() => { if (state.busy || state.job && activeStatuses.has(state.job.status)) $("elapsed").textContent = time((Date.now() - state.jobStarted) / 1000); }, 1000);

  async function init() {
    setControls();
    await loadCapabilities();
    const linked = (() => { try { return new URLSearchParams(location.hash.replace(/^#/, "")).get("job") || ""; } catch (_) { return ""; } })();
    const last = /^[a-f0-9]{32}$/.test(linked) ? linked : storageGet(storageKey);
    if (last) await openJob(last);
    refreshRecent();
  }
  init();
}());
