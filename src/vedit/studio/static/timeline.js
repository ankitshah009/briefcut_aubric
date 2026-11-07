"use strict";

/*
 * The Briefcut editor core, the timeline, and the transcript editor.
 *
 * window.BriefcutEditor is what every creative panel builds on:
 *
 *   draft()                 The live draft plan. Read from it; mutate it only inside change().
 *   change(label, fn, opts) Runs fn(draft), records undo, saves, and re-resolves the timeline.
 *                           Repeated calls with one label inside a moment become one undo step,
 *                           so sliders stay usable. Pass { coalesce: false } for discrete edits.
 *   on(event, fn)           change, job, timeline, seek, select (also selection), preview. Returns an unsubscribe.
 *   job()                   The current project.
 *   capabilities()          GET /api/capabilities, including .editor catalogues when the server has them.
 *   timeline()              { spans, duration, transition_seconds, captions, estimate } on the output clock.
 *                           estimate is true until the server has confirmed the current draft.
 *   seek(outputSeconds)     Moves the playhead. The monitor follows.
 *   api(path, options)      The Studio request helper. A path not starting with /api/ is relative
 *                           to the current project, so "/assets" means /api/campaigns/{id}/assets.
 *
 * Also available: selection(), select(item), undo(), redo(), canUndo(), canRedo(), playhead(),
 * play(), pause(), playing(), toSource(t), toOutput(s), draftChanged(), clock, locked().
 *
 * src/vedit/timeline.py owns the arithmetic. The Clock below mirrors it only so a
 * drag can repaint on the next frame; the server's answer replaces it within moments.
 */
