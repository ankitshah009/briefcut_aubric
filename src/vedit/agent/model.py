"""Local editorial agent types."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from vedit.ir import Brief, brief_from_mapping, brief_to_dict


def _pick(kind: type[Any], data: dict[str, Any]) -> dict[str, Any]:
    allowed = {item.name for item in kind.__dataclass_fields__.values()}
    return {key: value for key, value in data.items() if key in allowed}


def _as_path(value: str | Path) -> Path:
    if isinstance(value, Path):
        return value
    return Path(value)


@dataclass(frozen=True)
class SourceShot:
    name: str
    path: str
    kind: str
    duration: float
    width: int
    height: int
    fps: float
    has_video: bool
    has_audio: bool
    size_bytes: int
    silence_ratio: float | None = None
    scene_count: int | None = None
    loudness_i: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceShot:
        return cls(**_pick(cls, data))


@dataclass(frozen=True)
class Inventory:
    shots: tuple[SourceShot, ...]
    primary: str
    notes: tuple[str, ...] = ()

    def shot(self, name: str) -> SourceShot | None:
        for item in self.shots:
            if item.name == name:
                return item
        return None

    @property
    def primary_shot(self) -> SourceShot | None:
        return self.shot(self.primary)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shots": [item.to_dict() for item in self.shots],
            "primary": self.primary,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Inventory:
        raw = _pick(cls, data)
        raw["shots"] = tuple(
            item if isinstance(item, SourceShot) else SourceShot.from_dict(item) for item in data["shots"]
        )
        if "notes" in raw:
            raw["notes"] = tuple(raw["notes"])
        return cls(**raw)


@dataclass(frozen=True)
class Intent:
    goal: str = ""
    platform: str = "youtube"
    max_duration: float | None = None
    cut_silence: bool = False
    captions: bool = False
    loudnorm: bool = False
    music: str | None = None
    title: str | None = None
    hook: str | None = None
    cta: str | None = None
    reframe: str | None = None
    denoise: bool = False
    highpass: bool = False
    fade: bool = False
    watermark: str | None = None
    images: tuple[str, ...] = ()
    name: str = "edit"

    def __post_init__(self) -> None:
        if not isinstance(self.images, tuple):
            object.__setattr__(self, "images", tuple(self.images))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["images"] = list(self.images)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Intent:
        return cls(**_pick(cls, data))


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    fix: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(**_pick(cls, data))


@dataclass(frozen=True)
class Critique:
    ok: bool
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "findings": [item.to_dict() for item in self.findings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Critique:
        raw = _pick(cls, data)
        findings = tuple(item if isinstance(item, Finding) else Finding.from_dict(item) for item in raw["findings"])
        failed = any(item.severity == "fail" for item in findings)
        ok = False if failed else raw["ok"]
        return cls(ok=ok, findings=findings)


@dataclass
class Decision:
    rationale: tuple[str, ...]
    risks: tuple[str, ...]
    brief: Brief

    def to_dict(self) -> dict[str, Any]:
        return {
            "rationale": list(self.rationale),
            "risks": list(self.risks),
            "brief": brief_to_dict(self.brief),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Decision:
        raw = _pick(cls, data)
        if "rationale" in raw:
            raw["rationale"] = tuple(raw["rationale"])
        if "risks" in raw:
            raw["risks"] = tuple(raw["risks"])
        brief = data["brief"]
        raw["brief"] = brief if isinstance(brief, Brief) else brief_from_mapping(brief)
        return cls(**raw)


@dataclass
class Session:
    root: Path
    goal: str
    intent: Intent
    inventory: Inventory
    decision: Decision | None = None
    critique: Critique | None = None
    work_dir: Path | None = None
    status: str = "planned"
    revision: int = 0
    primary_output: str | None = None
    captions_skipped: bool = False
    decisions: dict[str, Any] | None = None
    proof: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "root": str(self.root),
            "goal": self.goal,
            "intent": self.intent.to_dict(),
            "inventory": self.inventory.to_dict(),
            "decision": None if self.decision is None else self.decision.to_dict(),
            "critique": None if self.critique is None else self.critique.to_dict(),
            "work_dir": None if self.work_dir is None else str(self.work_dir),
            "status": self.status,
            "revision": self.revision,
            "primary_output": self.primary_output,
            "captions_skipped": self.captions_skipped,
            "decisions": self.decisions,
            "proof": self.proof,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        intent = data["intent"]
        inventory = data["inventory"]
        raw: dict[str, Any] = {
            "root": _as_path(data["root"]),
            "goal": data["goal"],
            "intent": intent if isinstance(intent, Intent) else Intent.from_dict(intent),
            "inventory": inventory if isinstance(inventory, Inventory) else Inventory.from_dict(inventory),
        }
        if data.get("decision") is not None:
            decision = data["decision"]
            raw["decision"] = decision if isinstance(decision, Decision) else Decision.from_dict(decision)
        if data.get("critique") is not None:
            critique = data["critique"]
            raw["critique"] = critique if isinstance(critique, Critique) else Critique.from_dict(critique)
        if "work_dir" in data and data["work_dir"] is not None:
            raw["work_dir"] = _as_path(data["work_dir"])
        for key in ("status", "revision", "primary_output"):
            if key in data:
                raw[key] = data[key]
        if "captions_skipped" in data:
            raw["captions_skipped"] = bool(data["captions_skipped"])
        if isinstance(data.get("decisions"), dict):
            raw["decisions"] = data["decisions"]
        if isinstance(data.get("proof"), dict):
            raw["proof"] = data["proof"]
        return cls(**raw)
