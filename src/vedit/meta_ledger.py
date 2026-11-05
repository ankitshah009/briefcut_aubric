"""The project's Meta ledger: reservations against the campaign cap, settlement, and the call log.

Every paid call reserves its most-possible cost in ``meta-usage.json`` before
anything is sent, then settles to what Meta billed. The cap is the project's,
so the ledger is written under one lock across threads and processes. Beside
it, ``meta-calls.jsonl`` is the append-only call log Studio's "Meta calls"
panel shows: one line when a call starts and one when it ends, with purposes,
models, sizes, costs and response ids, and never a key, header, prompt,
transcript or media.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from vedit.meta_base import MetaError

try:
    import fcntl
except ImportError:  # Windows: the ledger is then guarded within one process only
    fcntl = None  # type: ignore[assignment]

_PRICES_AS_OF = "2026-11-04"  # every price in vedit.meta matches dev.meta.ai/docs/pricing-rate-limits on this date
_LEDGER = "meta-usage.json"
_LEDGER_LOCK = threading.RLock()
_LEDGER_LOCK_NAME = "meta-usage.lock"
_LEDGER_HOLDS: dict[Path, list[Any]] = {}  # work_dir -> [depth, lock file descriptor]; touched only under _LEDGER_LOCK
_MOST_USD = 1000  # no single call or recorded cost is ever this large; anything bigger is a damaged ledger
_CALL_LOG = "meta-calls.jsonl"
_CALL_ENDED = {"completed": "done", "failed_or_incomplete": "failed"}
_PURPOSES = {
    "story": "Plan the story",
    "review": "Review the finished cut",
    "patch": "Turn your note into edits",
    "translate": "Translate the captions",
    "find": "Find moments in the footage",
    "shorts": "Plan shorts from the footage",
    "candidates": "Find candidate moments in one part of a long video",
    "edit": "Pick the final moments of a long video from its candidates",
    "script": "Write a picture prompt for each scene",
    "image": "Generate a picture",
    "tracking": "Track the subject",
    "cover": "Make a cover from a frame",
    "transcribe": "Transcribe speech for captions",
    "emphasis": "Pick the punch words in the captions",
}
_CALL_SIZES = {"input_tokens": int, "output_tokens": int, "frames": int, "audio_seconds": float, "images": int}
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_CALL_WORD = re.compile(r"[A-Za-z0-9._:/-]{1,80}\Z")  # "/" for open-weight ids such as meta-models/Muse-Glimmer-30B
_RUNS_ON = ("meta-api", "local")
_CALL_TEXT = re.compile(r"[A-Za-z0-9 ._:/()+-]{1,120}\Z")
_RESPONSE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CALL_ROWS = 200
_ORPHAN_SECONDS = 900  # a ledger-only call with no end this old is not still running
_PURPOSE_CHARS = 200
_REASON_CHARS = 300


# --- One writer at a time ---


def _lock_ledger_file(work_dir: Path) -> int | None:
    """An exclusive ``flock`` on the project's ledger lock file, or None where the platform has none."""
    if fcntl is None:
        return None
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        handle = os.open(work_dir / _LEDGER_LOCK_NAME, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError:
        os.close(handle)
        return None
    return handle


def _unlock_ledger_file(handle: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


@contextmanager
def _ledger_locked(work_dir: Path) -> Iterator[None]:
    """One writer at a time in a project's ledger, across threads and across processes.

    The thread lock orders this process. The exclusive ``flock`` on
    ``meta-usage.lock`` orders the CLI, Studio and the MCP server when they share
    a project, so two reservations that each read the ledger cannot lose each
    other's entry or together exceed the cap. The ledger is read only once the
    lock is held. It is re-entrant per work_dir, like the thread lock, because
    ``_reserve`` writes through ``_ledger`` while holding it. Without ``fcntl``
    (Windows) only the thread lock applies.
    """
    key = Path(work_dir).resolve()
    with _LEDGER_LOCK:
        hold = _LEDGER_HOLDS.get(key)
        if hold is None:
            hold = _LEDGER_HOLDS[key] = [0, _lock_ledger_file(key)]
        hold[0] += 1
        try:
            yield
        finally:
            hold[0] -= 1
            if hold[0] == 0:
                del _LEDGER_HOLDS[key]
                if hold[1] is not None:
                    _unlock_ledger_file(hold[1])


def _write_json(path: Path, value: Any) -> None:
    """Write ``value`` as JSON through a temporary file, so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_text(path: Path, value: str) -> None:
    """Write text as latin-1 bytes atomically, so offsets into the file are character offsets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_bytes(value.encode("latin-1", "replace"))
        tmp.chmod(0o600)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


# --- Reservations and settlement ---


def _money(value: Any) -> float:
    """A recorded amount in USD, or 0.0 for anything that is not a plausible one."""
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= _MOST_USD
    ):
        return float(value)
    return 0.0


def _is_money(value: Any) -> bool:
    """Whether ``value`` is a plausible recorded amount."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= _MOST_USD
    )


def _ledger_shape_ok(data: Any) -> bool:
    """Whether a ledger file has calls with ids and plausible amounts; anything else is a damaged ledger.

    An amount that is absent is zero; one that is present and not a number is damage.
    """
    if not isinstance(data, dict) or not isinstance(data.get("calls"), list):
        return False
    for item in data["calls"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return False
        usage = item.get("usage") or {}
        if (
            not isinstance(usage, dict)
            or not _is_money(item.get("reserved_usd", 0))
            or not _is_money(usage.get("cost_usd", 0))
        ):
            return False
    return True


def _read_ledger(work_dir: Path) -> dict[str, Any]:
    """The project's ledger, or a fresh one. A ledger that cannot be trusted stops the call rather than guess at spend."""
    path = work_dir / _LEDGER
    if not path.is_file():
        return {"prices_as_of": _PRICES_AS_OF, "calls": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not _ledger_shape_ok(data):
        raise MetaError("The local Meta usage ledger is unreadable. Preserve it and start a new campaign.")
    return data


def held_usd(entry: Any) -> float:
    """What one ledger call holds against the campaign budget.

    A call Meta answered with a billed cost holds that cost: a finished Wonder
    review that billed $0.10 holds $0.10, not the $0.35 it reserved. When Meta
    billed more than the reservation (``_mark_overrun``), that larger cost is
    what holds. A call still running, one whose outcome is uncertain, or one
    priced only by estimate holds its reservation, so a retry can never spend it
    twice. A call Meta refused was released to $0 when it was settled.
    """
    if not isinstance(entry, dict):
        return 0.0
    reserved = _money(entry.get("reserved_usd"))
    raw_usage = entry.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    cost = usage.get("cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        return reserved
    if usage.get("cost_is_estimate"):
        return max(reserved, float(cost))
    return float(cost)


def _reserve(
    work_dir: Path,
    operation: str,
    model: str,
    maximum: float,
    budget: float,
    *,
    purpose: str = "",
    size: dict[str, Any] | None = None,
    runs_on: str = "meta-api",
    runtime: str = "",
    weights: str = "",
) -> str:
    """Hold ``maximum`` against the project's cap and log the call's start; returns the call id.

    The cap belongs to the project. Ambiguous charges stay reserved after
    timeouts and malformed responses; retries must never reset the allowance.
    ``purpose`` and ``size`` only describe the call in the project's call log.
    ``runs_on`` is "meta-api", or "local" for private mode (vedit.open_weights),
    which also names its ``runtime`` and GGUF ``weights`` file.
    """
    if runs_on not in _RUNS_ON:
        raise MetaError("A call runs on the Meta Model API or on this machine.")
    where = (
        {"runs_on": runs_on}
        if runs_on == "meta-api"
        else {"runs_on": runs_on, "runtime": " ".join(runtime.split())[:80], "weights": Path(weights).name[:120]}
    )
    with _ledger_locked(work_dir):
        data = _read_ledger(work_dir)
        spent = sum(held_usd(item) for item in data["calls"])
        # A $0 call runs on this machine (private mode): the cap is about Meta's bill, so it never refuses one.
        if maximum > 0 and spent + maximum > budget + 1e-9:
            # Say how far over it is and what number fixes it, not just that it is over.
            floor = math.ceil((spent + maximum) * 100 - 1e-6) / 100
            what = _PURPOSES.get(operation, "Ask Meta")
            raise MetaError(
                f"This {operation} needs up to ${maximum:.4f} ({what[:1].lower() + what[1:]}), and ${spent:.4f} of the ${budget:.4f} "
                f"campaign budget is already reserved, so it is ${spent + maximum - budget:.4f} short. Raise the campaign budget "
                f"to at least ${floor:.2f}, or use a shorter clip. Nothing was billed for this call.",
                billed=False,
            )
        ident = uuid.uuid4().hex
        started = time.time()
        _ledger(
            work_dir,
            {
                "id": ident,
                "operation": operation,
                "model": model,
                "reserved_usd": round(maximum, 8),
                "budget_usd": budget,
                "status": "reserved",
                "started_at": started,
                **where,
            },
        )
        _log_call(
            work_dir,
            {
                "event": "start",
                "id": ident,
                "at": started,
                "operation": operation,
                "model": model,
                "purpose": purpose or _PURPOSES.get(operation, "Ask Meta"),
                "reserved_usd": round(maximum, 8),
                "budget_usd": budget,
                "size": _call_size(size or {}),
                "pid": os.getpid(),
                **where,
            },
        )
        return ident


def _failure() -> dict[str, Any]:
    """Why the call being settled failed, read from the exception in flight, and whether its money can come back.

    Callers settle a failed call inside ``except``, so the error is at hand. Only
    a ``MetaError`` that says Meta certainly did not run the request (``billed``
    is False: refused, rate limited, or never received) releases the reservation.
    Anything uncertain stays reserved, so a retry can never spend it twice.
    """
    error = sys.exc_info()[1]
    if error is None:
        return {}
    if not isinstance(error, MetaError):
        return {"reason": "Briefcut stopped before the call finished."}
    fields: dict[str, Any] = {"reason": _reason(str(error))}
    if error.billed is False:
        fields.update(released=True, reserved_usd=0)
    return fields


def _reason(text: str) -> str:
    """A failure in one plain line for the call log. Meta's own text never gets here; this is belt and braces."""
    return re.sub(r"(?i)bearer\s+\S+", "[key]", " ".join(text.split()))[:_REASON_CHARS]


def _ledger(work_dir: Path, entry: dict[str, Any]) -> None:
    """Add or update one call's ledger entry by ``id``; a call that ended is also logged."""
    if entry.get("status") == "failed_or_incomplete":
        entry = {**_failure(), **entry}
    with _ledger_locked(work_dir):
        data = _read_ledger(work_dir)
        calls = data["calls"]
        existing = next((item for item in calls if item.get("id") == entry["id"]), None)
        if existing is None:
            calls.append(entry)
            existing = entry
        else:
            if entry.get("released") and existing.get("usage"):
                # Meta answered, so whatever it billed stays counted.
                entry = {name: value for name, value in entry.items() if name not in {"released", "reserved_usd"}}
            existing.update(entry)
        _write_json(work_dir / _LEDGER, data)
        if entry.get("status") in _CALL_ENDED:
            _log_end(work_dir, existing)


# --- The call log: what Studio's "Meta calls" panel shows ---
#
# One append-only JSON line when a paid call starts (in ``_reserve``) and one
# when it ends (in ``_ledger``), so every call path that reserves budget is
# logged without its own copy of this code. The only user text is a short
# phrase the user typed, such as a subject or a background prompt.


def _quoted(words: Any, limit: int = 60) -> str:
    """A short phrase the user typed, on one line, for a call's purpose."""
    text = "".join(c for c in " ".join(str(words).split()) if c.isprintable())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return f"“{text}”" if text else ""


def _response_id(response: Any) -> dict[str, str]:
    """Meta's id for this response, when it sent one, as a ledger field."""
    if isinstance(response, dict):
        for key in ("id", "request_id", "requestId"):
            value = response.get(key)
            if isinstance(value, str) and _RESPONSE_ID.fullmatch(value):
                return {"request_id": value}
    return {}


def _call_size(raw: dict[str, Any]) -> dict[str, int | float]:
    """The sizes a call log row shows, each a plausible count; anything else is left out."""
    size: dict[str, int | float] = {}
    for key, kind in _CALL_SIZES.items():
        value = raw.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and 0 <= value < 1e12
        ):
            size[key] = int(value) if kind is int else round(float(value), 2)
    return size


def _usage_size(usage: dict[str, Any]) -> dict[str, int | float]:
    """A ledger entry's usage as call log sizes."""
    return _call_size(
        {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "frames": usage.get("video_frames_processed"),
            "audio_seconds": usage.get("audio_seconds"),
        }
    )


def _log_call(work_dir: Path, record: dict[str, Any]) -> None:
    """Append one line. A full disk must not fail a call that Meta already billed."""
    try:
        line = json.dumps(dict(record, v=1), ensure_ascii=False, allow_nan=False) + "\n"
        work_dir.mkdir(parents=True, exist_ok=True)
        with _LEDGER_LOCK:
            handle = os.open(work_dir / _CALL_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(handle, line.encode("utf-8"))
            finally:
                os.close(handle)
    except (OSError, ValueError, TypeError):
        pass


def _timestamp(value: Any) -> float | None:
    """A finite, non-negative time, or None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _log_end(work_dir: Path, entry: dict[str, Any]) -> None:
    """The ``end`` line for a settled call: how long it took, what it cost, and why it failed."""
    raw_usage = entry.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    status = _CALL_ENDED[entry["status"]]
    size = _usage_size(usage)
    if status == "done" and entry.get("operation") in {"image", "cover"}:
        size["images"] = 1
    now = time.time()
    record: dict[str, Any] = {"event": "end", "id": entry["id"], "at": now, "status": status, "size": size}
    started = _timestamp(entry.get("started_at"))
    if started is not None and 0 < started <= now:
        record["seconds"] = round(now - started, 2)
    cost = usage.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
        record["cost_usd"] = round(float(cost), 8)
        record["cost_is_estimate"] = bool(usage.get("cost_is_estimate"))
    record.update(_response_id({"id": entry.get("request_id")}))
    if status == "failed":
        reason = entry.get("reason")
        if isinstance(reason, str) and reason.strip():
            record["reason"] = _reason(reason)
        if entry.get("released") is True:
            record["released"] = True  # Meta never ran it: nothing was billed, and the reservation is back
    _log_call(work_dir, record)


def _alive(pid: Any) -> bool:
    """Whether the process that started a call can still finish it."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):  # a pid too large for the platform is no process Briefcut started
        return True
    return True


def _failed_row(item: dict[str, Any], record: dict[str, Any]) -> None:
    """A failed row says why (``reason``), and a call Meta never ran costs nothing (``released``)."""
    if item.get("status") != "failed":
        return
    reason = record.get("reason")
    if isinstance(reason, str) and reason.strip():
        item["reason"] = _reason(reason)
    if record.get("released") is True:
        item.update(released=True, reserved_usd=0.0, cost_usd=0.0, cost_is_estimate=False)


def _new_row(ident: str) -> dict[str, Any]:
    return {
        "id": ident,
        "started_at": None,
        "operation": "",
        "model": "",
        "purpose": "",
        "status": "running",
        "seconds": None,
        "size": {},
        "reserved_usd": 0.0,
        "cost_usd": None,
        "cost_is_estimate": True,
        "request_id": None,
        "runs_on": "meta-api",
        "runtime": None,
        "weights": None,
        "_pid": None,
        "_ended": False,
    }


def _word(value: Any) -> str:
    """An operation or model name as the log may show it, else ''."""
    return value if isinstance(value, str) and _CALL_WORD.fullmatch(value) else ""


def _request_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _RESPONSE_ID.fullmatch(value) else None


def _place(item: dict[str, Any], record: dict[str, Any]) -> None:
    """Where the call ran. A record from before ``runs_on`` existed went to the Meta API."""
    local = record.get("runs_on") == "local" or (record.get("runs_on") is None and record.get("local") is True)
    item["runs_on"] = "local" if local else "meta-api"
    for key in ("runtime", "weights"):
        value = record.get(key)
        item[key] = value if local and isinstance(value, str) and _CALL_TEXT.fullmatch(value) else None


def _logged_id(event: Any) -> str | None:
    """The call id of one log line or ledger entry, when it has a usable one."""
    if not isinstance(event, dict):
        return None
    ident = event.get("id")
    return ident if isinstance(ident, str) and _CALL_ID.fullmatch(ident) else None


def _start_row(item: dict[str, Any], event: dict[str, Any]) -> None:
    """Fill a row from a call's ``start`` line."""
    raw_size = event.get("size")
    item["started_at"] = _timestamp(event.get("at"))
    item["operation"], item["model"] = _word(event.get("operation")), _word(event.get("model"))
    purpose = event.get("purpose")
    item["purpose"] = " ".join(purpose.split())[:_PURPOSE_CHARS] if isinstance(purpose, str) else ""
    item["reserved_usd"] = _money(event.get("reserved_usd"))
    item["size"] = {**_call_size(raw_size if isinstance(raw_size, dict) else {}), **item["size"]}
    item["_pid"] = event.get("pid")
    _place(item, event)


def _end_row(item: dict[str, Any], event: dict[str, Any]) -> None:
    """Fill a row from a call's ``end`` line."""
    raw_size = event.get("size")
    item["_ended"] = True
    status = event.get("status")
    item["status"] = status if isinstance(status, str) and status in {"done", "failed"} else "failed"
    seconds = _timestamp(event.get("seconds"))
    if seconds is not None:
        item["seconds"] = round(seconds, 2)
    item["size"].update(_call_size(raw_size if isinstance(raw_size, dict) else {}))
    if "cost_usd" in event:
        item["cost_usd"] = _money(event.get("cost_usd"))
        item["cost_is_estimate"] = bool(event.get("cost_is_estimate"))
    item["request_id"] = _request_id(event.get("request_id"))
    _failed_row(item, event)


def _log_rows(root: Path, rows: dict[str, dict[str, Any]]) -> None:
    """Rows from every call log under ``root``."""
    for path in sorted(root.glob("**/" + _CALL_LOG)):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue  # a line torn by a crash
            ident = _logged_id(event)
            if ident is None:
                continue
            item = rows.setdefault(ident, _new_row(ident))
            if event.get("event") == "start":
                _start_row(item, event)
            elif event.get("event") == "end":
                _end_row(item, event)


def _ledger_row(item: dict[str, Any], entry: dict[str, Any], now: float) -> None:
    """Fill a row from a ledger entry that has no call log lines (a project from before the log existed)."""
    item["operation"], item["model"] = _word(entry.get("operation")), _word(entry.get("model"))
    item["purpose"] = _PURPOSES.get(item["operation"], "Ask Meta")
    _place(item, entry)
    item["started_at"] = _timestamp(entry.get("started_at"))
    item["reserved_usd"] = _money(entry.get("reserved_usd"))
    raw_usage = entry.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    item["size"] = _usage_size(usage)
    if "cost_usd" in usage:
        item["cost_usd"] = _money(usage.get("cost_usd"))
        item["cost_is_estimate"] = bool(usage.get("cost_is_estimate"))
    item["request_id"] = _request_id(entry.get("request_id"))
    raw_status = entry.get("status")
    status = _CALL_ENDED.get(raw_status) if isinstance(raw_status, str) else None
    if status:
        item["status"], item["_ended"] = status, True
        _failed_row(item, entry)
    elif item["started_at"] is None or now - item["started_at"] > _ORPHAN_SECONDS:
        item["status"], item["_ended"] = "stopped", True


def _ledger_rows(root: Path, rows: dict[str, dict[str, Any]]) -> None:
    """Rows for calls recorded only in a cost ledger under ``root``."""
    now = time.time()
    for ledger in sorted(root.glob("**/" + _LEDGER)):
        try:
            data = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = data.get("calls") if isinstance(data, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            ident = _logged_id(entry)
            if ident is None or ident in rows:
                continue
            _ledger_row(rows.setdefault(ident, _new_row(ident)), entry, now)


def _settled(item: dict[str, Any]) -> tuple[float, float]:
    """Finish one row for display; returns what it spent and what it holds (the same rule as ``held_usd``)."""
    if not item.pop("_ended") and not _alive(item.pop("_pid")):
        item["status"] = "stopped"  # the process that made it is gone, so no answer is coming
    item.pop("_pid", None)
    billed, reserved = item["cost_usd"], item["reserved_usd"]
    if billed is not None and not item["cost_is_estimate"]:
        cost, kind, holds = float(billed), "actual", float(billed)
    else:
        cost = max(reserved, billed or 0.0)
        kind, holds = "reserved", max(reserved, cost)
    item["cost_usd"], item["cost_kind"] = round(cost, 8), kind
    del item["cost_is_estimate"]
    return cost, holds


def call_log(root: Path, *, budget_usd: float | None = None) -> dict[str, Any]:
    """Every paid Meta call one project made, newest first, with spend against its budget.

    Reads each ``meta-calls.jsonl`` under ``root``. Calls recorded only in the
    cost ledger, from projects made before the log existed, still appear. A row
    has purposes, models, sizes, costs and response ids, and nothing else.
    """
    root = Path(root)
    rows: dict[str, dict[str, Any]] = {}
    _log_rows(root, rows)
    _ledger_rows(root, rows)
    calls: list[dict[str, Any]] = []
    spent = held = 0.0
    for item in rows.values():
        cost, holds = _settled(item)
        spent += cost
        held += holds
        calls.append(item)
    calls.sort(key=lambda item: (item["started_at"] or 0, item["id"]), reverse=True)
    local = sum(item["runs_on"] == "local" for item in calls)
    totals: dict[str, Any] = {
        "calls": len(calls),
        "running": sum(item["status"] == "running" for item in calls),
        "spent_usd": round(spent, 8),
        "held_usd": round(held, 8),
        "api_calls": len(calls) - local,
        "local_calls": local,
    }
    if budget_usd is not None:
        cap = _money(budget_usd)
        totals.update(budget_usd=cap, remaining_usd=round(max(0.0, cap - held), 8))
    return {"calls": calls[:_CALL_ROWS], "totals": totals}
