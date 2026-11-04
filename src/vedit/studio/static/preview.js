"use strict";

/*
 * Whole-story live preview. Plays the source through the timeline's spans in the
 * browser with nothing rendered: two video elements take turns at each cut so the
 * next piece is already decoded when the current one ends. Speed, removed pieces,
 * captions, text, the look, and the frame all follow the draft as it changes.
 * Tracked reframing, cutout, and speech cleanup only exist in a render.
 */
(function () {
  const Editor = window.BriefcutEditor;
  if (!Editor) return;
  const $ = (id) => document.getElementById(id);
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const lerp = (a, b, t) => a + (b - a) * t;
  const SLOW_ZOOM = 0.18, PUNCH_ZOOM = 0.3;
  const CUT_LEAD = 0.012;
  /* The next piece starts playing silently under the current one, so its decoder is already running at the cut. */
  const PREROLL = 0.2;
  const TEXT_Y = { top: "at-top", center: "at-center", bottom: "at-bottom" };
  const LOOKS = window.BriefcutLookStyles.preview;
  const LOOK_VIGNETTE = window.BriefcutLookStyles.previewVignette;
  const IDENTITY = { contrast: 1, saturate: 1, brightness: 1, sepia: 0, grayscale: 0, "hue-rotate": 0 };

  const engine = { a: null, b: null, active: null, standby: null, spans: [], duration: 0, index: -1, time: 0, playing: false, url: "", prepared: -1, lead: 0, rolling: false, outgoing: null, waiting: false, peeking: false, raf: 0, cue: null, cueIndex: 0, word: -1, stamp: 0, labelIndex: -2, type: "none", failed: false };
  let frame = null, nodes = null, draftEdit = null, followRaf = 0;

  function studio() { return Editor._studio(); }
  function monitor() { const s = studio(); return s ? s.state.preview : "source"; }

  function element(tag, className) { const node = document.createElement(tag); if (className) node.className = className; return node; }
  function makeVideo(id) {
    const video = element("video", "pv-video");
    video.id = id; video.playsInline = true; video.preload = "auto"; video.disablePictureInPicture = true;
    video.setAttribute("aria-hidden", "true"); video.tabIndex = -1;
    video.want = null; video.moving = false;
    video.addEventListener("seeked", () => settled(video));
    video.addEventListener("loadedmetadata", () => { if (video.want !== null) moveTo(video, video.want, true); else if (video === engine.active) present(engine.time); });
    video.addEventListener("error", () => { if (video.getAttribute("src")) { engine.failed = true; $("preview-stage").classList.add("has-error"); } });
    return video;
  }
  function build() {
    const stage = $("preview-stage");
    frame = element("div", "pv-frame"); frame.id = "pv-frame";
    engine.a = makeVideo("pv-a"); engine.b = makeVideo("pv-b");
    engine.active = engine.a; engine.standby = engine.b;
    engine.a.style.zIndex = "2"; engine.b.style.zIndex = "1";
    nodes = { tint: element("div", "pv-tint"), vignette: element("div", "pv-vignette"), images: element("div", "pv-images"), texts: element("div", "pv-texts"), caption: element("div", "pv-caption"), wash: element("div", "pv-wash"), fade: element("div", "pv-fade") };
    frame.append(engine.a, engine.b, nodes.wash, nodes.tint, nodes.vignette, nodes.images, nodes.texts, nodes.caption, nodes.fade);
    const failure = element("p", "pv-failure"); failure.textContent = "This footage does not play in the browser. Make the video to see the edit.";
    stage.append(frame, failure);
    const transport = $("transport");
    const button = (id, label, path, action) => {
      const node = element("button", "tp-button"); node.type = "button"; node.id = id; node.title = label; node.setAttribute("aria-label", label);
      node.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="' + path + '"/></svg>';
      node.addEventListener("click", action);
      return node;
    };
    transport.append(
      button("tp-prev", "Previous cut (Up arrow)", "M15 5.5 8 10l7 4.5v-9ZM5 5v10", () => Editor._jumpCut(-1)),
      button("tp-play", "Play (Space)", "M6.5 4.5v11l9-5.5-9-5.5Z", () => Editor._togglePlay()),
      button("tp-next", "Next cut (Down arrow)", "M5 5.5 12 10l-7 4.5v-9ZM15 5v10", () => Editor._jumpCut(1)),
    );
  }
  function paintPlayButton() {
    const node = $("tp-play");
    if (!node) return;
    const playing = Editor.playing();
    node.setAttribute("aria-label", playing ? "Pause (Space)" : "Play (Space)");
    node.title = node.getAttribute("aria-label");
    node.querySelector("path").setAttribute("d", playing ? "M6 4.5h2.8v11H6zM11.2 4.5H14v11h-2.8z" : "M6.5 4.5v11l9-5.5-9-5.5Z");
    node.classList.toggle("is-playing", playing);
  }

  /* ---------- Seeking without piling up ---------- */
  function moveTo(video, time, force) {
    video.want = time;
    if (video.readyState < 1) return;
    if (video.moving && !force) return;
    if (Math.abs(video.currentTime - time) < 0.004) { video.moving = false; settled(video); return; }
    video.moving = true;
    video.currentTime = time;
  }
  function settled(video) {
    video.moving = false;
    if (video.want !== null && Math.abs(video.currentTime - video.want) > 0.03) { moveTo(video, video.want); return; }
    if (engine.waiting && video === engine.standby) { engine.waiting = false; advance(); }
  }

  /* ---------- Where the clock is ---------- */
  function spanAt(time) {
    let found = -1;
    const spans = engine.spans;
    for (let index = 0; index < spans.length; index += 1) {
      if (spans[index].output_start - 1e-4 > time) break;
      if (time <= spans[index].output_end + 1e-4) found = index;
    }
    return found;
  }
  function sourceAt(span, time) { return clamp(span.source_start + (time - span.output_start) * span.speed, span.source_start, span.source_end); }
  function continuous(span, next) { return next.segment === span.segment ? Math.abs(next.source_start - span.source_end) < 0.01 && next.speed === span.speed : Math.abs(next.source_start - span.source_end) < 0.01 && next.speed === span.speed && Math.abs(next.output_start - span.output_end) < 0.005; }
  function prepareNext() {
    const span = engine.spans[engine.index];
    const next = engine.spans[engine.index + 1];
    engine.prepared = -1; engine.rolling = false; engine.lead = 0;
    if (!next || engine.outgoing) return;
    if (continuous(span, next)) return;
    engine.prepared = engine.index + 1;
    setRate(engine.standby, next);
    const roomBefore = next.output_start - Math.max(span.output_start, engine.time);
    if (roomBefore > PREROLL + 0.06 && next.source_start - PREROLL * next.speed > 0) engine.lead = PREROLL;
    moveTo(engine.standby, next.source_start - engine.lead * next.speed);
  }
  function roll(time, next) {
    const standby = engine.standby;
    if (!engine.lead || engine.rolling || engine.prepared !== engine.index + 1) return;
    if (time < next.output_start - engine.lead || standby.moving || standby.readyState < 2) return;
    engine.rolling = true;
    standby.volume = 0;
    const started = standby.play();
    if (started && started.catch) started.catch(() => {});
  }
  function setRate(video, span) { if (video.playbackRate !== span.speed) video.playbackRate = span.speed; }
  function setVolume(video, span, gain) {
    const edit = draftEdit;
    const draft = Editor.draft();
    const segment = draft && draft.segments[span.segment - 1];
    const level = segment && Number.isFinite(segment.volume) ? segment.volume : 1;
    video.muted = !!(edit && edit.audio && edit.audio.mute_source);
    video.volume = clamp(level * (gain === undefined ? 1 : gain), 0, 1);
  }
  function resetLayer(video, top) {
    video.style.opacity = ""; video.style.clipPath = ""; video.style.zIndex = top ? "2" : "1"; video.style.translate = "";
    video.dataset.moved = "";
  }

  /* ---------- Presenting one instant ---------- */
  function present(time) {
    engine.time = clamp(time, 0, engine.duration);
    const index = spanAt(engine.time);
    if (index < 0) return;
    finishOutgoing();
    const span = engine.spans[index];
    engine.index = index;
    setRate(engine.active, span);
    setVolume(engine.active, span);
    moveTo(engine.active, sourceAt(span, engine.time));
    const previous = engine.spans[index - 1];
    if (previous && previous.segment !== span.segment && engine.time < previous.output_end - 0.002) {
      /* Inside a transition both moments are on screen, playing or not. */
      const video = engine.standby;
      engine.outgoing = { video, span: previous, start: span.output_start, end: previous.output_end };
      setRate(video, previous);
      moveTo(video, sourceAt(previous, engine.time));
      blend(engine.time);
    }
    prepareNext();
    paint(engine.time);
  }
  function finishOutgoing() {
    const out = engine.outgoing;
    if (!out) return;
    out.video.pause();
    resetLayer(out.video, false);
    resetLayer(engine.active, true);
    nodes.wash.style.opacity = "0";
    engine.outgoing = null;
  }
  function advance() {
    const nextIndex = engine.index + 1;
    const next = engine.spans[nextIndex];
    const current = engine.spans[engine.index];
    if (!next) return;
    const incoming = engine.standby, outgoing = engine.active;
    if (engine.rolling) {
      if (incoming.currentTime < next.source_start - 0.02 && !incoming.paused) return;
    } else {
      if (engine.prepared !== nextIndex || engine.lead) { engine.prepared = nextIndex; engine.lead = 0; setRate(incoming, next); moveTo(incoming, next.source_start); }
      if (incoming.moving || incoming.readyState < 2) { engine.waiting = true; outgoing.pause(); return; }
    }
    const overlap = current.output_end - next.output_start;
    const blends = overlap > 0.02 && next.segment !== current.segment;
    setRate(incoming, next);
    setVolume(incoming, next, blends ? 0 : 1);
    incoming.style.zIndex = "2";
    outgoing.style.zIndex = "1";
    engine.active = incoming; engine.standby = outgoing; engine.index = nextIndex;
    engine.labelIndex = -2;
    if (blends) { engine.outgoing = { video: outgoing, span: current, start: next.output_start, end: current.output_end }; incoming.style.opacity = "0"; }
    else outgoing.pause();
    if (!engine.rolling) { const playing = incoming.play(); if (playing && playing.catch) playing.catch(() => {}); }
    engine.rolling = false;
    if (!blends) prepareNext();
  }
  function blend(time) {
    const out = engine.outgoing;
    if (!out) return;
    const progress = clamp((time - out.start) / Math.max(0.01, out.end - out.start), 0, 1);
    if (progress >= 1) { finishOutgoing(); prepareNext(); return; }
    const incoming = engine.active, outgoing = out.video;
    const type = engine.type;
    setVolume(outgoing, out.span, 1 - progress);
    const span = engine.spans[engine.index];
    if (span) setVolume(incoming, span, progress);
    nodes.wash.style.opacity = "0";
    if (type === "fadeblack" || type === "fadewhite") {
      nodes.wash.style.background = type === "fadewhite" ? "#fff" : "#000";
      nodes.wash.style.opacity = String(1 - Math.abs(progress * 2 - 1));
      incoming.style.opacity = progress < 0.5 ? "0" : "1";
    } else if (type === "slideleft") { incoming.style.opacity = "1"; incoming.style.translate = (1 - progress) * 100 + "% 0"; outgoing.style.translate = -progress * 100 + "% 0"; }
    else if (type === "slideup") { incoming.style.opacity = "1"; incoming.style.translate = "0 " + (1 - progress) * 100 + "%"; outgoing.style.translate = "0 " + -progress * 100 + "%"; }
    else if (type === "wipeleft") { incoming.style.opacity = "1"; incoming.style.clipPath = "inset(0 0 0 " + (1 - progress) * 100 + "%)"; }
    else if (type === "circleopen") { incoming.style.opacity = "1"; incoming.style.clipPath = "circle(" + progress * 75 + "% at 50% 50%)"; }
    else incoming.style.opacity = String(progress);
  }

  /* ---------- Playback ---------- */
  function loop() {
    engine.raf = 0;
    if (!engine.playing) return;
    const spans = engine.spans;
    const span = spans[engine.index];
    if (!span) { stop(); return; }
    if (!engine.waiting) {
      const video = engine.active;
      let time = span.output_start + (video.currentTime - span.source_start) / span.speed;
      time = Math.max(time, span.output_start);
      const next = spans[engine.index + 1];
      if (next) {
        if (continuous(span, next)) { if (video.currentTime >= span.source_end - 0.005) { engine.index += 1; engine.labelIndex = -2; prepareNext(); } }
        else {
          roll(time, next);
          if (time >= next.output_start - CUT_LEAD) advance();
        }
      } else if (time >= span.output_end - 0.01 || video.ended) {
        engine.time = engine.duration; paint(engine.duration); Editor._tick(engine.duration); stop(); return;
      }
      if (video.currentTime > span.source_end + 0.02 && next && !continuous(span, next) && engine.index < spans.length - 1) time = span.output_end;
      engine.time = clamp(time, 0, engine.duration);
      blend(engine.time);
      paint(engine.time);
      Editor._tick(engine.time);
    }
    engine.raf = requestAnimationFrame(loop);
  }
  function start() {
    if (!engine.spans.length || engine.playing) return;
    if (monitor() !== "story") studio().setPreview("story");
    if (engine.time >= engine.duration - 0.03) present(0);
    engine.playing = true;
    const span = engine.spans[engine.index];
    if (span) { setRate(engine.active, span); setVolume(engine.active, span); }
    const playing = engine.active.play();
    if (playing && playing.catch) playing.catch(() => { stop(); });
    if (engine.outgoing) { const fading = engine.outgoing.video.play(); if (fading && fading.catch) fading.catch(() => {}); }
    syncMusic(true);
    paintPlayButton();
    if (!engine.raf) engine.raf = requestAnimationFrame(loop);
  }
  function stop() {
    engine.playing = false; engine.waiting = false;
    if (engine.raf) cancelAnimationFrame(engine.raf);
    engine.raf = 0;
    engine.a.pause(); engine.b.pause();
    finishOutgoing();
    if (engine.rolling) { engine.rolling = false; if (engine.index >= 0) { engine.time = Math.min(engine.time, engine.duration); prepareNext(); } }
    syncMusic(false);
    paintPlayButton();
  }

  /* ---------- Music ---------- */
  function assetURL(id) {
    const job = Editor.job();
    return job && job.assets && job.assets[id] ? "/api/campaigns/" + encodeURIComponent(job.id) + "/assets?id=" + encodeURIComponent(id) : "";
  }
  function syncMusic(playing) {
    const audio = nodes.music;
    if (!audio) return;
    const edit = draftEdit;
    const music = edit && edit.music;
    if (!playing || !music) { audio.pause(); return; }
    const url = assetURL(music.asset);
    if (!url) return;
    if (audio.getAttribute("src") !== url) audio.src = url;
    audio.volume = clamp(Math.pow(10, (Number(music.volume_db) || -18) / 20), 0, 1);
    try { audio.currentTime = (Number(music.start) || 0) + engine.time; } catch (_) { /* The file is still loading. */ }
    const started = audio.play();
    if (started && started.catch) started.catch(() => {});
  }

  /* ---------- Overlays ---------- */
  function paint(time) {
    paintCaption(time);
    paintTexts(time);
    paintImages(time);
    paintFade(time);
    paintMotion(time);
    paintReadout(time);
  }
  function wordsOf(cue) {
    if (cue.pvWords) return cue.pvWords;
    if (Array.isArray(cue.words) && cue.words.length) { cue.pvWords = cue.words.map((word) => ({ text: String(word.text || "").trim(), start: word.start, end: word.end })).filter((word) => word.text); return cue.pvWords; }
    const parts = String(cue.text || "").split(/\s+/).filter(Boolean);
    const total = parts.reduce((sum, part) => sum + part.length + 1, 0) || 1;
    let used = 0;
    cue.pvWords = parts.map((part) => { const start = cue.start + (cue.end - cue.start) * used / total; used += part.length + 1; return { text: part, start, end: cue.start + (cue.end - cue.start) * used / total }; });
    return cue.pvWords;
  }
  /* Captions and the frame are chosen per render, so the draft's choice wins over the project's. */
  function chosen(name) {
    const draft = Editor.draft();
    const options = draft && draft.render_options;
    if (options && options[name] !== undefined) return options[name];
    const job = Editor.job();
    return job && job.request ? job.request[name] : undefined;
  }
  function captionsOn() { return !!chosen("captions") || !!(draftEdit && draftEdit.captions && Object.keys(draftEdit.captions.edits || {}).length); }
  function findCue(cues, time) {
    let low = 0, high = cues.length;
    while (low < high) { const middle = (low + high) >> 1; if (cues[middle].end <= time) low = middle + 1; else high = middle; }
    const cue = cues[low];
    return cue && cue.start <= time ? cue : null;
  }
  /* Punch words: the draft's picks, else the ones Muse Spark made for the last render.
     A pick counts only while its line still has that word at that place, as in the render. */
  function punchPicks() {
    const captions = draftEdit && draftEdit.captions || {};
    if (captions.emphasize !== true) return [];
    const own = Array.isArray(captions.emphasis) ? captions.emphasis : [];
    if (own.length) return own;
    const job = Editor.job();
    const result = job && job.result;
    return result && Array.isArray(result.emphasis) ? result.emphasis : [];
  }
  function wordKey(text) { return String(text || "").replace(/^[^\p{L}\p{N}_]+|[^\p{L}\p{N}_]+$/gu, "").toLowerCase(); }
  function punchIndexes(cue, words) {
    const picks = punchPicks();
    const marked = [];
    if (!picks.length) return marked;
    const key = cue.key !== undefined && cue.key !== null ? String(cue.key) : String(Math.round(Number(cue.source_start !== undefined ? cue.source_start : cue.start) * 1000));
    const tokens = String(cue.text || "").split(/\s+/).filter(Boolean);
    picks.forEach((pick) => {
      if (!pick || String(pick.cue_key) !== key) return;
      const index = Number(pick.word_index), target = wordKey(pick.word);
      if (!target || !Number.isInteger(index) || index < 0 || index >= tokens.length || wordKey(tokens[index]) !== target) return;
      if (words.length === tokens.length && wordKey(words[index].text) === target) { marked.push(index); return; }
      const nth = tokens.slice(0, index).filter((token) => wordKey(token) === target).length;
      let seen = 0;
      for (let at = 0; at < words.length; at += 1) { if (wordKey(words[at].text) !== target) continue; if (seen === nth) { marked.push(at); break; } seen += 1; }
    });
    return marked;
  }
  function chunkOf(words, index) {
    const size = 3;
    const begin = Math.floor(Math.max(0, index) / size) * size;
    return [begin, Math.min(words.length, begin + size)];
  }
  function paintCaption(time) {
    const root = nodes.caption;
    const cues = captionsOn() ? Editor.timeline().captions || [] : [];
    const cue = findCue(cues, time);
    const settings = draftEdit && draftEdit.captions || {};
    const style = settings.style || "boxed";
    if (!cue) { if (engine.cue) { root.replaceChildren(); engine.cue = null; engine.word = -1; } return; }
    const words = wordsOf(cue);
    let current = -1;
    for (let index = 0; index < words.length; index += 1) { if (words[index].start <= time) current = index; else break; }
    const pop = style === "pop";
    const range = pop ? chunkOf(words, current) : [0, words.length];
    const punch = punchIndexes(cue, words);
    const key = cue.start + "|" + range[0] + "|" + style + "|" + cue.text + "|" + punch.join(",");
    if (engine.cue !== key) {
      engine.cue = key; engine.word = -2;
      const line = element("p", "pv-line");
      words.slice(range[0], range[1]).forEach((word, index) => {
        const node = element("span", punch.includes(index + range[0]) ? "pv-word is-punch" : "pv-word"); node.textContent = word.text;
        if (index) line.append(" ");
        line.append(node);
      });
      root.replaceChildren(line);
    }
    if (current !== engine.word) {
      engine.word = current;
      const spans = root.querySelectorAll(".pv-word");
      spans.forEach((node, index) => {
        const at = index + range[0];
        node.classList.toggle("is-said", style === "karaoke" && at <= current);
        node.classList.toggle("is-now", (style === "pop" || style === "karaoke") && at === current);
      });
    }
  }
  function paintTexts(time) {
    for (const node of nodes.texts.children) { const on = time >= node.pvStart && time < node.pvEnd; if (on === node.hidden) node.hidden = !on; }
  }
  function paintImages(time) {
    for (const node of nodes.images.children) { if (node.pvStart === undefined) continue; const on = time >= node.pvStart && time < node.pvEnd; if (on === node.hidden) node.hidden = !on; }
  }
  function paintFade(time) {
    const fade = draftEdit && draftEdit.fade || {};
    const fadeIn = Number(fade.in) || 0, fadeOut = Number(fade.out) || 0;
    let dark = 0;
    if (fadeIn > 0 && time < fadeIn) dark = 1 - time / fadeIn;
    if (fadeOut > 0 && time > engine.duration - fadeOut) dark = Math.max(dark, 1 - (engine.duration - time) / fadeOut);
    const text = dark > 0.002 ? String(dark) : "0";
    if (nodes.fade.style.opacity !== text) nodes.fade.style.opacity = text;
  }
  function momentWindow(segment) {
    let start = null, end = 0;
    for (const span of engine.spans) { if (span.segment !== segment) continue; if (start === null) start = span.output_start; end = span.output_end; }
    return start === null ? null : [start, end];
  }
  function motionAt(span, time) {
    const draft = Editor.draft();
    const segment = draft && draft.segments[span.segment - 1];
    const zoom = draftEdit && draftEdit.zoom || {};
    let from = null, to = null;
    if (segment && segment.motion && segment.motion.from && segment.motion.to) { from = segment.motion.from; to = segment.motion.to; }
    else if (zoom.mode === "slow") { from = { scale: 1, x: 0.5, y: 0.5 }; to = { scale: 1 + SLOW_ZOOM * (Number(zoom.strength) || 0), x: 0.5, y: 0.5 }; }
    else if (zoom.mode === "punch" && span.segment % 2 === 0) { from = { scale: 1 + PUNCH_ZOOM * (Number(zoom.strength) || 0), x: 0.5, y: 0.5 }; to = from; }
    if (!from) return "";
    const window_ = momentWindow(span.segment);
    const progress = window_ ? clamp((time - window_[0]) / Math.max(0.01, window_[1] - window_[0]), 0, 1) : 0;
    const scale = lerp(from.scale, to.scale, progress), x = lerp(from.x, to.x, progress), y = lerp(from.y, to.y, progress);
    return "scale(" + scale.toFixed(4) + ") translate(" + ((0.5 - x) * 100).toFixed(2) + "%," + ((0.5 - y) * 100).toFixed(2) + "%)";
  }
  function paintMotion(time) {
    const span = engine.spans[engine.index];
    if (!span) return;
    const value = motionAt(span, time);
    if (engine.active.style.transform !== value) engine.active.style.transform = value;
  }
  function stamp(seconds) {
    const value = Math.max(0, seconds);
    const whole = Math.floor(value + 1e-6);
    return String(Math.floor(whole / 60)).padStart(2, "0") + ":" + String(whole % 60).padStart(2, "0") + "." + Math.floor((value - whole + 1e-6) * 10);
  }
  function paintReadout(time) {
    if (engine.playing && performance.now() - engine.stamp < 80) return;
    engine.stamp = performance.now();
    $("playback-time").textContent = stamp(time) + " / " + stamp(engine.duration);
    if (engine.index !== engine.labelIndex) {
      engine.labelIndex = engine.index;
      const span = engine.spans[engine.index];
      const draft = Editor.draft();
      const segment = span && draft && draft.segments[span.segment - 1];
      if (segment) $("preview-label").textContent = "Moment " + span.segment + ": " + (segment.label || "Selected footage");
    }
  }

  /* ---------- Following the draft ---------- */
  function lookFilter(look) {
    if (!look) return "";
    const parts = new Map();
    const strength = Number.isFinite(look.intensity) ? look.intensity : 1;
    for (const [name, value] of LOOKS[look.preset] || []) parts.set(name, lerp(IDENTITY[name], value, strength));
    const multiply = (name, factor) => { if (factor !== 1) parts.set(name, (parts.has(name) ? parts.get(name) : 1) * factor); };
    multiply("brightness", 1 + (Number(look.brightness) || 0) * 0.45);
    multiply("contrast", 1 + (Number(look.contrast) || 0) * 0.5);
    multiply("saturate", 1 + (Number(look.saturation) || 0));
    return [...parts].map(([name, value]) => name + "(" + (Math.round(value * 1000) / 1000) + (name === "hue-rotate" ? "deg" : "") + ")").join(" ");
  }
  function hexToRgb(value) { const n = parseInt(String(value).slice(1), 16); return [n >> 16 & 255, n >> 8 & 255, n & 255]; }
  function applyStyle() {
    const draft = Editor.draft();
    draftEdit = draft && draft.edit || null;
    const edit = draftEdit;
    const job = Editor.job();
    const aspect = String(chosen("aspect") || "9:16").split(":").map(Number);
    frame.style.setProperty("--ar", (aspect[0] / aspect[1] || 0.5625).toFixed(5));
    if (!edit) return;
    const look = edit.look || {};
    const filter = lookFilter(look);
    engine.a.style.filter = filter; engine.b.style.filter = filter;
    const temperature = Number(look.temperature) || 0;
    nodes.tint.style.background = temperature > 0 ? "rgb(255 150 40 / " + temperature * 0.3 + ")" : temperature < 0 ? "rgb(60 130 255 / " + -temperature * 0.3 + ")" : "transparent";
    const vignette = Math.max(Number(look.vignette) || 0, (LOOK_VIGNETTE[look.preset] || 0) * (Number.isFinite(look.intensity) ? look.intensity : 1));
    nodes.vignette.style.opacity = String(vignette * 1.2);
    const captions = edit.captions || {};
    const root = nodes.caption;
    root.className = "pv-caption is-" + (captions.style || "boxed") + " at-" + (captions.position || "bottom");
    root.style.setProperty("--cap-scale", String(Number(captions.scale) || 1));
    root.style.setProperty("--cap-color", captions.color || "#FFFFFF");
    root.style.setProperty("--cap-high", captions.highlight || "#FFD60A");
    root.style.setProperty("--cap-punch", captions.emphasis_color || "#3CE88C");
    engine.cue = null;
    const transition = edit.transition || {};
    engine.type = transition.type || "none";
    nodes.texts.replaceChildren(...(edit.texts || []).map((item) => {
      const node = element("div", "pv-text is-" + (item.style || "label") + " " + (TEXT_Y[item.position] || "at-center"));
      const words = element("span"); words.textContent = item.text;
      node.append(words);
      node.style.setProperty("--text-scale", String(Number(item.scale) || 1));
      node.style.setProperty("--text-color", item.color || "#FFFFFF");
      const [r, g, b] = hexToRgb(item.color || "#FFFFFF");
      node.style.setProperty("--text-box", "rgb(" + r + " " + g + " " + b + ")");
      node.pvStart = Number(item.start) || 0; node.pvEnd = node.pvStart + (Number(item.duration) || 0);
      node.hidden = true;
      return node;
    }));
    const pictures = [];
    const addPicture = (item, timed) => {
      const url = assetURL(item.asset);
      if (!url) return;
      const node = element("img", "pv-image at-" + (item.position || "center"));
      node.src = url; node.alt = "";
      node.style.setProperty("--img-scale", String(Number(item.scale) || 0.3));
      if (item.opacity !== undefined) node.style.opacity = String(item.opacity);
      if (timed) { node.pvStart = Number(item.start) || 0; node.pvEnd = node.pvStart + (Number(item.duration) || 0); node.hidden = true; }
      pictures.push(node);
    };
    (edit.overlays || []).forEach((item) => addPicture(item, true));
    if (edit.logo) addPicture(edit.logo, false);
    nodes.images.replaceChildren(...pictures);
    paint(engine.time);
  }
  function onTimeline(next) {
    syncSource();
    const spans = Array.isArray(next.spans) ? next.spans : [];
    const sameCut = spans.length === engine.spans.length && spans.every((span, index) => { const old = engine.spans[index]; return old.source_start === span.source_start && old.source_end === span.source_end && old.output_start === span.output_start && old.speed === span.speed; });
    engine.spans = [...spans].sort((a, b) => a.output_start - b.output_start);
    engine.duration = Number(next.duration) || 0;
    engine.cue = null;
    if (sameCut && spans.length) { paint(engine.time); return; }
    if (engine.playing) stop();
    if (engine.time > engine.duration) engine.time = engine.duration;
    if (monitor() === "story" && !engine.peeking) present(engine.time);
    Editor._tick(engine.time);
  }
  function syncSource() {
    const s = studio();
    const url = s && s.state.sourceURL || "";
    if (url === engine.url) return;
    engine.url = url; engine.failed = false;
    $("preview-stage").classList.remove("has-error");
    stop();
    for (const video of [engine.a, engine.b]) {
      video.want = null; video.moving = false;
      if (url) { video.src = url; video.load(); } else { video.removeAttribute("src"); video.load(); }
    }
    engine.time = 0; engine.index = -1;
  }

  /* ---------- Transport shared with the timeline ---------- */
  const transport = {
    seek(time, options) {
      const mode = monitor();
      const player = $("player");
      if (mode === "final") {
        const dirty = Editor.draftChanged && Editor.draftChanged();
        if (!dirty) { player.currentTime = time; Editor._tick(time); return; }
        studio().setPreview("story");
      } else if (mode === "source") studio().setPreview("story");
      const wasPlaying = engine.playing;
      if (wasPlaying && options && options.scrub) stop();
      present(time);
      Editor._tick(engine.time);
      if (wasPlaying && !(options && options.scrub)) { engine.playing = false; start(); }
    },
    play() {
      const mode = monitor();
      if (mode === "story") { start(); return; }
      const player = $("player");
      const started = player.play();
      if (started && started.catch) started.catch(() => {});
    },
    pause() { stop(); $("player").pause(); },
    playing() { return monitor() === "story" ? engine.playing : !$("player").paused; },
  };
  function shownChanged(version) {
    if (version === "story") {
      syncSource();
      applyStyle();
      present(engine.time);
      $("playback-time").textContent = stamp(engine.time) + " / " + stamp(engine.duration);
      engine.labelIndex = -2;
      paintReadout(engine.time);
      $("monitor-meta").textContent = (chosen("aspect") || "9:16") + " live preview";
      $("monitor-meta").title = "Plays your edit without rendering. Subject tracking, cutout, and speech cleanup appear in the finished video.";
    } else {
      stop();
      $("monitor-meta").title = "";
      const player = $("player");
      $("playback-time").textContent = stamp(player.currentTime) + " / " + stamp(Number.isFinite(player.duration) ? player.duration : 0);
    }
    paintPlayButton();
  }
  function followPlayer() {
    followRaf = 0;
    const player = $("player");
    if (player.paused || monitor() === "story") return;
    tickFromPlayer();
    followRaf = requestAnimationFrame(followPlayer);
  }
  function tickFromPlayer() {
    const player = $("player");
    const mode = monitor();
    if (mode === "final") Editor._tick(player.currentTime);
    else if (mode === "source") { const output = Editor.toOutput(player.currentTime); if (output !== null) Editor._tick(output); }
  }
  function attach() {
    build();
    nodes.music = element("audio"); nodes.music.preload = "auto"; nodes.music.loop = true;
    Editor._transport(transport);
    /* While an edge is dragged the monitor shows that edge's frame. Null hands it back to the playhead. */
    Editor._peek = (time) => {
      if (monitor() !== "story") return;
      if (time === null) { engine.peeking = false; present(engine.time); return; }
      if (engine.playing) stop();
      finishOutgoing();
      engine.peeking = true;
      moveTo(engine.active, time);
    };
    Editor.on("timeline", onTimeline);
    Editor.on("change", applyStyle);
    Editor.on("job", () => { syncSource(); applyStyle(); });
    Editor.on("preview", shownChanged);
    const player = $("player");
    player.addEventListener("play", () => { if (!followRaf) followRaf = requestAnimationFrame(followPlayer); paintPlayButton(); });
    player.addEventListener("pause", paintPlayButton);
    player.addEventListener("seeked", tickFromPlayer);
    player.addEventListener("timeupdate", () => { if (player.paused) tickFromPlayer(); });
  }
  attach();
}());
