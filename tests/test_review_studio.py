"""Review fixes for Studio: each test failed before its fix.

The JavaScript tests lift single functions out of the page scripts and run them in
node against a small DOM stand-in, so they check behaviour, not spelling.
"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from vedit.studio.server import StudioHandler, _LoopbackServer

STATIC = Path(__file__).resolve().parents[1] / "src" / "vedit" / "studio" / "static"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    """The full text of ``function name(...) { ... }``, matching braces outside strings."""
    start = source.index("function " + name + "(")
    if source[max(0, start - 6) : start] == "async ":
        start -= 6
    depth, at, quote = 0, source.index("{", source.index(")", start)), ""
    while True:
        char = source[at]
        if quote:
            if char == "\\":
                at += 1
            elif char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : at + 1]
        at += 1


def _const(source: str, name: str) -> str:
    return re.search(r"^\s*(const " + re.escape(name) + r" = .*;)\s*$", source, re.M).group(1)


# A DOM just big enough for the painters: elements, text nodes, attributes.
DOM = r"""
class Text { constructor(t) { this.nodeType = 3; this.data = String(t); } get textContent() { return this.data; } }
class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.nodeType = 1; this.className = ""; this.children = []; this.attrs = {}; this.hidden = false; this.listeners = {}; this.dataset = {}; }
  append(...items) { items.forEach((item) => this.children.push(item instanceof Node || item instanceof Text ? item : new Text(item))); }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.children = v === "" || v === null || v === undefined ? [] : [new Text(v)]; }
  find(cls) { const out = []; const walk = (n) => { if (n instanceof Node) { if (n.className.split(" ").includes(cls)) out.push(n); n.children.forEach(walk); } }; walk(this); return out; }
}
const ids = {};
const document = { createElement: (t) => new Node(t), createTextNode: (t) => new Text(t), getElementById: (id) => ids[id] || (ids[id] = new Node("div")) };
const $ = (id) => document.getElementById(id);
"""


def _node(script: str) -> object:
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- Receipts: advisory notes are not checks ------------------------------------------------


def _receipts_script(body: str) -> str:
    studio = _read("studio.js")
    parts = [DOM]
    parts += [_const(studio, name) for name in ("checkBases", "basisLabels", "instrumentNames")]
    parts += [
        _function(studio, name)
        for name in (
            "element",
            "instrumentName",
            "basisTitle",
            "basisBreakdown",
            "normalizeChecks",
            "countChecks",
            "clockText",
            "paintChecks",
        )
    ]
    parts.append(
        "const finite = (v, f) => { const n = Number(v); return Number.isFinite(n) ? n : f; }; const jumpToFinal = () => {}; const describePatch = (p) => p.op;"
    )
    parts.append(body)
    return "\n".join(parts)


RECEIPTS = [
    {
        "name": "auto: duration matches the target",
        "passed": True,
        "basis": "derived",
        "by": "lint",
        "advisory": True,
        "note": "The cut runs 10.0 s, 40% of the 25.0 s target. The target is a ceiling, so this is not a failure.",
    },
    {"name": "auto: captions cover the speech", "passed": True, "basis": "derived", "by": "lint"},
    {"name": "director: hook lands", "passed": False, "basis": "inferred", "by": "muse-spark"},
]


@needs_node
def test_normalize_keeps_advisory_and_counting_skips_it() -> None:
    out = _node(
        _receipts_script(
            "const checks = normalizeChecks(" + json.dumps(RECEIPTS) + ");"
            "process.stdout.write(JSON.stringify({ advisory: checks.map((c) => c.advisory), count: countChecks(checks, null, null), basis: basisBreakdown(checks) }));"
        )
    )
    assert out["advisory"] == [True, False, False]
    # One real pass, one real fail. The note is neither.
    assert out["count"] == {"passed": 1, "total": 2}
    assert out["basis"] == "1 measured, 1 model opinion"


@needs_node
def test_an_advisory_receipt_paints_as_a_note() -> None:
    out = _node(
        _receipts_script(
            "const checks = normalizeChecks(" + json.dumps(RECEIPTS) + ");"
            "const count = countChecks(checks, null, null);"
            "paintChecks({ revision: 2, checks, passed: count.passed, total: count.total }, []);"
            "const items = $('checks-list').children;"
            "process.stdout.write(JSON.stringify({ summary: $('checks-summary').textContent,"
            "  classes: items.map((li) => li.className), glyphs: items.map((li) => li.find('check-glyph')[0].textContent),"
            "  sr: items.map((li) => li.find('sr').map((n) => n.textContent).join('|')), names: items.map((li) => li.find('check-name')[0].textContent), text: items.map((li) => li.textContent) }));"
        )
    )
    assert out["classes"][0] == "check is-note"
    assert out["glyphs"] == ["i", "✓", "✕"]
    assert "Passed" not in out["sr"][0] and "Failed" not in out["sr"][0]
    assert out["names"][0].startswith("Note: auto: duration")
    assert "not a failure" in out["text"][0]
    assert out["sr"][1].startswith("Passed: ") and out["sr"][2].startswith("Failed: ")
    assert out["summary"].startswith("1 of 2 passed")
    assert "1 note" in out["summary"]


def test_a_note_has_its_own_quiet_style() -> None:
    css = _read("studio.css")
    assert ".check.is-note .check-glyph" in css
    assert ".check-advisory" in css


# --- Find: a search with nothing for SAM to check ---------------------------------------------


@needs_node
def test_a_find_without_verification_leaves_no_null_text() -> None:
    # verificationNote answers null when SAM checked nothing (a spoken match). The panel must not print "null".
    mediabin = _read("mediabin.js")
    out = _node(
        DOM
        + _function(mediabin, "verificationNote")
        + "\nprocess.stdout.write(JSON.stringify(verificationNote(null)));"
    )
    assert out is None
    paint = _function(mediabin, "paintFind")
    assert "nodes.find.replaceChildren(...rows.filter(Boolean))" in paint


# --- Server: headers on the page itself -------------------------------------------------------


@pytest.fixture()
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    httpd = _LoopbackServer(("127.0.0.1", 0), StudioHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(port: int, path: str, headers: dict[str, str] | None = None) -> http.client.HTTPResponse:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    response = connection.getresponse()
    response.read()
    connection.close()
    return response


def test_the_editor_page_cannot_be_framed_by_another_site(studio: int) -> None:
    # Inside a frame every request is same-origin, so a framing page could click
    # "Make the video" or "Delete" for the person. The page must refuse to be framed.
    for path in ("/", "/studio.js", "/static/studio.css"):
        response = _get(studio, path)
        assert response.status == 200
        assert response.getheader("X-Frame-Options") == "DENY"
        assert "frame-ancestors 'none'" in (response.getheader("Content-Security-Policy") or "")
        assert response.getheader("X-Content-Type-Options") == "nosniff"


def test_the_page_names_an_icon_so_the_browser_logs_no_404() -> None:
    page = _read("index.html")
    assert re.search(r'<link rel="icon" href="data:image/svg\+xml,[^"]+">', page)


# --- Server: the routes added today keep the same boundaries ----------------------------------


@pytest.fixture()
def project_studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    from vedit.campaign import get_manager

    manager = get_manager()
    httpd = _LoopbackServer(("127.0.0.1", 0), StudioHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    ident = "e" * 32
    now = "2026-11-04T18:00:00+00:00"
    job = {
        "id": ident,
        "status": "complete",
        "stage": "complete",
        "revision": 1,
        "request": {"goal": "A bowl", "budget_usd": 0.5},
        "created_at": now,
        "updated_at": now,
    }
    with manager._connect() as connection:
        connection.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?)", (ident, "fingerprint", "complete", now, now, json.dumps(job))
        )
    (manager.root / ident).mkdir()
    try:
        yield int(httpd.server_address[1]), manager, ident
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        manager.close()


def _call(
    port: int, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        response = connection.getresponse()
        raw = response.read()
        try:
            return response.status, json.loads(raw) if raw else {}
        except ValueError:
            return response.status, {"raw": raw[:40]}
    finally:
        connection.close()


NEW_POSTS = ("hero-word", "motion-card", "find")


@pytest.mark.parametrize("action", NEW_POSTS)
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Host": "evil.example"},
    ],
)
def test_new_suggestion_routes_refuse_other_sites(project_studio, action: str, headers: dict[str, str]) -> None:
    port, _, ident = project_studio
    status, data = _call(
        port, "POST", f"/api/campaigns/{ident}/{action}", b"{}", {"Content-Type": "application/json", **headers}
    )
    assert status == 403 and data["ok"] is False


@pytest.mark.parametrize("action", NEW_POSTS)
def test_new_suggestion_routes_take_only_json(project_studio, action: str) -> None:
    # A form or text/plain POST is CORS-simple: it must never reach a paid call.
    port, _, ident = project_studio
    status, data = _call(port, "POST", f"/api/campaigns/{ident}/{action}", b"query=x", {"Content-Type": "text/plain"})
    assert status == 415 and data["ok"] is False


@pytest.mark.parametrize("action", ("hero-word", "motion-card"))
def test_a_suggestion_without_meta_says_what_is_missing(project_studio, action: str) -> None:
    port, _, ident = project_studio
    status, data = _call(port, "POST", f"/api/campaigns/{ident}/{action}", b"{}", {"Content-Type": "application/json"})
    assert status == 409 and data["ok"] is False
    assert "Restart" not in data["error"]


@pytest.mark.parametrize("action", ("find-thumbnail", "meta-calls"))
def test_new_read_routes_refuse_other_sites(project_studio, action: str) -> None:
    port, _, ident = project_studio
    status, data = _call(
        port, "GET", f"/api/campaigns/{ident}/{action}?name=a.jpg", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert status == 403 and data["ok"] is False


@pytest.mark.parametrize(
    "name", ["../job.json", "..%2Fjob.json", "a/b.jpg", "/etc/hosts", "A.JPG", "x.jpg.html", "", "x.png"]
)
def test_find_thumbnail_answers_only_its_own_folder(project_studio, name: str) -> None:
    port, _, ident = project_studio
    for method in ("GET", "HEAD"):
        status, _ = _call(port, method, f"/api/campaigns/{ident}/find-thumbnail?name={name}")
        assert status in {400, 404}, (method, name, status)


def test_find_thumbnail_does_not_follow_a_symlink_out_of_the_project(project_studio, tmp_path: Path) -> None:
    port, manager, ident = project_studio
    folder = manager.root / ident / "meta" / "verified-find"
    folder.mkdir(parents=True)
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"\xff\xd8secret")
    (folder / "leak.jpg").symlink_to(secret)
    (folder / "real.jpg").write_bytes(b"\xff\xd8\xff\xe0real")
    status, _ = _call(port, "GET", f"/api/campaigns/{ident}/find-thumbnail?name=leak.jpg")
    assert status == 404
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(
        "GET", f"/api/campaigns/{ident}/find-thumbnail?name=real.jpg", headers={"Host": f"127.0.0.1:{port}"}
    )
    response = connection.getresponse()
    assert response.status == 200 and response.read().endswith(b"real")
    assert (
        response.getheader("Content-Type") == "image/jpeg" and response.getheader("X-Content-Type-Options") == "nosniff"
    )
    connection.close()


def test_the_effects_tab_fits_the_1280_inspector() -> None:
    # At 1280 px the inspector body is about 200 px wide. Two action buttons side by side
    # (215 px) widened the panel and pushed "Remove" and the end time off the screen.
    css = _read("inspector.css")
    assert ".ix-panel-body, .ix-group, .ix-hero-edit { grid-template-columns: minmax(0, 1fr); }" in css
    actions = re.search(r"\.ix-hero-actions \{[^}]*\}", css).group(0)
    assert "flex-wrap: wrap" in actions
    assert ".ix-hero-reason, .ix-hero-adjusted { overflow-wrap: anywhere; }" in css


# --- Server: the JSON body parser -------------------------------------------------------------


@pytest.mark.parametrize("body", [b"\xff", b"[" * 200_000, b'{"goal": '])
@pytest.mark.parametrize("route", ["/api/run", "/api/plan", "/api/campaigns", "/api/settings", "/api/projects/cleanup"])
def test_a_malformed_body_is_a_400_not_a_crash(studio: int, route: str, body: bytes) -> None:
    # Bad UTF-8 or nesting deeper than Python's recursion limit dropped the connection
    # (/api/run) or answered 500 "restart the editor" (/api/campaigns, /api/settings).
    connection = http.client.HTTPConnection("127.0.0.1", studio, timeout=10)
    connection.request(
        "POST", route, body=body, headers={"Host": f"127.0.0.1:{studio}", "Content-Type": "application/json"}
    )
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()
    assert response.status == 400, (route, data)
    assert data["ok"] is False and "Restart" not in data["error"]


# --- Server: a picture never runs as a page ---------------------------------------------------


def test_an_svg_asset_downloads_instead_of_running_in_the_editor(project_studio) -> None:
    # SVG is a document that can run script. Shown inline it would run with the
    # editor's origin, so it could call every route as the person.
    port, manager, ident = project_studio
    folder = manager.root / ident / "assets"
    folder.mkdir()
    picture = folder / "a1.svg"
    picture.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding="utf-8")
    plain = folder / "a2.png"
    plain.write_bytes(b"\x89PNG\r\n\x1a\nrest")
    with manager._connect() as connection:
        row = connection.execute("SELECT data FROM jobs WHERE id = ?", (ident,)).fetchone()
        job = json.loads(row[0])
        job["assets"] = {
            "a1": {"id": "a1", "kind": "image", "path": str(picture)},
            "a2": {"id": "a2", "kind": "image", "path": str(plain)},
        }
        connection.execute("UPDATE jobs SET data = ? WHERE id = ?", (json.dumps(job), ident))
    for asset, inline in (("a1", False), ("a2", True)):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", f"/api/campaigns/{ident}/assets?id={asset}", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 200
        if inline:
            assert (
                response.getheader("Content-Type") == "image/png" and response.getheader("Content-Disposition") is None
            )
        else:
            assert response.getheader("Content-Type") == "application/octet-stream"
            assert (response.getheader("Content-Disposition") or "").startswith("attachment")


# --- Hero word: the "Behind" field is what SAM looks for ---------------------------------------


def _hero_script(body: str) -> str:
    inspector = _read("inspector.js")
    parts = [
        DOM,
        "const state = { hero: { busy: false, error: '', job: '', proposal: null, adjusted: [], subject: null, wordError: '' } };",
    ]
    parts.append(
        "let project = null, view = {}, sent = []; const paintPanels = () => {}; const clock = (s) => String(s); const heroBlock = () => '';"
    )
    parts.append(
        "const Kit = { job: () => project, view: () => view, api: async (path, options) => { sent.push([path, options.body]); return { hero_word: { word: 'Oak', start: 1, end: 2 } }; }, edit: () => true, announce: () => {} };"
    )
    parts += [_function(inspector, name) for name in ("adjustedNotes", "heroState", "heroSubject", "suggestHero")]
    parts.append("(async () => {" + body + "})();")
    return "\n".join(parts)


@needs_node
def test_suggest_sends_the_project_subject_from_the_behind_field() -> None:
    out = _node(
        _hero_script(
            "project = { id: 'a', request: { subject: 'the oak bowl' } };"
            "await suggestHero();"
            # The person types a different subject: that is what goes.
            "heroState().subject = '  the lathe  ';"
            "await suggestHero();"
            "process.stdout.write(JSON.stringify(sent));"
        )
    )
    assert out == [["/hero-word", {"subject": "the oak bowl"}], ["/hero-word", {"subject": "the lathe"}]]


@needs_node
def test_suggest_sends_nothing_without_a_subject() -> None:
    # The server used to guess "the speaker": on footage with no person SAM was billed and the word dropped.
    out = _node(
        _hero_script(
            "project = { id: 'b', request: { subject: '' } };"
            "await suggestHero();"
            "project = { id: 'c', request: { subject: 'the bowl' } }; heroState().subject = '   ';"
            "await suggestHero();"
            "process.stdout.write(JSON.stringify(sent));"
        )
    )
    assert out == []


def test_the_behind_field_says_what_it_needs() -> None:
    inspector = _read("inspector.js")
    panel = _function(inspector, "heroWord")
    assert '"What the word sits behind, e.g. the person in the red jacket"' in panel
    assert "!heroSubject()" in panel  # the button waits for a subject
    assert "Say what the word sits behind first." in panel
    assert "The person speaking" not in panel


def test_a_project_thumbnail_never_serves_svg_inline(tmp_path) -> None:
    """A project thumbnail is served as a picture only when it cannot run script."""
    from vedit.studio import product

    sent: dict[str, str] = {}

    class Handler:
        def _serve_path(self, target, *, send_body, content_type):
            sent["type"] = content_type

    svg = tmp_path / "thumb.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding="utf-8")
    product._serve_thumbnail(Handler(), svg)
    assert sent["type"] == "application/octet-stream"
    jpg = tmp_path / "thumb.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0")
    product._serve_thumbnail(Handler(), jpg)
    assert sent["type"] == "image/jpeg"
