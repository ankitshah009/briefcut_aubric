"use strict";

/*
 * The shared kit for the creative panels, then the media bin.
 *
 * Every choice shown here comes from capabilities().editor, and every edit goes
 * through BriefcutEditor.change so undo and the timeline stay in one place.
 * Creative choices are shown on a frame from the person's own footage.
 */
(function () {
  const Kit = (window.BriefcutKit = {});
  const $ = (id) => document.getElementById(id);
  const VIDEO_NAME = /\.(mp4|mov|m4v|mkv|webm)$/i;
  const IMAGE_NAME = /\.(jpe?g|png|webp|bmp|tiff?|heic)$/i;
  const AUDIO_NAME = /\.(mp3|m4a|aac|wav|flac|ogg|opus)$/i;
  // Mirrors the server's /api/capabilities limits (project_media.py, lint.MAX_MOMENTS) for when that call fails.
  const FALLBACK_LIMITS = { upload_bytes: 4000000000, source_seconds: 7200, segments: 32, clips: 30 };
  const ICONS = {
    look: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm0 0v14m0-11 4.5 2M10 9l6 1m-6 3 4.5 2",
    transition: "M3 5h8v10H3zM9 5h8v10M13 8l2 2-2 2",
    captions: "M3 5h14v10H3zM6 12h5M13 12h1M6 9h1M9 9h5",
    text: "M4 6V4h12v2M10 4v12M7.5 16h5",
    audio: "M3 9v2M6 7v6M9 4v12M12 6v8M15 8v4M18 9v2",
    motion: "M3 10h9M9 6l4 4-4 4M15 5v10",
    cutout: "M10 4a2.6 2.6 0 1 1 0 5.2A2.6 2.6 0 0 1 10 4ZM4.5 17c.7-3.8 2.9-5.6 5.5-5.6s4.8 1.8 5.5 5.6M2 6V2h4M14 2h4v4M18 14v4h-4M6 18H2v-4",
    export: "M10 3v10M6 9l4 4 4-4M3 14v3h14v-3",
    plus: "M10 4v12M4 10h12",
    up: "m5 12 5-5 5 5",
    down: "m5 8 5 5 5-5",
    close: "m5 5 10 10M15 5 5 15",
    retry: "M4 10a6 6 0 1 0 2-4.5M4 3v4h4",
    send: "M10 16V4M5 9l5-5 5 5",
    search: "M9 3.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM13 13l4 4",
    music: "M8 15V5l8-2v10M8 15a2 2 0 1 1-4 0 2 2 0 0 1 4 0ZM16 13a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z",
    image: "M3 4h14v12H3zM3 13l4-4 3 3 3-2 4 4M13 7h.01",
    film: "M3 4h14v12H3zM7 4v12M13 4v12M3 8h4M3 12h4M13 8h4M13 12h4",
    effects: "M8 2.5 9.6 7 14 8.6 9.6 10.2 8 14.6 6.4 10.2 2 8.6 6.4 7 8 2.5ZM15 12l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8.8-2Z",
  };

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "on") Object.entries(value).forEach(([name, fn]) => node.addEventListener(name, fn));
      else if (key === "style") Object.assign(node.style, value);
      else if (key === "data") Object.assign(node.dataset, value);
      else if (key === "checked" || key === "disabled" || key === "hidden" || key === "open" || key === "draggable") node[key] = !!value;
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    node.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
    return node;
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", ICONS[name] || "");
    svg.append(path);
    return svg;
  }

  const clone = (value) => JSON.parse(JSON.stringify(value));
  const finite = (value, fallback) => { const number = Number(value); return Number.isFinite(number) ? number : fallback; };
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const isObject = (value) => !!value && typeof value === "object" && !Array.isArray(value);

  function merge(base, patch) {
    const merged = clone(base || {});
    Object.entries(patch || {}).forEach(([key, value]) => {
      merged[key] = isObject(value) && isObject(merged[key]) ? merge(merged[key], value) : value === undefined ? merged[key] : clone(value);
    });
    return merged;
  }
  function getPath(source, path) { return path.split(".").reduce((node, key) => (node === null || node === undefined ? undefined : node[key]), source); }
  function setPath(target, path, value) {
    const keys = path.split(".");
    const last = keys.pop();
    let node = target;
    keys.forEach((key) => { if (!isObject(node[key])) node[key] = {}; node = node[key]; });
    node[last] = value;
    return target;
  }

  function clock(seconds, precise) {
    const value = Math.max(0, finite(seconds, 0));
    const whole = Math.floor(value);
    const base = Math.floor(whole / 60) + ":" + String(whole % 60).padStart(2, "0");
    return precise ? base + "." + Math.floor((value - whole) * 10) : base;
  }
  function bytes(value) {
    const size = Math.max(0, finite(value, 0));
    if (size >= 1e9) return (size / 1e9).toFixed(size % 1e9 ? 1 : 0) + " GB";
    if (size >= 1e6) return Math.round(size / 1e6) + " MB";
    if (size >= 1e3) return Math.round(size / 1e3) + " KB";
    return size + " bytes";
  }
  function plural(count, one, many) { return count + " " + (count === 1 ? one : many || one + "s"); }
  function money(value) { return value < .01 ? "under $0.01" : "about $" + value.toFixed(2); }

  const bridge = () => window.BriefcutEditor || null;
  const caps = () => { const editor = bridge(); return editor && editor.capabilities && editor.capabilities() || {}; };
  const catalogue = () => caps().editor || null;
  const job = () => { const editor = bridge(); return editor && editor.job && editor.job() || null; };
  const draft = () => { const editor = bridge(); return editor && editor.draft && editor.draft() || null; };
  const timeline = () => { const editor = bridge(); return editor && editor.timeline && editor.timeline() || null; };
  const locked = () => !!job() && bridge().locked();
  const limits = () => Object.assign({}, FALLBACK_LIMITS, caps().limits || {});
  const view = () => { const list = catalogue(); const current = draft(); return merge(list && list.defaults || {}, current && current.edit || {}); };
  function playhead() { const editor = bridge(); return editor && editor.playhead ? Math.max(0, finite(editor.playhead(), 0)) : 0; }
  function metaReady() { const meta = caps().meta || {}; return meta.available !== undefined ? !!meta.available : !!(meta.enabled && meta.configured); }
  function metaReason() { return (caps().meta || {}).reason || "Add your Meta API key in Settings to connect Meta."; }

  /* Repeated calls with one label inside a moment become one undo step, so a
     slider writes as it moves. Discrete choices pass { coalesce: false }. */
  function edit(label, change, options) {
    const editor = bridge();
    if (!editor || !draft()) return false;
    return editor.change(label, (plan) => { change(plan.edit, plan); }, options);
  }

  async function api(path, options) {
    const editor = bridge();
    if (!editor || !editor.api) throw new Error("The editor is still starting. Try again in a moment.");
    return editor.api(path, options);
  }

  function toast(message) { const studio = window.BriefcutStudio; if (studio && studio.toast) studio.toast(message); }
  function announce(message) { const node = $("live"); if (node) node.textContent = message; }

  /* ---------- What this build can do ---------- */
  /* ---------- Change feed ---------- */
  const listeners = new Set();
  let scheduled = false;
  function notify() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; listeners.forEach((fn) => fn()); });
  }
  function watch() {
    const editor = bridge();
    ["change", "job", "timeline", "select"].forEach((name) => editor.on(name, notify));
    /* Capabilities load once at startup and send no event. */
    const waiting = setInterval(() => { if (catalogue()) { clearInterval(waiting); notify(); } }, 120);
  }

  function ready(start) {
    let tries = 0;
    const attempt = () => {
      if (window.BriefcutEditor) { start(); return; }
      tries += 1;
      if (tries < 600) setTimeout(attempt, 100);
    };
    attempt();
  }

  /* ---------- Frames of the person's own footage ---------- */
  const frameCache = new Map();
  let captureChain = Promise.resolve();
  let stripURL = "";
  let stripImage = null;

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error("image"));
      image.src = url;
    });
  }
  function snapshot(source, width, height) {
    const canvas = document.createElement("canvas");
    const scale = Math.min(1, 360 / Math.max(width, height));
    canvas.width = Math.max(2, Math.round(width * scale));
    canvas.height = Math.max(2, Math.round(height * scale));
    canvas.getContext("2d").drawImage(source, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", .74);
  }
  function grabVideoFrame(url, seconds) {
    const task = () => new Promise((resolve) => {
      const video = document.createElement("video");
      let settled = false;
      const finish = (value) => { if (settled) return; settled = true; clearTimeout(timer); video.removeAttribute("src"); video.load(); resolve(value); };
      const timer = setTimeout(() => finish(null), 6000);
      video.muted = true; video.playsInline = true; video.preload = "auto";
      video.addEventListener("error", () => finish(null), { once: true });
      video.addEventListener("loadedmetadata", () => {
        const duration = Number.isFinite(video.duration) ? video.duration : 0;
        video.currentTime = clamp(seconds === null ? Math.min(1, duration * .2) : seconds, 0, Math.max(0, duration - .05));
      }, { once: true });
      video.addEventListener("seeked", () => {
        try { finish({ frame: snapshot(video, video.videoWidth, video.videoHeight), duration: video.duration, width: video.videoWidth, height: video.videoHeight }); }
        catch (_) { finish(null); }
      }, { once: true });
      video.src = url;
    });
    const run = captureChain.then(task, task);
    captureChain = run.catch(() => null);
    return run;
  }
  async function stripFrame(strip, seconds) {
    if (stripURL !== strip.url || !stripImage) { stripImage = await loadImage(strip.url); stripURL = strip.url; }
    const tile = clamp(Math.round(seconds / Math.max(.001, finite(strip.seconds_per_tile, 1))), 0, Math.max(0, finite(strip.count, 1) - 1));
    const column = tile % strip.columns;
    const row = Math.floor(tile / strip.columns);
    const canvas = document.createElement("canvas");
    canvas.width = strip.tile_width; canvas.height = strip.tile_height;
    canvas.getContext("2d").drawImage(stripImage, column * strip.tile_width, row * strip.tile_height, strip.tile_width, strip.tile_height, 0, 0, strip.tile_width, strip.tile_height);
    return canvas.toDataURL("image/jpeg", .78);
  }
  function sourceURL() {
    const current = job();
    if (current && current.source && typeof current.source_url === "string" && current.source_url.startsWith("/api/campaigns/")) return current.source_url;
    return Kit.previewURL || "";
  }
  async function frameAt(seconds) {
    const current = job();
    const key = (current ? current.id : "unsaved") + ":" + Math.round(finite(seconds, 0) * 2);
    if (frameCache.has(key)) return frameCache.get(key);
    const pending = (async () => {
      const strip = current && current.source && current.source.filmstrip;
      if (strip && strip.url && strip.columns && strip.tile_width) {
        try { return await stripFrame(strip, finite(seconds, 0)); } catch (_) { /* Fall back to the video itself. */ }
      }
      const url = sourceURL();
      if (!url) return "";
      const grabbed = await grabVideoFrame(url, finite(seconds, 0));
      return grabbed ? grabbed.frame : "";
    })();
    frameCache.set(key, pending);
    const value = await pending;
    if (!value) frameCache.delete(key);
    return value;
  }
  /* Two moments of the story: the swatches use the first, transitions use both. */
  async function storyFrames() {
    const segments = draft() && draft().segments || [];
    const first = segments[0];
    const second = segments[1] || null;
    const middle = (segment) => (finite(segment.start, 0) + finite(segment.end, 0)) / 2;
    const span = job() && job().source ? finite(job().source.duration, 0) : 0;
    const a = await frameAt(first ? middle(first) : Math.min(1, span / 3));
    const b = await frameAt(second ? middle(second) : span ? span * .66 : 2);
    return { a, b: b || a };
  }
  function paintFrames(frames) {
    document.querySelectorAll("img[data-frame]").forEach((image) => {
      const source = frames[image.dataset.frame] || "";
      if (source && image.getAttribute("src") !== source) image.src = source;
      image.hidden = !source;
    });
  }
  let framesKey = "";
  async function refreshFrames() {
    const segments = draft() && draft().segments || [];
    const key = [job() && job().id, Kit.previewURL, segments.slice(0, 2).map((segment) => segment.start + ":" + segment.end).join(",")].join("|");
    if (key === framesKey) { if (Kit.frames) paintFrames(Kit.frames); return; }
    framesKey = key;
    const frames = await storyFrames();
    Kit.frames = frames;
    paintFrames(frames);
  }

  /* ---------- CSS approximations of a look ---------- */
  function lookStyle(look) {
    const value = look || {};
    const hint = window.BriefcutLookStyles.inspector[value.preset] || {};
    const amount = clamp(finite(value.intensity, 1), 0, 1);
    const mix = (key, neutral) => neutral + (finite(hint[key], neutral) - neutral) * amount;
    const parts = [];
    const brightness = mix("brightness", 1) + finite(value.brightness, 0) * .5;
    const contrast = mix("contrast", 1) * (1 + finite(value.contrast, 0) * .6);
    const saturate = mix("saturate", 1) * (1 + finite(value.saturation, 0));
    const sepia = mix("sepia", 0);
    const grayscale = mix("grayscale", 0);
    if (grayscale) parts.push("grayscale(" + grayscale.toFixed(3) + ")");
    if (sepia) parts.push("sepia(" + sepia.toFixed(3) + ")");
    if (brightness !== 1) parts.push("brightness(" + Math.max(0, brightness).toFixed(3) + ")");
    if (contrast !== 1) parts.push("contrast(" + Math.max(0, contrast).toFixed(3) + ")");
    if (saturate !== 1) parts.push("saturate(" + Math.max(0, saturate).toFixed(3) + ")");
    const temperature = clamp(finite(value.temperature, 0), -1, 1);
    let tint = hint.tint ? { color: hint.tint[0], opacity: hint.tint[1] * amount } : null;
    if (temperature) {
      const shift = { color: temperature > 0 ? "#ff9a3d" : "#2f7bff", opacity: Math.abs(temperature) * .34 };
      tint = !tint || shift.opacity > tint.opacity ? shift : tint;
    }
    return { filter: parts.join(" "), tint, vignette: clamp(finite(value.vignette, 0), 0, 1) };
  }

  Object.assign(Kit, {
    $, el, icon, clone, finite, clamp, merge, getPath, setPath, clock, bytes, plural, money, isObject,
    bridge, caps, catalogue, job, draft, timeline, view, locked, limits, playhead, metaReady, metaReason,
    edit, api, toast, announce, notify, frameAt, paintFrames, refreshFrames, lookStyle, previewURL: "", frames: null,
    onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    assets() { return Object.assign({}, bin.assets, current().assets || {}); },
    addAsset(asset) { bin.assets[asset.id] = asset; },
  });
  function current() { return job() || {}; }

  /* ---------- Media bin state ---------- */
  const bin = {
    items: [], assets: {}, thumbs: {}, uploads: 0, built: false, previewing: 0,
    find: { query: "", moments: null, busy: false, error: "", verification: null, dropped: [] },
    script: { busy: false, error: "", jobId: "" },
    picker: null, lastJob: "", dragging: null,
  };
  let nextItem = 1;

  const clipItems = () => bin.items.filter((item) => item.role === "clip");
  const assetItems = () => bin.items.filter((item) => item.role === "asset");
  const scriptItems = () => bin.items.filter((item) => item.role === "script");

  function kindOf(file) {
    const type = file.type || "";
    if (type.startsWith("video/") || VIDEO_NAME.test(file.name)) return "video";
    if (type.startsWith("image/") || IMAGE_NAME.test(file.name)) return "image";
    if (type.startsWith("audio/") || AUDIO_NAME.test(file.name)) return "audio";
    return "";
  }

  async function inspectFile(item) {
    try {
      if (item.kind === "image") {
        const image = await loadImage(item.url);
        item.width = image.naturalWidth; item.height = image.naturalHeight;
        item.thumb = snapshot(image, image.naturalWidth, image.naturalHeight);
      } else if (item.kind === "video") {
        const grabbed = await grabVideoFrame(item.url, null);
        if (grabbed) { item.thumb = grabbed.frame; item.duration = grabbed.duration; item.width = grabbed.width; item.height = grabbed.height; }
      } else {
        await new Promise((resolve) => {
          const audio = new Audio();
          audio.preload = "metadata";
          audio.addEventListener("loadedmetadata", () => { item.duration = audio.duration; resolve(); }, { once: true });
          audio.addEventListener("error", resolve, { once: true });
          audio.src = item.url;
        });
      }
    } catch (_) { /* The server still reads files the browser cannot preview. */ }
    paintBin();
    if (item.role === "clip") refreshFrames();
  }

  function addFiles(files, intent) {
    const problems = [];
    const project = job();
    const limit = limits();
    Array.from(files || []).forEach((file) => {
      const kind = kindOf(file);
      if (!kind) { problems.push(file.name + " is not a video, photo, or audio file."); return; }
      if (!file.size) { problems.push(file.name + " is empty."); return; }
      if (file.size > limit.upload_bytes) { problems.push(file.name + " is over the " + bytes(limit.upload_bytes) + " limit."); return; }
      if (kind === "video" && project) { problems.push("This project keeps its clips. Start a new project to add " + file.name + "."); return; }
      if (intent === "script" && kind !== "image") { problems.push(file.name + " is not an image."); return; }
      const role = intent === "script" ? "script" : intent === "logo" || kind === "audio" || project ? "asset" : "clip";
      if (role === "clip" && clipItems().length >= limit.clips) { problems.push("A project holds up to " + limit.clips + " clips."); return; }
      const item = { id: nextItem++, file, kind, role, intent: intent || "", name: file.name, size: file.size, url: URL.createObjectURL(file), thumb: "", duration: 0, width: 0, height: 0, status: "queued", progress: 0, path: "", error: "", xhr: null, waiters: [] };
      bin.items.push(item);
      inspectFile(item);
    });
    binMessage(problems.join(" "));
    if (problems.length) announce(problems.join(" "));
    syncClips();
    pump();
    paintBin();
  }

  function pump() {
    while (bin.uploads < 2) {
      const next = bin.items.find((item) => item.status === "queued");
      if (!next) return;
      upload(next);
    }
  }

  function upload(item) {
    bin.uploads += 1;
    item.status = "uploading"; item.progress = 0; item.error = "";
    const xhr = new XMLHttpRequest();
    item.xhr = xhr;
    const done = (error, path) => {
      bin.uploads -= 1;
      item.xhr = null;
      if (error) { item.status = "failed"; item.error = error; }
      else { item.status = "saved"; item.progress = 1; item.path = path; }
      item.waiters.splice(0).forEach((resolve) => resolve());
      if (!error && item.role === "asset") registerItem(item);
      pump(); paintBin();
    };
    xhr.open("POST", "/api/upload?name=" + encodeURIComponent(item.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.timeout = 240000;
    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      item.progress = event.loaded / event.total;
      paintProgress(item);
    });
    xhr.addEventListener("load", () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* Reported below. */ }
      const path = data && Array.isArray(data.paths) ? data.paths[0] : "";
      if (xhr.status >= 200 && xhr.status < 300 && data && data.ok && path) done("", path);
      else done(data && data.error || "The editor did not save this file.");
    });
    xhr.addEventListener("error", () => done("The transfer stopped. Keep the editor running and retry."));
    xhr.addEventListener("timeout", () => done("The transfer took too long. Retry, or use a smaller file."));
    xhr.addEventListener("abort", () => done("Cancelled."));
    xhr.send(item.file);
    paintBin();
  }

  function retry(item) {
    item.status = "queued"; item.error = "";
    pump(); paintBin();
  }
  function removeItem(item) {
    if (item.xhr) item.xhr.abort();
    bin.items = bin.items.filter((other) => other !== item);
    URL.revokeObjectURL(item.url);
    syncClips(); paintBin();
  }
  function moveClip(item, offset) {
    const clips = clipItems();
    const target = clips[clips.indexOf(item) + offset];
    if (!target) return;
    const a = bin.items.indexOf(item), b = bin.items.indexOf(target);
    bin.items[a] = target; bin.items[b] = item;
    syncClips(); paintBin();
    const row = document.querySelector('[data-item="' + item.id + '"]');
    const button = row && row.querySelector(offset < 0 ? ".mb-up" : ".mb-down");
    if (button && !button.disabled) button.focus(); else if (row) row.querySelector("button").focus();
    announce(item.name + " moved to position " + (clipItems().indexOf(item) + 1));
  }

  /* What createStory reads. Waits for uploads, then gives the clips in bin order. */
  function clipPaths() {
    const pending = clipItems().filter((item) => item.status === "queued" || item.status === "uploading");
    return Promise.all(pending.map((item) => new Promise((resolve) => item.waiters.push(resolve)))).then(() => {
      const failed = clipItems().find((item) => item.status === "failed");
      if (failed) throw new Error(failed.name + " did not upload. Retry it or remove it, then create the story.");
      return clipItems().map((item) => item.path);
    });
  }
  window.BriefcutClips = {
    count: () => clipItems().length,
    paths: clipPaths,
    signature: () => clipItems().map((item) => item.name + ":" + item.size + ":" + item.file.lastModified).join("|"),
  };

  /* The first video plays in the monitor while the clip list is being built. */
  function syncClips() {
    if (job()) return;
    const clips = clipItems();
    const first = clips.find((item) => item.kind === "video") || null;
    Kit.previewURL = first ? first.url : "";
    const studio = window.BriefcutStudio;
    if (studio) {
      if (first && bin.previewing !== first.id) { bin.previewing = first.id; studio.selectFile(first.file); }
      else if (!first && bin.previewing) { bin.previewing = 0; studio.resetProject(false); }
    }
    if ($("source-badge") && clips.length) $("source-badge").textContent = plural(clips.length, "clip");
    if ($("drop-title") && clips.length) $("drop-title").textContent = "Add more footage";
    if ($("clip-name") && clips.length) $("clip-name").textContent = "The originals stay untouched.";
    refreshFrames();
  }

  /* ---------- Assets ---------- */
  async function registerAsset(path) {
    if (!job()) throw new Error("Create a story first.");
    const data = await api("/assets", { method: "POST", body: { path }, timeout: 120000 });
    if (!data || !data.asset || !data.asset.id) throw new Error("The editor did not register this file.");
    bin.assets[data.asset.id] = data.asset;
    return data.asset;
  }
  async function registerItem(item) {
    if (item.asset || item.registering || !job()) return;
    item.registering = true; item.error = "";
    paintBin();
    try {
      const asset = await registerAsset(item.path);
      item.asset = asset;
      if (item.thumb) bin.thumbs[asset.id] = item.thumb;
      if (item.intent === "logo") bin.pendingLogo = asset;
    } catch (error) { item.error = error.message; }
    item.registering = false;
    paintBin(); notify();
  }
  function useLogo(asset) {
    const applied = edit("Add logo", (plan) => { plan.logo = Object.assign({ position: "top-right", scale: .14, opacity: .9 }, plan.logo || {}, { asset: asset.id }); }, { coalesce: false });
    if (applied) toast(asset.name + " is your logo. Adjust it in Text.");
  }
  function useMusic(asset) {
    const applied = edit("Add music", (plan) => { plan.music = Object.assign({ volume_db: -18, duck: true, fade_out: 1.5, start: 0, beat_sync: false }, plan.music || {}, { asset: asset.id }); }, { coalesce: false });
    if (applied) toast(asset.name + " is your music. Adjust it in Audio.");
  }
  function addOverlay(asset) {
    const start = Math.round(Kit.playhead() * 10) / 10;
    const applied = edit("Add image", (plan) => { plan.overlays = (plan.overlays || []).concat([{ asset: asset.id, start, duration: 3, position: "center", scale: .3 }]); }, { coalesce: false });
    if (applied) toast(asset.name + " is on the timeline at " + clock(start) + ".");
  }

  /* ---------- Find ---------- */
  async function runFind(event) {
    event.preventDefault();
    const query = bin.find.query.trim();
    if (!job() || !query || bin.find.busy) return;
    bin.find.busy = true; bin.find.error = ""; bin.find.moments = null; bin.find.warnings = []; bin.find.verification = null; bin.find.dropped = [];
    paintFind();
    try {
      // SAM 3.1 checks each moment Muse Spark saw, so the search can take a little longer.
      const data = await api("/find", { method: "POST", body: { query }, timeout: 90000 });
      const timed = (list) => (Array.isArray(list) ? list.filter((moment) => moment && Number.isFinite(Number(moment.start)) && Number.isFinite(Number(moment.end))) : []);
      bin.find.moments = timed(data.moments);
      bin.find.warnings = Array.isArray(data.warnings) ? data.warnings.filter((item) => typeof item === "string") : [];
      bin.find.verification = data.verification && typeof data.verification === "object" ? data.verification : null;
      bin.find.dropped = timed(bin.find.verification && bin.find.verification.dropped);
      const seen = bin.find.moments.filter((moment) => moment.verified === true).length;
      const missed = bin.find.moments.filter(unseen).length;
      announce(plural(bin.find.moments.length, "moment") + " found." + (seen || missed ? " SAM 3.1 saw " + seen + " of " + (seen + missed) + "." : ""));
    } catch (error) { bin.find.error = error.message; }
    bin.find.busy = false;
    paintFind();
    const input = $("mb-find-input");
    if (input) input.focus();
  }
  function addMoment(moment) {
    const plan = draft();
    const max = limits().segments;
    if (!plan) return;
    if (plan.segments.length >= max) { toast("A story holds up to " + max + " moments. Remove one first."); return; }
    const label = bin.find.query.trim().slice(0, 80) || "Found moment";
    const added = edit("Add moment", (_, story) => { story.segments.push({ start: Number(moment.start), end: Number(moment.end), label, reason: moment.reason || "Found by search." }); }, { coalesce: false });
    if (added) toast("Added to the end of the story.");
  }
  /* ---------- What SAM 3.1 saw ---------- */
  const UNSEEN_HINT = "SAM 3.1 looked for it and did not find it. SAM can miss stylised or unnamed objects, so preview it to check.";
  function sameOriginArtifact(value) {
    if (typeof value !== "string" || !value) return "";
    try { const url = new URL(value, location.href); return url.origin === location.origin && url.pathname.startsWith("/api/") ? url.href : ""; } catch (_) { return ""; }
  }
  function coverageText(value) { const share = Number(value); return Number.isFinite(share) ? Math.round(clamp(share, 0, 1) * 100) + "%" : ""; }
  /* verified is true, false, or null. null means nothing was checked, as for a spoken match, so no badge.
     A flagged moment is one SAM did not see that is kept, ranked last, so it stays clickable. */
  function unseen(moment) { return moment.verified === false || moment.verified !== true && moment.flagged === true; }
  function seenBadge(moment) {
    const detail = typeof moment.verification === "string" && moment.verification ? " " + moment.verification : "";
    if (moment.verified === true) {
      const share = coverageText(moment.coverage);
      return el("span", { class: "mb-seen is-seen", title: ("SAM 3.1 found it on screen" + (share ? " in " + share + " of the frames it checked" : "") + "." + detail).trim() },
        el("span", { class: "mb-seen-mark", "aria-hidden": "true", text: "\u2713" }), "Seen by SAM 3.1" + (share ? " \u00b7 " + share : ""));
    }
    if (unseen(moment)) {
      return el("span", { class: "mb-seen is-unseen", title: UNSEEN_HINT + detail },
        el("span", { class: "mb-seen-mark", "aria-hidden": "true", text: "!" }), "SAM 3.1 did not see it", el("span", { class: "sr", text: ". " + UNSEEN_HINT }));
    }
    return null;
  }
  function verificationNote(verification) {
    if (!verification) return null;
    const receipt = verification.receipt && typeof verification.receipt === "object" ? verification.receipt : null;
    const text = receipt && typeof receipt.note === "string" && receipt.note ? receipt.note
      : typeof verification.phrase === "string" && verification.phrase ? "SAM 3.1 checked each moment for \u201c" + verification.phrase + "\u201d." : "";
    const spend = verification.spend;
    const spent = Number.isFinite(Number(verification.spent_usd)) ? Number(verification.spent_usd)
      : Array.isArray(spend) ? spend.reduce((total, item) => total + finite(item && item.cost_usd, 0), 0)
      : spend && typeof spend === "object" ? finite(spend.cost_usd !== undefined ? spend.cost_usd : spend.reserved_usd, NaN) : Number(spend);
    if (!text && !Number.isFinite(spent)) return null;
    return el("p", { class: "mb-note mb-verify-note" }, text, Number.isFinite(spent) && spent > 0 ? " " + (spent < .01 ? "Under $0.01." : "$" + spent.toFixed(2) + ".") : "");
  }
  function previewMoment(moment) {
    const source = $("preview-source");
    if (source && source.getAttribute("aria-pressed") !== "true") source.click();
    const player = $("player");
    if (player) player.currentTime = Number(moment.start);
  }

  /* ---------- Templates ---------- */
  function templateEdit(template, currentEdit) {
    const list = catalogue();
    const settings = template.edit || {};
    const next = merge(merge(list.defaults, settings), { template: template.id });
    ["texts", "logo", "overlays", "music", "removed", "cutout", "export"].forEach((key) => {
      if (!(key in settings) && key in currentEdit) next[key] = clone(currentEdit[key]);
    });
    if (next.captions && currentEdit.captions) {
      next.captions.edits = clone(currentEdit.captions.edits || {});
      next.captions.language = currentEdit.captions.language === undefined ? null : currentEdit.captions.language;
    }
    return next;
  }
  function applyTemplate(template) {
    const applied = edit(template.name + " template", (plan) => {
      const next = templateEdit(template, clone(plan));
      Object.keys(plan).forEach((key) => delete plan[key]);
      Object.assign(plan, next);
    }, { coalesce: false });
    if (applied) toast(template.name + " applied. Undo takes it back.");
  }
  /* ---------- Start from a script ---------- */
  function scriptInfo() { return caps().script || {}; }
  function scriptVoices() {
    const info = scriptInfo();
    const rows = Array.isArray(info.voices) ? info.voices : [];
    const english = rows.filter((row) => /^en/i.test(String(row.language || "")));
    return (english.length ? english : rows).map((row) => (typeof row === "string" ? { id: row, name: row } : row)).filter((row) => row && row.id);
  }
  async function submitScript(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const script = form.elements.script.value.trim();
    const info = scriptInfo();
    if (!script || bin.script.busy) return;
    bin.script.busy = true; bin.script.error = "";
    paintScript();
    try {
      await Promise.all(scriptItems().filter((item) => item.status === "queued" || item.status === "uploading").map((item) => new Promise((resolve) => item.waiters.push(resolve))));
      const failed = scriptItems().find((item) => item.status === "failed");
      if (failed) throw new Error(failed.name + " did not upload. Retry it or remove it.");
      const images = scriptItems().map((item) => item.path);
      if (!images.length && !info.generates_images) throw new Error(info.image_reason || "Add your own images to make a video from this script.");
      const body = { script, aspect: form.elements.aspect.value };
      if (images.length) body.images = images;
      if (form.elements.voice && form.elements.voice.value) body.voice = form.elements.voice.value;
      const data = await api("/api/script", { method: "POST", body, timeout: 120000 });
      if (!data.job || !data.job.id) throw new Error("The editor did not create a project from this script.");
      bin.script.jobId = data.job.id;
      bin.items = bin.items.filter((item) => item.role !== "script");
      openProject(data.job);
    } catch (error) { bin.script.error = error.message; }
    bin.script.busy = false;
    paintScript();
  }
  function openProject(project) {
    const studio = window.BriefcutStudio;
    if (studio) studio.adoptJob(project, true);
  }
  Kit.openProject = openProject;

  /* ---------- Painting ---------- */
  const nodes = {};
  function binMessage(text) {
    [nodes.message, nodes.sourceMessage].forEach((node) => { if (node) { node.textContent = text; node.hidden = !text; } });
  }

  function pick(accept, intent) {
    if (!bin.picker) {
      bin.picker = el("input", { type: "file", multiple: true, class: "sr", tabindex: "-1", "aria-hidden": "true", on: { change: () => { addFiles(bin.picker.files, bin.picker.dataset.intent); bin.picker.value = ""; } } });
      document.body.append(bin.picker);
    }
    bin.picker.accept = accept;
    bin.picker.dataset.intent = intent || "";
    bin.picker.multiple = intent !== "logo";
    bin.picker.click();
  }

  function limitText() {
    const limit = limits();
    const minutes = Math.round(limit.source_seconds / 60);
    const total = minutes >= 120 && minutes % 60 === 0 ? minutes / 60 + " hours" : minutes + " minutes";
    return "Video, photos, and audio. Up to " + limit.clips + " clips, " + bytes(limit.upload_bytes) + " each, " + total + " in all.";
  }

  function build() {
    const mount = $("media-bin-mount");
    if (!mount || bin.built) return;
    bin.built = true;

    nodes.drop = el("div", { class: "mb-drop" },
      el("div", { class: "mb-drop-actions" },
        el("button", { type: "button", class: "button button-secondary mb-add", on: { click: () => pick("image/*,audio/*,.jpg,.jpeg,.png,.webp,.mp3,.m4a,.wav,.aac,.flac,.ogg") } }, icon("plus"), "Add audio or images"),
        el("button", { type: "button", class: "button button-secondary mb-add-logo", on: { click: () => pick("image/*,.png,.jpg,.jpeg,.webp", "logo") } }, "Add logo")));
    ["dragenter", "dragover"].forEach((name) => mount.addEventListener(name, (event) => { if (!event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes("Files")) return; event.preventDefault(); nodes.drop.classList.add("is-hot"); }));
    mount.addEventListener("dragleave", (event) => { if (!mount.contains(event.relatedTarget)) nodes.drop.classList.remove("is-hot"); });
    mount.addEventListener("drop", (event) => { if (!event.dataTransfer || !event.dataTransfer.files.length) return; event.preventDefault(); nodes.drop.classList.remove("is-hot"); addFiles(event.dataTransfer.files); });

    nodes.message = el("p", { class: "mb-message", role: "status", hidden: true });
    nodes.find = el("section", { class: "mb-section mb-find", "aria-label": "Find a moment" });
    nodes.clips = el("section", { class: "mb-section", "aria-labelledby": "mb-clips-heading" });
    nodes.assets = el("section", { class: "mb-section", "aria-labelledby": "mb-assets-heading" });
    mount.replaceChildren(el("div", { class: "mb" }, el("div", { class: "mb-panel" }, nodes.find, nodes.clips, nodes.drop, nodes.message, nodes.assets)));

    mountSource();
    mountTemplates();
    adoptPicker();
  }

  /* Before a project exists the Edit workspace is not on screen, so the clip
     list and the script starter sit beside the footage drop zone. */
  function mountSource() {
    const zone = $("drop");
    if (!zone) return;
    nodes.sourceMessage = el("p", { class: "mb-message", role: "status", hidden: true });
    nodes.source = el("div", { class: "mb-section", id: "mb-source" });
    nodes.script = el("details", { class: "mb-script", id: "mb-script" });
    zone.after(el("div", { class: "mb mb-beside" }, nodes.sourceMessage, nodes.source, nodes.script));
    buildScript();
    const empty = $("monitor-empty");
    if (empty) {
      nodes.scriptLink = el("button", { type: "button", class: "text-button mb-script-link", text: "Or start from a script", on: { click: () => { nodes.script.open = true; const field = $("mb-script-text"); if (field) field.focus(); } } });
      empty.append(nodes.scriptLink);
    }
  }

  /* Templates get their own tab beside Transcript and Media. */
  function mountTemplates() {
    const tabs = document.querySelector(".ed-tabs");
    const panel = $("media-panel");
    if (!tabs || !panel) return;
    const tab = el("button", { type: "button", role: "tab", id: "left-tab-templates", "aria-controls": "templates-mount", "aria-selected": "false", tabindex: "-1", text: "Templates" });
    nodes.templatesTab = tab;
    nodes.templates = el("div", { id: "templates-mount", class: "ed-tabpanel", role: "tabpanel", "aria-labelledby": tab.id, hidden: true });
    tabs.append(tab);
    tabs.setAttribute("aria-label", "Transcript, media, and templates");
    panel.append(nodes.templates);
    const others = [["left-tab-transcript", "left-transcript"], ["left-tab-media", "media-bin-mount"]];
    const open = () => {
      others.forEach(([tabId, panelId]) => { $(tabId).setAttribute("aria-selected", "false"); $(tabId).tabIndex = -1; $(panelId).hidden = true; });
      tab.setAttribute("aria-selected", "true"); tab.tabIndex = 0;
      nodes.templates.hidden = false;
      paintTemplates();
    };
    const close = () => { tab.setAttribute("aria-selected", "false"); tab.tabIndex = -1; nodes.templates.hidden = true; };
    tab.addEventListener("click", open);
    others.forEach(([tabId]) => $(tabId).addEventListener("click", close));
  }

  /* Footage chosen over an open project starts a new project, as it always has. */
  function takeFootage(files) {
    const studio = window.BriefcutStudio;
    if (job() && studio) { studio.resetProject(false); settleProject(); }
    addFiles(files);
  }
  function adoptPicker() {
    const input = $("clip");
    const zone = $("drop");
    if (input) {
      input.multiple = true;
      input.accept = "video/*,image/*,audio/*,.mp4,.mov,.m4v,.webm,.mkv,.jpg,.jpeg,.png,.webp,.heic,.mp3,.m4a,.wav,.aac,.flac,.ogg";
      input.addEventListener("change", (event) => {
        if (!input.files || !input.files.length) return;
        event.stopImmediatePropagation();
        takeFootage(input.files);
        input.value = "";
      }, true);
    }
    if (zone) zone.addEventListener("drop", (event) => {
      if (!event.dataTransfer || !event.dataTransfer.files.length) return;
      event.preventDefault(); event.stopImmediatePropagation();
      zone.classList.remove("is-hot");
      takeFootage(event.dataTransfer.files);
    }, true);
    const fresh = $("new-project");
    if (fresh) fresh.addEventListener("click", () => {
      bin.items.forEach((item) => { if (item.xhr) item.xhr.abort(); URL.revokeObjectURL(item.url); });
      bin.items = []; bin.previewing = 0;
      Kit.previewURL = "";
      binMessage("");
      paintBin(); paintScript();
    });
    const hint = zone && zone.querySelector(".file-hint");
    if (hint) hint.textContent = "or drop videos and photos here";
  }

  /* Lists repaint while a person works in them, so focus returns to the same control. */
  function replaceList(container, key, children) {
    if (container.dataset.key === key) return;
    container.dataset.key = key;
    const active = document.activeElement;
    const name = active && container.contains(active) ? active.getAttribute("aria-label") || active.textContent : "";
    container.replaceChildren(...children.filter(Boolean));
    if (!name) return;
    const again = Array.from(container.querySelectorAll("button")).find((button) => (button.getAttribute("aria-label") || button.textContent) === name);
    if (again && !again.disabled) again.focus();
  }

  function thumb(source, kind) {
    return el("span", { class: "mb-thumb" }, source ? el("img", { src: source, alt: "" }) : icon(kind === "audio" ? "music" : kind === "image" ? "image" : "film"));
  }

  function paintProgress(item) {
    const row = document.querySelector('[data-item="' + item.id + '"]');
    if (!row) return;
    const bar = row.querySelector("progress");
    if (bar) bar.value = item.progress;
    const label = row.querySelector(".mb-status");
    if (label && item.status === "uploading") label.textContent = "Saving " + Math.round(item.progress * 100) + "%";
  }

  function itemStatus(item) {
    if (item.status === "failed") return item.error;
    if (item.status === "uploading") return "Saving " + Math.round(item.progress * 100) + "%";
    if (item.status === "queued") return "Waiting to save";
    if (item.error) return item.error;
    if (item.registering) return "Attaching to the project";
    if (item.role === "asset" && !item.asset) return "Attaches when the story is ready";
    const size = item.duration ? clock(item.duration) : item.width ? item.width + " × " + item.height : "";
    return [size, bytes(item.size)].filter(Boolean).join(", ");
  }

  function clipRow(item, index, total) {
    const failed = item.status === "failed";
    const row = el("li", { class: "mb-clip" + (failed ? " is-failed" : ""), data: { item: String(item.id) }, draggable: true },
      thumb(item.thumb, item.kind),
      el("div", { class: "mb-clip-text" },
        el("strong", { text: item.name, title: item.name }),
        el("span", { class: "mb-status", text: itemStatus(item) }),
        item.status === "uploading" || item.status === "queued" ? el("progress", { max: "1", value: String(item.progress), "aria-label": "Saving " + item.name }) : null),
      el("div", { class: "mb-clip-actions" },
        failed ? el("button", { type: "button", class: "mb-icon", title: "Retry", "aria-label": "Retry " + item.name, on: { click: () => retry(item) } }, icon("retry")) : null,
        item.kind === "image" ? el("button", { type: "button", class: "mb-text-button", title: "Use this image as a logo instead of a clip", "aria-label": "Use " + item.name + " as a logo", text: "Logo", on: { click: () => { item.role = "asset"; item.intent = "logo"; syncClips(); paintBin(); } } }) : null,
        el("button", { type: "button", class: "mb-icon mb-up", title: "Move earlier", "aria-label": "Move " + item.name + " earlier", disabled: index === 0, on: { click: () => moveClip(item, -1) } }, icon("up")),
        el("button", { type: "button", class: "mb-icon mb-down", title: "Move later", "aria-label": "Move " + item.name + " later", disabled: index === total - 1, on: { click: () => moveClip(item, 1) } }, icon("down")),
        el("button", { type: "button", class: "mb-icon", title: "Remove", "aria-label": "Remove " + item.name, on: { click: () => removeItem(item) } }, icon("close"))));
    row.addEventListener("dragstart", (event) => { bin.dragging = item; row.classList.add("is-dragging"); event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", item.name); });
    row.addEventListener("dragend", () => { bin.dragging = null; row.classList.remove("is-dragging"); });
    row.addEventListener("dragover", (event) => { if (bin.dragging && bin.dragging !== item) { event.preventDefault(); row.classList.add("is-target"); } });
    row.addEventListener("dragleave", () => row.classList.remove("is-target"));
    row.addEventListener("drop", (event) => {
      if (!bin.dragging || bin.dragging === item) return;
      event.preventDefault(); event.stopPropagation();
      const moved = bin.dragging;
      bin.items.splice(bin.items.indexOf(moved), 1);
      bin.items.splice(bin.items.indexOf(item), 0, moved);
      bin.dragging = null;
      syncClips(); paintBin();
    });
    return row;
  }

  function paintSource() {
    if (!nodes.source) return;
    const clips = job() ? [] : clipItems();
    const limit = limits();
    nodes.source.hidden = !clips.length;
    const key = JSON.stringify(clips.map((item) => [item.id, item.status === "uploading" ? "busy" : item.status, item.error, !!item.thumb, item.duration, item.width]));
    const list = el("ol", { class: "mb-clips" });
    clips.forEach((item, index) => list.append(clipRow(item, index, clips.length)));
    replaceList(nodes.source, key, [
      el("div", { class: "mb-heading" }, el("h3", { text: "Clips" }), el("span", { text: clips.length + " of " + limit.clips })),
      list,
      clips.length > 1 ? el("p", { class: "mb-note", text: "Clips play in this order. Drag, or use the arrows." }) : null]);
  }

  function projectClips(project) {
    const source = project.source || {};
    if (Array.isArray(source.clips) && source.clips.length) return source.clips;
    if (!source.duration) return [];
    const path = project.request && project.request.paths && project.request.paths[0] || "Source footage";
    return [{ name: String(path).split(/[\\/]/).pop(), kind: "video", start: 0, end: source.duration }];
  }

  function paintClips() {
    const project = job();
    if (!nodes.clips) return;
    const clips = project ? projectClips(project) : [];
    const key = JSON.stringify(clips.map((clip) => [clip.name, clip.start, clip.end])) + Boolean(project && project.source && project.source.filmstrip);
    if (nodes.clips.dataset.key === key) return;
    nodes.clips.dataset.key = key;
    const list = el("ol", { class: "mb-clips" });
    clips.forEach((clip) => {
      const image = el("img", { alt: "", hidden: true });
      frameAt((finite(clip.start, 0) + finite(clip.end, 0)) / 2).then((frame) => { if (frame) { image.src = frame; image.hidden = false; } });
      list.append(el("li", { class: "mb-clip is-project" },
        el("button", { type: "button", class: "mb-clip-jump", "aria-label": "Show " + clip.name + " in the preview", on: { click: () => previewMoment({ start: clip.start }) } },
          el("span", { class: "mb-thumb" }, image),
          el("span", { class: "mb-clip-text" }, el("strong", { text: clip.name, title: clip.name }), el("span", { class: "mb-status", text: clock(finite(clip.end, 0) - finite(clip.start, 0)) })))));
    });
    nodes.clips.replaceChildren(el("div", { class: "mb-heading" }, el("h3", { id: "mb-clips-heading", text: clips.length === 1 ? "Clip" : "Clips" }), el("span", { text: clips.length > 1 ? String(clips.length) : "" })),
      clips.length ? list : el("p", { class: "mb-empty", text: "Your clips appear here." }));
  }

  function assetRow(asset) {
    const settings = view();
    const canEdit = !!draft() && !locked();
    const inUse = (settings.music && settings.music.asset === asset.id) || (settings.logo && settings.logo.asset === asset.id);
    const facts = asset.duration ? clock(asset.duration) : asset.width ? asset.width + " × " + asset.height : "";
    const action = (text, label, run) => el("button", { type: "button", class: "mb-text-button", disabled: !canEdit, "aria-label": label, text, on: { click: run } });
    return el("li", { class: "mb-clip is-asset" },
      thumb(bin.thumbs[asset.id] || (asset.kind === "image" ? asset.url : ""), asset.kind),
      el("div", { class: "mb-clip-text" }, el("strong", { text: asset.name, title: asset.name }), el("span", { class: "mb-status", text: inUse ? "In use" : facts })),
      el("div", { class: "mb-clip-actions" }, asset.kind === "audio"
        ? action("Use as music", "Use " + asset.name + " as music", () => useMusic(asset))
        : [action("Logo", "Use " + asset.name + " as the logo", () => useLogo(asset)), action("Add", "Add " + asset.name + " at the playhead", () => addOverlay(asset))]));
  }

  function paintAssets() {
    if (!nodes.assets) return;
    const settings = view();
    const records = Object.values(Kit.assets()).filter((asset) => asset && asset.id);
    const pending = assetItems().filter((item) => !item.asset);
    const key = JSON.stringify([records.map((asset) => asset.id), pending.map((item) => [item.id, item.status === "uploading" ? "busy" : item.status, item.error, item.registering, !!item.thumb]), settings.music && settings.music.asset, settings.logo && settings.logo.asset, !!draft() && !locked()]);
    const list = el("ul", { class: "mb-clips" });
    records.forEach((asset) => list.append(assetRow(asset)));
    pending.forEach((item) => {
      const failed = item.status === "failed" || !!item.error;
      list.append(el("li", { class: "mb-clip is-asset" + (failed ? " is-failed" : ""), data: { item: String(item.id) } },
        thumb(item.thumb, item.kind),
        el("div", { class: "mb-clip-text" }, el("strong", { text: item.name, title: item.name }), el("span", { class: "mb-status", text: itemStatus(item) }),
          item.status === "uploading" || item.status === "queued" ? el("progress", { max: "1", value: String(item.progress), "aria-label": "Saving " + item.name }) : null),
        el("div", { class: "mb-clip-actions" },
          failed ? el("button", { type: "button", class: "mb-icon", title: "Retry", "aria-label": "Retry " + item.name, on: { click: () => (item.status === "failed" ? retry(item) : registerItem(item)) } }, icon("retry")) : null,
          el("button", { type: "button", class: "mb-icon", title: "Remove", "aria-label": "Remove " + item.name, on: { click: () => removeItem(item) } }, icon("close")))));
    });
    const count = list.childElementCount;
    nodes.assets.hidden = !count;
    replaceList(nodes.assets, key, [el("div", { class: "mb-heading" }, el("h3", { id: "mb-assets-heading", text: "Audio and images" }), el("span", { text: count ? String(count) : "" })), list]);
  }

  function paintFind() {
    if (!nodes.find) return;
    const hasStory = !!job() && !!draft();
    const key = JSON.stringify([hasStory, bin.find.busy, bin.find.error, bin.find.moments, bin.find.warnings, bin.find.verification, locked()]);
    if (nodes.find.dataset.key === key) return;
    nodes.find.dataset.key = key;
    const disabled = !hasStory || bin.find.busy;
    const submit = el("button", { type: "submit", class: "button button-secondary", disabled: disabled || !bin.find.query.trim(), "aria-label": "Find moments", title: "Find moments" }, icon("search"));
    const input = el("input", { id: "mb-find-input", type: "search", maxlength: "200", placeholder: "Someone laughing, the word price", value: bin.find.query, disabled: !hasStory, autocomplete: "off", on: { input: (event) => { bin.find.query = event.target.value; submit.disabled = disabled || !event.target.value.trim(); } } });
    const form = el("form", { role: "search", on: { submit: runFind } },
      el("label", { for: "mb-find-input", text: "Find a moment" }),
      el("div", { class: "mb-find-row" }, input, submit));
    const rows = [form];
    if (bin.find.busy) rows.push(el("p", { class: "mb-note", role: "status", text: "Muse Spark is searching your footage. SAM 3.1 then checks what it finds." }));
    if (bin.find.error) rows.push(el("p", { class: "mb-note is-error", role: "alert", text: bin.find.error }));
    (bin.find.warnings || []).forEach((warning) => rows.push(el("p", { class: "mb-note", text: warning })));
    if (Array.isArray(bin.find.moments)) {
      if (!bin.find.moments.length) rows.push(el("p", { class: "mb-note", text: "No moments matched. Try a word that was said." }));
      else {
        rows.push(verificationNote(bin.find.verification));
        rows.push(resultList(bin.find.moments, true));
      }
      if (bin.find.dropped.length) {
        rows.push(el("details", { class: "mb-dropped" },
          el("summary", { text: plural(bin.find.dropped.length, "moment") + " SAM 3.1 did not see, left out" }),
          resultList(bin.find.dropped, false)));
      }
    }
    const active = document.activeElement;
    const refocus = active && active.id === "mb-find-input";
    // verificationNote answers null when SAM checked nothing; append would print that as the word "null".
    nodes.find.replaceChildren(...rows.filter(Boolean));
    if (refocus && !input.disabled) input.focus();
  }

  /* One row per moment: its time, what Muse Spark saw, and what SAM 3.1 saw. The thumbnail
     is SAM's own frame when it sent one, with the mask drawn, else a frame of the footage. */
  function resultList(moments, addable) {
    const list = el("ul", { class: "mb-results" });
    moments.forEach((moment) => {
      const image = el("img", { alt: "", hidden: true, loading: "lazy" });
      const sam = sameOriginArtifact(moment.thumbnail_url);
      const fallback = () => frameAt(Number(moment.start)).then((frame) => { if (frame) { image.src = frame; image.hidden = false; } });
      if (sam) { image.addEventListener("load", () => { image.hidden = false; }, { once: true }); image.addEventListener("error", fallback, { once: true }); image.src = sam; }
      else fallback();
      const when = clock(moment.start) + " to " + clock(moment.end);
      const badge = seenBadge(moment);
      list.append(el("li", { class: unseen(moment) ? "is-unseen" : moment.verified === true ? "is-seen" : "" },
        el("button", { type: "button", class: "mb-thumb mb-thumb-button", title: "Preview " + when, "aria-label": "Preview the moment from " + when, on: { click: () => previewMoment(moment) } }, image),
        el("div", { class: "mb-clip-text" },
          el("strong", {}, el("button", { type: "button", class: "mb-time-button", text: when, title: "Go to " + clock(moment.start), on: { click: () => previewMoment(moment) } })),
          moment.reason ? el("span", { class: "mb-status mb-reason" }, el("span", { class: "sr", text: "Muse Spark: " }), moment.reason) : null,
          badge),
        el("div", { class: "mb-clip-actions" },
          el("button", { type: "button", class: "mb-text-button", text: "Preview", "aria-label": "Preview the moment from " + when, on: { click: () => previewMoment(moment) } }),
          addable ? el("button", { type: "button", class: "mb-text-button is-strong", text: "Add to story", "aria-label": "Add the moment from " + when + " to the story", disabled: locked(), on: { click: () => addMoment(moment) } }) : null)));
    });
    return list;
  }

  function buildScript() {
    const aspects = $("aspect") ? Array.from($("aspect").options).map((option) => ({ value: option.value, text: option.textContent })) : [{ value: "9:16", text: "9:16" }];
    const voices = scriptVoices();
    const info = scriptInfo();
    nodes.scriptImages = el("ul", { class: "mb-clips" });
    nodes.scriptImageNote = el("p", { class: "mb-note", id: "mb-script-images-note" });
    const form = el("form", { class: "mb-script-form", on: { submit: submitScript } },
      el("label", { for: "mb-script-text", text: "Your script" }),
      el("textarea", { id: "mb-script-text", name: "script", rows: "5", maxlength: "6000", required: true, placeholder: "Write what the video should say. Each paragraph becomes a scene." }),
      el("div", { class: "mb-script-row" },
        el("div", {}, el("label", { for: "mb-script-aspect", text: "Frame" }), el("select", { id: "mb-script-aspect", name: "aspect" }, aspects.map((option) => el("option", { value: option.value, text: option.text })))),
        voices.length ? el("div", {}, el("label", { for: "mb-script-voice", text: "Voice" }), el("select", { id: "mb-script-voice", name: "voice" }, voices.map((voice) => el("option", { value: voice.id, text: voice.name || voice.id, selected: voice.id === info.default_voice })))) : null),
      el("div", { class: "mb-script-pictures" },
        el("button", { type: "button", class: "text-button", text: "Add images", on: { click: () => pick("image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp", "script") } }),
        nodes.scriptImageNote),
      nodes.scriptImages,
      el("button", { type: "submit", class: "button button-secondary", id: "mb-script-submit", text: "Make a draft from this script" }),
      el("progress", { id: "mb-script-progress", max: "1", value: "0", "aria-label": "Script progress", hidden: true }),
      el("p", { class: "mb-note", id: "mb-script-status", role: "status", hidden: true }));
    nodes.script.replaceChildren(el("summary", { text: "Start from a script" }), form);
    nodes.script.dataset.voices = String(voices.length);
  }

  function paintScript() {
    if (!nodes.script) return;
    const project = job();
    const info = scriptInfo();
    if (nodes.script.dataset.voices !== String(scriptVoices().length) && !nodes.script.open) buildScript();
    const showing = !project || project.id === bin.script.jobId;
    nodes.script.hidden = !showing || clipItems().length > 0;
    if (nodes.scriptLink) nodes.scriptLink.hidden = nodes.script.hidden || !!project;
    const submit = $("mb-script-submit");
    const status = $("mb-script-status");
    const bar = $("mb-script-progress");
    if (!submit || !status) return;
    const pictures = scriptItems();
    const absent = info.available === false;
    const running = !!project && project.id === bin.script.jobId && locked();
    const needsPictures = !info.generates_images && !pictures.length;
    submit.disabled = absent || bin.script.busy || running || needsPictures;
    submit.textContent = bin.script.busy || running ? "Making your draft" : "Make a draft from this script";
    nodes.scriptImageNote.textContent = pictures.length ? plural(pictures.length, "image") + " in scene order." : info.image_reason || "";
    nodes.scriptImages.replaceChildren(...pictures.map((item) => el("li", { class: "mb-clip" + (item.status === "failed" ? " is-failed" : ""), data: { item: String(item.id) } },
      thumb(item.thumb, "image"),
      el("div", { class: "mb-clip-text" }, el("strong", { text: item.name, title: item.name }), el("span", { class: "mb-status", text: itemStatus(item) })),
      el("div", { class: "mb-clip-actions" },
        item.status === "failed" ? el("button", { type: "button", class: "mb-icon", title: "Retry", "aria-label": "Retry " + item.name, on: { click: () => retry(item) } }, icon("retry")) : null,
        el("button", { type: "button", class: "mb-icon", title: "Remove", "aria-label": "Remove " + item.name, on: { click: () => { removeItem(item); paintScript(); } } }, icon("close"))))));
    const silent = info.narration === false ? "No narration on this machine. The video will be silent, with captions." : "";
    const notes = absent ? info.reason || "Scripts are not available on this machine." : bin.script.error || (running ? project.message || "Making your draft." : bin.script.busy ? "Sending your script." : silent);
    status.hidden = !notes;
    status.className = "mb-note" + (bin.script.error ? " is-error" : "");
    status.textContent = notes;
    if (bar) { bar.hidden = !running; if (running) bar.value = clamp(finite(project.progress, 0), 0, 1); }
  }

  function paintTemplates() {
    const list = catalogue();
    if (!nodes.templates || nodes.templates.hidden) return;
    if (!list || !Array.isArray(list.templates)) {
      nodes.templates.replaceChildren(el("p", { class: "mb-empty", text: "Templates are not in this build yet." }));
      return;
    }
    const settings = view();
    const chosen = draft() ? settings.template : "";
    const line = String(Kit.sampleLine && Kit.sampleLine() || "Every clip has a story").split(/\s+/).slice(0, 4).join(" ");
    const key = JSON.stringify([chosen, line, locked(), !!draft()]);
    if (nodes.templates.dataset.key === key) { if (Kit.frames) paintFrames(Kit.frames); return; }
    nodes.templates.dataset.key = key;
    const cards = list.templates.map((template) => {
      const after = merge(list.defaults, template.edit || {});
      const style = lookStyle(after.look);
      const blank = !Object.keys(template.edit || {}).length;
      const zoomed = after.zoom.mode !== list.defaults.zoom.mode;
      return el("button", { type: "button", class: "mb-template", "aria-pressed": String(chosen === template.id), disabled: locked(), on: { click: () => applyTemplate(template) } },
        el("span", { class: "mb-ba" + (blank ? " is-blank" : "") },
          el("img", { class: "mb-ba-before", alt: "", data: { frame: "a" }, hidden: true }),
          blank ? null : el("span", { class: "mb-ba-after" },
            el("img", { alt: "", data: { frame: "a" }, hidden: true, style: { filter: style.filter, transform: zoomed ? "scale(1.14)" : "" } }),
            style.tint ? el("i", { class: "mb-tint", style: { background: style.tint.color, opacity: String(style.tint.opacity) } }) : null,
            el("span", { class: "ix-cap", data: { captionStyle: after.captions.style } }, Kit.captionWords ? Kit.captionWords(line) : line)),
          blank ? null : el("i", { class: "mb-ba-rule" })),
        el("strong", { text: template.name }),
        el("small", { text: template.description }));
    });
    nodes.templates.replaceChildren(el("div", { class: "mb mb-template-view" }, el("div", { class: "mb-panel" },
      el("p", { class: "mb-note", text: "Left is your footage. Right is the template. Your text, music, and logo stay." }),
      el("div", { class: "mb-templates" }, cards))));
    if (Kit.frames) paintFrames(Kit.frames); else refreshFrames();
  }

  function paintBin() {
    if (!bin.built) return;
    paintSource(); paintClips(); paintAssets();
    if (scriptItems().length) paintScript();
  }

  function settleProject() {
    const project = job();
    const id = project ? project.id : "";
    if (id === bin.lastJob) return;
    if (id && !bin.lastJob) {
      /* The clips became the project's source, so their rows are finished. */
      bin.items = bin.items.filter((item) => item.role !== "clip");
      bin.previewing = 0;
    } else if (bin.lastJob) {
      bin.items.filter((item) => item.asset || item.role === "clip").forEach((item) => URL.revokeObjectURL(item.url));
      bin.items = bin.items.filter((item) => item.role === "asset" && !item.asset);
      bin.assets = {}; bin.thumbs = {}; bin.previewing = 0;
    }
    bin.find = { query: "", moments: null, busy: false, error: "", warnings: [] };
    bin.lastJob = id;
  }

  function sync() {
    build();
    if (!bin.built) return;
    settleProject();
    const project = job();
    if (project) assetItems().filter((item) => item.status === "saved" && !item.asset && !item.registering && !item.error).forEach(registerItem);
    if (bin.pendingLogo && draft() && !locked()) { const asset = bin.pendingLogo; bin.pendingLogo = null; useLogo(asset); }
    const legacy = $("upload-limit");
    if (legacy) legacy.textContent = limitText();
    paintBin(); paintFind(); paintScript(); paintTemplates();
    refreshFrames();
  }

  ready(() => {
    watch();
    Kit.onChange(sync);
    sync();
  });
}());
