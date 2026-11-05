"""Receipts anyone can check: each claim about a render gets a content id.

The idea is REA's evidence model (MIT): an evidence id is ``"ev_"`` plus the
sha256 of the record's canonical JSON, so changing a record changes its id.
Briefcut gives every check, the probe, the plan, the Meta calls, the versions
and the limitations their own id. A root id covers the delivered file's hash
and every record id, in order. ``verify`` re-hashes the file and each record
and names exactly what differs. It reads only local files and calls no network.

The ids prove that nothing changed since Briefcut wrote them. They are not a
signature: someone who edits a record and recomputes every id makes a new,
consistent set. Compare the root id with one you got from a trusted place
(Studio, the job, or the person who sent the file) to rule that out.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import unicodedata
import zipfile
import zlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

FORMAT = "briefcut.receipts"
FORMAT_VERSION = 1
RECEIPTS = "receipts.json"
PLACES = 6  # floats are rounded to 6 decimal places, so 0.1 + 0.2 and 0.3 share an id
_CHUNK = 1 << 20
# Records in root order. Checks follow, in the order they were graded.
SECTIONS = ("video", "probe", "plan", "meta_calls", "versions", "files", "limitations")
_CHECK_FIELDS = ("name", "passed", "at", "note", "basis", "by", "inputs")
# ``verify(expect=...)`` takes a prefix of the root id, so a person can read one off a
# screen. Below 16 hex digits (64 bits) a prefix is short enough to forge by brute force.
EXPECT_MIN_HEX = 16
_UNREADABLE = (UnicodeDecodeError, zipfile.BadZipFile, zlib.error, OSError)


# ---------------------------------------------------------------------------
# Canonical form and ids
# ---------------------------------------------------------------------------

def _normal(value: Any) -> Any:
    """JSON values in one spelling: NFC text, rounded floats, whole floats as ints."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None  # JSON has no NaN or infinity; a missing measurement reads as null
        rounded = round(value, PLACES)
        if rounded.is_integer():
            return int(rounded)  # 2.0 and 2 are the same number, and -0.0 is 0
        return rounded
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"Receipt keys must be text, not {type(key).__name__}.")
            name = unicodedata.normalize("NFC", key)
            if name in out:
                raise ValueError(f"Two receipt keys read the same after normalising: {name!r}.")
            out[name] = _normal(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_normal(item) for item in value]
    raise TypeError(f"A receipt holds JSON values only, not {type(value).__name__}.")


