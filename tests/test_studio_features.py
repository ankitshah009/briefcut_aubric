"""Static checks for the Studio controls that drive the Meta features added on 2026-11-04.

They pin the page to the backend contract: the reviewer choice (``director``), punch
words (``captions.emphasize`` / ``emphasis`` / ``emphasis_color``), verified find
(``verified`` / ``coverage`` / ``flagged`` / ``thumbnail_url`` / ``verification``),
model-labelled receipts (``by``), and the hero word (``POST .../hero-word``,
``edit.hero_word``). The browser behaviour itself was checked in headless Chrome.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from vedit import edit_spec

STATIC = Path(__file__).resolve().parents[1] / "src" / "vedit" / "studio" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, {key: value or "" for key, value in attrs}))


def _tags() -> list[tuple[str, dict[str, str]]]:
    parser = _Tags()
    parser.feed(_read("index.html"))
    return parser.tags


# --- Reviewer: Muse Spark or Muse Wonder (C1, C4) ---------------------------------------


def test_reviewer_choice_is_a_labelled_radio_group_beside_the_review_box() -> None:
    page = _read("index.html")
    tags = _tags()
    radios = [attrs for tag, attrs in tags if tag == "input" and attrs.get("name") == "director"]
    assert [attrs["value"] for attrs in radios] == ["spark", "wonder"]
    assert sum("checked" in attrs for attrs in radios) == 1 and "checked" in radios[0]
    assert all(attrs.get("type") == "radio" for attrs in radios)
    fieldset = next(attrs for tag, attrs in tags if tag == "fieldset" and attrs.get("id") == "director-choice")
    assert fieldset.get("aria-describedby") == "director-note"
    assert "<legend>Reviewer</legend>" in page
    assert "<strong>Muse Spark</strong>" in page and "<strong>Muse Wonder</strong>" in page
    assert "Watches and hears the cut" in page
    # The choice sits right after the review box, inside the Meta options.
    assert page.index('id="review"') < page.index('id="director-choice"') < page.index('id="captions"')
    raise_note = next(attrs for tag, attrs in tags if tag == "p" and attrs.get("id") == "director-raise")
    assert raise_note.get("role") == "status"
    assert any(attrs.get("id") == "director-by" for _, attrs in tags)


def test_director_is_sent_at_creation_and_on_revise() -> None:
    studio = _read("studio.js")
    assert 'review: $("review").checked, director: chosenDirector() };' in studio
    assert 'function directorId(value) { return /wonder/i.test(String(value || "")) ? "wonder" : "spark"; }' in studio
    # Revise sends it for the director and auto modes; a note is turned into patches by Spark.
    assert '{ mode: "note", note } : { mode, director: projectDirector(state.job) }' in studio
    # Opening a saved project shows its reviewer.
    assert "setDirector(req.director);" in studio


def test_wonder_raises_the_project_limit_and_says_so() -> None:
    studio = _read("studio.js")
    # The minimum comes from the server, which prices it from Wonder's output allowance.
    assert "state.capabilities.meta.directors" in studio and "wonder_min_budget_usd" in studio
    assert "const wonderMinimumBudget =" not in studio, "no second copy of the server's number"
    # Every use calls it: comparing a number to the function itself is always false.
    assert not re.search(r"wonderMinimumBudget(?!\()", studio)
    assert '"The project API limit is now " + dollars(next.value) + ", so the review can run. "' in studio
    assert '" (too low for Muse Wonder)"' in studio
    assert "option.disabled = needsWonder && low;" in studio
    assert (
        '"A Muse Wonder review needs a project API limit of $" + wonderMinimumBudget().toFixed(2) + " or more.'
        in studio
    )
    # A budget choice that covers the server's real minimum exists in the form.
    from vedit import meta

    minimum = meta.capabilities()["directors"]["wonder_min_budget_usd"]
    page = _read("index.html")
    options = [
        float(value)
        for value in re.findall(
            r'<option value="([0-9.]+)">\$',
            page[page.index('id="budget"') : page.index("</select>", page.index('id="budget"'))],
        )
    ]
    assert any(value >= minimum for value in options)
    # The honest cost line comes from docs/evidence/wonder-review.
    assert "A review costs about $0.10 to $0.15." in studio


def test_receipts_name_the_model_behind_an_opinion() -> None:
    studio = _read("studio.js")
    assert '"muse-wonder": "Muse Wonder"' in studio and '"muse-spark": "Muse Spark"' in studio
    assert (
        'const model = check.basis === "inferred" && /^muse-/.test(check.by) ? instrumentName(check.by) : "";' in studio
    )
    assert 'basisLabels[check.basis] + (model ? " \\u00b7 " + model : "")' in studio
    assert 'inferred: "Model opinion"' in studio and 'derived: "Measured"' in studio
    assert '"Muse Wonder watched and heard the finished video."' in studio
    assert "review.director" in studio and "review.model" in studio
    assert ".check-basis.by-muse-wonder" in _read("studio.css")


def test_a_bare_null_caption_suggestion_is_not_shown() -> None:
    assert "/^(null|none|n\\/a)$/i.test(suggested)" in _read("studio.js")


# --- Punch words (C2) ---------------------------------------------------------------------


def test_punch_words_toggle_writes_the_contract_fields() -> None:
    inspector = _read("inspector.js")
    assert (
        'toggle(panel, { label: "Punch words", hint: "Muse Spark picks the word that carries each line.", path: "captions.emphasize", gate: punchGate })'
        in inspector
    )
    assert 'colorField(panel, { label: "Accent", path: "captions.emphasis_color" })' in inspector
    assert "punchWords(panel)," in inspector
    # Picks after a render come from the result, with Spark's reason as the tooltip.
    assert "result && Array.isArray(result.emphasis)" in inspector
    assert 'typeof pick.why === "string"' in inspector
    assert 'title: reason ? "Muse Spark: " + reason' in inspector
    # Turning one off keeps the rest as the picks; the last one off turns punch words off,
    # because an empty list would ask Spark to pick again.
    assert (
        'setPath(edit, "captions.emphasis", rest); if (!rest.length) setPath(edit, "captions.emphasize", false);'
        in inspector
    )
    assert (
        "function cleanPick(item) { return { cue_key: String(item.cue_key), word_index: Number(item.word_index), word: String(item.word) }; }"
        in inspector
    )
    assert '"aria-label": "Turn off the punch word \\u201c" + pick.word + "\\u201d"' in inspector


def test_punch_fields_exist_in_the_edit_spec() -> None:
    captions = edit_spec.default_edit()["captions"]
    assert captions["emphasize"] is False and captions["emphasis"] == []
    assert captions["emphasis_color"] == "#3CE88C"
    picks = [{"cue_key": "565", "word_index": 3, "word": "finished"}]
    assert edit_spec.validate_emphasis(picks) == picks
    # The page strips "why" before writing picks back; the validator keeps only the three keys.
    assert edit_spec.validate_emphasis([dict(picks[0], why="A reason")]) == picks


def test_the_live_preview_draws_the_punch_accent() -> None:
    preview = _read("preview.js")
    assert "if (captions.emphasize !== true) return [];" in preview
    assert "result && Array.isArray(result.emphasis)" in preview
    assert '"pv-word is-punch"' in preview
    assert 'root.style.setProperty("--cap-punch", captions.emphasis_color || "#3CE88C");' in preview
    css = _read("editor.css")
    punch = css.index(".pv-caption .pv-word.is-punch")
    # Declared after the spoken-word highlight, so the accent wins as it does in the render.
    assert punch > css.index(".is-karaoke .pv-word.is-said") and punch > css.index(".is-pop .pv-word.is-now")


# --- Verified find (C3) -------------------------------------------------------------------


def test_find_results_show_what_sam_saw() -> None:
    media = _read("mediabin.js")
    assert '"Seen by SAM 3.1" + (share ? " \\u00b7 " + share : "")' in media
    assert '"SAM 3.1 did not see it"' in media
    assert "SAM can miss stylised or unnamed objects" in media
    assert (
        "function unseen(moment) { return moment.verified === false || moment.verified !== true && moment.flagged === true; }"
        in media
    )
    assert "moment.verified === true" in media and "coverageText(moment.coverage)" in media
    # Spoken matches carry verified: null and get no badge.
    assert "    return null;\n  }\n  function verificationNote" in media
    assert "sameOriginArtifact(moment.thumbnail_url)" in media
    assert 'url.origin === location.origin && url.pathname.startsWith("/api/")' in media
    assert 'data.verification && typeof data.verification === "object"' in media
    assert "bin.find.verification && bin.find.verification.dropped" in media
    # Every row seeks: the thumbnail, the time, and Preview are all buttons.
    assert '"aria-label": "Preview the moment from " + when' in media
    assert 'class: "mb-time-button"' in media
    inspector_css = _read("inspector.css")
    assert (
        ".mb-seen.is-seen { border-color: var(--green-line); background: var(--green-wash); color: var(--green); }"
        in inspector_css
    )
    assert (
        ".mb-seen.is-unseen { border-color: var(--amber-line); background: var(--amber-wash); color: var(--amber); cursor: help; }"
        in inspector_css
    )


# --- Hero word (C5) -----------------------------------------------------------------------


def test_effects_tab_holds_the_hero_word() -> None:
    inspector = _read("inspector.js")
    assert '{ id: "effects", label: "Effects", icon: "effects", build: buildEffects },' in inspector
    assert 'Kit.api("/hero-word", { method: "POST", body, timeout: 120000 })' in inspector
    assert '"Suggest a hero word"' in inspector
    assert 'note("Muse Spark picks the word. SAM 3.1 cuts out your subject so the word sits behind it.")' in inspector
    assert 'text: "Apply and render"' in inspector and 'text: "Remove"' in inspector
    assert 'note("Appears in the render. The preview does not draw it.")' in inspector
    assert "hero.adjusted = adjustedNotes(data.adjusted)" in inspector
    assert "proposal.reason" in inspector
    assert "edit.hero_word = Object.assign({}, data.hero_word);" in inspector
    assert "edit.hero_word = null;" in inspector
    assert 'effects: "M8 2.5' in _read("mediabin.js")


def test_hero_controls_read_the_catalogue_the_server_publishes() -> None:
    choices = edit_spec.catalogue()["hero_word"]
    inspector = _read("inspector.js")
    for key in ("sides", "fonts", "colors", "min_seconds", "max_seconds", "max_chars", "max_words"):
        assert key in choices
        assert "choices." + key in inspector
    names = json.loads(re.search(r"const HERO_COLOR_NAMES = (\{.*?\});", inspector).group(1))
    assert set(names) == {color.upper() for color in choices["colors"]}
    assert edit_spec.default_edit()["hero_word"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_hero_word_check_in_the_page_matches_the_server() -> None:
    inspector = _read("inspector.js")
    pattern = re.search(r"const HERO_WORD = (/.*?/u);", inspector).group(1)
    samples = [
        "oak",
        "Oak!",
        "rock 'n",
        "well-made",
        "it’s",
        "año",
        "24",
        "two words",
        "oak_tree",
        "oak!!",
        "-oak",
        "oak bowl.",
        "<b>",
        "",
    ]
    script = (
        "const re = "
        + pattern
        + "; process.stdout.write(JSON.stringify("
        + json.dumps(samples)
        + ".map((s) => re.test(s))));"
    )
    page = json.loads(
        subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30).stdout
    )
    server = [bool(edit_spec.HERO_WORD.fullmatch(sample)) for sample in samples]
    assert page == server


# --- Layout and code health -----------------------------------------------------------------


def test_edit_workspace_cannot_grow_past_the_window() -> None:
    css = _read("editor.css")
    assert (
        'body[data-mode="edit"] { height: 100dvh; overflow: hidden; display: grid; grid-template-columns: minmax(0, 1fr);'
        in css
    )
    settings_css = _read("settings.css")
    assert "@media (max-width: 1439.98px)" in settings_css and ".np-more { display: none; }" in settings_css
    page = _read("index.html")
    assert (
        '<button id="new-project" class="button button-quiet" type="button" aria-label="New project">New<span class="np-more">&nbsp;project</span></button>'
        in page
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("name", sorted(path.name for path in STATIC.glob("*.js")))
def test_studio_scripts_parse(name: str) -> None:
    result = subprocess.run(
        ["node", "--check", str(STATIC / name)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr


def test_new_copy_uses_plain_sentences_without_em_dashes() -> None:
    copy = [
        "Muse Spark picks the word that carries each line.",
        "Muse Spark picks the word. SAM 3.1 cuts out your subject so the word sits behind it.",
        "SAM 3.1 looked for it and did not find it. SAM can miss stylised or unnamed objects, so preview it to check.",
        "Muse Spark watches the cut and reads the captions. Choose Muse Wonder when the sound matters.",
        "Appears in the render. The preview does not draw it.",
    ]
    sources = _read("inspector.js") + _read("mediabin.js") + _read("studio.js") + _read("index.html")
    for line in copy:
        assert line in sources
        assert "—" not in line
    for name in ("inspector.js", "mediabin.js", "studio.js", "index.html"):
        text = _read(name)
        for marker in ("Punch words", "Hero word", "SAM 3.1 did not see", "Muse Wonder", "Reviewer"):
            for match in re.finditer(re.escape(marker), text):
                line = text[text.rfind("\n", 0, match.start()) + 1 : text.find("\n", match.end())]
                assert "—" not in line, (name, line[:120])


# --- 3D title card (C6) -------------------------------------------------------------------


def test_title_card_section_designs_edits_and_renders() -> None:
    inspector = _read("inspector.js")
    assert 'Kit.api("/motion-card", { method: "POST", body, timeout: 120000 })' in inspector
    assert 'const body = { kind: card.kind, backdrop: card.kind !== "lower_third" && card.backdrop };' in inspector
    assert (
        'note("Muse Spark designs it. Muse Image paints the backdrop. Blender renders it on this computer.")'
        in inspector
    )
    assert 'el("span", { text: "Design it" })' in inspector
    assert (
        'note("The first render takes about 25 to 60 s while Blender works. Later renders of the same card reuse it.")'
        in inspector
    )
    assert (
        "edit.motion_card = Object.assign({}, data.motion_card);" in inspector
        and "edit.motion_card = null;" in inspector
    )
    assert "card.poster = sameOrigin(data.poster);" in inspector
    assert "if (data.backdrop && data.backdrop.id) Kit.addAsset(data.backdrop);" in inspector
    for field in ('"style", "Title card style"', '"camera", "Title card camera"'):
        assert field in inspector
    assert "panel.body.append(heroWord(panel), titleCard(panel));" in inspector
    # The whole section hides where Blender is missing; Settings says why.
    assert "function cardsHere() { return Kit.caps().motion_cards === true && !!cardChoices(); }" in inspector
    assert "section.hidden = !cardsHere();" in inspector
    settings = _read("settings.js")
    assert '$("bc-blender-note").hidden = caps.motion_cards !== false;' in settings
    assert "Needs Blender for 3D title cards." in settings


def test_title_card_controls_read_the_catalogue_the_server_publishes() -> None:
    choices = edit_spec.catalogue()["motion_card"]
    inspector = _read("inspector.js")
    for key in ("kinds", "styles", "cameras", "palettes", "min_seconds", "max_seconds", "max_chars", "subtitle_chars"):
        assert key in choices
        assert "choices." + key in inspector
    assert {row["id"] for row in choices["styles"]} == {
        "brushed_metal",
        "warm_wood",
        "soft_clay",
        "neon_glass",
        "paper_cut",
    }
    assert {row["id"] for row in choices["cameras"]} == {"dolly_in", "orbit", "rise", "push_tilt"}
    assert len(choices["palettes"]) == 10
    assert f"const CARD_MAX_WORDS = {edit_spec.MOTION_CARD_MAX_WORDS};" in inspector
    assert edit_spec.default_edit()["motion_card"] is None


def test_suggestion_warnings_are_shown_with_the_notes() -> None:
    inspector = _read("inspector.js")
    assert "hero.adjusted = adjustedNotes(data.adjusted).concat(adjustedNotes(data.warnings));" in inspector
    assert "card.notes = adjustedNotes(data.notes).concat(adjustedNotes(data.warnings));" in inspector


# --- Private mode (Muse Glimmer 30B on this computer) -----------------------------------


def test_private_mode_toggle_saves_the_setting_and_shows_what_runs() -> None:
    settings = _read("settings.js")
    assert (
        'form.private = h("input", { type: "checkbox", id: "bc-private", "aria-describedby": "bc-private-note" });'
        in settings
    )
    assert (
        "Run director\\u2019s notes, caption translation and script scenes on Muse Glimmer 30B, Meta\\u2019s open-weight model, on this computer. No Meta key needed for those steps. Needs the local Glimmer server ("
        in settings
    )
    assert 'h("code", { text: "uv run python -m vedit.open_weights serve" })' in settings
    assert 'private_model: form.private.checked ? "glimmer" : ""' in settings
    assert 'form.private.checked = state.private_model === "glimmer";' in settings
    assert 'if (state && state.private_model_source === "environment") delete payload.private_model;' in settings
    # The live state comes from capabilities, not from the saved box.
    assert "caps.meta && caps.meta.private" in settings and "live.enabled" in settings and "live.runs_on" in settings
