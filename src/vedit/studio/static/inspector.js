"use strict";

/*
 * The inspector: eight tabs over the plan's edit, the note box, and the
 * delivery extras. Tabs read capabilities().editor and write through
 * BriefcutEditor.change, so a choice added to the catalogue appears here and
 * every edit can be undone.
 */
(function () {
  const Kit = window.BriefcutKit;
  if (!Kit) return;
  const { $, el, icon, finite, clamp, getPath, setPath, clock, bytes, plural, money } = Kit;
  const SPEEDS = [.25, .5, .75, 1, 1.25, 1.5, 2, 3, 4];
  const state = { tab: "look", built: false, moment: 0, textIndex: -1, overlayIndex: -1, selection: "", chat: [], chatJob: "", chatRevision: 0, chatOpen: false, cutoutMode: "", consent: false, refine: 0, options: {}, background: { busy: false, error: "" }, shorts: { busy: false, error: "", jobs: [], family: [], job: "", timer: 0, why: {} } };
  const panels = [];
  let idCounter = 0;
  const nextId = (name) => "ix-" + name + "-" + (idCounter += 1);

  /* ---------- Shared reads ---------- */
  function storyLength() { const span = Kit.timeline(); return span ? finite(span.duration, 0) : 0; }
  function captionCues() {
    const span = Kit.timeline();
    const cues = span && Array.isArray(span.captions) ? span.captions : [];
    return cues.filter((cue) => cue && typeof cue.text === "string" && Number.isFinite(Number(cue.start)) && Number.isFinite(Number(cue.end)));
  }
  function cueKey(cue) {
    if (cue.key !== undefined && cue.key !== null) return String(cue.key);
    return String(Math.round(Number(cue.source_start !== undefined ? cue.source_start : cue.start) * 1000));
  }
  function featureGate(name, reason) {
    const features = (Kit.catalogue() || {}).features || {};
    return features[name] === false ? reason : "";
  }
  function needsFfmpeg(name, what) { return featureGate(name, what + " on this computer needs ffmpeg-full."); }
  function metaFeature(name) { return !!(((Kit.caps().meta || {}).features || {})[name]); }
  /* Cutouts and backgrounds use Meta. Projects saved before Briefcut was Meta-only cannot. */
  function metaProjectBlock(feature, what) {
    if (!Kit.metaReady()) return Kit.metaReason();
    if (!metaFeature(feature)) return what + " are not ready on this computer yet.";
    const project = Kit.job();
    if (project && project.request && project.request.provider !== "meta") return "This project was saved before Briefcut ran on Meta. Create a new story to use them.";
    return "";
  }
  Kit.sampleLine = function sampleLine() {
    const cues = captionCues();
    if (cues.length) return cues[0].text;
    const project = Kit.job();
    const transcript = project && project.plan && project.plan.analysis && project.plan.analysis.transcript;
    if (Array.isArray(transcript) && transcript[0] && transcript[0].text) return transcript[0].text;
    const plan = Kit.draft();
    return plan && plan.title || "";
  };
  Kit.captionWords = function captionWords(line) {
    return String(line || "").trim().split(/\s+/).slice(0, 6).map((word, index) => el("span", { class: index === 1 ? "is-on" : "", text: word + " " }));
  };

  /* ---------- Controls ---------- */
  function group(title, ...children) { return el("section", { class: "ix-group" }, title ? el("h3", { text: title }) : null, ...children); }
  function note(text, tone) { return el("p", { class: "ix-note" + (tone ? " is-" + tone : ""), text }); }
  function rebuild(container, make) {
    const active = document.activeElement;
    const focusId = active && container.contains(active) ? active.id : "";
    const caret = focusId && typeof active.selectionStart === "number" ? active.selectionStart : null;
    container.replaceChildren(...make().filter(Boolean));
    if (!focusId) return;
    const again = $(focusId);
    if (again) { again.focus(); if (caret !== null) { try { again.setSelectionRange(caret, caret); } catch (_) { /* Not a text field. */ } } }
  }
  /* Writes once per frame while a control moves. change() merges them into one undo step. */
  function throttled(write) {
    let latest, queued = false;
    return (value) => {
      latest = value;
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; write(latest); });
    };
  }

  function rangeField(options) {
    const id = options.id || nextId("range");
    const { label, min, max, step } = options;
    const format = options.format || ((value) => Math.round(value * 100) + "%");
    const write = throttled(options.write);
    const input = el("input", { id, class: "ix-range", type: "range", min: String(min), max: String(max), step: String(step) });
    const output = el("output", { for: id, class: "ix-value" });
    const origin = min < 0 && max > 0 ? (0 - min) / (max - min) * 100 : 0;
    let touched = -Infinity;
    const paint = (value) => {
      const share = (value - min) / (max - min) * 100;
      input.style.setProperty("--from", Math.min(share, origin) + "%");
      input.style.setProperty("--to", Math.max(share, origin) + "%");
      const text = format(value);
      output.textContent = text;
      input.setAttribute("aria-valuetext", text);
    };
    input.addEventListener("input", () => {
      const value = Number(input.value);
      touched = performance.now();
      paint(value);
      write(value);
    });
    return {
      node: el("div", { class: "ix-field" }, el("div", { class: "ix-field-head" }, el("label", { for: id, text: label }), output), input),
      set(value) {
        if (input.matches(":active") || performance.now() - touched < 200) return;
        input.value = String(value);
        paint(Number(input.value));
      },
    };
  }
  function slider(panel, options) {
    const { label, path } = options;
    const read = options.read || ((view) => finite(getPath(view, path), 0));
    const write = options.write || ((value) => Kit.edit(options.undo || label, (plan) => setPath(plan, path, value)));
    const field = rangeField(Object.assign({}, options, { write }));
    panel.syncs.push((view) => field.set(read(view)));
    return field.node;
  }

  function toggle(panel, options) {
    const { label, hint, path } = options;
    const read = options.read || ((view) => !!getPath(view, path));
    const write = options.write || ((value) => Kit.edit(label + (value ? " on" : " off"), (plan) => setPath(plan, path, value), { coalesce: false }));
    const input = el("input", { type: "checkbox", role: "switch", class: "ix-switch-input", on: { change: () => write(input.checked) } });
    const reasonNode = el("small", { class: "ix-switch-reason", hidden: true });
    panel.syncs.push((view) => {
      input.checked = read(view);
      const reason = options.gate ? options.gate(view) : "";
      input.disabled = !!reason;
      reasonNode.hidden = !reason;
      reasonNode.textContent = reason;
    });
    return el("label", { class: "ix-switch" }, input, el("span", { class: "ix-switch-track", "aria-hidden": "true" }),
      el("span", { class: "ix-switch-text" }, el("span", { text: label }), hint ? el("small", { text: hint }) : null, reasonNode));
  }

  function segmented(panel, options) {
    const name = nextId("choice");
    const { legend, choices, path } = options;
    const read = options.read || ((view) => getPath(view, path));
    const write = options.write || ((value) => Kit.edit(options.undo ? options.undo(value) : legend, (plan) => setPath(plan, path, value), { coalesce: false }));
    const box = el("fieldset", { class: "ix-segmented" }, el("legend", { text: legend }), el("div", { class: "ix-segmented-row" }, choices.map((choice) => {
      const input = el("input", { type: "radio", class: "sr", name, value: String(choice.value), on: { change: () => { if (input.checked) write(choice.value); } } });
      return el("label", {}, input, el("span", { text: choice.label }));
    })));
    panel.syncs.push((view) => {
      const value = String(read(view));
      box.querySelectorAll("input").forEach((input) => { input.checked = input.value === value; });
    });
    return box;
  }

  function colorField(panel, options) {
    const id = nextId("color");
    const { label, path } = options;
    const read = options.read || ((view) => String(getPath(view, path) || "#FFFFFF"));
    const write = throttled(options.write || ((value) => Kit.edit(label, (plan) => setPath(plan, path, value))));
    const output = el("output", { for: id, class: "ix-value" });
    const input = el("input", { id, type: "color", class: "ix-color", on: {
      input: () => { output.textContent = input.value.toUpperCase(); write(input.value.toUpperCase()); },
    } });
    panel.syncs.push((view) => { const value = read(view); input.value = value.toLowerCase(); output.textContent = value.toUpperCase(); });
    return el("div", { class: "ix-color-field" }, el("label", { for: id, text: label }), input, output);
  }

  function radioCards(panel, options) {
    const name = nextId("card");
    const box = el("fieldset", { class: options.class }, el("legend", { class: "sr", text: options.legend }));
    options.choices.forEach((choice) => {
      const input = el("input", { type: "radio", class: "sr", name, value: choice.id, on: { change: () => { if (input.checked) options.write(choice); } } });
      box.append(el("label", { class: options.cardClass, title: choice.description }, input, options.face(choice), el("span", { class: "ix-card-name", text: choice.name })));
    });
    panel.syncs.push((view) => {
      const value = options.read(view);
      box.querySelectorAll("input").forEach((input) => { input.checked = input.value === value; if (options.gate) input.disabled = !!options.gate(input.value, view); });
    });
    return box;
  }
  function chosenDescription(panel, rows, read) {
    const node = note("");
    panel.syncs.push((view) => { const row = rows.find((item) => item.id === read(view)); node.textContent = row ? row.description : ""; });
    return node;
  }
  const signedPercent = (value) => (value > 0 ? "+" : value < 0 ? "−" : "") + Math.abs(Math.round(value * 100));
  const secondsText = (value) => value.toFixed(value < 1 ? 2 : 1) + " s";

  /* ---------- Look ---------- */
  function buildLook(panel) {
    const unavailable = needsFfmpeg("look", "Color grading");
    if (unavailable) { panel.body.append(note(unavailable)); return; }
    const list = Kit.catalogue();
    const defaults = list.defaults.look;
    const lookChoices = list.looks;
    const face = (look) => {
      const style = Kit.lookStyle(Object.assign({}, defaults, { preset: look.id, intensity: 1 }));
      return el("span", { class: "ix-frame" }, el("img", { alt: "", data: { frame: "a" }, hidden: true, style: { filter: style.filter } }),
        style.tint ? el("i", { class: "ix-tint", style: { background: style.tint.color, opacity: String(style.tint.opacity) } }) : null);
    };
    const swatches = radioCards(panel, { class: "ix-swatches", cardClass: "ix-swatch", legend: "Choose a look", choices: lookChoices, face, read: (view) => view.look.preset, write: (look) => Kit.edit(look.name + " look", (plan) => setPath(plan, "look.preset", look.id), { coalesce: false }) });
    const auto = lookChoices.find((look) => look.id === "auto");
    const autoButton = auto ? el("button", { type: "button", class: "button button-secondary ix-wide", title: auto.description, on: { click: () => Kit.edit(auto.name, (plan) => setPath(plan, "look.preset", auto.id), { coalesce: false }) } }, auto.name) : null;
    const strength = slider(panel, { label: "Strength", path: "look.intensity", min: 0, max: 1, step: .05, undo: "Look strength" });
    panel.syncs.push((view) => { strength.hidden = view.look.preset === defaults.preset; });
    const adjustments = [["brightness", "Brightness"], ["contrast", "Contrast"], ["saturation", "Saturation"], ["temperature", "Temperature"], ["vignette", "Vignette"], ["sharpen", "Sharpen"]].filter(([key]) => key in defaults);
    const reset = el("button", { type: "button", class: "ix-link", text: "Reset look", on: { click: () => Kit.edit("Reset look", (plan) => { plan.look = Kit.clone(defaults); }, { coalesce: false }) } });
    panel.body.append(
      group("", swatches, chosenDescription(panel, lookChoices, (view) => view.look.preset), autoButton, strength),
      group("Adjust", ...adjustments.map(([key, label]) => {
        const low = key === "vignette" || key === "sharpen" ? 0 : -1;
        return slider(panel, { label, path: "look." + key, min: low, max: 1, step: .05, format: low ? signedPercent : undefined });
      }), reset),
      note("Swatches and the preview approximate the look. The render applies the full grade, including sharpen."));
  }

  /* ---------- Transitions ---------- */
  function buildTransitions(panel) {
    const list = Kit.catalogue();
    const defaultType = list.defaults.transition.type;
    const face = (choice) => el("span", { class: "ix-trans", data: { transition: choice.id } },
      el("img", { class: "ix-trans-a", alt: "", data: { frame: "a" }, hidden: true }), el("img", { class: "ix-trans-b", alt: "", data: { frame: "b" }, hidden: true }));
    const offered = list.transitions.filter((row) => row.available !== false);
    const cards = radioCards(panel, { class: "ix-transitions", cardClass: "ix-swatch", legend: "Choose a transition", choices: offered, face, read: (view) => view.transition.type, write: (choice) => Kit.edit(choice.name + " transition", (plan) => setPath(plan, "transition.type", choice.id), { coalesce: false }) });
    const length = slider(panel, { label: "Length", path: "transition.seconds", min: .1, max: 2, step: .05, format: secondsText, undo: "Transition length" });
    const overlap = note("");
    panel.syncs.push((view) => {
      const off = view.transition.type === defaultType;
      length.hidden = off;
      const span = Kit.timeline();
      const used = span ? finite(span.transition_seconds, 0) : 0;
      overlap.hidden = off || !used;
      overlap.textContent = used && used < view.transition.seconds - .01 ? "Shortened to " + secondsText(used) + " so each moment still shows." : "Moments overlap for " + secondsText(used || view.transition.seconds) + ", so the story runs a little shorter.";
    });
    panel.body.append(
      group("", cards, chosenDescription(panel, list.transitions, (view) => view.transition.type), length, overlap,
        offered.length < list.transitions.length ? note("Only hard cuts here. This FFmpeg has no transition filters. Install ffmpeg-full to get the rest.") : null),
      "fade" in list.defaults ? group("Start and end",
        slider(panel, { label: "Fade in", path: "fade.in", min: 0, max: 5, step: .1, format: secondsText }),
        slider(panel, { label: "Fade out", path: "fade.out", min: 0, max: 5, step: .1, format: secondsText })) : null);
  }

  /* ---------- Captions ---------- */
  /* Captions and the frame are set on the project, but both apply at render
     time. Holding the choice in the draft gives it undo; render sends it. */
  function renderChoice(name, fallback) {
    const plan = Kit.draft();
    const chosen = plan && plan.render_options && plan.render_options[name];
    if (chosen !== undefined) return chosen;
    const project = Kit.job();
    return project && project.request ? project.request[name] : fallback();
  }
  function setRenderChoice(name, value, label) {
    return Kit.edit(label, (_, plan) => { plan.render_options = Object.assign({}, plan.render_options, { [name]: value }); }, { coalesce: false });
  }
  function captionsOn() { const box = $("captions"); return !!renderChoice("captions", () => !!box && box.checked); }
  /* The live preview reads the project, so the project follows the choice,
     including when undo takes it back. */
  function syncRenderChoices() {
    const project = Kit.job();
    const plan = Kit.draft();
    if (!project || !project.request || !plan) return;
    const stamp = project.id + ":" + project.revision;
    if (state.requestStamp !== stamp) { state.requestStamp = stamp; state.request = { captions: project.request.captions, aspect: project.request.aspect }; }
    let moved = false;
    Object.keys(state.request).forEach((name) => {
      const chosen = plan.render_options && plan.render_options[name];
      const value = chosen !== undefined ? chosen : state.request[name];
      if (project.request[name] !== value) { project.request[name] = value; moved = true; }
    });
    if (moved) Kit.bridge()._job(project, {});
  }
  function captionsGate() {
    const blocked = featureGate("captions", "Captions need ffmpeg-full on this computer.");
    if (blocked) return blocked;
    // Meta Voice Transcribe writes every caption.
    if (!Kit.metaReady()) return Kit.metaReason();
    return metaFeature("transcribe") ? "" : "Meta Voice Transcribe is not available right now.";
  }
  function setCaptions(on) { setRenderChoice("captions", on, on ? "Captions on" : "Captions off"); }
  function translationCost(cues) {
    const meta = Kit.caps().meta || {};
    const prices = meta.prices || {};
    const characters = cues.reduce((total, cue) => total + cue.text.length, 0);
    const input = (characters / 4 + 500) / 1e6 * finite(prices.input_per_million, 0);
    const output = (characters / 3) / 1e6 * finite(prices.output_per_million, 0);
    return input + output;
  }
  function languageChoices() {
    const list = Kit.catalogue() || {};
    if (Array.isArray(list.caption_languages) && list.caption_languages.length) return list.caption_languages.map((row) => (typeof row === "string" ? { id: row, name: row } : row));
    const select = $("language");
    return select ? Array.from(select.options).filter((option) => option.value !== "auto").map((option) => ({ id: option.value, name: option.textContent })) : [];
  }
  function buildCaptions(panel) {
    const list = Kit.catalogue();
    const on = toggle(panel, { label: "Add captions", read: captionsOn, write: setCaptions, gate: captionsGate });
    const samples = [];
    const face = (choice) => {
      const words = el("span", { class: "ix-cap", data: { captionStyle: choice.id } });
      samples.push(words);
      return el("span", { class: "ix-frame ix-capframe" }, el("img", { alt: "", data: { frame: "a" }, hidden: true }), words);
    };
    const cards = radioCards(panel, { class: "ix-capstyles", cardClass: "ix-capstyle", legend: "Choose a caption style", choices: list.caption_styles, face, read: (view) => view.captions.style, write: (choice) => Kit.edit(choice.name + " captions", (plan) => setPath(plan, "captions.style", choice.id), { coalesce: false }) });
    let sampleKey = "";
    panel.syncs.push((view) => {
      const line = Kit.sampleLine() || "Your words appear here";
      const key = line + view.captions.color + view.captions.highlight;
      if (key === sampleKey) return;
      sampleKey = key;
      samples.forEach((words) => {
        words.replaceChildren(...Kit.captionWords(line));
        words.style.setProperty("--cap-color", view.captions.color);
        words.style.setProperty("--cap-high", view.captions.highlight);
      });
    });
    const language = el("select", { id: "ix-caption-language", on: { change: () => { const value = language.value || null; Kit.edit("Caption language", (plan) => setPath(plan, "captions.language", value), { coalesce: false }); } } },
      el("option", { value: "", text: "Original language" }), languageChoices().map((row) => el("option", { value: row.id, text: row.name })));
    const languageNote = note("");
    panel.syncs.push((view) => {
      language.value = view.captions.language || "";
      const ready = Kit.metaReady() && metaFeature("translate");
      language.disabled = !ready;
      if (!ready) { languageNote.textContent = Kit.metaReady() ? "Translation is not available right now." : Kit.metaReason(); return; }
      const cues = captionCues();
      languageNote.textContent = view.captions.language
        ? "Translated by Meta, " + money(translationCost(cues)) + " for this story."
        : "Translation uses Meta, " + money(translationCost(cues)) + " for this story.";
    });
    const cueList = el("ol", { class: "ix-cues" });
    const cueEmpty = note("");
    let cueSignature = "";
    panel.syncs.push((view) => {
      const cues = captionCues();
      const edits = view.captions.edits || {};
      const signature = JSON.stringify([cues.map((cue) => [cue.start, cue.text]), edits]);
      cueEmpty.hidden = !!cues.length;
      cueEmpty.textContent = captionsOn() ? "Caption lines appear here once they are transcribed." : "Turn captions on to see each line here.";
      if (signature === cueSignature) return;
      cueSignature = signature;
      rebuild(cueList, () => cues.slice(0, 400).map((cue, index) => {
        const key = cueKey(cue);
        const edited = Object.prototype.hasOwnProperty.call(edits, key);
        const id = "ix-cue-" + key;
        const original = cue.original || cue.text;
        const input = el("input", { id, type: "text", maxlength: "300", value: edited ? edits[key] : cue.text, "aria-label": "Caption line " + (index + 1), on: { input: () => {
          const text = input.value.replace(/\s+/g, " ").trim();
          if (!text) return;
          Kit.edit("Edit caption", (plan) => {
            const next = Object.assign({}, plan.captions.edits);
            if (text === original) delete next[key]; else next[key] = text;
            plan.captions.edits = next;
          });
        } } });
        return el("li", { class: edited ? "is-edited" : "" },
          el("button", { type: "button", class: "ix-time", text: clock(cue.start), "aria-label": "Jump to " + clock(cue.start), on: { click: () => seek(Number(cue.start)) } }),
          input,
          edited ? el("button", { type: "button", class: "ix-icon", title: "Use the original line", "aria-label": "Use the original line " + (index + 1), on: { click: () => Kit.edit("Restore caption", (plan) => { const next = Object.assign({}, plan.captions.edits); delete next[key]; plan.captions.edits = next; }, { coalesce: false }) } }, icon("retry")) : null);
      }));
    });
    panel.body.append(
      group("", on),
      group("Style", cards, chosenDescription(panel, list.caption_styles, (view) => view.captions.style)),
      group("Placement",
        segmented(panel, { legend: "Position", choices: list.caption_positions.map((value) => ({ value, label: value[0].toUpperCase() + value.slice(1) })), path: "captions.position", undo: () => "Caption position" }),
        slider(panel, { label: "Size", path: "captions.scale", min: .5, max: 2, step: .05, undo: "Caption size" }),
        el("div", { class: "ix-color-row" }, colorField(panel, { label: "Text", path: "captions.color" }), colorField(panel, { label: "Highlight", path: "captions.highlight" }))),
      group("Language", el("label", { for: "ix-caption-language", class: "sr", text: "Caption language" }), language, languageNote),
      group("Lines", cueEmpty, cueList));
  }

  /* ---------- Text, logo, images ---------- */
  const titleCase = (value) => value.replace("-", " ").replace(/^./, (letter) => letter.toUpperCase());
  function textDefaults() {
    const list = Kit.catalogue();
    return { style: list.text_styles[0].id, position: list.text_positions.includes("center") ? "center" : list.text_positions[0], scale: 1, color: "#FFFFFF" };
  }
  function selectItem(kind, index) {
    const editor = Kit.bridge();
    if (editor.select) editor.select(index >= 0 ? { kind, index } : { kind: "none" });
  }
  function updateText(index, patch, label, options) {
    Kit.edit(label, (plan) => {
      const items = plan.texts.slice();
      if (!items[index]) return;
      items[index] = Object.assign({}, items[index], patch);
      plan.texts = items;
    }, options);
  }
  function addText() {
    const limit = finite(Kit.limits().texts, 24);
    const count = Kit.view().texts.length;
    if (count >= limit) { Kit.toast("A story holds up to " + limit + " text items."); return; }
    const length = storyLength();
    const start = Math.round(clamp(Kit.playhead(), 0, Math.max(0, length - .1)) * 10) / 10;
    const added = Kit.edit("Add text", (plan) => { plan.texts = plan.texts.concat([Object.assign({ text: "New text", start, duration: clamp(length - start, .1, 3) }, textDefaults())]); }, { coalesce: false });
    if (!added) return;
    state.textIndex = count;
    selectItem("text", count);
    paintPanels();
    const field = $("ix-text-" + count + "-body");
    if (field) { field.focus(); field.select(); }
  }
  function deleteText(index) {
    Kit.edit("Delete text", (plan) => { plan.texts = plan.texts.filter((_, other) => other !== index); }, { coalesce: false });
    state.textIndex = -1;
    selectItem("text", -1);
    paintPanels();
  }
  function textEditor(index) {
    const list = Kit.catalogue();
    const prefix = "ix-text-" + index + "-";
    const body = el("input", { id: prefix + "body", type: "text", maxlength: "160", on: { input: () => { const text = body.value.replace(/\s+/g, " ").trim(); if (text) updateText(index, { text }, "Edit text"); } } });
    const style = el("select", { id: prefix + "style", on: { change: () => updateText(index, { style: style.value }, "Text style", { coalesce: false }) } }, list.text_styles.map((row) => el("option", { value: row.id, text: row.name })));
    const position = el("select", { id: prefix + "position", on: { change: () => updateText(index, { position: position.value }, "Text position", { coalesce: false }) } }, list.text_positions.map((value) => el("option", { value, text: titleCase(value) })));
    const size = rangeField({ id: prefix + "size", label: "Size", min: .5, max: 2.5, step: .05, write: (value) => updateText(index, { scale: value }, "Text size") });
    const color = el("input", { id: prefix + "color", type: "color", class: "ix-color", on: { input: () => updateText(index, { color: color.value.toUpperCase() }, "Text color") } });
    const clampTime = (value, low) => clamp(value, low, Math.max(low, storyLength() || value));
    const start = el("input", { id: prefix + "start", type: "number", min: "0", step: ".1", on: { change: () => { const value = Number(start.value); if (Number.isFinite(value)) updateText(index, { start: clampTime(value, 0) }, "Text timing", { coalesce: false }); } } });
    const duration = el("input", { id: prefix + "duration", type: "number", min: ".1", step: ".1", on: { change: () => { const value = Number(duration.value); if (Number.isFinite(value)) updateText(index, { duration: clampTime(value, .1) }, "Text timing", { coalesce: false }); } } });
    const node = el("div", { class: "ix-item-editor" },
      el("label", { for: body.id, text: "Words" }), body,
      el("div", { class: "ix-pair" },
        el("div", {}, el("label", { for: style.id, text: "Style" }), style),
        el("div", {}, el("label", { for: position.id, text: "Position" }), position)),
      size.node,
      el("div", { class: "ix-color-field" }, el("label", { for: color.id, text: "Color" }), color),
      el("div", { class: "ix-pair" },
        el("div", { class: "ix-number" }, el("label", { for: start.id, text: "Starts at, seconds" }), start),
        el("div", { class: "ix-number" }, el("label", { for: duration.id, text: "Lasts, seconds" }), duration)),
      el("div", { class: "ix-item-actions" },
        el("button", { type: "button", class: "ix-link", text: "Move to playhead", on: { click: () => updateText(index, { start: Math.round(clamp(Kit.playhead(), 0, Math.max(0, storyLength() - .1)) * 10) / 10 }, "Text timing", { coalesce: false }) } }),
        el("button", { type: "button", class: "ix-link is-danger", text: "Delete text", on: { click: () => deleteText(index) } })));
    return {
      node,
      sync(item) {
        const settle = (field, value) => { if (document.activeElement !== field) field.value = String(value); };
        settle(body, item.text); settle(style, item.style); settle(position, item.position);
        settle(color, String(item.color || "#FFFFFF").toLowerCase());
        settle(start, Math.round(Number(item.start) * 100) / 100); settle(duration, Math.round(Number(item.duration) * 100) / 100);
        size.set(Number(item.scale));
      },
    };
  }
  function imageChoices() { return Object.values(Kit.assets()).filter((asset) => asset && asset.kind === "image"); }
  function logoEditor(panel) {
    const list = Kit.catalogue();
    const update = (patch, label, options) => Kit.edit(label, (plan) => { plan.logo = Object.assign({ position: "top-right", scale: .14, opacity: .9 }, plan.logo, patch); }, options);
    const select = el("select", { id: "ix-logo-asset", on: { change: () => {
      if (select.value) update({ asset: select.value }, "Change logo", { coalesce: false });
      else Kit.edit("Remove logo", (plan) => { plan.logo = null; }, { coalesce: false });
    } } });
    const position = el("select", { id: "ix-logo-position", on: { change: () => update({ position: position.value }, "Logo position", { coalesce: false }) } }, list.overlay_positions.map((value) => el("option", { value, text: titleCase(value) })));
    const size = rangeField({ id: "ix-logo-size", label: "Size", min: .03, max: .6, step: .01, write: (value) => update({ scale: value }, "Logo size") });
    const opacity = rangeField({ id: "ix-logo-opacity", label: "Opacity", min: .1, max: 1, step: .05, write: (value) => update({ opacity: value }, "Logo opacity") });
    const controls = el("div", { class: "ix-stack" }, el("div", {}, el("label", { for: position.id, text: "Position" }), position), size.node, opacity.node);
    const empty = note("Add a logo in the media bin.");
    let key = null;
    panel.syncs.push((view) => {
      const images = imageChoices();
      const next = images.map((asset) => asset.id).join();
      if (next !== key) { key = next; select.replaceChildren(el("option", { value: "", text: "No logo" }), ...images.map((asset) => el("option", { value: asset.id, text: asset.name }))); }
      const logo = view.logo;
      select.value = logo && logo.asset || "";
      controls.hidden = !logo;
      empty.hidden = !!images.length;
      if (!logo) return;
      if (document.activeElement !== position) position.value = logo.position;
      size.set(Number(logo.scale)); opacity.set(Number(logo.opacity));
    });
    return el("div", { class: "ix-logo" }, el("label", { for: select.id, text: "Image" }), select, empty, controls);
  }
  function overlayEditor(index) {
    const list = Kit.catalogue();
    const prefix = "ix-overlay-" + index + "-";
    const update = (patch, label, options) => Kit.edit(label, (plan) => { const next = plan.overlays.slice(); next[index] = Object.assign({}, next[index], patch); plan.overlays = next; }, options);
    const position = el("select", { id: prefix + "position", on: { change: () => update({ position: position.value }, "Image position", { coalesce: false }) } }, list.overlay_positions.map((value) => el("option", { value, text: titleCase(value) })));
    const size = rangeField({ id: prefix + "size", label: "Size", min: .03, max: 1, step: .01, write: (value) => update({ scale: value }, "Image size") });
    const clampTime = (value, low) => clamp(value, low, Math.max(low, storyLength() || value));
    const start = el("input", { id: prefix + "start", type: "number", min: "0", step: ".1", on: { change: () => { const value = Number(start.value); if (Number.isFinite(value)) update({ start: clampTime(value, 0) }, "Image timing", { coalesce: false }); } } });
    const duration = el("input", { id: prefix + "duration", type: "number", min: ".1", step: ".1", on: { change: () => { const value = Number(duration.value); if (Number.isFinite(value)) update({ duration: clampTime(value, .1) }, "Image timing", { coalesce: false }); } } });
    const node = el("div", { class: "ix-item-editor" },
      el("label", { for: position.id, text: "Position" }), position, size.node,
      el("div", { class: "ix-pair" },
        el("div", { class: "ix-number" }, el("label", { for: start.id, text: "Starts at, seconds" }), start),
        el("div", { class: "ix-number" }, el("label", { for: duration.id, text: "Lasts, seconds" }), duration)),
      el("div", { class: "ix-item-actions" }, el("button", { type: "button", class: "ix-link is-danger", text: "Remove image", on: { click: () => { Kit.edit("Remove image", (plan) => { plan.overlays = plan.overlays.filter((_, other) => other !== index); }, { coalesce: false }); state.overlayIndex = -1; selectItem("overlay", -1); paintPanels(); } } })));
    return {
      node,
      sync(item) {
        const settle = (field, value) => { if (document.activeElement !== field) field.value = String(value); };
        settle(position, item.position); settle(start, Math.round(Number(item.start) * 100) / 100); settle(duration, Math.round(Number(item.duration) * 100) / 100);
        size.set(Number(item.scale));
      },
    };
  }
  /* A list of timed items. Only a change of shape rebuilds it, so a drag or a
     picker inside the open item is never replaced while it is in use. */
  function itemList(panel, options) {
    const list = el("ul", { class: "ix-items" });
    const heads = [];
    let shape = "", editor = null;
    panel.syncs.push((view) => {
      const items = options.items(view);
      const open = options.open();
      const next = items.length + ":" + open;
      if (next !== shape) {
        shape = next;
        heads.length = 0;
        editor = null;
        rebuild(list, () => items.map((item, index) => {
          const title = el("span");
          const when = el("small");
          heads[index] = { title, when };
          const row = el("li", { class: "ix-item" + (open === index ? " is-open" : "") },
            el("button", { type: "button", class: "ix-item-head", id: options.id + "-head-" + index, "aria-expanded": String(open === index), on: { click: () => options.toggle(index, open === index, Number(items[index].start)) } }, title, when));
          if (open === index) { editor = options.editor(index); row.append(editor.node); }
          return row;
        }));
      }
      items.forEach((item, index) => {
        heads[index].title.textContent = options.title(item);
        heads[index].when.textContent = clock(item.start, true) + " for " + secondsText(Number(item.duration));
      });
      if (editor && items[open]) editor.sync(items[open]);
    });
    return list;
  }
  function buildText(panel) {
    const texts = itemList(panel, {
      id: "ix-text", items: (view) => view.texts || [], open: () => state.textIndex, editor: textEditor, title: (item) => item.text,
      toggle: (index, open, start) => { state.textIndex = open ? -1 : index; selectItem("text", state.textIndex); paintPanels(); if (!open) seek(start); },
    });
    const images = itemList(panel, {
      id: "ix-overlay", items: (view) => view.overlays || [], open: () => state.overlayIndex, editor: overlayEditor,
      title: (item) => { const asset = Kit.assets()[item.asset]; return asset ? asset.name : "Image"; },
      toggle: (index, open, start) => { state.overlayIndex = open ? -1 : index; selectItem("overlay", state.overlayIndex); paintPanels(); if (!open) seek(start); },
    });
    const noText = featureGate("text", "Text needs ffmpeg-full on this computer.");
    const noOverlays = featureGate("overlays", "Logos and images need ffmpeg-full on this computer.");
    const empty = note("Add a title, a name, or a label at the playhead. It appears on the timeline.");
    panel.syncs.push((view) => { empty.hidden = !!(view.texts || []).length; });
    panel.body.append(
      group("", noText ? note(noText) : el("button", { type: "button", class: "button button-primary ix-wide", on: { click: addText } }, icon("plus"), "Add text at playhead"), noText ? null : empty, texts),
      noOverlays ? group("Logo and images", note(noOverlays)) : group("Logo", logoEditor(panel)),
      noOverlays ? null : group("Images", images, note("Add images in the media bin. They start at the playhead.")));
  }

  /* ---------- Audio ---------- */
  function musicChoices() {
    const list = Kit.catalogue() || {};
    const beds = Array.isArray(list.music_beds) ? list.music_beds : [];
    const uploaded = Object.values(Kit.assets()).filter((asset) => asset && asset.kind === "audio");
    return { beds, uploaded };
  }
  function buildAudio(panel) {
    const select = el("select", { id: "ix-music", on: { change: () => {
      const value = select.value;
      if (!value) { Kit.edit("Remove music", (plan) => { plan.music = null; }, { coalesce: false }); return; }
      Kit.edit("Change music", (plan) => { plan.music = Object.assign({ volume_db: -18, duck: true, fade_out: 1.5, start: 0, beat_sync: false }, plan.music, { asset: value }); }, { coalesce: false });
    } } });
    let musicKey = "";
    const musicControls = el("div", { class: "ix-stack" },
      slider(panel, { label: "Volume", path: "music.volume_db", min: -40, max: 0, step: 1, format: (value) => (value === 0 ? "0" : "−" + Math.abs(value)) + " dB", read: (view) => view.music ? finite(view.music.volume_db, -18) : -18, undo: "Music volume" }),
      toggle(panel, { label: "Lower music under speech", read: (view) => !!(view.music && view.music.duck), path: "music.duck" }),
      slider(panel, { label: "Fade out", path: "music.fade_out", min: 0, max: 10, step: .5, format: secondsText, read: (view) => view.music ? finite(view.music.fade_out, 1.5) : 1.5, undo: "Music fade" }),
      toggle(panel, { label: "Cut to the beat", hint: "Moves quiet cuts onto nearby beats.", read: (view) => !!(view.music && view.music.beat_sync), path: "music.beat_sync", gate: (view) => {
        const asset = view.music && Kit.assets()[view.music.asset];
        return asset && Array.isArray(asset.beats) && !asset.beats.length ? "No clear beat in this track." : "";
      } }));
    const musicEmpty = note("Add an audio file in the media bin to use it here.");
    panel.syncs.push((view) => {
      const { beds, uploaded } = musicChoices();
      const key = JSON.stringify([beds, uploaded.map((asset) => asset.id)]);
      if (key !== musicKey) {
        musicKey = key;
        select.replaceChildren(el("option", { value: "", text: "No music" }),
          ...uploaded.map((asset) => el("option", { value: asset.id, text: asset.name })),
          ...beds.map((bed) => el("option", { value: bed.id, text: bed.name })));
      }
      select.value = view.music && view.music.asset || "";
      musicControls.hidden = !view.music;
      musicEmpty.hidden = !!view.music || !!(uploaded.length || beds.length);
    });
    const analysisOf = () => { const plan = Kit.draft(); return plan && plan.analysis || {}; };
    const speechNeeded = () => (Array.isArray(analysisOf().speech) && analysisOf().speech.length ? "" : "Needs speech in the footage.");
    const wordsNeeded = () => (Array.isArray(analysisOf().words) && analysisOf().words.length ? "" : "Needs a transcript with word timing.");
    const list = Kit.catalogue();
    panel.body.append(
      group("Speech",
        toggle(panel, { label: "Enhance voice", hint: "Clearer, more even speech.", path: "audio.enhance_voice", gate: () => needsFfmpeg("voice", "Voice cleanup") }),
        toggle(panel, { label: "Remove background noise", path: "audio.denoise", gate: () => needsFfmpeg("voice", "Voice cleanup") }),
        toggle(panel, { label: "Keep the original loudness", hint: "Skips the usual loudness leveling.", path: "audio.keep_loudness" }),
        "trim_pauses" in list.defaults ? toggle(panel, { label: "Trim pauses", hint: "Shortens silent gaps inside moments.", path: "trim_pauses", gate: speechNeeded }) : null,
        "remove_fillers" in list.defaults ? toggle(panel, { label: "Remove filler words", hint: "Cuts um, uh, and similar.", path: "remove_fillers", gate: wordsNeeded }) : null,
        toggle(panel, { label: "Mute original sound", path: "audio.mute_source" })),
      needsFfmpeg("music", "Music") ? group("Music", note(needsFfmpeg("music", "Music"))) : group("Music", el("label", { for: "ix-music", text: "Track" }), select, musicEmpty, musicControls),
      note("Voice cleanup and noise removal apply in the render."));
  }

  /* ---------- Motion ---------- */
  function segmentsOf() { const plan = Kit.draft(); return plan && Array.isArray(plan.segments) ? plan.segments : []; }
  function currentMoment() { return clamp(state.moment, 0, Math.max(0, segmentsOf().length - 1)); }
  /* A selection on the timeline opens the control for that item. */
  function followSelection() {
    const editor = Kit.bridge();
    const chosen = editor && editor.selection ? editor.selection() : { kind: "none" };
    const key = chosen.kind + ":" + chosen.index;
    if (key === state.selection) return;
    state.selection = key;
    if (chosen.kind === "clip") state.moment = chosen.index;
    else if (chosen.kind === "text") { state.textIndex = chosen.index; showTab("text"); }
    else if (chosen.kind === "overlay") { state.overlayIndex = chosen.index; showTab("text"); }
    else if (chosen.kind === "music") showTab("audio");
    else if (chosen.kind === "transition") showTab("transitions");
    else if (chosen.kind === "caption") {
      showTab("captions");
      const row = $("ix-cue-" + Math.round(finite(chosen.sourceStart, 0) * 1000));
      if (row) row.scrollIntoView({ block: "nearest" });
    } else if (chosen.kind === "none") { state.textIndex = -1; state.overlayIndex = -1; }
  }
  function updateMoment(index, label, change, options) {
    Kit.edit(label, (_, plan) => { if (plan.segments[index]) change(plan.segments[index]); }, options);
  }
  function keyframeControl(panel) {
    const image = el("img", { alt: "", data: { frame: "a" }, hidden: true });
    const frame = el("div", { class: "ix-kf-frame" }, el("div", { class: "ix-kf-picture" }, image));
    const boxes = {};
    const blank = () => ({ from: { scale: 1, x: .5, y: .5 }, to: { scale: 1, x: .5, y: .5 } });
    const live = { motion: blank() };
    const place = (name) => {
      const point = live.motion[name];
      const box = boxes[name];
      box.style.width = 100 / point.scale + "%";
      box.style.height = 100 / point.scale + "%";
      box.style.left = (point.x - .5 / point.scale) * 100 + "%";
      box.style.top = (point.y - .5 / point.scale) * 100 + "%";
      box.setAttribute("aria-label", (name === "from" ? "Start of the move" : "End of the move") + ", " + Math.round(point.scale * 100) + " percent, " + Math.round(point.x * 100) + " across, " + Math.round(point.y * 100) + " down. Arrows move it. Plus and minus zoom.");
    };
    const keep = (point) => {
      point.scale = clamp(point.scale, 1, 3);
      const edge = .5 / point.scale;
      point.x = clamp(point.x, edge, 1 - edge);
      point.y = clamp(point.y, edge, 1 - edge);
    };
    const commit = () => {
      const index = currentMoment();
      const motion = Kit.clone(live.motion);
      const still = ["from", "to"].every((name) => motion[name].scale === 1 && motion[name].x === .5 && motion[name].y === .5);
      updateMoment(index, "Moment move", (segment) => { if (still) delete segment.motion; else segment.motion = motion; }, { coalesce: false });
    };
    ["from", "to"].forEach((name) => {
      const box = el("button", { type: "button", class: "ix-kf is-" + name, on: {
        keydown: (event) => {
          const step = event.shiftKey ? .1 : .02;
          const point = live.motion[name];
          const moves = { ArrowLeft: ["x", -step], ArrowRight: ["x", step], ArrowUp: ["y", -step], ArrowDown: ["y", step] };
          if (moves[event.key]) { point[moves[event.key][0]] += moves[event.key][1]; }
          else if (event.key === "+" || event.key === "=") point.scale += .1;
          else if (event.key === "-" || event.key === "_") point.scale -= .1;
          else return;
          event.preventDefault(); keep(point); place(name); sync(); commit();
        },
        pointerdown: (event) => {
          if (event.button !== 0) return;
          event.preventDefault();
          const rect = frame.getBoundingClientRect();
          const point = live.motion[name];
          const origin = { x: event.clientX, y: event.clientY, px: point.x, py: point.y };
          box.setPointerCapture(event.pointerId);
          const move = (next) => { point.x = origin.px + (next.clientX - origin.x) / rect.width; point.y = origin.py + (next.clientY - origin.y) / rect.height; keep(point); place(name); };
          const up = () => { box.removeEventListener("pointermove", move); box.removeEventListener("pointerup", up); box.removeEventListener("pointercancel", up); commit(); };
          box.addEventListener("pointermove", move); box.addEventListener("pointerup", up); box.addEventListener("pointercancel", up);
        },
      } }, el("span", { text: name === "from" ? "Start" : "End" }));
      boxes[name] = box;
      frame.append(box);
    });
    const scales = {};
    const scaleRow = (name, label) => {
      const id = nextId("kf");
      const output = el("output", { class: "ix-value", for: id });
      const input = el("input", { id, class: "ix-range", type: "range", min: "1", max: "3", step: ".05", on: {
        input: () => { live.motion[name].scale = Number(input.value); keep(live.motion[name]); place(name); sync(); },
        change: commit,
      } });
      scales[name] = { input, output };
      return el("div", { class: "ix-field" }, el("div", { class: "ix-field-head" }, el("label", { for: id, text: label }), output), input);
    };
    const sync = () => {
      Object.entries(scales).forEach(([name, parts]) => {
        const value = live.motion[name].scale;
        parts.input.value = String(value);
        parts.output.textContent = Math.round(value * 100) + "%";
        parts.input.style.setProperty("--from", "0%");
        parts.input.style.setProperty("--to", (value - 1) / 2 * 100 + "%");
      });
    };
    const reset = el("button", { type: "button", class: "ix-link", text: "Remove move", on: { click: () => { live.motion = blank(); place("from"); place("to"); sync(); commit(); } } });
    const play = el("button", { type: "button", class: "ix-link", text: "Play the move", on: { click: () => {
      const crop = (point) => "translate(" + (.5 - point.x * point.scale) * 100 + "%, " + (.5 - point.y * point.scale) * 100 + "%) scale(" + point.scale + ")";
      if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      frame.classList.add("is-playing");
      const animation = image.animate([{ transform: crop(live.motion.from) }, { transform: crop(live.motion.to) }], { duration: 1800, easing: "ease-in-out" });
      animation.finished.catch(() => null).finally(() => frame.classList.remove("is-playing"));
    } } });
    panel.syncs.push(() => {
      const [wide, tall] = String(jobAspect()).split(":").map(Number);
      if (wide && tall) { frame.style.aspectRatio = wide + " / " + tall; frame.classList.toggle("is-portrait", wide < tall); }
      const segment = segmentsOf()[currentMoment()];
      const motion = segment && segment.motion;
      const active = document.activeElement;
      if (active && frame.parentNode && frame.parentNode.contains(active) && active.type === "range") return;
      live.motion = motion && motion.from && motion.to ? Kit.clone(motion) : blank();
      place("from"); place("to"); sync();
    });
    return el("div", { class: "ix-keyframes" }, frame,
      note("Drag each box to where the shot starts and ends."),
      scaleRow("from", "Start zoom"), scaleRow("to", "End zoom"),
      el("div", { class: "ix-item-actions" }, play, reset));
  }
  function buildMotion(panel) {
    const list = Kit.catalogue();
    const defaults = list.defaults.zoom;
    const moments = el("div", { class: "ix-moments", role: "group", "aria-label": "Moment" });
    let momentKey = "";
    const speedIndex = (value) => {
      let best = 0;
      SPEEDS.forEach((speed, index) => { if (Math.abs(speed - value) < Math.abs(SPEEDS[best] - value)) best = index; });
      return best;
    };
    const speed = slider(panel, { label: "Speed", min: 0, max: SPEEDS.length - 1, step: 1, format: (value) => SPEEDS[value] + "×",
      read: () => { const segment = segmentsOf()[currentMoment()]; return speedIndex(finite(segment && segment.speed, 1)); },
      write: (value) => updateMoment(currentMoment(), "Moment speed", (segment) => { if (SPEEDS[value] === 1) delete segment.speed; else segment.speed = SPEEDS[value]; }),
    });
    const volume = slider(panel, { label: "Volume", min: 0, max: 2, step: .05,
      read: () => { const segment = segmentsOf()[currentMoment()]; return finite(segment && segment.volume, 1); },
      write: (value) => updateMoment(currentMoment(), "Moment volume", (segment) => { if (value === 1) delete segment.volume; else segment.volume = value; }),
    });
    panel.syncs.push(() => {
      const segments = segmentsOf();
      const index = currentMoment();
      const key = JSON.stringify(segments.map((segment) => segment.label)) + index;
      if (key === momentKey) return;
      momentKey = key;
      rebuild(moments, () => segments.map((segment, position) => el("button", { id: "ix-moment-" + position, type: "button", class: "ix-moment", "aria-pressed": String(position === index), title: segment.label, "aria-label": "Moment " + (position + 1) + ", " + (segment.label || ""), text: String(position + 1), on: { click: () => { state.moment = position; Kit.bridge().select({ kind: "clip", index: position }); paintPanels(); } } })));
    });
    const momentName = note("");
    panel.syncs.push(() => { const segment = segmentsOf()[currentMoment()]; momentName.textContent = segment ? segment.label || "" : ""; });
    const zoomCards = segmented(panel, { legend: "Zoom", choices: list.zoom_modes.map((row) => ({ value: row.id, label: row.name })), path: "zoom.mode", undo: () => "Zoom" });
    const zoomStrength = slider(panel, { label: "Strength", path: "zoom.strength", min: 0, max: 1, step: .05, undo: "Zoom strength" });
    panel.syncs.push((view) => { zoomStrength.hidden = view.zoom.mode === defaults.mode; });
    const effects = group("Depth effect");
    panel.effects = effects;
    panel.body.append(
      needsFfmpeg("zoom", "Zooming") ? group("Every moment", note(needsFfmpeg("zoom", "Zooming"))) : group("Every moment", zoomCards, chosenDescription(panel, list.zoom_modes, (view) => view.zoom.mode), zoomStrength),
      group("One moment", moments, momentName, speed, volume, keyframeControl(panel)),
      group("Clean up",
        toggle(panel, { label: "Stabilize", hint: "Steadies handheld shots. Slower to render.", path: "stabilize", gate: () => needsFfmpeg("stabilize", "Stabilizing") }),
        toggle(panel, { label: "Reduce picture noise", hint: "Helps low-light footage. Slower to render.", path: "denoise_picture", })),
      effects);
    adoptEffects(panel);
  }
  function adoptEffects(panel) {
    const section = $("effects-section");
    const effects = Kit.caps().effects || {};
    if (!panel.effects) return;
    const reason = effects.supported === false ? effects.reason || "Depth effects need extra FFmpeg filters." : "";
    if (section && section.parentNode !== panel.effects) {
      panel.effects.append(section);
      const title = section.querySelector(".section-title");
      if (title) title.hidden = true;
    }
    let message = panel.effects.querySelector(".ix-effects-reason");
    if (reason && !message) { message = note(reason); message.classList.add("ix-effects-reason"); panel.effects.append(message); }
    if (message) message.hidden = !reason;
  }

  /* ---------- Cutout ---------- */
  function cutoutCost() {
    const prices = (Kit.caps().meta || {}).prices || {};
    const fps = finite(Kit.view().export.fps, 30);
    return Math.max(50, storyLength() * fps) / 1000 * finite(prices.sam_per_thousand_frames, 0);
  }
  function commitCutout(patch, label) {
    Kit.edit(label, (plan) => {
      plan.cutout = Object.assign({}, plan.cutout, patch);
      if (plan.cutout.mode !== "image") plan.cutout.asset = null;
    }, { coalesce: false });
  }
  function submitBackground(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const prompt = form.elements.prompt.value.trim();
    if (!Kit.job() || !prompt || state.background.busy) return;
    state.background = { busy: true, error: "" };
    paintPanels();
    Kit.api("/background", { method: "POST", body: { prompt, consent: state.consent }, timeout: 90000 })
      .then((data) => {
        if (!data.asset || !data.asset.id) throw new Error("The editor did not return a background.");
        Kit.addAsset(data.asset);
        state.background = { busy: false, error: "" };
        form.reset();
        commitCutout({ mode: "image", asset: data.asset.id }, "Generated background");
        state.cutoutMode = "";
      })
      .catch((error) => { state.background = { busy: false, error: error.message }; })
      .finally(paintPanels);
  }
  function buildCutout(panel) {
    const list = Kit.catalogue();
    const defaults = list.defaults.cutout;
    const off = defaults.mode;
    const face = (choice) => el("span", { class: "ix-frame ix-cutcard", data: { cutout: choice.id } },
      el("img", { class: "ix-cut-back", alt: "", data: { frame: "a" }, hidden: true }),
      el("i", { class: "ix-cut-solid" }),
      el("img", { class: "ix-cut-front", alt: "", data: { frame: "a" }, hidden: true }));
    const blocked = () => needsFfmpeg("cutout", "Cutting out a subject") || metaProjectBlock("cutout", "Cutouts");
    const pendingMode = () => state.cutoutMode || Kit.view().cutout.mode;
    /* A cutout writes only when it is complete, so the draft never holds one the server would refuse. */
    const missing = (mode, view, subject) => {
      if (mode === off) return "";
      if (!state.consent) return "consent";
      if (!String(subject === undefined ? view.cutout.subject : subject).trim()) return "subject";
      if (mode === "image" && !view.cutout.asset) return "image";
      return "";
    };
    const tryCommit = (subject) => {
      const view = Kit.view();
      const mode = pendingMode();
      if (missing(mode, view, subject)) return false;
      state.cutoutMode = "";
      commitCutout(subject === undefined ? { mode } : { mode, subject }, "Cutout");
      return true;
    };
    const cards = radioCards(panel, { class: "ix-cutmodes", cardClass: "ix-swatch", legend: "Choose a cutout", choices: list.cutout_modes, face,
      read: () => pendingMode(),
      gate: (id) => (id === off ? "" : blocked()),
      write: (choice) => {
        const need = missing(choice.id, Kit.view());
        if (!need) { state.cutoutMode = ""; commitCutout({ mode: choice.id }, choice.name); return; }
        state.cutoutMode = choice.id;
        paintPanels();
        const field = need === "consent" ? $("ix-cut-consent") : need === "subject" ? $("ix-cut-subject") : $("ix-cut-image");
        if (field) field.focus();
      },
    });
    const subject = el("input", { id: "ix-cut-subject", type: "text", maxlength: "150", placeholder: "The wooden bowl", on: { change: () => {
      const text = subject.value.replace(/\s+/g, " ").trim();
      if (pendingMode() !== off && !text) { subject.value = Kit.view().cutout.subject || ""; return; }
      if (!tryCommit(text)) commitCutout({ subject: text }, "Cutout subject");
    } } });
    const subjectRow = el("div", { class: "ix-stack" }, el("label", { for: subject.id, text: "What to keep" }), subject);
    const color = colorField(panel, { label: "Background", path: "cutout.color" });
    const blur = slider(panel, { label: "Blur", path: "cutout.blur", min: 0, max: 1, step: .05, undo: "Background blur" });
    const image = el("select", { id: "ix-cut-image", on: { change: () => {
      if (!image.value) return;
      commitCutout({ asset: image.value }, "Cutout background");
      tryCommit();
    } } });
    const generateStatus = note("");
    const generate = el("form", { class: "ix-stack", on: { submit: submitBackground } },
      el("label", { for: "ix-cut-prompt", text: "Generate a background" }),
      el("input", { id: "ix-cut-prompt", name: "prompt", type: "text", maxlength: "300", placeholder: "A sunlit workshop wall" }),
      el("button", { type: "submit", class: "button button-secondary ix-wide", id: "ix-cut-generate", text: "Generate" }), generateStatus);
    const imageRow = el("div", { class: "ix-stack" }, el("label", { for: image.id, text: "Background image" }), image, generate);
    const consent = el("input", { type: "checkbox", id: "ix-cut-consent", on: { change: () => {
      state.consent = consent.checked;
      tryCommit();
      paintPanels();
      const next = $("ix-cut-subject");
      if (consent.checked && next && !next.value) next.focus();
    } } });
    const consentRow = el("label", { class: "ix-check" }, consent, el("span", { text: "Send this footage to Meta and use my Meta credit." }));
    const cost = note("");
    const gateNote = note("");
    panel.syncs.push((view) => {
      const mode = pendingMode();
      const on = mode !== off;
      if (view.cutout.mode !== off) state.consent = true;
      if (document.activeElement !== subject) subject.value = view.cutout.subject || "";
      subjectRow.hidden = !on;
      color.hidden = mode !== "color";
      blur.hidden = mode !== "blur";
      imageRow.hidden = mode !== "image";
      consentRow.hidden = !on;
      consent.checked = state.consent;
      const reason = blocked();
      gateNote.hidden = !reason;
      gateNote.textContent = reason;
      cost.hidden = !on || !!reason;
      cost.textContent = "Cutting out this story costs " + money(cutoutCost()) + " of your Meta credit. The cutout shows in the render.";
      const images = imageChoices();
      const key = images.map((asset) => asset.id).join();
      if (image.dataset.key !== key) {
        image.dataset.key = key;
        image.replaceChildren(el("option", { value: "", text: images.length ? "Choose an image" : "No images yet" }), ...images.map((asset) => el("option", { value: asset.id, text: asset.name })));
      }
      image.value = view.cutout.asset || "";
      const noBackground = metaProjectBlock("background", "Generated backgrounds");
      const prices = (Kit.caps().meta || {}).prices || {};
      const button = $("ix-cut-generate");
      if (button) { button.disabled = !!noBackground || state.background.busy || !state.consent; button.textContent = state.background.busy ? "Generating" : "Generate"; }
      generateStatus.className = "ix-note" + (state.background.error ? " is-error" : "");
      generateStatus.textContent = state.background.error || noBackground || money(finite(prices.cover_per_image, 0)).replace(/^./, (letter) => letter.toUpperCase()) + " for each image.";
    });
    panel.body.append(gateNote,
      group("", cards, chosenDescription(panel, list.cutout_modes, () => pendingMode())),
      group("", consentRow, subjectRow, color, blur, imageRow, cost));
  }

  /* ---------- Export ---------- */
  /* Encoding is quality-based, so size depends on the footage. A range says that plainly. */
  function exportSize(view, aspect) {
    const format = String(view.export.format).toLowerCase();
    const isGif = format === "gif";
    const quality = { "4k": 2160 }[String(view.export.quality).toLowerCase()] || parseInt(view.export.quality, 10) || 1080;
    const short = isGif ? Math.min(quality, 540) : quality;
    const rate = isGif ? Math.min(finite(view.export.fps, 30), 15) : finite(view.export.fps, 30);
    const parts = String(aspect).split(":").map(Number);
    const ratio = parts[0] && parts[1] ? Math.max(parts[0], parts[1]) / Math.min(parts[0], parts[1]) : 16 / 9;
    const seconds = storyLength();
    const frames = short * short * ratio * rate * seconds;
    const sound = isGif ? 0 : 24000 * seconds;
    const [low, high] = isGif ? [.08, .3] : format === "webm" ? [.02, .12] : [.03, .2];
    return { low: frames * low / 8 + sound, high: frames * high / 8 + sound };
  }
  function jobAspect() { return renderChoice("aspect", () => ($("aspect") ? $("aspect").value : "9:16")); }
  function buildExport(panel) {
    const list = Kit.catalogue();
    const options = list.export;
    const aspects = $("aspect") ? Array.from($("aspect").options).map((option) => ({ value: option.value, label: option.textContent.split(/\s+/)[0] })) : [];
    const aspect = aspects.length ? segmented(panel, { legend: "Frame", choices: aspects, read: jobAspect, write: (value) => setRenderChoice("aspect", value, "Frame " + value) }) : null;
    const size = el("p", { class: "ix-estimate" });
    const sizeNote = note("");
    const formatNote = note("GIF has no sound, and exports at up to 15 fps and 540 pixels.");
    panel.syncs.push((view) => {
      const seconds = storyLength();
      const range = exportSize(view, jobAspect());
      size.textContent = seconds ? bytes(range.low) + " to " + bytes(range.high) : "Size appears once a story exists.";
      sizeNote.textContent = seconds ? "For " + clock(seconds) + " of video. Detailed, busy footage lands near the top." : "";
      formatNote.hidden = String(view.export.format).toLowerCase() !== "gif";
    });
    panel.body.append(group("",
      segmented(panel, { legend: "Quality", choices: options.qualities.map((value) => ({ value, label: value })), path: "export.quality", undo: () => "Export quality" }),
      segmented(panel, { legend: "Frame rate", choices: options.fps.map((value) => ({ value, label: value + " fps" })), path: "export.fps", undo: () => "Frame rate" }),
      segmented(panel, { legend: "Format", choices: options.formats.map((value) => ({ value, label: value.toUpperCase() })), path: "export.format", undo: () => "Export format" }),
      formatNote, aspect),
    group("Estimated size", size, sizeNote));
  }

  /* ---------- Tabs ---------- */
  const TABS = [
    { id: "look", label: "Look", icon: "look", build: buildLook },
    { id: "transitions", label: "Transitions", icon: "transition", build: buildTransitions },
    { id: "captions", label: "Captions", icon: "captions", build: buildCaptions },
    { id: "text", label: "Text", icon: "text", build: buildText },
    { id: "audio", label: "Audio", icon: "audio", build: buildAudio },
    { id: "motion", label: "Motion", icon: "motion", build: buildMotion },
    { id: "cutout", label: "Cutout", icon: "cutout", build: buildCutout },
    { id: "export", label: "Export", icon: "export", build: buildExport },
  ];
  function showTab(id, focus) {
    state.tab = id;
    panels.forEach((panel) => {
      const on = panel.id === id;
      panel.tab.setAttribute("aria-selected", String(on));
      panel.tab.tabIndex = on ? 0 : -1;
      panel.node.hidden = !on;
    });
    const active = panels.find((panel) => panel.id === id);
    if (focus && active) active.tab.focus();
    if (active) active.tab.scrollIntoView({ block: "nearest", inline: "nearest" });
    try { sessionStorage.setItem("briefcut.inspector.tab", id); } catch (_) { /* Optional. */ }
  }
  function railKeys(event) {
    const index = panels.findIndex((panel) => panel.id === state.tab);
    const last = panels.length - 1;
    const target = { ArrowDown: index + 1, ArrowRight: index + 1, ArrowUp: index - 1, ArrowLeft: index - 1, Home: 0, End: last }[event.key];
    if (target === undefined) return;
    event.preventDefault();
    showTab(panels[(target + panels.length) % panels.length].id, true);
  }
  function build() {
    const mount = $("inspector-mount");
    if (!mount) return;
    const list = Kit.catalogue();
    if (state.built) return;
    if (!list) {
      const text = Kit.caps().render ? "Editing choices are not in this build." : "Loading editing choices.";
      if (mount.textContent !== text) mount.replaceChildren(el("p", { class: "ix-note ix-waiting", text }));
      return;
    }
    state.built = true;
    const rail = el("div", { class: "ix-rail", role: "tablist", "aria-label": "Edit settings", "aria-orientation": "vertical", on: { keydown: railKeys } });
    const stage = el("div", { class: "ix-stage" });
    TABS.forEach((tab) => {
      const button = el("button", { type: "button", role: "tab", id: "ix-tab-" + tab.id, class: "ix-tab", "aria-controls": "ix-panel-" + tab.id, on: { click: () => showTab(tab.id) } }, icon(tab.icon), el("span", { text: tab.label }));
      const body = el("div", { class: "ix-panel-body" });
      const fieldset = el("fieldset", { class: "ix-lock" }, body);
      const gate = el("p", { class: "ix-gate", role: "status", hidden: true });
      const node = el("div", { id: "ix-panel-" + tab.id, class: "ix-panel", role: "tabpanel", "aria-labelledby": button.id, tabindex: "0" }, gate, fieldset);
      const panel = { id: tab.id, tab: button, node, body, gate, fieldset, syncs: [], build: tab.build };
      panels.push(panel);
      rail.append(button);
      stage.append(node);
    });
    const narrow = matchMedia("(max-width: 700px)");
    const orient = () => rail.setAttribute("aria-orientation", narrow.matches ? "horizontal" : "vertical");
    narrow.addEventListener("change", orient);
    orient();
    mount.replaceChildren(el("div", { class: "ix" }, rail, stage, chatNode()));
    panels.forEach((panel) => { try { panel.build(panel); } catch (error) { panel.body.replaceChildren(note("This tab could not load: " + error.message, "error")); } });
    let saved = "";
    try { saved = sessionStorage.getItem("briefcut.inspector.tab") || ""; } catch (_) { /* Optional. */ }
    showTab(panels.some((panel) => panel.id === saved) ? saved : state.tab);
    Kit.notify();
  }
  function paintPanels() {
    if (!state.built) return;
    followSelection();
    const view = Kit.view();
    const plan = Kit.draft();
    const lockedNow = Kit.locked();
    panels.forEach((panel) => {
      const reason = !plan ? "Create a story, then these choices shape it." : lockedNow ? "Rendering. These open again when it finishes." : "";
      panel.gate.hidden = !reason;
      panel.gate.textContent = reason;
      panel.fieldset.disabled = !plan || lockedNow;
      panel.syncs.forEach((sync) => { try { sync(view); } catch (error) { console.error(error); } });
    });
    const motion = panels.find((panel) => panel.id === "motion");
    if (motion) adoptEffects(motion);
  }

  function seek(seconds) {
    const editor = Kit.bridge();
    if (editor && editor.seek) editor.seek(seconds);
  }

  /* ---------- Direct the edit ---------- */
  const chat = {};
  function patchWords(patch) { return !patch || patch.skipped ? "" : patch.summary || patch.reason || "Changed the story."; }
  function noteBlock() {
    const project = Kit.job();
    if (!Kit.metaReady()) return Kit.metaReason();
    if (!project || !project.request) return "Create a story, render it, then direct changes here.";
    if (project.request.provider !== "meta") return "This project was saved before Briefcut ran on Meta. Create a new story to direct it by note.";
    if (!(project.result && project.result.proof && project.result.proof.has_video)) return "Render a version first. Then direct changes here.";
    return "";
  }
  function chatNode() {
    chat.log = el("ol", { class: "ix-chat-log", "aria-live": "polite", hidden: true });
    chat.status = el("p", { class: "ix-chat-status" });
    chat.chips = el("div", { class: "ix-chips", role: "group", "aria-label": "Suggestions" }, ["Warmer", "Tighter pacing", "Bigger captions"].map((text) => el("button", { type: "button", class: "ix-chip", text, on: { click: () => sendNote(text) } })));
    chat.input = el("textarea", { id: "ix-chat-input", rows: "1", maxlength: "500", placeholder: "Hold the finished bowl longer", on: {
      keydown: (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); chat.form.requestSubmit(); } },
    } });
    chat.send = el("button", { type: "submit", class: "button button-primary ix-send", "aria-label": "Send note", title: "Send note" }, icon("send"));
    chat.form = el("form", { class: "ix-chat-form", on: { submit: (event) => { event.preventDefault(); sendNote(chat.input.value); } } }, chat.input, chat.send);
    chat.root = el("section", { class: "ix-chat", "aria-labelledby": "ix-chat-heading" },
      el("label", { id: "ix-chat-heading", for: "ix-chat-input", class: "ix-chat-title", text: "Direct the edit" }), chat.log, chat.status, chat.chips, chat.form);
    return chat.root;
  }
  async function sendNote(raw) {
    const text = String(raw || "").trim();
    const studio = window.BriefcutStudio;
    if (!text || !Kit.job() || noteBlock() || Kit.locked() || state.chatSending || !studio) return;
    state.chatSending = true;
    state.chat.push({ who: "you", text });
    chat.input.value = "";
    paintChat();
    const accepted = await studio.revise("note", text);
    if (accepted) {
      state.chatRevision = finite(accepted.revision, 0);
      state.chat.push({ who: "editor", text: "Working on version " + state.chatRevision + "." });
    } else {
      const reason = $("error-text") ? $("error-text").textContent : "";
      state.chat.push({ who: "editor", text: reason || "That note did not go through. Your verified version is unchanged.", error: true });
    }
    state.chatSending = false;
    paintChat();
  }
  function watchRevision() {
    const project = Kit.job();
    if (!project || !state.chatRevision || finite(project.revision, 0) !== state.chatRevision) return;
    if (project.status === "complete") {
      const changes = (Array.isArray(project.applied_patches) ? project.applied_patches : []).map(patchWords).filter(Boolean);
      state.chat.push({ who: "editor", text: project.note_summary || "Version " + state.chatRevision + " is ready.", changes, revision: state.chatRevision });
      state.chatRevision = 0;
    } else if (project.status === "failed" || project.status === "cancelled") {
      state.chat.push({ who: "editor", text: project.error || "That note did not make a new version. Your verified version is unchanged.", error: true });
      state.chatRevision = 0;
    }
  }
  function openVersion(revision) {
    const tab = Array.from(document.querySelectorAll("#version-tabs .version-tab")).find((button) => button.textContent.trim().startsWith("v" + revision));
    if (tab) tab.click();
    const deliver = document.querySelector('.workflow [data-mode="deliver"]');
    if (deliver && !deliver.disabled) deliver.click();
    if (tab) tab.scrollIntoView({ block: "nearest" });
  }
  function paintChat() {
    if (!chat.root) return;
    const project = Kit.job();
    const id = project ? project.id : "";
    if (id !== state.chatJob) { state.chatJob = id; state.chat = []; state.chatRevision = 0; }
    const reason = noteBlock();
    const working = Kit.locked() || !!state.chatSending;
    chat.status.textContent = reason || (working ? "Working. Notes open again when this version is ready." : "");
    chat.status.hidden = !chat.status.textContent;
    chat.status.classList.toggle("is-blocked", !!reason);
    chat.root.classList.toggle("is-blocked", !!reason);
    chat.input.disabled = !!reason || working;
    chat.send.disabled = !!reason || working;
    chat.chips.querySelectorAll("button").forEach((button) => { button.disabled = !!reason || working; });
    chat.log.hidden = !state.chat.length;
    const key = JSON.stringify(state.chat);
    if (chat.log.dataset.key === key) return;
    chat.log.dataset.key = key;
    chat.log.replaceChildren(...state.chat.slice(-6).map((entry) => el("li", { class: "is-" + entry.who + (entry.error ? " is-error" : "") },
      el("p", { text: entry.text }),
      entry.changes && entry.changes.length ? el("ul", {}, entry.changes.map((change) => el("li", { text: change }))) : null,
      entry.revision ? el("button", { type: "button", class: "ix-link", text: "Show version " + entry.revision, on: { click: () => openVersion(entry.revision) } }) : null)));
    chat.log.scrollTop = chat.log.scrollHeight;
  }

  /* ---------- Render options, sent with the render call ---------- */
  window.BriefcutRenderOptions = function renderOptions() {
    const plan = Kit.draft();
    const options = Object.assign({}, plan && plan.render_options);
    if (state.refine > 0 && !refineBlock()) options.auto_refine = state.refine;
    return options;
  };
  function refineBlock() {
    const project = Kit.job();
    if (!Kit.metaReady()) return Kit.metaReason();
    if (project && project.request && project.request.provider !== "meta") return "This project was saved before Briefcut ran on Meta. Create a new story to auto-refine it.";
    return "";
  }
  function mountRefine() {
    const make = $("make");
    if (!make || $("ix-refine")) return;
    const rounds = finite((Kit.caps().director || {}).auto_refine_rounds, 3);
    const select = el("select", { id: "ix-refine-rounds", on: { change: () => { state.refine = Number(select.value); } } }, el("option", { value: "0", text: "Off" }),
      Array.from({ length: rounds }, (_, index) => el("option", { value: String(index + 1), text: plural(index + 1, "round") })));
    make.before(el("div", { class: "ix-refine", id: "ix-refine" }, el("label", { for: select.id, text: "Auto-refine" }), select));
    state.refineSelect = select;
  }
  function paintRefine() {
    const box = $("ix-refine");
    if (!box || !state.refineSelect) return;
    const reason = refineBlock();
    state.refineSelect.disabled = !!reason || Kit.locked();
    box.title = reason || "After a render, Meta reviews the cut and Briefcut renders its fixes, up to this many times.";
    if (reason) { state.refine = 0; state.refineSelect.value = "0"; }
  }

  /* ---------- More shorts ---------- */
  function mountShorts() {
    const panel = $("delivery-panel");
    if (!panel || $("ix-shorts")) return;
    const list = el("ul", { class: "ix-family", id: "ix-shorts-family" });
    const count = el("select", { id: "ix-shorts-count", "aria-label": "How many shorts" }, [2, 3, 4, 5].map((value) => el("option", { value: String(value), text: plural(value, "short"), selected: value === 3 })));
    const button = el("button", { type: "button", class: "button button-secondary", id: "ix-shorts-make", text: "Make shorts", on: { click: () => makeShorts(Number(count.value)) } });
    const status = el("p", { class: "fine-print", id: "ix-shorts-status" });
    const after = $("version-changes") || $("revise-form");
    panel.insertBefore(el("section", { class: "ix-shorts", id: "ix-shorts", "aria-labelledby": "ix-shorts-heading" },
      el("div", { class: "ix-shorts-head" }, el("div", {}, el("h3", { id: "ix-shorts-heading", text: "More shorts from this clip" }), status), el("div", { class: "ix-shorts-actions" }, count, button)), list), after);
  }
  async function makeShorts(count) {
    const project = Kit.job();
    if (!project || state.shorts.busy) return;
    state.shorts.busy = true; state.shorts.error = "";
    paintShorts();
    try {
      const data = await Kit.api("/shorts", { method: "POST", body: { count }, timeout: 60000 });
      state.shorts.jobs = Array.isArray(data.jobs) ? data.jobs : [];
      state.shorts.job = project.id;
      Kit.toast(plural(state.shorts.jobs.length, "short") + " started. Each one is its own project.");
    } catch (error) { state.shorts.error = error.message; }
    state.shorts.busy = false;
    paintShorts();
  }
  function familyOf(project) {
    if (!project) return [];
    const mine = state.shorts.job === project.id;
    const rows = [].concat(mine ? state.shorts.jobs : [], Array.isArray(project.family) ? project.family : [], mine ? state.shorts.family : []).filter((row) => row && row.id && row.id !== project.id);
    const seen = new Map();
    rows.forEach((row) => { seen.set(row.id, Object.assign({}, seen.get(row.id), row)); });
    return Array.from(seen.values());
  }
  /* A short runs as its own project, so its row is refreshed from the parent while it works. */
  function watchFamily(project, family) {
    const working = family.some((row) => !["complete", "failed", "cancelled", "planned"].includes(row.status));
    if (!project || !working || state.shorts.timer) return;
    const id = project.id;
    state.shorts.timer = setTimeout(async () => {
      state.shorts.timer = 0;
      try {
        const data = await Kit.api("/api/campaigns/" + encodeURIComponent(id), { timeout: 8000 });
        if (Kit.job() && Kit.job().id === id && data.job) { state.shorts.job = id; state.shorts.family = data.job.family || []; }
      } catch (_) { /* The next look tries again. */ }
      paintShorts();
    }, 3000);
  }
  /* A row carries only a status, so a failed short's reason comes from its own project, once. */
  function explainFailures(family) {
    family.filter((row) => row.status === "failed" && state.shorts.why[row.id] === undefined).forEach(async (row) => {
      state.shorts.why[row.id] = "";
      try {
        const data = await Kit.api("/api/campaigns/" + encodeURIComponent(row.id), { timeout: 8000 });
        state.shorts.why[row.id] = data.job && data.job.error || "";
      } catch (_) { /* The status still says it needs attention. */ }
      paintShorts();
    });
  }
  function familyStatus(row) {
    if (row.status === "complete") return "Ready";
    if (row.status === "failed" || row.status === "cancelled") return "Needs attention";
    if (row.status === "planned") return "Ready to edit";
    return "Working";
  }
  function paintShorts() {
    const list = $("ix-shorts-family");
    if (!list) return;
    const project = Kit.job();
    const button = $("ix-shorts-make");
    const status = $("ix-shorts-status");
    button.disabled = state.shorts.busy || Kit.locked() || !project;
    button.textContent = state.shorts.busy ? "Starting" : "Make shorts";
    status.textContent = state.shorts.error || "Other moments from this footage, each as its own project.";
    status.classList.toggle("is-error", !!state.shorts.error);
    const family = familyOf(project);
    watchFamily(project, family);
    explainFailures(family);
    const key = JSON.stringify(family.map((row) => [row.id, row.status, row.title, state.shorts.why[row.id]]));
    if (list.dataset.key === key) return;
    list.dataset.key = key;
    list.replaceChildren(...family.map((row, index) => {
      const title = row.title || row.plan && row.plan.title || "Short " + (index + 1);
      const why = state.shorts.why[row.id];
      return el("li", {}, el("span", { text: title }), el("small", { text: familyStatus(row) }),
        el("button", { type: "button", class: "ix-link", text: "Open", "aria-label": "Open " + title, on: { click: () => window.BriefcutStudio.openJob(row.id) } }),
        why ? el("p", { class: "ix-family-why", text: why }) : null);
    }));
  }

  function sync() {
    build();
    syncRenderChoices();
    mountRefine(); mountShorts();
    watchRevision();
    paintPanels(); paintChat(); paintRefine(); paintShorts();
  }

  (function waitForKit() {
    let tries = 0;
    const attempt = () => {
      if (window.BriefcutEditor) { Kit.onChange(sync); sync(); return; }
      tries += 1;
      if (tries < 600) setTimeout(attempt, 100);
    };
    attempt();
  }());
}());
