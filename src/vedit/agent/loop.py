"""Run one editorial session: inventory, decide, render, critique, revise."""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from vedit.agent.critique import critique_output
from vedit.agent.decide import decide
from vedit.agent.explain import drop_captions, explain_decisions, local_transcriber_ready
from vedit.agent.intent import parse_intent
from vedit.agent.inventory import inventory_paths
from vedit.agent.model import Critique, Decision, Finding, Inventory, Intent, Session
from vedit.agent.revise import revise_brief
from vedit.compile import compile_brief
from vedit.errors import ProbeError, VeditError
from vedit.ir import Brief, brief_dumps
from vedit.media import probe, trace
from vedit.pipeline import execute, write_codegen, write_report

_SLUG_WORD = re.compile(r"[a-z0-9]+")
_LEAD = "\"'`([{<“”‘’"
_TAIL = "\"'`)]}>“”‘’,;:!?"
_MEDIA_SUFFIXES = (
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".m4v",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
)


def run_session(
    goal: str,
    paths: list[str],
    *,
    root: Path | None = None,
    render: bool = True,
    analyze: bool = True,
    hw: bool | None = None,
    max_revisions: int = 1,
) -> Session:
    root = _session_dir(root, goal)
    media = [str(item) for item in paths]
    if not media:
        media = _paths_from_goal(goal)
    if not media:
        session = _missing_media(root, goal)
        save_session(session)
        return session

    trace("inventory-enter")
    inventory = inventory_paths(media, analyze=analyze)
    trace("inventory-exit")
    intent = parse_intent(goal, sources={shot.name: shot.path for shot in inventory.shots})
    trace("intent-exit")
    decision = decide(intent, inventory)
    trace("decide-exit")
    if hw is not None:
        decision.brief.hw = hw
    decision, captions_skipped, decisions = _settle_captions(intent, decision, inventory)
    trace("captions-exit")
    _write_plan(root, intent, inventory, decision, decisions)
    trace("plan-written")

    work = root / "work"
    revision = 0
    critique: Critique | None = None
    primary: str | None = None
    proof: dict[str, Any] | None = None
    try:
        compiler = _stage(decision.brief, work)
        work = compiler.work_dir
        if not render:
            session = Session(
                root=root,
                goal=goal,
                intent=intent,
                inventory=inventory,
                decision=decision,
                critique=None,
                work_dir=work,
                status="planned",
                revision=0,
                primary_output=None,
                captions_skipped=captions_skipped,
                decisions=decisions,
                proof=None,
            )
            save_session(session)
            return session
        critique, primary, proof = _render_and_judge(
            compiler, intent, enforce_captions=not captions_skipped
        )
        while not critique.ok and revision < max_revisions:
            revision += 1
            new_brief, notes = revise_brief(decision.brief, critique, intent)
            if captions_skipped:
                new_brief, _dropped = drop_captions(new_brief)
            if hw is not None:
                new_brief.hw = hw
            note_lines = _note_lines(notes)
            _append_lines(root / "decisions.md", note_lines)
            decision = Decision(
                rationale=tuple(decision.rationale) + note_lines,
                risks=decision.risks,
                brief=new_brief,
            )
            decisions = explain_decisions(
                intent, decision, inventory, captions_skipped=captions_skipped
            )
            compiler = _stage(decision.brief, root / f"rev{revision}")
            work = compiler.work_dir
            critique, primary, proof = _render_and_judge(
                compiler, intent, enforce_captions=not captions_skipped
            )
        status = "verified" if critique.ok else "failed"
    except VeditError as exc:
        session = Session(
            root=root,
            goal=goal,
            intent=intent,
            inventory=inventory,
            decision=decision,
            critique=_render_failed(exc),
            work_dir=work,
            status="failed",
            revision=revision,
            primary_output=None,
            captions_skipped=captions_skipped,
            decisions=decisions,
            proof=None,
        )
        save_session(session)
        return session

    session = Session(
        root=root,
        goal=goal,
        intent=intent,
        inventory=inventory,
        decision=decision,
        critique=critique,
        work_dir=work,
        status=status,
        revision=revision,
        primary_output=primary,
        captions_skipped=captions_skipped,
        decisions=decisions,
        proof=proof,
    )
    save_session(session)
    return session


def save_session(session: Session) -> Path:
    root = Path(session.root)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / "session.json"
    _write_json(dest, session.to_dict())
    _write_json(root / "intent.json", session.intent.to_dict())
    _write_json(root / "inventory.json", session.inventory.to_dict())
    if session.decision is not None:
        (root / "brief.yaml").write_text(brief_dumps(session.decision.brief), encoding="utf-8")
    return dest


def load_session(root: Path) -> Session:
    data = json.loads((Path(root) / "session.json").read_text(encoding="utf-8"))
    return Session.from_dict(data)


def _session_dir(root: Path | None, goal: str) -> Path:
    if root is None:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        parent = Path.cwd() / "vedit-sessions"
        parent.mkdir(parents=True, exist_ok=True)
        # Atomic allocation keeps simultaneous plans and renders independent.
        path = Path(tempfile.mkdtemp(prefix=f"{_slug(goal)}-{stamp}-", dir=parent))
    else:
        path = Path(root).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(goal: str) -> str:
    words = _SLUG_WORD.findall(goal.lower())
    return "-".join(words[:4]) or "edit"