(function () {
  const finiteNumber = (value) => typeof value === "number" && Number.isFinite(value);
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const round3 = (value) => Math.round(value * 1000) / 1000;
  const round2 = (value) => Math.round(value * 100) / 100;

  /* ---------- The one clock, mirrored for instant repaints ---------- */
  const Clock = (() => {
    const MIN_KEEP = 0.08, MIN_SPEED = 0.25, MAX_SPEED = 4, PAUSE_SECONDS = 0.6, PAUSE_PAD = 0.12, TRANSITION_SHARE = 0.4, FILLER_PAD = 0.02, STUTTER_GAP = 0.4;
    const FILLERS = new Set(["um", "umm", "uhm", "uh", "uhh", "er", "erm", "ah", "ahh", "hmm", "hm", "mm", "mhm"]);
    const WORD = /[\p{L}\p{N}]+(?:['’][\p{L}\p{N}]+)?/u;
    const plain = (word) => { const found = String(word).toLowerCase().match(WORD); return found ? found[0] : ""; };

    function speedOf(segment) { const speed = segment.speed; return !finiteNumber(speed) || speed <= 0 ? 1 : clamp(speed, MIN_SPEED, MAX_SPEED); }
    function keepsOf(segment) {
      const start = segment.start, end = segment.end;
      if (!finiteNumber(start) || !finiteNumber(end) || end <= start) return [];
      const pieces = [];
      if (Array.isArray(segment.keep)) {
        for (const item of segment.keep) {
          const lo = Array.isArray(item) ? item[0] : item && item.start, hi = Array.isArray(item) ? item[1] : item && item.end;
          if (!finiteNumber(lo) || !finiteNumber(hi)) continue;
          const a = Math.max(lo, start), b = Math.min(hi, end);
          if (b - a >= MIN_KEEP) pieces.push([a, b]);
        }
      }
      if (!pieces.length) return [[start, end]];
      pieces.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      const merged = [pieces[0]];
      for (const piece of pieces.slice(1)) {
        const last = merged[merged.length - 1];
        if (piece[0] <= last[1] + 1e-6) last[1] = Math.max(last[1], piece[1]); else merged.push(piece);
      }
      return merged;
    }
    function outputSeconds(segment) { return keepsOf(segment).reduce((sum, [lo, hi]) => sum + hi - lo, 0) / speedOf(segment); }
    function subtract(start, end, removals) {
      const cuts = removals.map(([lo, hi]) => [Math.max(lo, start), Math.min(hi, end)]).filter(([lo, hi]) => hi > lo).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      const keeps = [];
      let cursor = start;
      for (const [lo, hi] of cuts) {
        if (lo - cursor >= MIN_KEEP) keeps.push([cursor, lo]);
        cursor = Math.max(cursor, hi);
      }
      if (end - cursor >= MIN_KEEP) keeps.push([cursor, end]);
      return keeps;
    }
    function pauseRemovals(speech, start, end) {
      const bands = [];
      for (const band of speech) {
        if (finiteNumber(band.start) && finiteNumber(band.end) && Math.min(band.end, end) > Math.max(band.start, start)) bands.push([Math.max(band.start, start), Math.min(band.end, end)]);
      }
      if (!bands.length) return [];
      bands.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      const removals = [];
      let cursor = start;
      for (const [lo, hi] of bands) {
        if (lo - cursor >= PAUSE_SECONDS) {
          const begin = cursor > start ? cursor + PAUSE_PAD : cursor;
          if (lo - PAUSE_PAD - begin >= MIN_KEEP) removals.push([begin, lo - PAUSE_PAD]);
        }
        cursor = Math.max(cursor, hi);
      }
      if (end - cursor >= PAUSE_SECONDS && end - cursor - PAUSE_PAD >= MIN_KEEP) removals.push([cursor + PAUSE_PAD, end]);
      return removals;
    }
    function spokenWords(text) {
      const found = String(text).toLowerCase().match(new RegExp(WORD.source, "gu"));
      return found || [];
    }
    function onlyFiller(text) {
      const words = spokenWords(text);
      return words.length > 0 && words.every((word) => FILLERS.has(word));
    }
    function sameLine(left, right) {
      const plainLeft = spokenWords(left).join(" ");
      return plainLeft.length > 0 && plainLeft === spokenWords(right).join(" ");
    }
    function overlaps(lo, hi, start, end) { return Math.min(hi, end) > Math.max(lo, start); }
    function heardIntervals(words) {
      const intervals = [];
      for (const word of words) {
        if (word.estimated) continue;
        if (finiteNumber(word.start) && finiteNumber(word.end) && word.end > word.start) intervals.push([word.start, word.end]);
      }
      return intervals;
    }
    function heardOverlaps(heard, lo, hi) { return heard.some(([wordLo, wordHi]) => overlaps(wordLo, wordHi, lo, hi)); }
    function paddedCut(lo, hi, start, end) {
      if (!overlaps(lo, hi, start, end)) return null;
      const cutLo = Math.max(start, lo - FILLER_PAD), cutHi = Math.min(end, hi + FILLER_PAD);
      return cutHi > cutLo ? [cutLo, cutHi] : null;
    }
    function transcriptLines(cues) {
      const lines = [];
      for (const cue of cues) {
        if (!cue || !finiteNumber(cue.start) || !finiteNumber(cue.end) || cue.end <= cue.start) continue;
        const text = String(cue.text || "").trim();
        if (text) lines.push([cue.start, cue.end, text]);
      }
      lines.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      return lines;
    }
    function fillerRemovals(words, cues, start, end) {
      const inside = [];
      for (const word of words) {
        if (word.estimated) continue;
        if (finiteNumber(word.start) && finiteNumber(word.end) && word.end > word.start && word.start >= start && word.end <= end) inside.push([word.start, word.end, plain(word.text || "")]);
      }
      const removals = [];
      inside.forEach(([lo, hi, text], index) => {
        if (FILLERS.has(text)) removals.push([Math.max(start, lo - FILLER_PAD), Math.min(end, hi + FILLER_PAD)]);
        else if (index + 1 < inside.length && text && text === inside[index + 1][2] && inside[index + 1][0] - hi <= STUTTER_GAP) removals.push([Math.max(start, lo - FILLER_PAD), inside[index + 1][0]]);
      });
      const heard = heardIntervals(words);
      const lines = transcriptLines(cues);
      for (const [lo, hi, text] of lines) {
        if (!onlyFiller(text) || heardOverlaps(heard, lo, hi)) continue;
        const cut = paddedCut(lo, hi, start, end);
        if (cut) removals.push(cut);
      }
      for (let index = 0; index < lines.length - 1; index += 1) {
        const firstHi = lines[index][1], firstText = lines[index][2];
        const [secondLo, secondHi, secondText] = lines[index + 1];
        if (secondLo - firstHi > STUTTER_GAP || !sameLine(firstText, secondText) || heardOverlaps(heard, secondLo, secondHi)) continue;
        const cut = paddedCut(secondLo, secondHi, start, end);
        if (cut) removals.push(cut);
      }
      return removals;
    }
    function resolve(plan) {
      const edit = plan.edit && typeof plan.edit === "object" ? plan.edit : {};
      const analysis = plan.analysis && typeof plan.analysis === "object" ? plan.analysis : {};
      const speech = (analysis.speech || []).filter((item) => item && typeof item === "object");
      const words = (analysis.words || []).filter((item) => item && typeof item === "object");
      const cues = (analysis.transcript || []).filter((item) => item && typeof item === "object");
      const manual = [];
      for (const item of edit.removed || []) {
        const lo = Array.isArray(item) ? item[0] : item && item.start, hi = Array.isArray(item) ? item[1] : item && item.end;
        if (finiteNumber(lo) && finiteNumber(hi) && hi > lo) manual.push([lo, hi]);
      }
      return (plan.segments || []).filter((segment) => segment && typeof segment === "object").map((segment) => {
        const item = Object.assign({}, segment);
        delete item.keep;
        const { start, end } = item;
        if (!finiteNumber(start) || !finiteNumber(end) || end <= start) return item;
        const removals = manual.slice();
        if (edit.trim_pauses) removals.push(...pauseRemovals(speech, start, end));
        if (edit.remove_fillers) removals.push(...fillerRemovals(words, cues, start, end));
        if (removals.length) {
          const keeps = subtract(start, end, removals);
          if (keeps.length && !(keeps.length === 1 && keeps[0][0] === start && keeps[0][1] === end)) item.keep = keeps.map(([lo, hi]) => [round3(lo), round3(hi)]);
        }
        return item;
      });
    }
    function plan(planValue) {
      const segments = resolve(planValue);
      const edit = planValue.edit && typeof planValue.edit === "object" ? planValue.edit : {};
      const transition = edit.transition && typeof edit.transition === "object" ? edit.transition : {};
      const wanted = transition.type && transition.type !== "none" ? Number(transition.seconds) || 0 : 0;
      const lengths = segments.map(outputSeconds);
      const overlap = wanted <= 0 || lengths.length < 2 ? 0 : Math.round(Math.min(wanted, TRANSITION_SHARE * Math.min(...lengths)) * 1e6) / 1e6;
      const spans = [];
      let clock = 0;
      segments.forEach((segment, index) => {
        const keeps = keepsOf(segment);
        if (!keeps.length) return;
        const speed = speedOf(segment);
        if (spans.length) clock -= overlap;
        for (const [lo, hi] of keeps) {
          const length = (hi - lo) / speed;
          spans.push({ segment: index + 1, source_start: round3(lo), source_end: round3(hi), output_start: round3(clock), output_end: round3(clock + length), speed });
          clock += length;
        }
      });
      return { segments, spans, transition_seconds: overlap, duration: round3(spans.reduce((most, span) => Math.max(most, span.output_end), 0)) };
    }
    function mapInterval(spans, start, end) {
      const pieces = [];
      for (const span of spans) {
        const lo = Math.max(start, span.source_start), hi = Math.min(end, span.source_end);
        if (hi - lo <= 1e-6) continue;
        const begin = span.output_start + (lo - span.source_start) / span.speed, finish = span.output_start + (hi - span.source_start) / span.speed;
        const last = pieces[pieces.length - 1];
        if (last && last[0] === span.segment && begin - last[2] < 1e-3) last[2] = finish;
        else pieces.push([span.segment, begin, finish]);
      }
      return pieces;
    }
    function shareText(text, weights) {
      const words = text.split(/\s+/).filter(Boolean);
      const total = weights.reduce((sum, weight) => sum + weight, 0);
      if (total <= 0 || weights.length < 2) return [text];
      const parts = [];
      let used = 0, spent = 0;
      weights.forEach((weight, index) => {
        spent += weight;
        const upto = index === weights.length - 1 ? words.length : Math.round(words.length * spent / total);
        parts.push(words.slice(used, upto).join(" "));
        used = Math.max(used, upto);
      });
      return parts;
    }
    function mapCues(cues, spans) {
      const moved = [];
      for (const cue of cues) {
        const { start, end } = cue;
        const text = String(cue.text || "").trim();
        if (!finiteNumber(start) || !finiteNumber(end) || end <= start || !text) continue;
        const words = Array.isArray(cue.words) ? cue.words : null;
        if (words && words.length) {
          const placed = new Map();
          for (const word of words) {
            if (!word || !finiteNumber(word.start) || !finiteNumber(word.end) || word.end < word.start) continue;
            const pieces = mapInterval(spans, word.start, Math.max(word.end, word.start + 1e-3));
            if (!pieces.length) continue;
            const key = pieces[0][0];
            if (!placed.has(key)) placed.set(key, []);
            placed.get(key).push({ start: round3(pieces[0][1]), end: round3(pieces[pieces.length - 1][2]), text: String(word.text || "") });
          }
          for (const list of placed.values()) moved.push({ start: list[0].start, end: list[list.length - 1].end, text: list.map((item) => item.text.trim()).filter(Boolean).join(" "), words: list, source_start: start, key: cue.key });
          continue;
        }
        const pieces = mapInterval(spans, start, end);
        if (!pieces.length) continue;
        const groups = new Map();
        for (const [segment, begin, finish] of pieces) {
          const group = groups.get(segment);
          if (group) { group[0] = Math.min(group[0], begin); group[1] = Math.max(group[1], finish); } else groups.set(segment, [begin, finish]);
        }
        const ranges = [...groups.values()];
        const shares = shareText(text, ranges.map(([lo, hi]) => hi - lo));
        ranges.forEach(([lo, hi], index) => { if (shares[index]) moved.push({ start: round3(lo), end: round3(hi), text: shares[index], source_start: start, key: cue.key }); });
      }
      moved.sort((a, b) => a.start - b.start || a.end - b.end);
      return moved;
    }
    function toSource(spans, output) {
      let found = null;
      for (const span of spans) {
        if (span.output_start - 1e-6 <= output && output <= span.output_end + 1e-6) found = { segment: span.segment, time: span.source_start + (output - span.output_start) * span.speed, span };
      }
      return found;
    }
    function toOutput(spans, source, segment) {
      for (const span of spans) {
        if (segment !== undefined && span.segment !== segment) continue;
        if (span.source_start - 1e-6 <= source && source <= span.source_end + 1e-6) return span.output_start + (source - span.source_start) / span.speed;
      }
      return null;
    }
    return { plan, resolve, keepsOf, speedOf, outputSeconds, mapCues, toSource, toOutput, MIN_SPEED, MAX_SPEED };
  })();
  window.BriefcutClock = Clock;

  /* ---------- Core state ---------- */
  const HISTORY_LIMIT = 200;
  const PERSISTED_STEPS = 60;
  const PERSISTED_BYTES = 600000;
  const COALESCE_MS = 700;
  const RESOLVE_DELAY = 110;
  const SNAPSHOT_SKIP = new Set(["analysis", "timeline", "usage", "provider", "source_duration", "duration", "warnings", "decisions", "ignored_edits", "effect"]);
  const EDIT_FALLBACK = { look: { preset: "none", intensity: 1, brightness: 0, contrast: 0, saturation: 0, temperature: 0, vignette: 0, sharpen: 0 }, transition: { type: "none", seconds: 0.35 }, captions: { style: "boxed", position: "bottom", scale: 1, color: "#FFFFFF", highlight: "#FFD60A", language: null, edits: {} }, trim_pauses: false, remove_fillers: false, removed: [], audio: { enhance_voice: false, denoise: false, keep_loudness: false, mute_source: false }, music: null, zoom: { mode: "none", strength: 0.5 }, texts: [], overlays: [], logo: null, fade: { in: 0, out: 0 } };

  const listeners = new Map();
  let studio = null;
  let boundDraft = null;
  let baseline = {};
  let history = { past: [], future: [] };
  let current = { spans: [], duration: 0, transition_seconds: 0, captions: [], estimate: true };
  let resolveTimer = null, resolveController = null, resolveSeq = 0, routeMissing = false;
  let playhead = 0;
  let selection = { kind: "none" };
  let transport = null;
  let lastJob = null;
  let captionMemo = null;

  function emit(event, ...args) { for (const fn of [...(listeners.get(event) || [])]) { try { fn(...args); } catch (error) { console.error("Briefcut " + event + " listener failed", error); } } }
  function on(event, fn) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(fn);
    if ((event === "job" && lastJob) || (event === "timeline" && boundDraft)) queueMicrotask(() => { if (listeners.get(event) && listeners.get(event).has(fn)) fn(event === "job" ? lastJob : current); });
    return () => listeners.get(event).delete(fn);
  }
  const state = () => studio && studio.state;
  const liveDraft = () => { const s = state(); return s && s.draft || null; };
  const clone = (value) => JSON.parse(JSON.stringify(value));
  function editDefaults() {
    const caps = state() && state().capabilities;
    const defaults = caps && caps.editor && caps.editor.defaults;
    return defaults && typeof defaults === "object" ? defaults : EDIT_FALLBACK;
  }
  function ensureEdit(draft) {
    if (!draft) return draft;
    if (!draft.edit || typeof draft.edit !== "object") draft.edit = {};
    const defaults = editDefaults();
    for (const key of Object.keys(defaults)) if (draft.edit[key] === undefined) draft.edit[key] = clone(defaults[key]);
    if (!Array.isArray(draft.edit.removed)) draft.edit.removed = [];
    if (!draft.edit.captions || typeof draft.edit.captions !== "object") draft.edit.captions = clone(EDIT_FALLBACK.captions);
    if (!draft.edit.captions.edits || typeof draft.edit.captions.edits !== "object") draft.edit.captions.edits = {};
    if (!Array.isArray(draft.edit.texts)) draft.edit.texts = [];
    if (!Array.isArray(draft.edit.overlays)) draft.edit.overlays = [];
    return draft;
  }
  function snapshot(draft) {
    const shot = {};
    for (const key of Object.keys(draft)) {
      if (SNAPSHOT_SKIP.has(key)) continue;
      if (key === "edit" && draft.edit && typeof draft.edit === "object") { for (const name of Object.keys(draft.edit)) shot["edit." + name] = JSON.stringify(draft.edit[name]); }
      else shot[key] = JSON.stringify(draft[key]);
    }
    return shot;
  }
  function difference(before, after) {
    const keys = {};
    let changed = false;
    for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
      if (before[key] !== after[key]) { keys[key] = [before[key] === undefined ? null : before[key], after[key] === undefined ? null : after[key]]; changed = true; }
    }
    return changed ? keys : null;
  }
  function applyKeys(draft, keys, side) {
    for (const [key, pair] of Object.entries(keys)) {
      const text = pair[side];
      const value = text === null ? undefined : JSON.parse(text);
      if (key.startsWith("edit.")) { const name = key.slice(5); if (value === undefined) delete draft.edit[name]; else draft.edit[name] = value; }
      else if (value === undefined) delete draft[key];
      else draft[key] = value;
    }
  }
  function sameKeys(a, b) { const x = Object.keys(a), y = Object.keys(b); return x.length === y.length && x.every((key) => key in b); }

  /* ---------- Change pipeline ---------- */
  function record(label, keys, options) {
    const now = Date.now();
    const last = history.past[history.past.length - 1];
    if (last && options.coalesce !== false && last.label === label && now - last.at < COALESCE_MS && sameKeys(last.keys, keys)) {
      for (const key of Object.keys(keys)) last.keys[key][1] = keys[key][1];
      last.at = now;
    } else {
      history.past.push({ label, at: now, keys });
      if (history.past.length > HISTORY_LIMIT) history.past.shift();
    }
    history.future = [];
  }
  function afterChange(label, options) {
    const draft = liveDraft();
    estimate();
    if (!options.fromSave && studio) studio.saveDraft();
    if (!options.fromStory && studio) studio.renderStory();
    scheduleResolve();
    emit("change", { label, draft });
    if (Shell) Shell.history();
    return draft;
  }
  function commit(label, options) {
    const draft = liveDraft();
    if (!draft) return false;
    const after = snapshot(draft);
    const keys = difference(baseline, after);
    if (!keys) return false;
    baseline = after;
    record(label, keys, options || {});
    afterChange(label, options || {});
    return true;
  }
  function locked() { return !studio || !studio.isActive || studio.isActive(); }
  function change(label, fn, options) {
    const draft = Editor.draft();
    if (!draft) return false;
    if (locked()) { if (studio) studio.toast("Wait for the render to finish before you change the edit."); return false; }
    const before = baseline;
    try { fn(draft); }
    catch (error) { undoTo(before); throw error; }
    return commit(String(label || "Edit"), options);
  }
  function undoTo(shot) {
    const draft = liveDraft();
    if (!draft) return;
    const keys = difference(snapshot(draft), shot);
    if (keys) applyKeys(draft, keys, 1);
  }
  function step(direction) {
    const draft = Editor.draft();
    if (!draft || locked()) return false;
    const from = direction < 0 ? history.past : history.future;
    const to = direction < 0 ? history.future : history.past;
    const entry = from.pop();
    if (!entry) return false;
    applyKeys(draft, entry.keys, direction < 0 ? 0 : 1);
    to.push(entry);
    baseline = snapshot(draft);
    keepSelectionValid();
    afterChange(entry.label, { fromUndo: true });
    if (studio) studio.announce((direction < 0 ? "Undid " : "Redid ") + entry.label);
    return true;
  }
  function keepSelectionValid() {
    const draft = liveDraft();
    if (!draft) return;
    const lists = { clip: draft.segments, text: draft.edit && draft.edit.texts, overlay: draft.edit && draft.edit.overlays };
    if (selection.kind in lists && !(lists[selection.kind] || [])[selection.index]) select({ kind: "none" });
  }
  function select(item) {
    selection = item && item.kind ? item : { kind: "none" };
    const s = state();
    if (s) s.selected = selection.kind === "clip" ? selection.index : -1;
    emit("select", selection);
    emit("selection", selection);
  }

  /* ---------- Timeline resolution ---------- */
  /*
   * The server shapes captions: line breaks, polish, caption edits. Between its
   * answers the last shape is carried on the source clock, so a drag moves the
   * same cues instead of swapping them for a different segmentation.
   */
  function rememberCaptions(timeline) {
    const spans = timeline.spans || [];
    const back = (time) => { const hit = Clock.toSource(spans, time); return hit ? hit.time : null; };
    captionMemo = (timeline.captions || []).map((cue) => {
      const words = (Array.isArray(cue.words) ? cue.words : []).map((word) => {
        const start = back(word.start + 1e-4), end = back(Math.max(word.start + 2e-4, word.end - 1e-4));
        return start === null || end === null ? null : { start, end, text: word.text };
      }).filter(Boolean);
      const start = finiteNumber(cue.source_start) ? cue.source_start : back(cue.start + 1e-4);
      const finish = words.length ? words[words.length - 1].end : back(cue.end - 1e-4);
      if (!finiteNumber(start) || !finiteNumber(finish) || finish <= start) return null;
      return { start, end: finish, text: cue.text, words, key: cue.key };
    }).filter(Boolean);
  }
  function captionCues(draft) {
    const analysis = draft.analysis && typeof draft.analysis === "object" ? draft.analysis : {};
    const edits = draft.edit && draft.edit.captions && draft.edit.captions.edits || {};
    const list = (cues) => cues.map((cue) => {
      const key = cue.key !== undefined ? cue.key : String(Math.round(Number(cue.start) * 1000));
      if (typeof edits[key] !== "string") return cue;
      return edits[key] ? { start: cue.start, end: cue.end, text: edits[key], key } : null;
    }).filter(Boolean);
    if (captionMemo) return list(captionMemo);
    const sentences = Array.isArray(analysis.sentences) ? analysis.sentences : [];
    const cues = sentences.length ? sentences : Array.isArray(analysis.transcript) ? analysis.transcript : [];
    return list(cues.map((cue) => ({ start: cue.start, end: cue.end, text: cue.text })));
  }
  function serverAnswer(timeline) {
    current = Object.assign({}, timeline, { estimate: false });
    if (!Array.isArray(current.captions)) current.captions = [];
    rememberCaptions(current);
  }
  function estimate() {
    const draft = liveDraft();
    if (!draft) { current = { spans: [], duration: 0, transition_seconds: 0, captions: [], estimate: true }; emit("timeline", current); return current; }
    const resolved = Clock.plan(draft);
    current = { spans: resolved.spans, duration: resolved.duration, transition_seconds: resolved.transition_seconds, captions: Clock.mapCues(captionCues(draft), resolved.spans), estimate: true };
    emit("timeline", current);
    return current;
  }
  function scheduleResolve(delay) {
    clearTimeout(resolveTimer);
    resolveSeq += 1;
    if (resolveController) { resolveController.abort(); resolveController = null; }
    if (routeMissing) return;
    resolveTimer = setTimeout(resolveNow, delay === undefined ? RESOLVE_DELAY : delay);
  }
  async function resolveNow() {
    const s = state();
    if (!s || !s.job || !s.draft) return;
    const seq = resolveSeq;
    const controller = new AbortController();
    resolveController = controller;
    try {
      /* The server keeps its own analysis, so the draft travels without it. */
      const body = { plan: Object.assign({}, s.draft, { analysis: undefined, timeline: undefined, title: s.draft.title || "Untitled" }) };
      const data = await studio.request("/api/campaigns/" + encodeURIComponent(s.job.id) + "/timeline", { method: "POST", body, signal: controller.signal, timeout: 15000 });
      if (seq !== resolveSeq || !data.timeline) return;
      serverAnswer(data.timeline);
      Shell.notice("");
      emit("timeline", current);
    } catch (error) {
      if (error && error.name === "AbortError" || seq !== resolveSeq) return;
      if (/route not found/i.test(String(error && error.message))) { routeMissing = true; return; }
      Shell.notice(String(error && error.message || "The server could not place this edit."));
    } finally { if (resolveController === controller) resolveController = null; }
  }

  /* ---------- Binding to Studio ---------- */
  function bindDraft() {
    const draft = liveDraft();
    if (draft === boundDraft) return false;
    const project = Editor.job();
    const sameProject = !!boundDraft && !!draft && !!project && project.id === boundJobId;
    boundDraft = draft;
    boundJobId = project && project.id;
    if (draft) { ensureEdit(draft); baseline = snapshot(draft); }
    else baseline = {};
    if (!sameProject && !pendingHistory) history = { past: [], future: [] };
    if (pendingHistory) { history = pendingHistory; pendingHistory = null; }
    if (!sameProject) { selection = { kind: "none" }; playhead = 0; captionMemo = null; }
    return true;
  }
  let boundJobId = null;
  let pendingHistory = null;

  const Editor = {
    draft() { return ensureEdit(liveDraft()); },
    change, on,
    job() { const s = state(); return s && s.job || null; },
    capabilities() { const s = state(); return s && s.capabilities || {}; },
    timeline() { return current; },
    seek(seconds, options) {
      const t = clamp(Number(seconds) || 0, 0, current.duration || 0);
      if (transport) transport.seek(t, options || {});
      else tick(t);
    },
    api(path, options) {
      const text = String(path || "");
      if (/^\/?api\//.test(text)) return studio.request("/" + text.replace(/^\/+/, ""), options);
      const s = state();
      if (!s || !s.job) return Promise.reject(new Error("Open a project first."));
      return studio.request("/api/campaigns/" + encodeURIComponent(s.job.id) + "/" + text.replace(/^\/+/, ""), options);
    },
    selection() { return selection; },
    select,
    undo() { return step(-1); },
    redo() { return step(1); },
    canUndo() { return history.past.length > 0; },
    canRedo() { return history.future.length > 0; },
    undoLabel() { const entry = history.past[history.past.length - 1]; return entry ? entry.label : ""; },
    redoLabel() { const entry = history.future[history.future.length - 1]; return entry ? entry.label : ""; },
    playhead() { return playhead; },
    play() { if (transport) transport.play(); },
    pause() { if (transport) transport.pause(); },
    playing() { return !!(transport && transport.playing()); },
    toSource(seconds) { return Clock.toSource(current.spans, seconds); },
    toOutput(seconds, segment) { return Clock.toOutput(current.spans, seconds, segment); },
    clock: Clock,
    locked,
    /* The rest is the wiring between studio.js, preview.js, and this file. */
    _attach(bridge) { studio = bridge; Shell.attach(); },
    _transport(value) { transport = value; },
    _studio() { return studio; },
    draftChanged,
    _transient() { return estimate(); },
    _commit(label, options) { return commit(label, options); },
    _jumpCut(direction) { Workspace.jumpCut(direction); },
    _togglePlay() { Workspace.togglePlay(); },
    _renderTranscript() { Transcript.render(); },
    _tick: tick,
    _job(job, details) {
      lastJob = job;
      bindDraft();
      Shell.job(job, details || {});
      emit("job", job);
      if (details && details.idChanged) routeMissing = false;
    },
    _story() { if (bindDraft()) { estimate(); firstResolve(); } Shell.story(); },
    _noteDraft() {
      const draft = liveDraft();
      if (!draft) return;
      ensureEdit(draft);
      if (draft !== boundDraft) { bindDraft(); return; }
      const keys = difference(baseline, snapshot(draft));
      if (keys) commit(keys.title ? "Title" : keys.caption ? "Post copy" : keys.hashtags ? "Hashtags" : keys.segments ? "Moments" : "Edit", { fromSave: true, fromStory: true });
    },
    _history() { return persistableHistory(); },
    _loadHistory(saved) { pendingHistory = saved && Array.isArray(saved.past) ? { past: saved.past, future: Array.isArray(saved.future) ? saved.future : [] } : null; },
    _reset() { boundDraft = null; boundJobId = null; lastJob = null; baseline = {}; captionMemo = null; history = { past: [], future: [] }; selection = { kind: "none" }; playhead = 0; clearTimeout(resolveTimer); current = { spans: [], duration: 0, transition_seconds: 0, captions: [], estimate: true }; Shell.reset(); emit("timeline", current); },
    _preview(version) { Shell.preview(version); },
    _setMode(mode) { Shell.mode(mode); },
  };
  /* True when the draft holds something the last render did not. */
  function draftChanged() {
    const draft = liveDraft();
    const plan = lastJob && lastJob.plan;
    if (!draft || !plan) return false;
    const saved = snapshot(plan), now = snapshot(draft), defaults = editDefaults();
    for (const key of Object.keys(now)) {
      if (key in saved) { if (saved[key] !== now[key]) return true; }
      else if (key.startsWith("edit.")) { const name = key.slice(5); if (name in defaults && JSON.stringify(defaults[name]) !== now[key]) return true; }
    }
    return false;
  }
  function firstResolve() {
    const s = state();
    const plan = s && s.job && s.job.plan;
    if (plan && plan.timeline && Array.isArray(plan.timeline.spans) && JSON.stringify(snapshot(plan)) === JSON.stringify(snapshot(liveDraft()))) {
      serverAnswer(plan.timeline);
      emit("timeline", current);
      return;
    }
    scheduleResolve(0);
  }
  function persistableHistory() {
    const past = history.past.slice(-PERSISTED_STEPS), future = history.future.slice(-PERSISTED_STEPS);
    let saved = { past, future };
    while (saved.past.length && JSON.stringify(saved).length > PERSISTED_BYTES) { saved.past.shift(); }
    return saved;
  }
  function tick(seconds) {
    playhead = seconds;
    emit("seek", seconds);
  }

  window.BriefcutEditor = Editor;

  /* ---------- Workspace shell: Story, Edit, Deliver ---------- */
  const Shell = (() => {
    const $ = (id) => document.getElementById(id);
    const MODES = ["story", "edit", "deliver"];
    let mode = "story";
    let lastId = "";
    let lastStatus = "";
    const modeKey = (id) => "vedit.studio.mode." + id;
    function storedMode(id) { try { return localStorage.getItem(modeKey(id)) || ""; } catch (_) { return ""; } }
    function storeMode(id, value) { try { localStorage.setItem(modeKey(id), value); } catch (_) { /* Remembering the tab is a convenience. */ } }
    function linkedMode() { try { return new URLSearchParams(location.hash.replace(/^#/, "")).get("mode") || ""; } catch (_) { return ""; } }
    const verified = () => { const job = Editor.job(); return !!(job && job.result && job.result.proof && job.result.proof.has_video); };
    function allowed(next) {
      if (next === "edit") return !!liveDraft();
      if (next === "deliver") return verified();
      return true;
    }
    function paintNav() {
      document.querySelectorAll(".workflow [data-mode]").forEach((button) => {
        const name = button.dataset.mode;
        button.setAttribute("aria-pressed", String(name === mode));
        button.disabled = !allowed(name);
      });
    }
    function setMode(next, options) {
      const settings = options || {};
      let target = MODES.includes(next) ? next : "story";
      if (!allowed(target)) target = liveDraft() ? "edit" : "story";
      mode = target;
      document.body.dataset.mode = mode;
      paintNav();
      const job = Editor.job();
      if (job && settings.remember !== false) storeMode(job.id, mode);
      if (studio) {
        const shown = state().preview;
        if (mode === "edit" && shown !== "story" && !settings.keepMonitor) studio.setPreview("story");
        else if (mode === "deliver" && shown !== "final") studio.setPreview("final");
        else if (mode === "story" && shown === "story") studio.setPreview("source");
      }
      Workspace.resized();
      if (settings.focus) {
        const target = mode === "edit" ? $("tl-focus") : mode === "deliver" ? $("delivery-heading") : $("project-name");
        if (target) target.focus({ preventScroll: mode !== "deliver" });
      }
      if (settings.announce && studio) studio.announce({ story: "Story", edit: "Edit", deliver: "Deliver" }[mode] + " workspace");
    }
    function chooseLeftTab(name) {
      const media = name === "media";
      $("left-tab-transcript").setAttribute("aria-selected", String(!media));
      $("left-tab-media").setAttribute("aria-selected", String(media));
      $("left-tab-transcript").tabIndex = media ? -1 : 0;
      $("left-tab-media").tabIndex = media ? 0 : -1;
      $("left-transcript").hidden = media;
      $("media-bin-mount").hidden = !media;
    }
    function attach() {
      document.body.dataset.mode = mode;
      document.querySelectorAll(".workflow [data-mode]").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode, { focus: true, announce: true })));
      $("preview-story").addEventListener("click", () => studio.setPreview("story"));
      $("left-tab-transcript").addEventListener("click", () => chooseLeftTab("transcript"));
      $("left-tab-media").addEventListener("click", () => chooseLeftTab("media"));
      document.querySelector(".ed-tabs").addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        const tabs = [...event.currentTarget.querySelectorAll('[role="tab"]')];
        const next = tabs[(tabs.indexOf(event.target) + (event.key === "ArrowLeft" ? tabs.length - 1 : 1)) % tabs.length];
        if (!next) return;
        event.preventDefault();
        next.click();
        next.focus();
      });
      window.addEventListener("hashchange", () => { const linked = linkedMode(); if (linked && lastId) setMode(linked); });
      Workspace.attach();
    }
    function job(value, details) {
      const hasStory = !!liveDraft();
      document.body.classList.toggle("has-story", hasStory);
      const changedProject = value.id !== lastId;
      const statusChanged = value.status !== lastStatus;
      const previous = lastStatus;
      lastId = value.id; lastStatus = value.status;
      if (changedProject) {
        const linked = linkedMode();
        const remembered = linked || storedMode(value.id);
        setMode(remembered || (value.status === "complete" ? "deliver" : hasStory ? "edit" : "story"), { remember: !!linked });
        if (remembered === "edit" && mode !== "edit") paintNav();
      } else if (statusChanged && value.status === "complete" && hasStory) setMode("deliver");
      else if (statusChanged && value.status === "planned" && (previous === "queued" || previous === "analyzing") && hasStory) setMode("edit");
      else if (details.fresh && value.status === "planned" && hasStory && mode === "story") setMode("edit");
      else paintNav();
      if (!changedProject && mode === "edit" && !hasStory) setMode("story");
      if (mode === "edit" && details.fresh && state().preview === "source") studio.setPreview("story");
      Workspace.job(value);
    }
    function story() {
      document.body.classList.toggle("has-story", !!liveDraft());
      paintNav();
      Workspace.story();
    }
    function preview(version) {
      const story = version === "story";
      $("monitor-picture").classList.toggle("is-story", story);
      $("preview-story").setAttribute("aria-pressed", String(story));
      $("preview-stage").hidden = !story;
      document.querySelector(".monitor").classList.toggle("is-preview", story);
      if (story) { const player = $("player"); if (!player.paused) player.pause(); }
      const badge = $("monitor-badge");
      badge.textContent = story ? "Preview" : version === "final" ? "Finished render" : "Source footage";
      badge.dataset.kind = version;
      Workspace.preview(version);
      emit("preview", version);
    }
    function reset() {
      document.body.classList.remove("has-story");
      lastId = ""; lastStatus = "";
      setMode("story", { remember: false });
      Workspace.reset();
    }
    return { attach, job, story, preview, reset, mode: setMode, history() { Workspace.history(); }, notice(text) { Workspace.notice(text); }, current() { return mode; } };
  })();

  /* ---------- Small DOM helpers ---------- */
  const SVG = "http://www.w3.org/2000/svg";
  const ICONS = {
    undo: "M7.5 4.5 4 8l3.5 3.5M4.5 8H12a4 4 0 0 1 0 8H9",
    redo: "M12.5 4.5 16 8l-3.5 3.5M15.5 8H8a4 4 0 0 0 0 8h3",
    split: "M10 3v14M6.5 7 3.5 10l3 3M13.5 7l3 3-3 3",
    trash: "M4 6h12M8 6V4h4v2M6 6l.9 10.5h6.2L14 6",
    left: "M10 5H5v10h5M16 10H8m3-3-3 3 3 3",
    right: "M10 5h5v10h-5M4 10h8M9 7l3 3-3 3",
    ripple: "M3 10h3M8 6v8M12 6v8M14 10h3",
    magnet: "M5 4v6.5a5 5 0 0 0 10 0V4h-3.2v6.5a1.8 1.8 0 0 1-3.6 0V4H5ZM5 7.5h3.2M11.8 7.5H15",
    minus: "M5 10h10",
    plus: "M5 10h10M10 5v10",
    fit: "M3 7V4h3M17 7V4h-3M3 13v3h3M17 13v3h-3M7 10h6",
    text: "M4 6V4h12v2M10 4v12M7.5 16h5",
    captions: "M3 5h14v10H3zM6 12h5M13 12h1M6 9h1M9 9h5",
    film: "M3 4h14v12H3zM7 4v12M13 4v12M3 8h4M3 12h4M13 8h4M13 12h4",
    wave: "M3 9v2M6 7v6M9 4v12M12 6v8M15 8v4M18 9v2",
    music: "M8 15V5l8-2v10M8 15a2 2 0 1 1-4 0 2 2 0 0 1 4 0ZM16 13a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z",
  };
  function icon(name) {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVG, "path");
    path.setAttribute("d", ICONS[name] || "");
    svg.append(path);
    return svg;
  }
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function toolButton(id, label, iconName, title, text) {
    const button = el("button", "tl-tool");
    button.type = "button"; button.id = id; button.title = title || label; button.setAttribute("aria-label", label);
    if (iconName) button.append(icon(iconName));
    if (text) button.append(el("span", "", text));
    return button;
  }
  function clockLabel(seconds, tenths) {
    const value = Math.max(0, seconds);
    const whole = Math.floor(value + 1e-6);
    const text = Math.floor(whole / 60) + ":" + String(whole % 60).padStart(2, "0");
    return tenths ? text + "." + Math.floor((value - whole + 1e-6) * 10) : text;
  }
  const spoken = (seconds) => (Math.round(seconds * 10) / 10) + (Math.round(seconds * 10) === 10 ? " second" : " seconds");
  const momentName = (segment, index) => "Moment " + (index + 1) + (segment && segment.label ? ", " + segment.label : "");

  /* ---------- The timeline: canvas for pixels, DOM for what you can grab ---------- */
  const Workspace = (() => {
    const $ = (id) => document.getElementById(id);
    const PAD = 14, TAIL = 96, SNAP_PX = 7, FRAME = 1 / 30, MIN_CLIP = 0.1, MAX_PPS = 320, FOLLOW_MARGIN = 72;
    const SPEEDS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4];
    const view = { pps: 40, fit: true, scrollX: 0, width: 0, height: 0, ripple: true, snap: true, playhead: 0, focusClip: 0, lanes: 1 };
    const rows = { ruler: { y: 0, h: 22 }, text: { y: 0, h: 24 }, captions: { y: 0, h: 24 }, video: { y: 0, h: 48 }, audio: { y: 0, h: 34 }, music: { y: 0, h: 22 } };
    const media = { strip: null, stripURL: "", peaks: null, peakJob: "" };
    const nodes = {};
    const clipPool = [], itemPool = [], joinPool = [];
    let groups = [];
    let ctx = null, tokens = null, framePending = false, gesture = null, ariaStamp = 0, boundaries = [], boundaryKey = null;

    /* Tokens are read once per theme so a frame never touches computed style. */
    function readTokens() {
      const style = getComputedStyle(document.documentElement);
      const pick = (name) => style.getPropertyValue(name).trim();
      tokens = { ground: pick("--tl-ground"), track: pick("--tl-track"), track2: pick("--tl-track-2"), tick: pick("--tl-tick"), text: pick("--tl-text"), wave: pick("--tl-wave"), waveDim: pick("--tl-wave-dim"), cue: pick("--tl-cue"), cueLine: pick("--tl-cue-line"), cueInk: pick("--tl-cue-ink"), fallback: pick("--tl-clip-fallback"), splice: pick("--tl-splice"), select: pick("--tl-select") };
    }
    const xOf = (t) => PAD + t * view.pps - view.scrollX;
    const timeAt = (clientX) => Math.max(0, (clientX - nodes.stage.getBoundingClientRect().left + nodes.scroll.scrollLeft - PAD) / view.pps);
    const duration = () => current.duration || 0;
    function fitScale() { return view.width > PAD * 2 + 40 && duration() > 0 ? (view.width - PAD * 2 - 40) / duration() : 40; }

    function build() {
      const root = $("timeline-panel");
      const bar = el("div", "tl-bar"); bar.setAttribute("role", "toolbar"); bar.setAttribute("aria-label", "Timeline tools");
      const history = el("div", "tl-group");
      history.append(toolButton("tl-undo", "Undo", "undo"), toolButton("tl-redo", "Redo", "redo"));
      const edits = el("div", "tl-group");
      edits.append(toolButton("tl-split", "Split at the playhead", "split", "Split at the playhead (S)"), toolButton("tl-delete", "Delete the selection", "trash", "Delete the selection (Delete)"), toolButton("tl-left", "Move the moment earlier", "left", "Move earlier (Alt+Left)"), toolButton("tl-right", "Move the moment later", "right", "Move later (Alt+Right)"));
      const speed = el("label", "tl-speed");
      const speedName = el("span", "", "Speed");
      const speedSelect = el("select"); speedSelect.id = "tl-speed"; speedSelect.setAttribute("aria-label", "Speed of the selected moment");
      speed.append(speedName, speedSelect);
      const modes = el("div", "tl-group");
      const ripple = toolButton("tl-ripple", "Ripple", "ripple", "Text and overlays move with the footage (R)", "Ripple");
      const snap = toolButton("tl-snap", "Snap", "magnet", "Snap to cuts, sentences, and the playhead (N)", "Snap");
      modes.append(ripple, snap);
      const notice = el("p", "tl-notice"); notice.id = "tl-notice"; notice.setAttribute("role", "status");
      const total = el("span", "tl-total"); total.id = "tl-total";
      const zoom = el("div", "tl-zoom");
      const zoomRange = el("input"); zoomRange.type = "range"; zoomRange.id = "tl-zoom"; zoomRange.min = "0"; zoomRange.max = "1000"; zoomRange.value = "0"; zoomRange.setAttribute("aria-label", "Timeline zoom");
      zoom.append(toolButton("tl-zoom-out", "Zoom out", "minus", "Zoom out (-)"), zoomRange, toolButton("tl-zoom-in", "Zoom in", "plus", "Zoom in (=)"), toolButton("tl-fit", "Fit the story to the timeline", "fit", "Fit the story (Shift+Z)"));
      bar.append(history, edits, speed, modes, notice, total, zoom);

      const body = el("div", "tl-body");
      const heads = el("div", "tl-heads"); heads.setAttribute("aria-hidden", "true");
      const rulerHead = el("div", "tl-head is-ruler");
      heads.append(rulerHead);
      for (const [key, name, glyph] of [["text", "Text", "text"], ["captions", "Captions", "captions"], ["video", "Video", "film"], ["audio", "Audio", "wave"], ["music", "Music", "music"]]) {
        const head = el("div", "tl-head"); head.dataset.row = key;
        head.append(icon(glyph), el("span", "", name));
        heads.append(head);
        nodes["head" + key] = head;
      }
      const scroll = el("div", "tl-scroll"); scroll.id = "tl-scroll";
      const spacer = el("div", "tl-spacer"); spacer.id = "tl-spacer";
      const stage = el("div", "tl-stage"); stage.id = "tl-stage";
      const canvas = el("canvas", "tl-canvas"); canvas.id = "tl-canvas"; canvas.setAttribute("aria-hidden", "true");
      const content = el("div", "tl-content"); content.id = "tl-content";
      const clips = el("div", "tl-clips"); clips.id = "tl-clips"; clips.setAttribute("role", "group"); clips.setAttribute("aria-label", "Moments in story order");
      const items = el("div", "tl-items"); items.id = "tl-items"; items.setAttribute("role", "group"); items.setAttribute("aria-label", "Text and overlays");
      const joins = el("div", "tl-joins"); joins.id = "tl-joins"; joins.setAttribute("role", "group"); joins.setAttribute("aria-label", "Transitions");
      const musicLane = el("div", "tl-items"); musicLane.id = "tl-music"; 
      content.append(joins, clips, items, musicLane);
      const ruler = el("div", "tl-ruler"); ruler.id = "tl-focus"; ruler.tabIndex = 0;
      ruler.setAttribute("role", "slider"); ruler.setAttribute("aria-label", "Playhead"); ruler.setAttribute("aria-orientation", "horizontal"); ruler.setAttribute("aria-valuemin", "0");
      const playhead = el("div", "tl-playhead"); playhead.id = "tl-playhead"; playhead.append(el("i"));
      const snapLine = el("div", "tl-snapline"); snapLine.id = "tl-snapline"; snapLine.hidden = true;
      const ghost = el("div", "tl-ghost"); ghost.id = "tl-ghost"; ghost.hidden = true;
      const cue = el("div", "tl-cue-focus"); cue.id = "tl-cue-focus"; cue.hidden = true;
      stage.append(canvas, content, ruler, cue, snapLine, playhead, ghost);
      spacer.append(stage); scroll.append(spacer);
      body.append(heads, scroll);
      root.append(bar, body);
      Object.assign(nodes, { root, bar, scroll, spacer, stage, canvas, content, clips, items, joins, musicLane, ruler, playhead, snapLine, ghost, cueFocus: cue, notice, total, speedSelect, speed, zoomRange });
      ctx = canvas.getContext("2d", { alpha: true, desynchronized: false });
      SPEEDS.forEach((value) => speedSelect.append(new Option(value + "×", String(value))));
      paintToggles();
    }
    function paintToggles() {
      $("tl-ripple").setAttribute("aria-pressed", String(view.ripple));
      $("tl-snap").setAttribute("aria-pressed", String(view.snap));
    }

    /* ---------- Geometry ---------- */
    function layoutRows() {
      const available = view.height;
      const tracks = Math.max(0, available - rows.ruler.h - rows.text.h - rows.captions.h - rows.music.h - 5);
      rows.video.h = Math.max(40, Math.round(tracks * 0.6));
      rows.audio.h = Math.max(28, tracks - rows.video.h);
      let y = rows.ruler.h + 1;
      for (const key of ["text", "captions", "video", "audio", "music"]) {
        rows[key].y = y;
        const head = nodes["head" + key];
        if (head) { head.style.height = rows[key].h + "px"; head.style.top = y + "px"; }
        y += rows[key].h + 1;
      }
    }
    function measure() {
      const width = nodes.scroll.clientWidth, height = nodes.scroll.clientHeight;
      if (!width || !height) return false;
      const changed = width !== view.width || height !== view.height;
      view.width = width; view.height = height;
      if (changed) {
        const ratio = Math.min(2.5, window.devicePixelRatio || 1);
        nodes.canvas.width = Math.round(width * ratio); nodes.canvas.height = Math.round(height * ratio);
        nodes.canvas.style.width = width + "px"; nodes.canvas.style.height = height + "px";
        nodes.stage.style.width = width + "px";
        view.ratio = ratio;
        layoutRows();
      }
      return true;
    }
    function applyScale() {
      if (view.fit && !gesture) view.pps = fitScale();
      const width = Math.ceil(PAD * 2 + duration() * view.pps + TAIL);
      if (nodes.spacerWidth !== width) { nodes.spacerWidth = width; nodes.spacer.style.width = width + "px"; }
      nodes.content.style.width = width + "px";
      const low = fitScale();
      const value = view.pps <= low ? 0 : Math.round(1000 * Math.log(view.pps / low) / Math.log(Math.max(MAX_PPS, low * 1.01) / low));
      if (document.activeElement !== nodes.zoomRange) nodes.zoomRange.value = String(value);
    }
    function groupSpans(spans) {
      const result = [];
      let group = null;
      for (const span of spans) {
        if (!group || group.segment !== span.segment) { group = { segment: span.segment, start: span.output_start, end: span.output_end, spans: [] }; result.push(group); }
        group.spans.push(span);
        group.end = Math.max(group.end, span.output_end);
      }
      return result;
    }

    /* ---------- DOM layers ---------- */
    function makeClip() {
      const root = el("div", "tl-clip");
      const body = el("button", "tl-clip-body"); body.type = "button"; body.tabIndex = -1;
      const label = el("span", "tl-clip-label"), badge = el("span", "tl-clip-badge");
      body.append(label, badge);
      const start = el("div", "tl-edge is-start"), end = el("div", "tl-edge is-end");
      for (const edge of [start, end]) { edge.setAttribute("role", "slider"); edge.setAttribute("aria-orientation", "horizontal"); edge.tabIndex = -1; edge.append(el("i")); }
      root.append(body, start, end);
      return { root, body, label, badge, start, end };
    }
    function selectedClips() {
      if (selection.kind !== "clip") return [];
      return Array.isArray(selection.indices) && selection.indices.length ? selection.indices : [selection.index];
    }
    function syncClips() {
      const draft = liveDraft();
      groups = groupSpans(current.spans);
      if (!draft) { nodes.clips.replaceChildren(); clipPool.length = 0; return; }
      while (clipPool.length < groups.length) { const clip = makeClip(); clipPool.push(clip); nodes.clips.append(clip.root); }
      while (clipPool.length > groups.length) clipPool.pop().root.remove();
      const chosen = new Set(selectedClips());
      const size = draft.segments.length;
      if (view.focusClip >= groups.length) view.focusClip = Math.max(0, groups.length - 1);
      const sourceLength = sourceSeconds();
      groups.forEach((group, position) => {
        const index = group.segment - 1;
        const segment = draft.segments[index] || {};
        const clip = clipPool[position];
        const left = PAD + group.start * view.pps, width = Math.max(3, (group.end - group.start) * view.pps);
        clip.root.style.transform = "translate3d(" + left + "px,0,0)";
        clip.root.style.width = width + "px";
        clip.root.style.top = rows.video.y + "px"; clip.root.style.height = rows.video.h + "px";
        clip.root.dataset.index = String(index);
        const on = chosen.has(index);
        clip.root.classList.toggle("is-selected", on);
        clip.root.classList.toggle("is-tight", width < 30);
        clip.root.classList.toggle("is-narrow", width < 52);
        clip.root.classList.toggle("is-tiny", width < 18);
        const seconds = group.end - group.start;
        const speed = Clock.speedOf(segment);
        const name = (segment.label || "Moment " + (index + 1));
        if (clip.label.textContent !== (index + 1) + "  " + name) clip.label.textContent = (index + 1) + "  " + name;
        const badge = speed !== 1 ? speed + "×" : "";
        clip.badge.textContent = badge; clip.badge.hidden = !badge;
        clip.body.tabIndex = position === view.focusClip ? 0 : -1;
        clip.body.setAttribute("aria-pressed", String(on));
        clip.body.setAttribute("aria-label", "Moment " + (index + 1) + " of " + size + ", " + name + ", " + spoken(seconds) + (speed !== 1 ? ", " + speed + " times speed" : ""));
        clip.body.title = name + " (" + seconds.toFixed(1) + " s)";
        for (const [edge, key, word] of [[clip.start, "start", "In point"], [clip.end, "end", "Out point"]]) {
          edge.dataset.edge = key;
          edge.tabIndex = on ? 0 : -1;
          edge.setAttribute("aria-label", word + " of moment " + (index + 1));
          edge.setAttribute("aria-valuemin", "0"); edge.setAttribute("aria-valuemax", String(round2(sourceLength)));
          edge.setAttribute("aria-valuenow", String(round2(Number(segment[key]) || 0)));
          edge.setAttribute("aria-valuetext", spoken(Number(segment[key]) || 0) + " into the source");
        }
      });
    }
    function sourceSeconds() {
      const job = Editor.job();
      const length = job && job.source && Number(job.source.duration);
      const draft = liveDraft();
      return length > 0 ? length : Number(draft && draft.source_duration) || 0;
    }
    function assetName(id) { const job = Editor.job(); const record = job && job.assets && job.assets[id]; return record && record.name || "Image"; }
    function makeItem() {
      const root = el("div", "tl-item");
      const body = el("button", "tl-item-body"); body.type = "button";
      const label = el("span");
      body.append(label);
      const start = el("div", "tl-edge is-start"), end = el("div", "tl-edge is-end");
      for (const edge of [start, end]) { edge.setAttribute("role", "slider"); edge.setAttribute("aria-orientation", "horizontal"); edge.tabIndex = -1; edge.append(el("i")); }
      root.append(body, start, end);
      return { root, body, label, start, end };
    }
    function timedItems() {
      const draft = liveDraft();
      if (!draft || !draft.edit) return [];
      const list = [];
      (draft.edit.texts || []).forEach((item, index) => list.push({ kind: "text", index, start: Number(item.start) || 0, length: Number(item.duration) || 0, name: item.text || "Text" }));
      (draft.edit.overlays || []).forEach((item, index) => list.push({ kind: "overlay", index, start: Number(item.start) || 0, length: Number(item.duration) || 0, name: assetName(item.asset) }));
      return list;
    }
    /* Items that overlap in time take separate lanes, up to three. */
    function assignLanes(list) {
      const ends = [];
      for (const entry of [...list].sort((a, b) => a.start - b.start)) {
        let lane = ends.findIndex((end) => end <= entry.start + 1e-3);
        if (lane < 0) lane = ends.length < 3 ? ends.length : ends.indexOf(Math.min(...ends));
        ends[lane] = entry.start + entry.length;
        entry.lane = lane;
      }
      return Math.max(1, ends.length);
    }
    function syncItems() {
      const list = timedItems();
      const lanes = assignLanes(list);
      if (lanes !== view.lanes) { view.lanes = lanes; rows.text.h = lanes === 1 ? 24 : lanes * 20 + 2; layoutRows(); syncClips(); syncMusic(); syncJoins(); }
      while (itemPool.length < list.length) { const item = makeItem(); itemPool.push(item); nodes.items.append(item.root); }
      while (itemPool.length > list.length) itemPool.pop().root.remove();
      list.forEach((entry, position) => {
        const item = itemPool[position];
        const left = PAD + entry.start * view.pps, width = Math.max(10, entry.length * view.pps);
        item.root.style.transform = "translate3d(" + left + "px,0,0)";
        item.root.style.width = width + "px";
        const laneHeight = view.lanes === 1 ? 24 : 20;
        item.root.style.top = rows.text.y + entry.lane * laneHeight + "px"; item.root.style.height = laneHeight + "px";
        item.root.dataset.kind = entry.kind; item.root.dataset.index = String(entry.index);
        item.root.classList.toggle("is-overlay", entry.kind === "overlay");
        const on = selection.kind === entry.kind && selection.index === entry.index;
        item.root.classList.toggle("is-selected", on);
        item.label.textContent = entry.name;
        item.body.setAttribute("aria-pressed", String(on));
        item.body.setAttribute("aria-label", (entry.kind === "text" ? "Text: " : "Overlay: ") + entry.name + ", from " + spoken(entry.start) + ", for " + spoken(entry.length));
        item.body.title = entry.name;
        item.start.tabIndex = on ? 0 : -1; item.end.tabIndex = on ? 0 : -1;
        item.start.dataset.edge = "start"; item.end.dataset.edge = "end";
        item.start.setAttribute("aria-label", "Start of " + entry.name); item.end.setAttribute("aria-label", "End of " + entry.name);
        item.start.setAttribute("aria-valuemin", "0"); item.end.setAttribute("aria-valuemin", "0");
        item.start.setAttribute("aria-valuemax", String(duration())); item.end.setAttribute("aria-valuemax", String(duration()));
        item.start.setAttribute("aria-valuenow", String(round2(entry.start))); item.end.setAttribute("aria-valuenow", String(round2(entry.start + entry.length)));
      });
    }
    function syncMusic() {
      const draft = liveDraft();
      const music = draft && draft.edit && draft.edit.music;
      const lane = nodes.musicLane;
      if (!music || !music.asset) { if (lane.firstChild) lane.replaceChildren(); return; }
      let block = lane.firstChild;
      if (!block) {
        block = el("button", "tl-music-block"); block.type = "button"; block.append(el("span"));
        block.addEventListener("click", () => { select({ kind: "music" }); paintSelection(); });
        lane.append(block);
      }
      const job = Editor.job();
      const record = job && job.assets && job.assets[music.asset];
      const name = record && record.name || "Music";
      block.style.left = PAD + "px"; block.style.width = Math.max(10, duration() * view.pps) + "px";
      block.style.top = rows.music.y + "px"; block.style.height = rows.music.h + "px";
      block.classList.toggle("is-selected", selection.kind === "music");
      block.firstChild.textContent = name + (music.duck !== false ? " (lowers under speech)" : "");
      block.setAttribute("aria-label", "Music: " + name);
    }
    function syncJoins() {
      const draft = liveDraft();
      const transition = draft && draft.edit && draft.edit.transition;
      const active = transition && transition.type && transition.type !== "none";
      const list = active ? groups.slice(1) : [];
      while (joinPool.length < list.length) {
        const join = el("button", "tl-join"); join.type = "button"; join.append(el("i"));
        join.addEventListener("click", () => { select({ kind: "transition", index: Number(join.dataset.index) }); paintSelection(); });
        joinPool.push(join); nodes.joins.append(join);
      }
      while (joinPool.length > list.length) joinPool.pop().remove();
      const overlap = current.transition_seconds || 0;
      const catalogue = Editor.capabilities().editor;
      const entry = active && catalogue && (catalogue.transitions || []).find((row) => row.id === transition.type);
      const name = entry ? entry.name : "Transition";
      list.forEach((group, position) => {
        const join = joinPool[position];
        const width = Math.max(5, overlap * view.pps);
        join.classList.toggle("is-thin", width < 12);
        join.dataset.index = String(position);
        join.style.left = PAD + (group.start + overlap / 2) * view.pps - width / 2 + "px";
        join.style.width = width + "px";
        join.style.top = rows.video.y + "px"; join.style.height = rows.video.h + "px";
        join.classList.toggle("is-selected", selection.kind === "transition" && selection.index === position);
        join.title = name + " (" + overlap.toFixed(2) + " s)";
        join.setAttribute("aria-label", name + " between moments " + (position + 1) + " and " + (position + 2));
      });
    }
    function paintSelection() {
      syncClips(); syncItems(); syncMusic(); syncJoins(); paintTools(); invalidate();
    }
    function paintTools() {
      const draft = liveDraft();
      const busy = !draft || locked();
      const chosen = selectedClips();
      const single = chosen.length === 1 ? chosen[0] : -1;
      const segments = draft ? draft.segments : [];
      nodes.root.classList.toggle("is-locked", !!draft && locked());
      $("tl-split").disabled = busy;
      const removable = selection.kind === "text" || selection.kind === "overlay" || selection.kind === "music" || selection.kind === "clip" && segments.length > chosen.length;
      $("tl-delete").disabled = busy || !removable;
      $("tl-left").disabled = busy || single < 1;
      $("tl-right").disabled = busy || single < 0 || single >= segments.length - 1;
      nodes.speedSelect.disabled = busy || single < 0;
      nodes.speed.classList.toggle("is-off", nodes.speedSelect.disabled);
      if (single >= 0 && segments[single]) {
        const speed = Clock.speedOf(segments[single]);
        if (!SPEEDS.includes(speed) && ![...nodes.speedSelect.options].some((option) => Number(option.value) === speed)) nodes.speedSelect.append(new Option(speed + "×", String(speed)));
        nodes.speedSelect.value = String(speed);
      } else nodes.speedSelect.value = "1";
      paintHistory();
    }
    function paintHistory() {
      const undo = $("tl-undo"), redo = $("tl-redo");
      if (!undo) return;
      const busy = locked();
      undo.disabled = busy || !Editor.canUndo();
      redo.disabled = busy || !Editor.canRedo();
      const mod = (window.BriefcutPlatform && window.BriefcutPlatform.mod) || "Ctrl";
      undo.title = Editor.canUndo() ? "Undo " + Editor.undoLabel() + " (" + mod + "+Z)" : "Nothing to undo";
      redo.title = Editor.canRedo() ? "Redo " + Editor.redoLabel() + " (Shift+" + mod + "+Z)" : "Nothing to redo";
    }

    /* ---------- Canvas ---------- */
    function invalidate() {
      if (framePending) return;
      framePending = true;
      requestAnimationFrame(frame);
    }
    function frame() {
      framePending = false;
      if (!view.width || !nodes.root.offsetParent) return;
      const scroll = nodes.scroll.scrollLeft;
      view.scrollX = scroll;
      nodes.content.style.transform = "translate3d(" + -scroll + "px,0,0)";
      placePlayhead();
      draw();
    }
    function draw() {
      const ratio = view.ratio || 1;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, view.width, view.height);
      const from = (view.scrollX - PAD) / view.pps, to = from + view.width / view.pps;
      ctx.font = "600 10px \"Avenir Next\", Avenir, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif";
      ctx.textBaseline = "middle";
      for (const key of ["text", "captions", "video", "audio", "music"]) {
        ctx.fillStyle = key === "video" || key === "audio" ? tokens.track2 : tokens.track;
        ctx.fillRect(0, rows[key].y, view.width, rows[key].h);
      }
      drawRuler(from, to);
      drawVideo(from, to);
      drawAudio(from, to);
      drawCaptions(from, to);
    }
    function drawRuler(from, to) {
      const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600];
      const major = steps.find((step) => step * view.pps >= 62) || 7200;
      const minor = major / (major === 15 || major === 30 || major === 60 ? 3 : major === 2 || major === 0.2 || major === 120 ? 4 : 5);
      const ruler = rows.ruler;
      ctx.fillStyle = tokens.ground;
      ctx.fillRect(0, 0, view.width, ruler.h);
      ctx.fillStyle = tokens.tick;
      const first = Math.max(0, Math.floor(from / minor));
      const last = Math.ceil(to / minor);
      for (let index = first; index <= last; index += 1) {
        const time = index * minor;
        const x = Math.round(xOf(time)) + 0.5;
        const isMajor = Math.abs(time / major - Math.round(time / major)) < 1e-6;
        ctx.fillRect(x - 0.5, ruler.h - (isMajor ? 8 : 4), 1, isMajor ? 8 : 4);
      }
      ctx.fillStyle = tokens.text;
      ctx.textAlign = "left";
      const fine = major < 1;
      for (let index = Math.max(0, Math.floor(from / major)); index * major <= to; index += 1) ctx.fillText(clockLabel(index * major, fine), Math.round(xOf(index * major)) + 4, 9);
    }
    function drawVideo(from, to) {
      const row = rows.video;
      const strip = media.strip;
      for (const group of groups) {
        if (group.end < from || group.start > to) continue;
        const left = xOf(group.start), right = xOf(group.end);
        ctx.fillStyle = tokens.fallback;
        ctx.fillRect(left, row.y, right - left, row.h);
        for (const span of group.spans) {
          const x0 = xOf(span.output_start), x1 = xOf(span.output_end);
          if (x1 < 0 || x0 > view.width) continue;
          if (strip) {
            const tile = Math.max(24, row.h * strip.tile_width / strip.tile_height);
            ctx.save();
            ctx.beginPath(); ctx.rect(x0, row.y, x1 - x0, row.h); ctx.clip();
            const first = x0 < 0 ? Math.floor(-x0 / tile) : 0;
            for (let step = first; x0 + step * tile < Math.min(x1, view.width); step += 1) {
              const x = x0 + step * tile;
              const output = span.output_start + (x + tile / 2 - x0) / view.pps;
              const source = clamp(span.source_start + (output - span.output_start) * span.speed, span.source_start, span.source_end);
              const number = clamp(Math.floor(source / strip.seconds_per_tile), 0, strip.count - 1);
              const sx = (number % strip.columns) * strip.tile_width, sy = Math.floor(number / strip.columns) * strip.tile_height;
              ctx.drawImage(strip.image, sx, sy, strip.tile_width, strip.tile_height, x, row.y, tile + 0.5, row.h);
            }
            ctx.restore();
          }
          if (span !== group.spans[0]) {
            ctx.fillStyle = tokens.splice;
            ctx.fillRect(Math.round(x0) - 1, row.y, 2, row.h);
          }
        }
        ctx.fillStyle = tokens.splice;
        ctx.fillRect(Math.round(left) - 1, row.y, 2, row.h);
      }
    }
    function peakAt(low, high) {
      const peaks = media.peaks;
      const per = peaks.seconds;
      const a = low / per, b = high / per;
      const span = b - a;
      if (span < 1) {
        const data = peaks.levels[0];
        const place = Math.max(0, a - 0.5), left = Math.floor(place), share = place - left;
        return (data[left] || 0) * (1 - share) + (data[left + 1] || 0) * share;
      }
      const level = Math.min(peaks.levels.length - 1, Math.floor(Math.log2(span)));
      const data = peaks.levels[level];
      const scale = 1 << level;
      const start = Math.floor(a / scale), stop = Math.ceil(b / scale);
      let most = 0;
      for (let index = Math.max(0, start); index < stop && index < data.length; index += 1) if (data[index] > most) most = data[index];
      return most;
    }
    function drawAudio(from, to) {
      const row = rows.audio;
      const draft = liveDraft();
      const muted = !!(draft && draft.edit && draft.edit.audio && draft.edit.audio.mute_source);
      const middle = row.y + row.h / 2;
      const height = row.h / 2 - 2;
      ctx.fillStyle = muted ? tokens.waveDim : tokens.wave;
      for (const span of groups.flatMap((group) => group.spans)) {
        if (span.output_end < from || span.output_start > to) continue;
        const x0 = Math.max(0, Math.floor(xOf(span.output_start))), x1 = Math.min(view.width, Math.ceil(xOf(span.output_end)));
        if (media.peaks) {
          const perPixel = span.speed / view.pps;
          ctx.beginPath();
          const start = span.source_start + (x0 - xOf(span.output_start)) * perPixel;
          const heights = [];
          for (let x = x0; x <= x1; x += 1) {
            const low = start + (x - x0) * perPixel;
            heights.push(Math.max(0.6, peakAt(low, low + perPixel) * height));
          }
          ctx.moveTo(x0, middle - heights[0]);
          heights.forEach((value, index) => ctx.lineTo(x0 + index, middle - value));
          for (let index = heights.length - 1; index >= 0; index -= 1) ctx.lineTo(x0 + index, middle + heights[index]);
          ctx.closePath(); ctx.fill();
        } else {
          const analysis = draft && draft.analysis;
          for (const band of analysis && analysis.speech || []) {
            const low = Math.max(band.start, span.source_start), high = Math.min(band.end, span.source_end);
            if (high <= low) continue;
            const a = xOf(span.output_start + (low - span.source_start) / span.speed), b = xOf(span.output_start + (high - span.source_start) / span.speed);
            ctx.fillRect(a, middle - height * 0.55, Math.max(1, b - a), height * 1.1);
          }
        }
        ctx.fillStyle = tokens.splice;
        ctx.fillRect(Math.round(xOf(span.output_start)) - 1, row.y, 2, row.h);
        ctx.fillStyle = muted ? tokens.waveDim : tokens.wave;
      }
    }
    function drawCaptions(from, to) {
      const row = rows.captions;
      const cues = current.captions || [];
      const chosen = selection.kind === "caption" ? selection.key : null;
      ctx.textAlign = "left";
      const hold = view.hold;
      const reach = hold ? Math.abs(hold.offset) : 0;
      const lo = lowerBound(cues, from - reach);
      for (let index = lo; index < cues.length; index += 1) {
        const cue = cues[index];
        if (cue.start > to + reach) break;
        const left = xOf(cue.start + (hold && cue.start >= hold.from ? hold.offset : 0)), width = Math.max(2, (cue.end - cue.start) * view.pps - 1);
        const on = chosen !== null && chosen === cue.key;
        ctx.fillStyle = tokens.cue;
        ctx.beginPath(); ctx.roundRect(left, row.y + 2, width, row.h - 4, 3); ctx.fill();
        ctx.strokeStyle = on ? tokens.select : tokens.cueLine; ctx.lineWidth = on ? 2 : 1; ctx.stroke();
        if (width > 22) {
          ctx.save();
          ctx.beginPath(); ctx.rect(left + 4, row.y + 2, width - 8, row.h - 4); ctx.clip();
          ctx.fillStyle = tokens.cueInk;
          ctx.fillText(cue.text, left + 5, row.y + row.h / 2 + 0.5);
          ctx.restore();
        }
      }
    }
    function lowerBound(cues, time) {
      let low = 0, high = cues.length;
      while (low < high) { const middle = (low + high) >> 1; if (cues[middle].end < time) low = middle + 1; else high = middle; }
      return low;
    }
    function placePlayhead() {
      nodes.playhead.style.transform = "translate3d(" + (xOf(view.playhead) - 1) + "px,0,0)";
    }

    /* ---------- Editing operations ---------- */
    function nextKept(spans, time) {
      let best = null;
      for (const span of spans) if (span.source_start >= time && (best === null || span.source_start < best.source_start)) best = span;
      return best ? best.output_start : spans.reduce((most, span) => Math.max(most, span.output_end), 0);
    }
    /* Text and overlays stay on the same footage when the cut around them changes. */
    function rippleItems(draft, before) {
      const after = Clock.plan(draft).spans;
      const total = after.reduce((most, span) => Math.max(most, span.output_end), 0);
      const beforeTotal = before.reduce((most, span) => Math.max(most, span.output_end), 0);
      for (const item of [...draft.edit.texts, ...draft.edit.overlays]) {
        if (item && item.style === "callout" && window.BriefcutTimelineItems &&
            window.BriefcutTimelineItems.anchorEndCallout(item, beforeTotal, total)) continue;
        const from = Clock.toSource(before, Number(item.start) || 0);
        if (!from) continue;
        const to = Clock.toOutput(after, from.time);
        item.start = round2(clamp(to === null ? nextKept(after, from.time) : to, 0, Math.max(0, total - 0.1)));
      }
    }
    function structural(label, fn, options) {
      const before = current.spans;
      return Editor.change(label, (draft) => {
        fn(draft);
        if (view.ripple) rippleItems(draft, before);
      }, Object.assign({ coalesce: false }, options || {}));
    }
    function say(message) { if (studio) studio.announce(message); }
    function warn(message) { if (studio) { studio.toast(message); studio.announce(message); } }
    function partLabel(label) {
      const text = String(label || "Moment");
      const found = text.match(/^(.*), part (\d+)$/);
      const next = found ? found[1] + ", part " + (Number(found[2]) + 1) : text + ", part 2";
      return next.slice(0, 100);
    }
    function clipAtPlayhead() { const hit = Clock.toSource(current.spans, view.playhead); return hit ? hit.segment - 1 : -1; }
    function split() {
      const draft = Editor.draft();
      if (!draft) return;
      const hit = Clock.toSource(current.spans, view.playhead);
      if (!hit) return;
      const index = hit.segment - 1;
      const segment = draft.segments[index];
      const at = round3(hit.time);
      if (at - segment.start < MIN_CLIP || segment.end - at < MIN_CLIP) { warn("Move the playhead away from the edge of the moment to split it."); return; }
      if (draft.segments.length >= studio.maxSegments()) { warn("A story holds up to " + studio.maxSegments() + " moments."); return; }
      const done = structural("Split moment " + (index + 1), (plan) => {
        const first = plan.segments[index];
        const second = clone(first);
        first.end = at; second.start = at;
        second.label = partLabel(first.label);
        delete first.keep; delete second.keep;
        if (first.motion && first.motion.from && first.motion.to) {
          const share = (at - segment.start) / (segment.end - segment.start);
          const middle = {};
          for (const key of ["scale", "x", "y"]) middle[key] = round3(first.motion.from[key] + (first.motion.to[key] - first.motion.from[key]) * share);
          first.motion.to = middle; second.motion.from = Object.assign({}, middle);
        }
        plan.segments.splice(index + 1, 0, second);
      });
      if (done) { select({ kind: "clip", index: index + 1 }); view.focusClip = index + 1; paintSelection(); say("Split moment " + (index + 1) + " at " + clockLabel(view.playhead, true) + ". " + draft.segments.length + " moments."); }
    }
    function removeSelection() {
      const draft = Editor.draft();
      if (!draft) return false;
      if (selection.kind === "text" || selection.kind === "overlay") {
        const list = selection.kind === "text" ? "texts" : "overlays";
        const name = selection.kind === "text" ? "text" : "overlay";
        const done = Editor.change("Remove " + name, (plan) => { plan.edit[list].splice(selection.index, 1); }, { coalesce: false });
        if (done) { select({ kind: "none" }); paintSelection(); say("Removed the " + name + "."); }
        return true;
      }
      if (selection.kind === "music") {
        const done = Editor.change("Remove music", (plan) => { plan.edit.music = null; }, { coalesce: false });
        if (done) { select({ kind: "none" }); paintSelection(); say("Removed the music."); }
        return true;
      }
      if (selection.kind !== "clip") return false;
      const chosen = [...selectedClips()].sort((a, b) => b - a);
      if (chosen.length >= draft.segments.length) { warn("Keep at least one moment in the story."); return true; }
      const label = chosen.length === 1 ? "Remove moment " + (chosen[0] + 1) : "Remove " + chosen.length + " moments";
      const done = structural(label, (plan) => { chosen.forEach((index) => plan.segments.splice(index, 1)); });
      if (done) {
        const next = Math.min(chosen[chosen.length - 1], draft.segments.length - 1);
        select({ kind: "clip", index: next }); view.focusClip = next; paintSelection();
        say(label + ". The story closes the gap and now runs " + spoken(current.duration) + ".");
        focusClip();
      }
      return true;
    }
    function moveClip(index, delta) {
      const draft = Editor.draft();
      const target = index + delta;
      if (!draft || target < 0 || target >= draft.segments.length) return;
      const done = structural("Move moment " + (index + 1), (plan) => { const [segment] = plan.segments.splice(index, 1); plan.segments.splice(target, 0, segment); });
      if (done) { select({ kind: "clip", index: target }); view.focusClip = target; paintSelection(); focusClip(); say("Moment moved to position " + (target + 1) + " of " + draft.segments.length + "."); }
    }
    function setSpeed(index, value) {
      const draft = Editor.draft();
      if (!draft || !draft.segments[index]) return;
      const speed = clamp(Math.round(value * 100) / 100, Clock.MIN_SPEED, Clock.MAX_SPEED);
      const done = structural("Speed of moment " + (index + 1), (plan) => { if (speed === 1) delete plan.segments[index].speed; else plan.segments[index].speed = speed; }, { coalesce: true });
      if (done) { paintSelection(); say("Moment " + (index + 1) + " plays at " + speed + " times speed and now runs " + spoken((groups.find((group) => group.segment === index + 1) || { start: 0, end: 0 }).end - (groups.find((group) => group.segment === index + 1) || { start: 0 }).start) + "."); }
    }
    function stepSpeed(index, direction) {
      const draft = liveDraft();
      const speed = draft && draft.segments[index] ? Clock.speedOf(draft.segments[index]) : 1;
      const next = direction > 0 ? SPEEDS.find((value) => value > speed + 1e-6) : [...SPEEDS].reverse().find((value) => value < speed - 1e-6);
      if (next) setSpeed(index, next);
    }
    function trimBy(index, side, delta) {
      const draft = Editor.draft();
      const segment = draft && draft.segments[index];
      if (!segment) return;
      const maximum = sourceSeconds() || Infinity;
      const done = structural("Trim moment " + (index + 1), (plan) => {
        const item = plan.segments[index];
        if (side === "start") item.start = round3(clamp(item.start + delta, 0, item.end - MIN_CLIP));
        else item.end = round3(clamp(item.end + delta, item.start + MIN_CLIP, maximum));
      }, { coalesce: true });
      if (done) { paintSelection(); say((side === "start" ? "In point" : "Out point") + " of moment " + (index + 1) + " is " + spoken(side === "start" ? segment.start : segment.end) + " into the source. The story runs " + spoken(current.duration) + "."); peek(side === "start" ? segment.start : segment.end); }
    }
    function trimToPlayhead(side) {
      const draft = Editor.draft();
      const index = selection.kind === "clip" ? selection.index : clipAtPlayhead();
      const segment = draft && draft.segments[index];
      if (!segment) return;
      const hit = Clock.toSource(current.spans, view.playhead);
      if (!hit || hit.segment !== index + 1) { warn("Put the playhead inside the moment you want to trim."); return; }
      const at = round3(hit.time);
      if (side === "start" ? segment.end - at < MIN_CLIP : at - segment.start < MIN_CLIP) { warn("That would leave less than 0.1 seconds of the moment."); return; }
      const output = groups.find((group) => group.segment === index + 1);
      const done = structural("Trim moment " + (index + 1), (plan) => { plan.segments[index][side] = at; });
      if (done) { select({ kind: "clip", index }); paintSelection(); if (side === "start" && output) Editor.seek(output.start); say((side === "start" ? "In point" : "Out point") + " of moment " + (index + 1) + " set at the playhead."); }
    }
    function slip(index, delta) {
      const draft = Editor.draft();
      const segment = draft && draft.segments[index];
      if (!segment) return;
      const maximum = sourceSeconds() || Infinity;
      const applied = clamp(delta, -segment.start, maximum - segment.end);
      if (Math.abs(applied) < 1e-6) return;
      const done = structural("Nudge moment " + (index + 1), (plan) => { plan.segments[index].start = round3(segment.start + applied); plan.segments[index].end = round3(segment.end + applied); }, { coalesce: true });
      if (done) say("Moment " + (index + 1) + " now shows " + clockLabel(segment.start, true) + " to " + clockLabel(segment.end, true) + " of the source.");
    }
    function setItem(kind, index, start, length, label, coalesce) {
      const list = kind === "text" ? "texts" : "overlays";
      const total = current.duration;
      return Editor.change(label, (plan) => {
        const item = plan.edit[list][index];
        const span = round2(clamp(length, 0.1, Math.max(0.1, total)));
        item.duration = span;
        item.start = round2(clamp(start, 0, Math.max(0, total - span)));
      }, { coalesce });
    }
    function peek(sourceTime) { if (Editor._peek) Editor._peek(sourceTime); }
    function focusClip() {
      requestAnimationFrame(() => { const clip = clipPool[view.focusClip]; if (clip) { clip.body.focus({ preventScroll: true }); revealTime(groups[view.focusClip] ? groups[view.focusClip].start : 0); } });
    }
    function revealTime(time) {
      const x = PAD + time * view.pps;
      if (x < nodes.scroll.scrollLeft + 24 || x > nodes.scroll.scrollLeft + view.width - 80) nodes.scroll.scrollLeft = x - view.width / 3;
    }
    function cuts() {
      const points = new Set([0, current.duration]);
      for (const span of current.spans) { points.add(span.output_start); points.add(span.output_end); }
      return [...points].sort((a, b) => a - b);
    }
    function jumpCut(direction) {
      const list = cuts();
      const now = view.playhead;
      const target = direction > 0 ? list.find((time) => time > now + 0.01) : [...list].reverse().find((time) => time < now - 0.01);
      if (target !== undefined) { Editor.seek(target); revealTime(target); say("Playhead at " + clockLabel(target, true)); }
    }
    function selectClip(index, options) {
      const settings = options || {};
      if (settings.range && selection.kind === "clip") {
        const anchor = selection.index;
        const low = Math.min(anchor, index), high = Math.max(anchor, index);
        const indices = [];
        for (let i = low; i <= high; i += 1) indices.push(i);
        select({ kind: "clip", index: anchor, indices });
      } else if (settings.toggle && selection.kind === "clip") {
        const set = new Set(selectedClips());
        if (set.has(index)) set.delete(index); else set.add(index);
        const indices = [...set].sort((a, b) => a - b);
        if (indices.length) select({ kind: "clip", index: indices[indices.length - 1], indices });
        else select({ kind: "none" });
      } else select({ kind: "clip", index });
      const position = groups.findIndex((group) => group.segment === index + 1);
      if (position >= 0) view.focusClip = position;
      paintSelection();
    }

    /* ---------- Direct manipulation ---------- */
    function snapTargets(skip) {
      const list = [view.playhead, 0, current.duration];
      for (const group of groups) if (group.segment !== skip) list.push(group.start, group.end);
      for (const entry of timedItems()) list.push(entry.start, entry.start + entry.length);
      return list;
    }
    function snapTime(time, targets, event) {
      if (!view.snap || (event && event.altKey)) { hideSnap(); return time; }
      const tolerance = SNAP_PX / view.pps;
      let best = time, gap = tolerance;
      for (const target of targets) { const distance = Math.abs(target - time); if (distance < gap) { gap = distance; best = target; } }
      if (best !== time) showSnap(best); else hideSnap();
      return best;
    }
    function showSnap(time) { nodes.snapLine.hidden = false; nodes.snapLine.style.transform = "translate3d(" + Math.round(xOf(time)) + "px,0,0)"; }
    function hideSnap() { nodes.snapLine.hidden = true; }
    function sourceBoundaries() {
      const draft = liveDraft();
      const analysis = draft && draft.analysis;
      if (analysis === boundaryKey) return boundaries;
      boundaryKey = analysis;
      const points = [];
      for (const sentence of analysis && analysis.sentences || []) points.push(sentence.start, sentence.end);
      for (const scene of analysis && analysis.scenes || []) points.push(scene);
      boundaries = points.filter(finiteNumber).sort((a, b) => a - b);
      return boundaries;
    }
    function snapSource(time, speed, event) {
      if (!view.snap || event.altKey) return { time, snapped: false };
      const list = sourceBoundaries();
      if (!list.length) return { time, snapped: false };
      let low = 0, high = list.length;
      while (low < high) { const middle = (low + high) >> 1; if (list[middle] < time) low = middle + 1; else high = middle; }
      const tolerance = SNAP_PX / view.pps * speed;
      let best = time, gap = tolerance;
      for (const candidate of [list[low - 1], list[low]]) { if (candidate === undefined) continue; const distance = Math.abs(candidate - time); if (distance < gap) { gap = distance; best = candidate; } }
      return { time: best, snapped: best !== time };
    }
    function extended(draft, index, patch) {
      const segment = draft.segments[index];
      const probe = Object.assign({}, draft, { segments: [Object.assign({}, segment, patch)] });
      return Clock.keepsOf(Clock.resolve(probe)[0]);
    }
    function endForLength(draft, index, length) {
      const segment = draft.segments[index];
      const maximum = sourceSeconds() || segment.end;
      let left = length * Clock.speedOf(segment);
      for (const [low, high] of extended(draft, index, { end: maximum })) { if (left <= high - low) return low + left; left -= high - low; }
      return maximum;
    }
    function startForLength(draft, index, length) {
      const segment = draft.segments[index];
      let left = length * Clock.speedOf(segment);
      for (const [low, high] of extended(draft, index, { start: 0 }).reverse()) { if (left <= high - low) return high - left; left -= high - low; }
      return 0;
    }

    function onPointerDown(event) {
      if (event.button !== 0 || !liveDraft() || gesture) return;
      const edge = event.target.closest(".tl-edge");
      const clip = event.target.closest(".tl-clip");
      const item = event.target.closest(".tl-item");
      if (event.target.closest(".tl-join, .tl-music-block")) return;
      const frozen = locked();
      let next = null;
      const base = { pointer: event.pointerId, x0: event.clientX, scroll0: nodes.scroll.scrollLeft, x: event.clientX, active: false, event };
      if (edge && !frozen && clip) {
        const index = Number(clip.dataset.index);
        const group = groups.find((entry) => entry.segment === index + 1);
        next = Object.assign(base, { type: "trim", index, side: edge.dataset.edge, group: Object.assign({}, group), before: current.spans, base: clone(liveDraft().segments), wasDuration: current.duration });
        selectClip(index);
      } else if (edge && !frozen && item) {
        const entry = timedItems().find((row) => row.kind === item.dataset.kind && row.index === Number(item.dataset.index));
        next = Object.assign(base, { type: "itemTrim", side: edge.dataset.edge, entry });
        select({ kind: entry.kind, index: entry.index }); paintSelection();
      } else if (clip) {
        const index = Number(clip.dataset.index);
        next = Object.assign(base, { type: "clip", index, frozen, start: index, before: current.spans, base: clone(liveDraft().segments), modifiers: { range: event.shiftKey, toggle: event.metaKey || event.ctrlKey }, grab: event.clientX - clip.getBoundingClientRect().left });
      } else if (item) {
        const entry = timedItems().find((row) => row.kind === item.dataset.kind && row.index === Number(item.dataset.index));
        next = Object.assign(base, { type: "item", entry, frozen });
      } else {
        const inCaptions = event.clientY - nodes.stage.getBoundingClientRect().top >= rows.captions.y && event.clientY - nodes.stage.getBoundingClientRect().top < rows.captions.y + rows.captions.h;
        next = Object.assign(base, { type: "scrub", caption: inCaptions, targets: cuts() });
        if (selection.kind === "caption" || !inCaptions && selection.kind !== "none" && !event.shiftKey) { select({ kind: "none" }); paintSelection(); }
        stepScrub(next, event);
      }
      gesture = next;
      nodes.stage.setPointerCapture(event.pointerId);
      event.preventDefault();
      if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
      nodes.stage.classList.add("is-busy");
      autoScroll();
    }
    function pointerShift(g) { return (g.x - g.x0 + nodes.scroll.scrollLeft - g.scroll0) / view.pps; }
    function stepScrub(g, event) {
      const time = clamp(snapTime(timeAt(event.clientX), g.targets, event), 0, current.duration);
      Editor.seek(time, { scrub: true });
    }
    function stepTrim(g, event) {
      const draft = liveDraft();
      const segment = draft.segments[g.index];
      const shift = pointerShift(g);
      if (!g.active && Math.abs(shift * view.pps) < 2) return;
      g.active = true;
      const speed = Clock.speedOf(segment);
      if (g.side === "end") {
        const free = g.group.end + shift;
        const edge = snapTime(free, snapTargets(g.index + 1).filter((time) => time <= g.group.start + 1e-6 || time === view.playhead), event);
        const wanted = endForLength(draft, g.index, Math.max(0, edge - g.group.start));
        const landed = edge === free ? snapSource(wanted, speed, event) : { time: wanted, snapped: false };
        segment.end = round3(clamp(landed.time, segment.start + MIN_CLIP, sourceSeconds() || landed.time));
        Editor._transient();
        if (landed.snapped) { const group = groups.find((entry) => entry.segment === g.index + 1); if (group) showSnap(group.end); }
        peek(segment.end);
      } else {
        const free = g.group.start + shift;
        const edge = snapTime(free, snapTargets(g.index + 1).filter((time) => time <= g.group.end - 0.05), event);
        const wanted = startForLength(draft, g.index, Math.max(0, g.group.end - edge));
        const landed = edge === free ? snapSource(wanted, speed, event) : { time: wanted, snapped: false };
        segment.start = round3(clamp(landed.time, 0, segment.end - MIN_CLIP));
        Editor._transient();
        holdRight(g);
        if (landed.snapped) { const group = groups.find((entry) => entry.segment === g.index + 1); if (group) showSnap(group.start); }
        peek(segment.start);
      }
    }
    /* Trimming the left edge keeps everything else still until release, then the gap closes. */
    function holdRight(g) {
      const mine = current.spans.filter((span) => span.segment === g.index + 1);
      if (!mine.length) return;
      const offset = g.group.end - mine.reduce((most, span) => Math.max(most, span.output_end), 0);
      const from = mine[0].output_start;
      const spans = current.spans.map((span) => span.segment >= g.index + 1 ? Object.assign({}, span, { output_start: span.output_start + offset, output_end: span.output_end + offset }) : span);
      view.hold = { spans, offset, from };
      groups = groupSpans(spans);
      groups.forEach((group, position) => {
        const clip = clipPool[position];
        if (!clip) return;
        clip.root.style.transform = "translate3d(" + (PAD + group.start * view.pps) + "px,0,0)";
        clip.root.style.width = Math.max(3, (group.end - group.start) * view.pps) + "px";
      });
      invalidate();
    }
    function stepClip(g, event) {
      if (g.frozen) return;
      const distance = Math.abs(g.x - g.x0);
      if (!g.active) {
        if (distance < 5) return;
        g.active = true;
        if (selection.kind !== "clip" || !selectedClips().includes(g.index) || selectedClips().length > 1) selectClip(g.index);
        nodes.stage.classList.add("is-moving");
        const draft = liveDraft();
        const name = draft.segments[g.index].label || "Moment " + (g.index + 1);
        nodes.ghost.textContent = (g.index + 1) + "  " + name;
        nodes.ghost.hidden = false;
        nodes.ghost.style.top = rows.video.y - 3 + "px"; nodes.ghost.style.height = rows.video.h + 6 + "px";
      }
      const left = g.x - nodes.stage.getBoundingClientRect().left - Math.min(g.grab, 60);
      nodes.ghost.style.transform = "translate3d(" + left + "px,0,0)";
      const draft = liveDraft();
      const wanted = timeAt(g.x) - g.grab / view.pps;
      const others = groups.filter((group) => group.segment !== g.index + 1);
      let cursor = 0, position = 0, gap = Infinity;
      others.concat(null).forEach((group, slot) => {
        if (Math.abs(cursor - wanted) < gap) { gap = Math.abs(cursor - wanted); position = slot; }
        if (group) cursor += group.end - group.start;
      });
      if (position !== g.index) {
        const [segment] = draft.segments.splice(g.index, 1);
        draft.segments.splice(position, 0, segment);
        g.index = position;
        select({ kind: "clip", index: position });
        view.focusClip = position;
        Editor._transient();
        say("Position " + (position + 1) + " of " + draft.segments.length);
      }
    }
    function stepItem(g, event) {
      if (g.frozen) return;
      const shift = pointerShift(g);
      if (!g.active && Math.abs(shift * view.pps) < 4) return;
      const entry = g.entry;
      if (!g.active) { g.active = true; select({ kind: entry.kind, index: entry.index }); }
      const start = snapTime(Math.max(0, entry.start + shift), snapTargets(0), event);
      const draft = liveDraft();
      const item = draft.edit[entry.kind === "text" ? "texts" : "overlays"][entry.index];
      item.start = round2(clamp(start, 0, Math.max(0, current.duration - entry.length)));
      syncItems();
    }
    function stepItemTrim(g, event) {
      const shift = pointerShift(g);
      if (!g.active && Math.abs(shift * view.pps) < 2) return;
      g.active = true;
      const entry = g.entry;
      const draft = liveDraft();
      const item = draft.edit[entry.kind === "text" ? "texts" : "overlays"][entry.index];
      if (g.side === "end") {
        const end = snapTime(entry.start + entry.length + shift, snapTargets(0), event);
        item.duration = round2(clamp(end - entry.start, 0.1, current.duration - entry.start));
      } else {
        const stop = entry.start + entry.length;
        const start = snapTime(clamp(entry.start + shift, 0, stop - 0.1), snapTargets(0), event);
        item.start = round2(start); item.duration = round2(stop - start);
      }
      syncItems();
    }
    function onPointerMove(event) {
      if (!gesture || event.pointerId !== gesture.pointer) return;
      gesture.x = event.clientX; gesture.event = event;
      stepGesture(event);
    }
    function stepGesture(event) {
      const g = gesture;
      if (g.type === "scrub") stepScrub(g, event);
      else if (g.type === "trim") stepTrim(g, event);
      else if (g.type === "clip") stepClip(g, event);
      else if (g.type === "item") stepItem(g, event);
      else if (g.type === "itemTrim") stepItemTrim(g, event);
    }
    function autoScroll() {
      if (!gesture) return;
      if (gesture.type !== "scrub" || gesture.active !== null) {
        const box = nodes.stage.getBoundingClientRect();
        const inset = 36;
        const over = gesture.x > box.right - inset ? gesture.x - (box.right - inset) : gesture.x < box.left + inset ? gesture.x - (box.left + inset) : 0;
        if (over && (gesture.active || gesture.type === "scrub")) {
          const before = nodes.scroll.scrollLeft;
          nodes.scroll.scrollLeft = before + over * 0.4;
          if (nodes.scroll.scrollLeft !== before) stepGesture(gesture.event);
        }
      }
      requestAnimationFrame(autoScroll);
    }
    function endGesture(cancelled) {
      const g = gesture;
      if (!g) return;
      gesture = null;
      nodes.stage.classList.remove("is-busy", "is-moving");
      nodes.ghost.hidden = true;
      hideSnap();
      view.hold = null;
      if (g.type === "trim") peek(null);
      if (g.type === "scrub") {
        if (!cancelled) {
          if (g.caption && Math.abs(g.x - g.x0) < 4) pickCue(timeAt(g.x));
          Editor.seek(view.playhead, {});
        }
      } else if (g.type === "trim") {
        const draft = liveDraft();
        if (cancelled) draft.segments = g.base;
        else if (g.active) {
          if (view.ripple) rippleItems(draft, g.before);
          Editor._commit("Trim moment " + (g.index + 1), { coalesce: false });
          const segment = draft.segments[g.index];
          say("Moment " + (g.index + 1) + " now runs " + clockLabel(segment.start, true) + " to " + clockLabel(segment.end, true) + " of the source. The story runs " + spoken(current.duration) + ".");
        }
        Editor._transient();
      } else if (g.type === "clip") {
        const draft = liveDraft();
        if (g.active) {
          if (cancelled) { draft.segments = g.base; select({ kind: "clip", index: g.start }); }
          else if (g.index !== g.start) {
            if (view.ripple) rippleItems(draft, g.before);
            Editor._commit("Move moment " + (g.start + 1), { coalesce: false });
            say("Moved to position " + (g.index + 1) + " of " + draft.segments.length + ".");
          }
          Editor._transient();
        } else if (!cancelled) selectClip(g.index, g.modifiers);
      } else if (g.type === "item" || g.type === "itemTrim") {
        if (g.active) {
          const draft = liveDraft();
          const list = draft.edit[g.entry.kind === "text" ? "texts" : "overlays"];
          const item = list[g.entry.index];
          if (cancelled) { item.start = g.entry.start; item.duration = g.entry.length; syncItems(); }
          else { Editor._commit(g.type === "item" ? "Move " + g.entry.kind : "Trim " + g.entry.kind, { coalesce: false }); say(g.entry.name + " now starts at " + clockLabel(item.start, true) + " and lasts " + spoken(item.duration) + "."); }
        } else if (!cancelled) { select({ kind: g.entry.kind, index: g.entry.index }); paintSelection(); }
      }
      syncAll();
    }
    function cueAt(time) {
      const cues = current.captions || [];
      return cues.find((cue) => cue.start <= time && time < cue.end) || null;
    }
    function pickCue(time) {
      const cue = cueAt(time);
      if (!cue) return;
      select({ kind: "caption", cue: cue.start, key: cue.key });
      paintSelection();
    }

    /* ---------- Keyboard ---------- */
    const isTyping = (target) => !!target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || (target.isContentEditable && !target.closest("#transcript-list")));
    const inPanel = (target) => !!target && !!target.closest && !!target.closest("#inspector-mount, #media-bin-mount, #templates-mount, .ed-tabs");
    function onKey(event) {
      if (event.defaultPrevented || document.querySelector("dialog[open]")) return;
      const target = event.target;
      const command = event.metaKey || event.ctrlKey;
      if (event.altKey && !command && /^Digit[123]$/.test(event.code) && liveDraft()) {
        event.preventDefault(); event.stopPropagation();
        Shell.mode(["story", "edit", "deliver"][Number(event.code.slice(5)) - 1], { focus: true, announce: true });
        return;
      }
      if (!liveDraft()) return;
      const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
      if (key === "Escape" && gesture) { event.preventDefault(); event.stopPropagation(); endGesture(true); return; }
      if (gesture) return;
      if (command && !event.altKey && key === "z") {
        if (isTyping(target) && target.id !== "tl-speed") return;
        event.preventDefault(); event.stopPropagation();
        if (event.shiftKey) Editor.redo(); else Editor.undo();
        return;
      }
      if (command && !event.altKey && !event.shiftKey && key === "y" && !isTyping(target)) { event.preventDefault(); event.stopPropagation(); Editor.redo(); return; }
      if (Shell.current() !== "edit" || isTyping(target)) return;
      if (command && !event.altKey && key === "b") { event.preventDefault(); event.stopPropagation(); split(); return; }
      if (command) return;
      if (inPanel(target)) return;
      const handled = run(event, key, target);
      if (handled) { event.preventDefault(); event.stopPropagation(); }
    }
    function run(event, key, target) {
      const shift = event.shiftKey, alt = event.altKey;
      const edge = target.closest && target.closest(".tl-edge");
      const clipBody = target.closest && target.closest(".tl-clip-body");
      const itemBody = target.closest && target.closest(".tl-item-body");
      const nativeButton = target.tagName === "BUTTON" || target.tagName === "A" || target.tagName === "SUMMARY";
      const inTranscript = target.closest && !!target.closest("#transcript-list");
      const clipIndex = selection.kind === "clip" ? selection.index : -1;
      const step = shift ? 1 : 0.1;
      if (edge) {
        const clip = edge.closest(".tl-clip"), item = edge.closest(".tl-item");
        if (key === "ArrowLeft" || key === "ArrowRight") {
          const direction = key === "ArrowLeft" ? -1 : 1;
          if (clip) trimBy(Number(clip.dataset.index), edge.dataset.edge, direction * step);
          else if (item) {
            const entry = timedItems().find((row) => row.kind === item.dataset.kind && row.index === Number(item.dataset.index));
            if (edge.dataset.edge === "start") setItem(entry.kind, entry.index, entry.start + direction * step, entry.length - direction * step, "Trim " + entry.kind, true);
            else setItem(entry.kind, entry.index, entry.start, entry.length + direction * step, "Trim " + entry.kind, true);
            paintSelection();
          }
          return true;
        }
      }
      if (itemBody && (key === "ArrowLeft" || key === "ArrowRight") && !alt) {
        const item = itemBody.closest(".tl-item");
        const entry = timedItems().find((row) => row.kind === item.dataset.kind && row.index === Number(item.dataset.index));
        setItem(entry.kind, entry.index, entry.start + (key === "ArrowLeft" ? -step : step), entry.length, "Move " + entry.kind, true);
        paintSelection();
        const moved = timedItems().find((row) => row.kind === entry.kind && row.index === entry.index);
        if (moved) say(moved.name + " starts at " + clockLabel(moved.start, true) + ".");
        return true;
      }
      if (clipBody && (key === "ArrowLeft" || key === "ArrowRight") && !alt && !shift) {
        const next = clamp(view.focusClip + (key === "ArrowLeft" ? -1 : 1), 0, groups.length - 1);
        if (next !== view.focusClip) { view.focusClip = next; selectClip(groups[next].segment - 1); focusClip(); const draft = liveDraft(); say(momentName(draft.segments[groups[next].segment - 1], groups[next].segment - 1)); }
        return true;
      }
      if (inTranscript && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown", "Delete", "Backspace", "Tab"].includes(key)) return false;
      switch (key) {
        case " ": if (nativeButton && !clipBody && !itemBody) return false; togglePlay(); return true;
        case "k": togglePlay(); return true;
        case "j": Editor.seek(view.playhead - 5); return true;
        case "l": Editor.seek(view.playhead + 5); return true;
        case "ArrowLeft":
        case "ArrowRight": {
          const direction = key === "ArrowLeft" ? -1 : 1;
          if (alt) { if (clipIndex >= 0) moveClip(clipIndex, direction); return true; }
          Editor.pause();
          Editor.seek(view.playhead + direction * (shift ? 1 : FRAME));
          revealTime(view.playhead);
          return true;
        }
        case "ArrowUp":
        case "ArrowDown":
          if (alt) { if (clipIndex >= 0) stepSpeed(clipIndex, key === "ArrowUp" ? 1 : -1); return true; }
          jumpCut(key === "ArrowUp" ? -1 : 1);
          return true;
        case "Home": Editor.seek(0); revealTime(0); return true;
        case "End": Editor.seek(current.duration); revealTime(current.duration); return true;
        case "s": split(); return true;
        case "Delete":
        case "Backspace": return removeSelection();
        case "q": case "i": trimToPlayhead("start"); return true;
        case "w": case "o": trimToPlayhead("end"); return true;
        case "[": case "{": if (clipIndex >= 0) trimBy(clipIndex, "start", key === "[" ? -0.1 : 0.1); return clipIndex >= 0;
        case "]": case "}": if (clipIndex >= 0) trimBy(clipIndex, "end", key === "]" ? 0.1 : -0.1); return clipIndex >= 0;
        case ",": case "<": if (clipIndex >= 0) slip(clipIndex, key === "," ? -0.1 : -1); return clipIndex >= 0;
        case ".": case ">": if (clipIndex >= 0) slip(clipIndex, key === "." ? 0.1 : 1); return clipIndex >= 0;
        case "r": view.ripple = !view.ripple; paintToggles(); say(view.ripple ? "Ripple on. Text and overlays move with the footage." : "Ripple off. Text and overlays stay where they are."); return true;
        case "n": view.snap = !view.snap; paintToggles(); say(view.snap ? "Snapping on." : "Snapping off."); return true;
        case "=": case "+": zoomBy(1.4); return true;
        case "-": case "_": zoomBy(1 / 1.4); return true;
        case "z": if (shift) { fit(); return true; } return false;
        case "c": { const s = state(); if ($("preview-final").disabled) return false; studio.setPreview(s.preview === "final" ? "story" : "final"); studio.toast(s.preview === "final" ? "Finished video" : "Preview"); return true; }
        case "Escape": if (selection.kind !== "none") { select({ kind: "none" }); paintSelection(); return true; } return false;
        default:
          if (/^[1-9]$/.test(key)) {
            const draft = liveDraft();
            const index = Number(key) - 1;
            if (!draft.segments[index]) return false;
            selectClip(index);
            const group = groups.find((entry) => entry.segment === index + 1);
            if (group) { Editor.seek(group.start); revealTime(group.start); }
            focusClip();
            say(momentName(draft.segments[index], index));
            return true;
          }
          return false;
      }
    }
    function togglePlay() { if (Editor.playing()) Editor.pause(); else Editor.play(); }

    /* ---------- Zoom ---------- */
    function setScale(pps, anchor) {
      const low = fitScale();
      const value = clamp(pps, low, Math.max(MAX_PPS, low));
      const time = anchor === undefined ? view.playhead : anchor;
      const screen = PAD + time * view.pps - nodes.scroll.scrollLeft;
      view.pps = value;
      view.fit = value <= low * 1.001;
      syncAll();
      nodes.scroll.scrollLeft = PAD + time * value - (screen >= 0 && screen <= view.width ? screen : view.width / 2);
      invalidate();
    }
    function zoomBy(factor) { setScale(view.pps * factor); }
    function fit() { view.fit = true; syncAll(); nodes.scroll.scrollLeft = 0; invalidate(); }

    /* ---------- Media ---------- */
    function loadStrip(job) {
      const strip = job && job.source && job.source.filmstrip;
      const url = strip && strip.url || "";
      if (url === media.stripURL) return;
      media.stripURL = url; media.strip = null;
      if (!url) return;
      const image = new Image();
      image.decoding = "async";
      image.onload = () => { if (media.stripURL === url) { media.strip = Object.assign({}, strip, { image }); invalidate(); } };
      image.src = url;
    }
    function buildPeaks(data) {
      const base = Float32Array.from(data.peaks || []);
      if (!base.length || !(data.seconds_per_peak > 0)) return null;
      const levels = [base];
      while (levels[levels.length - 1].length > 2) {
        const above = levels[levels.length - 1];
        const next = new Float32Array(Math.ceil(above.length / 2));
        for (let index = 0; index < next.length; index += 1) next[index] = Math.max(above[index * 2], index * 2 + 1 < above.length ? above[index * 2 + 1] : 0);
        levels.push(next);
      }
      return { seconds: data.seconds_per_peak, levels };
    }
    /* Until the waveform exists, or for a project without one, speech bands stand in. */
    async function loadPeaks(job) {
      if (!job || !job.source || !job.source.waveform || media.peakJob === job.id) return;
      const id = job.id;
      media.peakJob = id; media.peaks = null;
      try {
        const data = await Editor.api("/api/campaigns/" + encodeURIComponent(id) + "/waveform", { timeout: 30000 });
        if (media.peakJob === id) { media.peaks = buildPeaks(data); invalidate(); }
      } catch (_) { if (media.peakJob === id) media.peakJob = ""; }
    }

    /* ---------- Wiring ---------- */
    function syncAll() {
      if (!nodes.scroll || !measure()) return;
      applyScale();
      if (!view.hold) syncClips();
      syncItems(); syncMusic(); syncJoins();
      nodes.ruler.setAttribute("aria-valuemax", String(current.duration));
      if (!nodes.ruler.hasAttribute("aria-valuenow")) { nodes.ruler.setAttribute("aria-valuenow", "0"); nodes.ruler.setAttribute("aria-valuetext", "0:00.0"); }
      nodes.total.textContent = current.duration ? clockLabel(current.duration, true) : "";
      nodes.total.title = "Length of the finished video";
      paintTools();
      invalidate();
    }
    function onSeek(seconds) {
      view.playhead = seconds;
      if (!nodes.playhead) return;
      if (Editor.playing()) {
        const x = PAD + seconds * view.pps;
        const left = nodes.scroll.scrollLeft;
        if (x > left + view.width - FOLLOW_MARGIN || x < left) nodes.scroll.scrollLeft = x - FOLLOW_MARGIN;
      }
      view.scrollX = nodes.scroll.scrollLeft;
      placePlayhead();
      const now = performance.now();
      if (now - ariaStamp > 250) {
        ariaStamp = now;
        nodes.ruler.setAttribute("aria-valuenow", String(round2(seconds)));
        nodes.ruler.setAttribute("aria-valuetext", clockLabel(seconds, true) + " of " + clockLabel(current.duration, true));
      }
      Transcript.now(seconds);
    }
    function attach() {
      build();
      readTokens();
      const theme = matchMedia("(prefers-color-scheme: dark)");
      theme.addEventListener("change", () => { readTokens(); invalidate(); });
      new MutationObserver(() => { readTokens(); invalidate(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class"] });
      new ResizeObserver(() => { const before = view.width; if (measure() && (before !== view.width || view.fit)) syncAll(); invalidate(); }).observe(nodes.scroll);
      nodes.scroll.addEventListener("scroll", invalidate, { passive: true });
      nodes.scroll.addEventListener("wheel", (event) => {
        if (event.ctrlKey || event.metaKey) { event.preventDefault(); const time = timeAt(event.clientX); setScale(view.pps * Math.exp(-event.deltaY * 0.01), time); }
        else if (!event.shiftKey && Math.abs(event.deltaY) > Math.abs(event.deltaX) && document.body.scrollHeight <= innerHeight) nodes.scroll.scrollLeft += event.deltaY;
      }, { passive: false });
      nodes.stage.addEventListener("pointerdown", onPointerDown);
      nodes.stage.addEventListener("pointermove", onPointerMove);
      nodes.stage.addEventListener("pointerup", (event) => { if (gesture && event.pointerId === gesture.pointer) endGesture(false); });
      nodes.stage.addEventListener("pointercancel", (event) => { if (gesture && event.pointerId === gesture.pointer) endGesture(true); });
      nodes.stage.addEventListener("lostpointercapture", (event) => { if (gesture && event.pointerId === gesture.pointer) endGesture(false); });
      nodes.stage.addEventListener("dblclick", (event) => {
        if (event.clientY - nodes.stage.getBoundingClientRect().top < rows.captions.y || event.clientY - nodes.stage.getBoundingClientRect().top >= rows.captions.y + rows.captions.h) return;
        const cue = cueAt(timeAt(event.clientX));
        if (cue) Transcript.editCue(cue, { left: event.clientX - 150, top: nodes.stage.getBoundingClientRect().top + rows.captions.y - 8, width: 300 });
      });
      nodes.clips.addEventListener("click", (event) => {
        const body = event.target.closest(".tl-clip-body");
        if (body && event.detail === 0) selectClip(Number(body.closest(".tl-clip").dataset.index));
      });
      nodes.items.addEventListener("click", (event) => {
        const body = event.target.closest(".tl-item-body");
        if (body && event.detail === 0) { const item = body.closest(".tl-item"); select({ kind: item.dataset.kind, index: Number(item.dataset.index) }); paintSelection(); }
      });
      $("tl-undo").addEventListener("click", () => Editor.undo());
      $("tl-redo").addEventListener("click", () => Editor.redo());
      $("tl-split").addEventListener("click", split);
      $("tl-delete").addEventListener("click", removeSelection);
      $("tl-left").addEventListener("click", () => { if (selection.kind === "clip") moveClip(selection.index, -1); });
      $("tl-right").addEventListener("click", () => { if (selection.kind === "clip") moveClip(selection.index, 1); });
      nodes.speedSelect.addEventListener("change", () => { if (selection.kind === "clip") setSpeed(selection.index, Number(nodes.speedSelect.value)); });
      $("tl-ripple").addEventListener("click", () => { view.ripple = !view.ripple; paintToggles(); });
      $("tl-snap").addEventListener("click", () => { view.snap = !view.snap; paintToggles(); });
      $("tl-zoom-in").addEventListener("click", () => zoomBy(1.4));
      $("tl-zoom-out").addEventListener("click", () => zoomBy(1 / 1.4));
      $("tl-fit").addEventListener("click", fit);
      nodes.zoomRange.addEventListener("input", () => {
        const low = fitScale(), high = Math.max(MAX_PPS, low * 1.01);
        setScale(low * Math.pow(high / low, Number(nodes.zoomRange.value) / 1000));
      });
      document.addEventListener("keydown", onKey, true);
      document.addEventListener("click", (event) => {
        if (event.detail === 0 || Shell.current() !== "edit") return;
        const button = event.target.closest && event.target.closest("button");
        if (button && !button.closest("dialog")) button.blur();
      }, true);
      Editor.on("timeline", () => { syncAll(); if (!gesture) Transcript.paint(); });
      Editor.on("seek", onSeek);
      Editor.on("select", paintSelection);
      Transcript.attach();
    }
    return {
      attach,
      job(job) { loadStrip(job); loadPeaks(job); paintTools(); syncAll(); Transcript.job(job); },
      story() { syncAll(); Transcript.render(); },
      preview() { paintTools(); },
      reset() { media.stripURL = ""; media.strip = null; media.peakJob = ""; media.peaks = null; syncAll(); Transcript.render(); },
      history() { paintHistory(); },
      notice(text) { if (nodes.notice) { nodes.notice.textContent = text; nodes.notice.title = text; } },
      resized() { syncAll(); },
      jumpCut,
      togglePlay,
      paintSelection,
      structural,
      outputNear(time) { return nextKept(current.spans, time); },
      view,
    };
  })();

  /* ---------- Transcript editing: cut by words, restore, fix captions ---------- */
  const Transcript = (() => {
    const $ = (id) => document.getElementById(id);
    const CLASSES = ["tw", "tw is-cut", "tw is-cleaned", "tw is-out"];
    const MAX_REMOVED = 400, EDGE_PAD = 0.03, RESTORE_PAD = 0.05, MIN_LEFT = 0.08;
    let lines = [], tokens = [], precise = false, builtKey = "", shownEdits = "", query = "", nowToken = null, lastLine = null, editing = null, selectionTimer = 0;
    const buttons = {};

    function listOf(draft) {
      const analysis = draft && draft.analysis;
      if (!analysis) return [];
      const sentences = Array.isArray(analysis.sentences) ? analysis.sentences : [], transcript = Array.isArray(analysis.transcript) ? analysis.transcript : [];
      return (sentences.length ? sentences : transcript).filter((cue) => cue && typeof cue.text === "string" && cue.text.trim() && Number.isFinite(Number(cue.start)) && Number.isFinite(Number(cue.end)));
    }
    function merged(pairs) {
      const sorted = pairs.filter((pair) => pair[1] > pair[0]).map((pair) => [pair[0], pair[1]]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      const out = [];
      for (const pair of sorted) {
        const last = out[out.length - 1];
        if (last && pair[0] <= last[1] + 0.005) last[1] = Math.max(last[1], pair[1]); else out.push(pair);
      }
      return out;
    }
    function minus(pairs, cuts) {
      let result = pairs.map((pair) => [pair[0], pair[1]]);
      for (const [low, high] of merged(cuts)) {
        const next = [];
        for (const [a, b] of result) {
          if (high <= a || low >= b) { next.push([a, b]); continue; }
          if (low - a >= 0.02) next.push([a, low]);
          if (b - high >= 0.02) next.push([high, b]);
        }
        result = next;
      }
      return result;
    }
    function wordsOf(draft) {
      const words = draft && draft.analysis && Array.isArray(draft.analysis.words) ? draft.analysis.words : [];
      return words.filter((word) => word && String(word.text || "").trim() && Number.isFinite(Number(word.start)) && Number.isFinite(Number(word.end)));
    }
    /* Heard words are the most faithful text, so they decide the lines when they exist. */
    function wordLines(words) {
      const result = [];
      let run = [];
      const flush = () => { if (run.length) result.push({ start: run[0].start, end: run[run.length - 1].end, text: run.map((word) => String(word.text).trim()).join(" "), words: run }); run = []; };
      words.forEach((word, index) => {
        const next = words[index + 1];
        run.push(word);
        if (/[.!?…]["”’)\]]*$/.test(String(word.text).trim()) || run.length >= 22 || next && next.start - word.end > 1.2) flush();
      });
      flush();
      return result;
    }
    function tokenize(cue) {
      if (cue.words) return cue.words.map((word) => ({ text: String(word.text).trim(), start: word.start, end: word.end, timed: true }));
      const parts = String(cue.text).trim().split(/\s+/).filter(Boolean);
      const start = Number(cue.start), end = Number(cue.end);
      const total = parts.reduce((sum, part) => sum + part.length + 1, 0) || 1;
      let used = 0;
      return parts.map((part) => {
        const a = start + (end - start) * used / total;
        used += part.length + 1;
        return { text: part, start: a, end: start + (end - start) * used / total, timed: false };
      });
    }
    function build() {
      const draft = liveDraft();
      const list = $("transcript-list");
      const cues = listOf(draft);
      const panel = $("transcript-panel");
      const hasStory = !!draft;
      const hasText = cues.length > 0 || wordsOf(draft).length > 0;
      panel.hidden = !hasText || !hasStory;
      $("transcript-empty").hidden = !hasStory || hasText;
      lines = []; tokens = []; nowToken = null; lastLine = null;
      const words = wordsOf(draft);
      precise = words.length > 0;
      const fragment = document.createDocumentFragment();
      (precise ? wordLines(words) : cues).forEach((cue, index) => {
        const line = { index, cue, tokens: [], node: el("li", "tx-line") };
        line.node.dataset.line = String(index);
        const stamp = el("span", "tx-time", clockLabel(Number(cue.start)));
        stamp.contentEditable = "false"; stamp.dataset.start = String(cue.start);
        const body = el("span", "tx-body");
        tokenize(cue).forEach((word, position) => {
          const token = Object.assign(word, { line, state: 0, index: tokens.length, node: null });
          const node = el("span", "tw", word.text);
          node.dataset.w = String(token.index);
          token.node = node;
          tokens.push(token); line.tokens.push(token);
          if (position) body.append(" ");
          body.append(node);
        });
        line.node.append(stamp, body);
        lines.push(line);
        fragment.append(line.node);
      });
      list.replaceChildren(fragment);
      showEdits();
      filter();
      paint();
    }
    /* A caption edit changes what is read on screen, not what was said, so it sits under the line. */
    function showEdits() {
      const draft = liveDraft();
      const edits = draft && draft.edit && draft.edit.captions && draft.edit.captions.edits || {};
      for (const node of $("transcript-list").querySelectorAll(".tx-caption")) node.remove();
      for (const key of Object.keys(edits).sort((a, b) => Number(a) - Number(b))) {
        const line = lineForSource(Number(key) / 1000) || lines[lines.length - 1];
        if (!line) continue;
        const row = el("span", "tx-caption");
        row.contentEditable = "false";
        row.dataset.key = key;
        row.append(el("span", "", edits[key] ? "Caption: " + edits[key] : "Caption hidden"));
        const reset = el("button", "tx-reset", "Reset"); reset.type = "button"; reset.tabIndex = -1;
        reset.setAttribute("aria-label", "Reset this caption to the words that were said");
        row.append(reset);
        line.node.append(row);
      }
    }
    function render() {
      const draft = liveDraft();
      const job = Editor.job();
      const key = (job && job.id || "") + "|" + (draft && draft.analysis ? listOf(draft).length + wordsOf(draft).length : 0);
      const edits = draft && draft.edit && draft.edit.captions && JSON.stringify(draft.edit.captions.edits) || "";
      if (key !== builtKey) { builtKey = key; shownEdits = edits; build(); }
      else { if (edits !== shownEdits) { shownEdits = edits; showEdits(); } paint(); }
      syncLock();
    }
    function paint() {
      if (!tokens.length) return;
      const draft = liveDraft();
      if (!draft) return;
      const kept = merged(current.spans.map((span) => [span.source_start, span.source_end]));
      const moments = merged(draft.segments.map((segment) => [Number(segment.start) || 0, Number(segment.end) || 0]));
      const removed = merged((draft.edit && draft.edit.removed || []).map((pair) => Array.isArray(pair) ? pair : [pair.start, pair.end]));
      let a = 0, b = 0, c = 0;
      for (const token of tokens) {
        const middle = (token.start + token.end) / 2;
        while (a < kept.length && kept[a][1] <= middle) a += 1;
        while (b < moments.length && moments[b][1] <= middle) b += 1;
        while (c < removed.length && removed[c][1] <= middle) c += 1;
        const inKept = a < kept.length && kept[a][0] <= middle;
        const inMoment = b < moments.length && moments[b][0] <= middle;
        const inRemoved = c < removed.length && removed[c][0] <= middle;
        const next = inKept ? 0 : inRemoved && inMoment ? 1 : inMoment ? 2 : 3;
        if (next !== token.state || !token.painted) { token.state = next; token.painted = true; token.node.className = CLASSES[next] + (token.hit ? " is-hit" : ""); }
      }
      for (const line of lines) line.node.classList.toggle("is-out", line.tokens.length > 0 && line.tokens.every((token) => token.state === 3));
      const seconds = removed.reduce((sum, pair) => sum + pair[1] - pair[0], 0);
      $("transcript-count").textContent = removed.length ? removed.length + (removed.length === 1 ? " cut" : " cuts") + ", " + seconds.toFixed(1) + " s" : lines.length + (lines.length === 1 ? " line" : " lines");
      updateActions();
    }
    function filter() {
      for (const line of lines) {
        const hit = !query || line.tokens.map((token) => token.text).join(" ").toLowerCase().includes(query);
        line.node.hidden = !hit;
        for (const token of line.tokens) {
          const on = !!query && hit && (token.text.toLowerCase().includes(query) || query.includes(token.text.toLowerCase().replace(/[^\p{L}\p{N}']/gu, "")) && token.text.length > 2);
          if (on !== !!token.hit) { token.hit = on; token.node.classList.toggle("is-hit", on); }
        }
      }
    }

    /* What the selection means. */
    function tokensOf(node) {
      const word = (node.nodeType === 1 ? node : node.parentElement);
      return word && word.closest ? word.closest(".tw") : null;
    }
    function caretToken(range) {
      const list = $("transcript-list");
      let node = range.startContainer;
      if (!list.contains(node)) return null;
      let word = tokensOf(node);
      if (word) return tokens[Number(word.dataset.w)];
      if (node.nodeType === 3) {
        word = node.previousSibling || node.nextSibling;
        if (word && word.dataset && word.dataset.w) return tokens[Number(word.dataset.w)];
      } else if (node.childNodes && node.childNodes[range.startOffset]) {
        const child = node.childNodes[Math.min(range.startOffset, node.childNodes.length - 1)];
        const found = child.nodeType === 1 ? (child.matches(".tw") ? child : child.querySelector(".tw")) : null;
        if (found) return tokens[Number(found.dataset.w)];
      }
      return null;
    }
    function picked() {
      const selection = window.getSelection();
      const list = $("transcript-list");
      if (!selection || !selection.rangeCount) return { tokens: [], collapsed: true };
      const range = selection.getRangeAt(0);
      if (!list.contains(range.commonAncestorContainer)) return { tokens: [], collapsed: true };
      if (range.collapsed) { const token = caretToken(range); return { tokens: token ? [token] : [], collapsed: true }; }
      const found = [];
      const probe = document.createRange();
      for (const line of lines) {
        if (!range.intersectsNode(line.node)) continue;
        for (const token of line.tokens) {
          probe.selectNodeContents(token.node);
          if (range.compareBoundaryPoints(Range.START_TO_END, probe) > 0 && range.compareBoundaryPoints(Range.END_TO_START, probe) < 0) found.push(token);
        }
      }
      return { tokens: found, collapsed: false };
    }
    function updateActions() {
      const now = picked();
      const locked_ = locked();
      const live = now.tokens.filter((token) => token.state === 0);
      const struck = now.tokens.filter((token) => token.state === 1);
      buttons.cut.disabled = locked_ || now.collapsed || !live.length;
      buttons.restore.disabled = locked_ || !struck.length;
      buttons.edit.disabled = locked_ || !now.tokens.some((token) => token.state === 0);
      buttons.add.disabled = locked_ || !now.tokens.length;
      const auto = now.tokens.some((token) => token.state === 2);
      buttons.restore.title = auto && !struck.length ? "Trim pauses or Remove fillers took this out. Turn that off in the inspector to bring it back." : "Bring the selected words back";
    }
    function sentenceRanges(chosen) {
      const draft = liveDraft();
      const analysis = draft.analysis || {};
      const sentences = Array.isArray(analysis.sentences) && analysis.sentences.length ? analysis.sentences : listOf(draft);
      const pairs = [];
      for (const token of chosen) {
        const middle = (token.start + token.end) / 2;
        const hit = sentences.find((item) => item.start <= middle && middle < item.end);
        pairs.push(hit ? [hit.start, hit.end] : [token.line.cue.start, token.line.cue.end]);
      }
      return merged(pairs);
    }
    function wordRanges(chosen) {
      const sorted = [...chosen].sort((a, b) => a.index - b.index);
      const pairs = [];
      let run = null;
      for (const token of sorted) {
        if (run && token.index === run.last.index + 1) run.last = token;
        else { if (run) pairs.push(run); run = { first: token, last: token }; }
      }
      if (run) pairs.push(run);
      return pairs.map(({ first, last }) => {
        const before = tokens[first.index - 1], after = tokens[last.index + 1];
        const lead = before ? Math.min(EDGE_PAD, Math.max(0, first.start - before.end) / 2) : EDGE_PAD;
        const trail = after ? Math.min(EDGE_PAD, Math.max(0, after.start - last.end) / 2) : EDGE_PAD;
        return [round3(Math.max(0, first.start - lead)), round3(last.end + trail)];
      });
    }
    function quote(chosen) {
      const text = chosen.slice(0, 6).map((token) => token.text).join(" ");
      return "“" + text + (chosen.length > 6 ? "…" : "") + "”";
    }
    function cut() {
      const now = picked();
      const chosen = now.tokens.filter((token) => token.state === 0);
      if (now.collapsed || !chosen.length) return false;
      const draft = Editor.draft();
      if (!draft) return false;
      const fresh = precise && chosen.every((token) => token.timed) ? wordRanges(chosen) : sentenceRanges(chosen);
      const combined = merged(draft.edit.removed.map((pair) => [pair[0], pair[1]]).concat(fresh));
      const covered = [];
      draft.segments.forEach((segment, index) => {
        const left = minus([[segment.start, segment.end]], combined).reduce((sum, pair) => sum + pair[1] - pair[0], 0);
        if (left < MIN_LEFT) covered.push(index);
      });
      if (covered.length >= draft.segments.length) { warnUser("Keep at least one moment in the story."); return true; }
      if (combined.length > MAX_REMOVED) { warnUser("This story holds up to " + MAX_REMOVED + " cuts. Restore some text first."); return true; }
      const wholes = covered.map((index) => [draft.segments[index].start, draft.segments[index].end]);
      const label = "Cut " + quote(chosen);
      const done = Workspace.structural(label, (plan) => {
        [...covered].reverse().forEach((index) => plan.segments.splice(index, 1));
        plan.edit.removed = minus(combined, wholes).map(([low, high]) => [round3(low), round3(high)]);
      });
      if (done) {
        Editor.select({ kind: "none" });
        Workspace.paintSelection();
        const selection = window.getSelection();
        if (selection) selection.collapseToEnd();
        studio.announce(label + ". The story runs " + spoken(current.duration) + ".");
      }
      return true;
    }
    function restore() {
      const now = picked();
      const chosen = now.tokens.filter((token) => token.state === 1);
      if (!chosen.length) return false;
      const draft = Editor.draft();
      const list = draft.edit.removed.map((pair) => [pair[0], pair[1]]);
      let targets;
      if (now.collapsed) {
        const middle = (chosen[0].start + chosen[0].end) / 2;
        targets = list.filter((pair) => pair[0] <= middle && middle < pair[1]);
      } else {
        const low = Math.min(...chosen.map((token) => token.start)), high = Math.max(...chosen.map((token) => token.end));
        targets = [[low - RESTORE_PAD, high + RESTORE_PAD]];
      }
      const label = "Restore " + quote(chosen);
      const done = Workspace.structural(label, (plan) => { plan.edit.removed = minus(list, targets).map(([low, high]) => [round3(low), round3(high)]); });
      if (done) studio.announce(label + ". The story runs " + spoken(current.duration) + ".");
      return true;
    }
    function warnUser(message) { studio.toast(message); studio.announce(message); }
    function addMoment() {
      const chosen = picked().tokens;
      if (!chosen.length) return;
      const low = Math.min(...chosen.map((token) => token.start)), high = Math.max(...chosen.map((token) => token.end));
      const text = chosen.map((token) => token.text).join(" ");
      studio.addSegment({ start: low, end: Math.max(low + 0.5, high), label: chosen.slice(0, 5).map((token) => token.text).join(" "), reason: "Spoken at " + clockLabel(low) + ": “" + text.slice(0, 200) + "”", kind: "speech", quiet: true });
    }

    /* Inline caption editing, shared by the transcript and the captions track. */
    function openEditor(key, rect, seed) {
      const box = $("cap-editor"), input = $("cap-input");
      editing = { key, original: seed };
      box.hidden = false;
      const width = Math.max(rect.width, 280);
      box.style.left = clamp(rect.left, 8, innerWidth - width - 8) + "px";
      box.style.width = width + "px";
      box.style.top = clamp(rect.top, 8, innerHeight - 52) + "px";
      input.value = seed;
      input.focus(); input.select();
    }
    function closeEditor(save) {
      const box = $("cap-editor"), input = $("cap-input");
      if (!editing) return;
      const { key, original } = editing;
      editing = null;
      box.hidden = true;
      const text = input.value.replace(/\s+/g, " ").trim();
      if (save && text !== original) {
        const done = Editor.change("Edit caption", (plan) => { plan.edit.captions.edits[key] = text; }, { coalesce: false });
        if (done) studio.announce(text ? "Caption changed." : "Caption hidden. Reset it in the transcript to bring it back.");
      }
      $("transcript-list").focus({ preventScroll: true });
    }
    function editCue(cue, rect) {
      if (locked() || !cue || cue.key === undefined) return;
      openEditor(String(cue.key), rect, String(cue.text).trim());
    }
    function resetCaption(key) {
      const done = Editor.change("Reset caption", (plan) => { delete plan.edit.captions.edits[key]; }, { coalesce: false });
      if (done) studio.announce("Caption reset to the words that were said.");
    }
    function lineForSource(time) {
      if (time === null || time === undefined) return null;
      let found = null;
      for (const line of lines) { if (line.cue.start - 1e-3 <= time) found = line; else break; }
      return found;
    }
    function cueForToken(token) {
      const output = Editor.toOutput(token.start + 1e-3);
      if (output === null) return null;
      const cues = current.captions || [];
      return cues.find((cue) => cue.start - 1e-3 <= output && output < cue.end) || cues.find((cue) => cue.start >= output) || null;
    }

    /* Clicking a word goes to that word. */
    function goToken(token) {
      if (token.state === 3) { studio.seekTo(token.start, false); return; }
      const kept = token.state === 0 ? Editor.toOutput(token.start + 1e-3) : null;
      const output = kept !== null ? kept : Workspace.outputNear(token.start);
      if (state().preview !== "story") studio.setPreview("story");
      Editor.seek(output);
    }
    function onClick(event) {
      const selection = window.getSelection();
      if (selection && !selection.isCollapsed) return;
      const stamp = event.target.closest(".tx-time");
      if (stamp) {
        const line = lines[Number(stamp.closest(".tx-line").dataset.line)];
        if (line && line.tokens[0]) goToken(line.tokens.find((token) => token.state === 0) || line.tokens[0]);
        return;
      }
      const reset = event.target.closest(".tx-reset");
      if (reset) { resetCaption(reset.closest(".tx-caption").dataset.key); return; }
      const word = event.target.closest(".tw");
      if (word) goToken(tokens[Number(word.dataset.w)]);
    }
    function now(seconds) {
      if (!tokens.length || $("transcript-panel").hidden) return;
      const hit = Editor.toSource(seconds);
      let token = null;
      if (hit) {
        let low = 0, high = tokens.length;
        while (low < high) { const middle = (low + high) >> 1; if (tokens[middle].end <= hit.time) low = middle + 1; else high = middle; }
        if (low < tokens.length && tokens[low].start <= hit.time) token = tokens[low];
      }
      if (token === nowToken) return;
      if (nowToken) nowToken.node.classList.remove("is-now");
      nowToken = token;
      if (token) {
        token.node.classList.add("is-now");
        if (token.line !== lastLine) {
          if (lastLine) lastLine.node.classList.remove("is-now");
          lastLine = token.line;
          lastLine.node.classList.add("is-now");
          if (Editor.playing() && document.activeElement !== $("transcript-list")) scrollLine(lastLine);
        }
      }
    }
    function scrollLine(line) {
      const list = $("transcript-list");
      const top = line.node.offsetTop - list.offsetTop;
      const bottom = top + line.node.offsetHeight;
      if (top < list.scrollTop + 8) list.scrollTop = Math.max(0, top - 12);
      else if (bottom > list.scrollTop + list.clientHeight - 8) list.scrollTop = bottom - list.clientHeight + 12;
    }
    function syncLock() {
      const list = $("transcript-list");
      const value = locked() ? "false" : "true";
      if (list.getAttribute("contenteditable") !== value) list.setAttribute("contenteditable", value);
    }
    function attach() {
      const list = $("transcript-list");
      list.setAttribute("role", "textbox");
      list.setAttribute("aria-multiline", "true");
      list.setAttribute("aria-label", "Transcript. Select words and press Delete to cut them from the story. The words stay here, struck through, so you can restore them.");
      list.setAttribute("inputmode", "none");
      list.setAttribute("spellcheck", "false");
      list.setAttribute("autocapitalize", "off");
      list.setAttribute("autocorrect", "off");
      list.tabIndex = 0;
      const bar = $("transcript-actions");
      for (const [key, label, title] of [["cut", "Cut", "Cut the selected words from the story (Delete)"], ["restore", "Restore", "Bring the selected words back"], ["edit", "Edit caption", "Change the caption for this line"], ["add", "Add as moment", "Add the selected words to the story as a new moment"]]) {
        const button = el("button", "tx-action", label); button.type = "button"; button.title = title; button.disabled = true;
        button.addEventListener("pointerdown", (event) => event.preventDefault());
        buttons[key] = button; bar.append(button);
      }
      buttons.cut.addEventListener("click", cut);
      buttons.restore.addEventListener("click", restore);
      buttons.edit.addEventListener("click", () => {
        const chosen = picked().tokens.filter((token) => token.state === 0);
        const cue = chosen.length ? cueForToken(chosen[0]) : null;
        if (cue) editCue(cue, chosen[0].line.node.getBoundingClientRect());
        else studio.toast("Only lines in the story have captions.");
      });
      buttons.add.addEventListener("click", addMoment);
      const editor = el("form", "cap-editor"); editor.id = "cap-editor"; editor.hidden = true;
      const input = el("input"); input.id = "cap-input"; input.type = "text"; input.maxLength = 300; input.autocomplete = "off"; input.setAttribute("aria-label", "Caption text. Enter saves, Escape cancels.");
      editor.append(input);
      document.body.append(editor);
      editor.addEventListener("submit", (event) => { event.preventDefault(); closeEditor(true); });
      input.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeEditor(false); } });
      input.addEventListener("blur", () => { if (editing) closeEditor(true); });
      list.addEventListener("beforeinput", (event) => { event.preventDefault(); });
      list.addEventListener("input", () => { builtKey = ""; render(); });
      for (const name of ["paste", "drop", "dragstart"]) list.addEventListener(name, (event) => event.preventDefault());
      list.addEventListener("cut", (event) => { event.preventDefault(); cut(); });
      list.addEventListener("keydown", (event) => {
        if (event.metaKey || event.ctrlKey || event.altKey) return;
        if (event.key === "Delete" || event.key === "Backspace") { event.preventDefault(); event.stopPropagation(); if (!cut()) restoreHint(); return; }
        if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); const chosen = picked().tokens; if (chosen.length) goToken(chosen[0]); }
      });
      list.addEventListener("click", onClick);
      document.addEventListener("selectionchange", () => {
        if (selectionTimer) return;
        selectionTimer = requestAnimationFrame(() => { selectionTimer = 0; if (Shell.current() === "edit" && !$("transcript-panel").hidden) updateActions(); });
      });
      $("transcript-search").addEventListener("input", () => { query = $("transcript-search").value.trim().toLowerCase(); filter(); });
    }
    function restoreHint() {
      const now = picked();
      if (now.tokens.some((token) => token.state === 1)) studio.announce("Those words are already cut. Use Restore to bring them back.");
      else if (now.collapsed) studio.announce("Select words first, then press Delete to cut them.");
    }
    return { attach, render, paint() { paint(); }, job() { syncLock(); updateActions(); }, now, editCue };
  })();

}());
