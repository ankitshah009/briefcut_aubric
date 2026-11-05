"""What every Meta module shares: the error type and the two bounded-value checks.

``vedit.meta`` is the facade callers import. The ledger (``vedit.meta_ledger``)
and SAM's wire parser (``vedit.meta_sam``) sit beneath it and raise the same
error with the same checks, so those live here rather than in the facade they
must not import.
"""

from __future__ import annotations

import math
from typing import Any

from vedit.errors import VeditError


class MetaError(VeditError):
    """A safe, actionable provider error, with no remote body or credentials.

    ``status`` is Meta's HTTP status when it sent one, and ``code`` its error
    code when that is a plain identifier. ``billed`` is False only when Meta
    certainly did not run the request (it refused it, or never received it);
    None means it may have. ``retry_after`` is the wait Meta asked for, in seconds.
    ``usage`` is the charge of a call Meta answered and billed before the error
    (an answer that could not be read), so a caller can still count the spend.
    The message is also ``plain``: it is written for the person, with no path.
    """

    def __init__(
        self,
        *args: object,
        plain: str | None = None,
        status: int | None = None,
        code: str | None = None,
        billed: bool | None = None,
        retry_after: float | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(*args, plain=plain if plain is not None else (str(args[0]) if args else None))
        self.status = status
        self.code = code
        self.billed = billed
        self.retry_after = retry_after
        self.usage = usage


def _number(value: Any, label: str, *, minimum: float = 0, maximum: float = 86_400) -> float:
    """``value`` as a finite float inside the bounds, or a ``MetaError`` naming ``label``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MetaError(f"{label} must be a finite number.")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise MetaError(f"{label} must be between {minimum:g} and {maximum:g}.")
    return result


def _text(value: Any, label: str, limit: int, *, empty: bool = False) -> str:
    """``value`` as stripped text of at most ``limit`` characters with no control characters, or a ``MetaError``."""
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise MetaError(f"{label} must be {'at most ' if empty else '1–'}{limit:,} characters.")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise MetaError(f"{label} contains unsupported control characters.")
    return value.strip()