def _clean_token(raw: str) -> str:
    token = raw
    while token:
        nxt = token.lstrip(_LEAD).rstrip(_TAIL)
        if nxt.endswith(".") and not nxt.endswith(".."):
            nxt = nxt[:-1]
        if nxt == token:
            return token
        token = nxt
    return token


def _paths_from_goal(goal: str) -> list[str]:
    found: list[str] = []
    for raw in goal.split():
        token = _clean_token(raw)
        if not token:
            continue
        if "/" in token or token.lower().endswith(_MEDIA_SUFFIXES):
            found.append(token)
    return found


def _missing_media(root: Path, goal: str) -> Session:
    return Session(
        root=root,
        goal=goal,
        intent=parse_intent(goal),
        inventory=Inventory(shots=(), primary="", notes=("no media paths",)),
        decision=None,
        critique=Critique(
            ok=False,
            findings=(
                Finding(
                    code="missing_output",
                    severity="fail",
                    message="no media paths",
                    fix="Pass a media path or include one in the goal.",
                ),
            ),
        ),
        work_dir=None,
        status="failed",
        revision=0,
        primary_output=None,
    )


def _settle_captions(
    intent: Intent,
    decision: Decision,
    inventory: Inventory,
) -> tuple[Decision, bool, dict[str, Any]]:
    skipped = bool(intent.captions) and not local_transcriber_ready()
    if skipped:
        brief, _dropped = drop_captions(decision.brief)
        decision = Decision(rationale=decision.rationale, risks=decision.risks, brief=brief)
    explained = explain_decisions(intent, decision, inventory, captions_skipped=skipped)
    return decision, skipped, explained


def _write_plan(
    root: Path,
    intent: Any,
    inventory: Inventory,
    decision: Decision,
    decisions: dict[str, Any] | None = None,
) -> None:
    _write_json(root / "intent.json", intent.to_dict())
    _write_json(root / "inventory.json", inventory.to_dict())
    (root / "brief.yaml").write_text(brief_dumps(decision.brief), encoding="utf-8")
    (root / "decisions.md").write_text(_decisions_text(decision, decisions), encoding="utf-8")


def _stage(brief: Brief, work: Path) -> Any:
    compiler = compile_brief(brief, work_dir=work)
    write_codegen(compiler.pipeline)
    (compiler.work_dir / "brief.yaml").write_text(brief_dumps(brief), encoding="utf-8")
    return compiler


def _render_and_judge(
    compiler: Any,
    intent: Any,
    *,
    enforce_captions: bool = True,
) -> tuple[Critique, str | None, dict[str, Any] | None]:
    reports = execute(compiler.pipeline, progress=False)
    write_report(compiler.pipeline, reports, finals=compiler.finals)
    primary = _choose_primary(compiler.finals)
    if primary is None:
        return _missing_output("no output file"), None, None
    critique = critique_output(
        primary,
        intent,
        work_dir=compiler.work_dir,
        measure=False,
        enforce_captions=enforce_captions,
    )
    return critique, primary, _proof_for(primary)


def _proof_for(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        info = probe(path)
    except ProbeError:
        return None
    return {
        "path": str(info.path),
        "duration": info.duration,
        "width": info.width,
        "height": info.height,
        "fps": info.fps,
        "has_audio": bool(info.has_audio),
        "has_video": bool(info.has_video),
    }


def _choose_primary(finals: list[Path]) -> str | None:
    if not finals:
        return None
    for path in finals:
        if Path(path).suffix.lower() != ".mp3":
            return str(path)
    return str(finals[0])


def _missing_output(message: str) -> Critique:
    return Critique(
        ok=False,
        findings=(
            Finding(
                code="missing_output",
                severity="fail",
                message=message,
                fix="Add an export op and render again so the output file exists.",
            ),
        ),
    )


def _render_failed(exc: VeditError) -> Critique:
    return Critique(
        ok=False,
        findings=(
            Finding(
                code="render_failed",
                severity="fail",
                message=str(exc),
                fix="read project.json and the step stderr",
            ),
        ),
    )


def _note_lines(notes: Any) -> tuple[str, ...]:
    if notes is None:
        return ()
    if isinstance(notes, str):
        return (notes,) if notes else ()
    return tuple(str(item) for item in notes)


def _lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in value]


def _decisions_text(decision: Decision, explained: dict[str, Any] | None = None) -> str:
    head = ""
    if explained:
        sentences = [str(item) for item in explained.get("sentences") or [] if item]
        if sentences:
            head = "\n".join(sentences) + "\n\n"
    rationale = _lines(decision.rationale)
    risks = _lines(decision.risks)
    blocks = [block for block in ("\n".join(rationale), "\n".join(risks)) if block]
    body = ("\n\n".join(blocks) + "\n") if blocks else ""
    return head + body


def _append_lines(path: Path, lines: tuple[str, ...]) -> None:
    if not lines:
        return
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    if existing and not existing.endswith("\n\n"):
        existing += "\n"
    path.write_text(existing + "\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