def canonical(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, NFC strings, floats rounded to 6 places."""
    return json.dumps(_normal(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(obj: Any) -> str:
    """sha256 hex of the canonical JSON."""
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def evidence_id(record: dict[str, Any]) -> str:
    """``"ev_"`` + sha256 of the record's canonical JSON, without its own ``evidence_id``."""
    return "ev_" + digest({key: value for key, value in record.items() if key != "evidence_id"})


def seal(record: dict[str, Any]) -> dict[str, Any]:
    """The record in canonical values, with its id."""
    body = _normal({key: value for key, value in record.items() if key != "evidence_id"})
    return {**body, "evidence_id": evidence_id(body)}


def root_id(receipts: dict[str, Any]) -> str:
    """The id over the file hash and every record id, in order: video, probe, ..., then each check."""
    video = receipts.get("video") if isinstance(receipts.get("video"), dict) else {}
    ids = [(receipts.get(name) or {}).get("evidence_id") for name in SECTIONS]
    ids += [check.get("evidence_id") if isinstance(check, dict) else None for check in receipts.get("checks") or []]
    return "ev_" + digest({
        "format": receipts.get("format"), "v": receipts.get("v"), "created_at": receipts.get("created_at"),
        "video_sha256": video.get("sha256"), "video_size_bytes": video.get("size_bytes"),
        "passed": receipts.get("passed"), "total": receipts.get("total"), "records": ids,
    })


def short_id(ident: str, length: int = EXPECT_MIN_HEX) -> str:
    """``ev_`` and the first hex digits, for a label.

    The default is the shortest prefix ``verify(expect=...)`` trusts, so any id
    shown this way can be copied straight into ``vedit verify --expect``.
    """
    return ident[: 3 + length] if ident.startswith("ev_") else ident[:length]


def _stream_sha256(handle: Any) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    while chunk := handle.read(_CHUNK):
        hasher.update(chunk)
        size += len(chunk)
    return hasher.hexdigest(), size


def file_sha256(path: Path | str) -> tuple[str, int]:
    """sha256 hex and byte count of a file, read in 1 MB pieces."""
    with open(path, "rb") as handle:
        return _stream_sha256(handle)


# ---------------------------------------------------------------------------
# Building receipts
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _ffmpeg_version() -> str | None:
    try:
        from vedit.media import version_line

        return version_line()
    except Exception:  # receipts must not fail a render that already passed
        return None


def _probe_record(proof: dict[str, Any], info: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": "probe",
        "duration": proof.get("duration"), "width": proof.get("width"), "height": proof.get("height"),
        "fps": proof.get("fps"), "has_audio": proof.get("has_audio"), "has_video": proof.get("has_video"),
        "size_bytes": proof.get("size_bytes"),
        "color": {"space": proof.get("color_space"), "range": proof.get("color_range"),
                  "primaries": proof.get("color_primaries"), "transfer": proof.get("color_transfer")},
        "format": getattr(info, "format_name", None),
        "streams": [],
    }
    for stream in getattr(info, "streams", ()) or ():
        record["streams"].append({
            "index": stream.index, "codec_type": stream.codec_type, "codec_name": stream.codec_name,
            "width": stream.width, "height": stream.height, "fps": stream.fps, "duration": stream.duration,
            "sample_rate": stream.sample_rate, "channels": stream.channels, "pix_fmt": stream.pix_fmt,
        })
    return record


def _plan_record(plan: dict[str, Any] | None, result: dict[str, Any], note: str | None) -> dict[str, Any]:
    plan = plan if isinstance(plan, dict) else {}
    segments = [[segment.get("start"), segment.get("end")] for segment in plan.get("segments") or [] if isinstance(segment, dict)]
    return {
        "kind": "plan",
        "edit_sha256": digest(plan.get("edit")) if plan.get("edit") is not None else None,
        "segments_sha256": digest(segments),
        "segments": len(segments),
        "duration": plan.get("duration"),
        "revision": result.get("revision"),
        "note": note,
        "applied_patches": len(result.get("applied_patches") or []),
    }


def _meta_record(project_root: Path | None) -> dict[str, Any]:
    record: dict[str, Any] = {"kind": "meta_calls", "log": None, "calls": [], "totals": None}
    if project_root is None:
        return record
    try:
        from vedit.meta import call_log

        logged = call_log(Path(project_root))
    except Exception:
        return record
    record["log"] = "meta/meta-calls.jsonl" if (Path(project_root) / "meta" / "meta-calls.jsonl").is_file() else None
    calls = sorted(logged.get("calls") or [], key=lambda item: (item.get("started_at") or 0, item.get("id") or ""))
    record["calls"] = [{key: call.get(key) for key in ("id", "operation", "model", "purpose", "status", "runs_on", "cost_usd", "cost_kind", "request_id")} for call in calls]
    totals = logged.get("totals") or {}
    record["totals"] = {key: totals.get(key) for key in ("calls", "api_calls", "local_calls", "spent_usd")}
    return record


def _check_record(index: int, check: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {"kind": "check", "index": index}
    for key in _CHECK_FIELDS:
        record[key] = check.get(key)
    record["passed"] = bool(record["passed"])
    record["inputs"] = list(record["inputs"] or [])
    return record


def _limitations(checks: list[dict[str, Any]], meta: dict[str, Any], plan: dict[str, Any] | None, result: dict[str, Any]) -> list[str]:
    notes = ["These ids prove the files and records are unchanged since Briefcut wrote them. They are not a signature. "
             "Compare the root id with one from a trusted place to know who made them."]
    inferred = sum(1 for check in checks if check.get("basis") == "inferred")
    if inferred:
        notes.append(f"{inferred} check{'s are' if inferred != 1 else ' is'} a model's judgment (basis: inferred), not a measurement.")
    analysis = (plan or {}).get("analysis") if isinstance((plan or {}).get("analysis"), dict) else {}
    heard_by_meta = analysis.get("transcribed_by") == "meta-voice-transcribe" or result.get("caption_provider") == "meta"
    estimated = any(isinstance(item, dict) and item.get("estimated") for item in analysis.get("transcript") or [])
    if heard_by_meta or estimated:
        notes.append("Word times are estimated. Meta Voice Transcribe times turns, not words.")
    if not meta["calls"]:
        notes.append("No Meta calls are recorded for this project.")
    else:
        if any(call.get("cost_kind") == "reserved" for call in meta["calls"]):
            notes.append("Some Meta costs are the amount reserved, not the amount billed.")
        notes.append("Meta response ids are copied as Meta sent them. Verify cannot confirm them with Meta.")
    notes.append("Stream checks prove a playable file, not a good edit.")
    return notes


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.name


def bundle_receipts(result: dict[str, Any], video_path: Path | str, *, plan: dict[str, Any] | None = None,
                    project_root: Path | str | None = None, base: Path | str | None = None, info: Any = None,
                    files: Iterable[tuple[Path, str]] = (), video_name: str | None = None, note: str | None = None,
                    created_at: str | None = None) -> dict[str, Any]:
    """The receipts record for one verified render.

    ``result`` is the campaign result (``checks``, ``proof``, ``revision``).
    ``base`` is the folder ``receipts.json`` will sit in; file paths are kept
    relative to it. ``files`` are other ``(path, bundle name)`` pairs to hash.
    ``project_root`` is the project folder whose ``meta/meta-calls.jsonl`` is
    summarised. ``info`` is the probe of the video, read again when omitted.
    """
    video = Path(video_path)
    base_dir = Path(base) if base is not None else video.parent
    if info is None:
        try:
            from vedit.media import probe

            info = probe(video)
        except Exception:
            info = None
    sha, size = file_sha256(video)
    proof = result.get("proof") if isinstance(result.get("proof"), dict) else {}
    checks = [check for check in result.get("checks") or [] if isinstance(check, dict)]
    meta = _meta_record(Path(project_root) if project_root is not None else None)
    hashed = []
    for path, name in files:
        path = Path(path)
        if path.is_file():
            file_hash, file_size = file_sha256(path)
            hashed.append({"name": name, "path": _relative(path, base_dir), "sha256": file_hash, "size_bytes": file_size})
    receipts: dict[str, Any] = {
        "format": FORMAT, "v": FORMAT_VERSION,
        "created_at": created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "passed": sum(1 for check in checks if check.get("passed")), "total": len(checks),
        "video": seal({"kind": "video", "name": video_name or "video" + video.suffix.lower(), "path": _relative(video, base_dir), "sha256": sha, "size_bytes": size}),
        "probe": seal(_probe_record(proof, info)),
        "plan": seal(_plan_record(plan, result, note)),
        "meta_calls": seal(meta),
        "versions": seal({"kind": "versions", "briefcut": _briefcut_version(), "ffmpeg": _ffmpeg_version(),
                          "python": platform.python_version(), "receipts": FORMAT_VERSION}),
        "files": seal({"kind": "files", "files": hashed}),
        "limitations": seal({"kind": "limitations", "items": _limitations(checks, meta, plan, result)}),
        "checks": [seal(_check_record(index, check)) for index, check in enumerate(checks)],
    }
    receipts["evidence_id"] = root_id(receipts)
    return receipts


def _briefcut_version() -> str:
    from vedit import __version__

    return __version__


def write_receipts(receipts: dict[str, Any], path: Path | str) -> Path:
    """Write receipts as readable JSON. Ids come from the canonical form, so indentation does not matter."""
    dest = Path(path)
    pending = dest.with_name(dest.name + ".part")
    pending.write_text(json.dumps(receipts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(pending, dest)
    return dest


# ---------------------------------------------------------------------------
# Verifying
# ---------------------------------------------------------------------------

class ReceiptsMissing(Exception):
    """There is no receipts.json to check."""


def _label(name: str, record: dict[str, Any]) -> str:
    if name == "check":
        return f"check {record.get('index')} \"{record.get('name')}\""
    return f"{name.replace('_', ' ')} record"


def _safe_member(raw: Any) -> str | None:
    """A relative path inside the folder, or None when it would leave it."""
    if not isinstance(raw, str) or not raw or "\\" in raw:
        return None
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


class _Source:
    """Where the receipts and the files they name are: a zip or a folder."""

    def __init__(self, target: Path) -> None:
        self.target = target
        self.archive: zipfile.ZipFile | None = None
        self.folder: Path | None = None
        # A video handed to `vedit verify`, or a zip cut short in transfer, is a plain
        # answer, not a UnicodeDecodeError or BadZipFile traceback.
        try:
            self._open(target)
        except _UNREADABLE as exc:
            self.close()
            raise ReceiptsMissing(f"{target.name} is not a readable receipts file or bundle.") from exc

    def _open(self, target: Path) -> None:
        if target.is_file() and zipfile.is_zipfile(target):
            self.archive = zipfile.ZipFile(target)
            if RECEIPTS not in self.archive.namelist():
                self.archive.close()
                raise ReceiptsMissing(f"{target.name} has no {RECEIPTS}. It was packed before Briefcut wrote receipts.")
            self.receipts_path = f"{target.name}!{RECEIPTS}"
            self.text = self.archive.read(RECEIPTS).decode("utf-8")
            return
        if target.is_file():
            self.folder, found = target.parent, target
        elif target.is_dir():
            found = _receipts_in(target)
            self.folder = found.parent  # files are named relative to receipts.json
        else:
            raise ReceiptsMissing(f"{target} does not exist.")
        self.receipts_path = str(found)
        self.text = found.read_text(encoding="utf-8")

    def hash(self, record: dict[str, Any]) -> tuple[str, int] | str:
        """The file's (sha256, size), or why it could not be read."""
        if self.archive is not None:
            name = record.get("name")
            try:
                with self.archive.open(str(name)) as handle:
                    return _stream_sha256(handle)
            except KeyError:
                return f"{name} is missing from the bundle"
            except _UNREADABLE:
                return f"{name} could not be read from the bundle: the archive is damaged"
        member = _safe_member(record.get("path"))
        if member is None:
            return f"{record.get('path')!r} is not a path inside this folder"
        path = self.folder / member  # type: ignore[operator]
        if not path.is_file():
            return f"{member} is missing"
        return file_sha256(path)

    def close(self) -> None:
        if self.archive is not None:
            self.archive.close()


def _receipts_in(folder: Path) -> Path:
    """receipts.json in this folder, else in the newest render-N folder of a project."""
    direct = folder / RECEIPTS
    if direct.is_file():
        return direct
    renders = []
    for child in folder.glob(f"render-*/{RECEIPTS}"):
        suffix = child.parent.name.removeprefix("render-")
        if suffix.isdigit():
            renders.append((int(suffix), child))
    if renders:
        return max(renders)[1]
    raise ReceiptsMissing(f"No {RECEIPTS} in {folder}. Render the project first, or point at its campaign.zip.")


def verify(target: Path | str, *, expect: str | None = None) -> dict[str, Any]:
    """Re-hash the video, every file and every record named in the receipts, and say what differs.

    ``target`` is a bundle zip, a render or project folder, or a receipts.json.
    ``expect`` is a root id from a trusted place; a different root is reported.
    Returns ``ok``, the stored ``evidence_id``, every ``item`` checked and the ``changed`` ones.
    Raises ReceiptsMissing when there is nothing to check.
    """
    source = _Source(Path(target))
    items: list[dict[str, Any]] = []

    def add(item: str, ok: bool, detail: str) -> None:
        items.append({"item": item, "ok": ok, "detail": detail})

    try:
        try:
            receipts = json.loads(source.text)
        except ValueError as exc:
            raise ReceiptsMissing(f"{source.receipts_path} is not valid JSON: {exc}.") from exc
        if not isinstance(receipts, dict) or receipts.get("format") != FORMAT:
            raise ReceiptsMissing(f"{source.receipts_path} is not a Briefcut receipts file.")
        video = receipts.get("video") if isinstance(receipts.get("video"), dict) else {}
        for name, record in [(name, receipts.get(name)) for name in SECTIONS] + [("check", check) for check in receipts.get("checks") or []]:
            if not isinstance(record, dict):
                add(f"{name} record", False, "missing from the receipts")
                continue
            label = _label(name, record)
            if record.get("evidence_id") != evidence_id(record):
                add(label, False, "its content does not match its evidence id " + short_id(str(record.get("evidence_id"))))
            else:
                add(label, True, "matches " + short_id(record["evidence_id"]))
        for record in [video] + list((receipts.get("files") or {}).get("files") or []):
            if not isinstance(record, dict):
                continue
            name = str(record.get("name") or record.get("path"))
            found = source.hash(record)
            if isinstance(found, str):
                add(name, False, found)
            elif found[0] != record.get("sha256"):
                add(name, False, f"sha256 differs: the bytes changed ({found[1]} bytes now, {record.get('size_bytes')} recorded)")
            else:
                add(name, True, f"sha256 matches ({found[1]} bytes)")
        checks = [check for check in receipts.get("checks") or [] if isinstance(check, dict)]
        counted = sum(1 for check in checks if check.get("passed"))
        if (receipts.get("passed"), receipts.get("total")) != (counted, len(checks)):
            add("passed/total", False, f"says {receipts.get('passed')} of {receipts.get('total')}, the checks say {counted} of {len(checks)}")
        else:
            add("passed/total", True, f"{counted} of {len(checks)} checks passed")
        stored = str(receipts.get("evidence_id") or "")
        computed = root_id(receipts)
        if stored != computed:
            add("root id", False, f"the records no longer add up to {short_id(stored)}")
        else:
            add("root id", True, f"{stored} covers the video and {len(SECTIONS) + len(checks)} records")
        if expect is not None:
            wanted = expect.strip()
            if stored == wanted:
                add("expected root", True, "matches " + short_id(wanted))
            elif len(wanted) < 3 + EXPECT_MIN_HEX:
                add("expected root", False, f"{wanted!r} is too short to trust: give the full id, or at least ev_ and {EXPECT_MIN_HEX} hex digits")
            elif stored.startswith(wanted):
                add("expected root", True, "matches " + short_id(wanted))
            else:
                add("expected root", False, f"is {short_id(stored)}, not {short_id(wanted)}")
    finally:
        source.close()
    changed = [item for item in items if not item["ok"]]
    return {"ok": not changed, "evidence_id": receipts.get("evidence_id"), "receipts": source.receipts_path,
            "video": video.get("name"), "passed": receipts.get("passed"), "total": receipts.get("total"),
            "items": items, "changed": changed}


def format_verification(report: dict[str, Any]) -> str:
    """Plain lines for a terminal: what matched, or a list of exactly what changed."""
    lines = [f"Receipts {report['receipts']}", f"Root id  {report['evidence_id']}"]
    if report["ok"]:
        lines += [f"  OK       {item['item']}: {item['detail']}" for item in report["items"]]
        lines.append(f"Verified. All {len(report['items'])} items match.")
    else:
        lines += [f"  CHANGED  {item['item']}: {item['detail']}" for item in report["changed"]]
        lines.append(f"Not verified. {len(report['changed'])} of {len(report['items'])} items changed.")
    return "\n".join(lines) + "\n"
