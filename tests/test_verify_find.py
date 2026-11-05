"""Verified find: SAM 3.1 checks the moments Muse Spark found, on real mask bytes, with every paid call stubbed."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("meta_sam_parser")

from vedit import meta, verify_find
from vedit.media import probe, resolve_ffmpeg
from vedit.verify_find import billed_usd, coverage, query_subject, sample_rate, verify_moments, window_for

FIXTURE = json.loads(Path(__file__).with_name("fixtures").joinpath("sam_bowl.json").read_text(encoding="utf-8"))["records"]
# Real SAM 3.1 output for the demo bowl at 960x540: one box and one lossless mask.
BOWL = FIXTURE["lossless_keyframe"]["line"].removeprefix("<0f>")
BOWL_SHARE = FIXTURE["lossless_keyframe"]["opaque_pixels"] / (960 * 540)


def bowl_text(frames: range | list[int]) -> str:
    return "\n".join(f"<{frame}f>{BOWL}" for frame in frames)


class Http:
    """Meta's boundary. Each upload is remembered, so a SAM answer can depend on which window it is for."""

    def __init__(self, answer: Any = "") -> None:
        self.answer, self.calls, self.files, self.lock = answer, [], {}, threading.Lock()

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with self.lock:
            self.calls.append((method, path))
            if method == "POST" and path == "/files":
                ident = f"file-{len(self.files) + 1}"
                self.files[ident] = Path(kwargs["body"].source)
                return {"id": ident, "status": "uploaded"}
        if method == "DELETE":
            return {"deleted": True}
        if method == "POST" and path == "/responses":
            payload = kwargs["payload"]
            assert payload["model"] == "sam-3.1"
            content = payload["input"][0]["content"]
            window = self.files[content[1]["file_id"]]
            start = int(re.search(r"-(\d+)-(\d+)\.mp4$", window.name).group(1))
            text = self.answer(start, content[0]["text"]) if callable(self.answer) else self.answer
            if isinstance(text, Exception):
                raise text
            return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                    "usage": {"video_frames_processed": 45}}
        pytest.fail(f"Unexpected provider request: {method} {path}")

    def paid(self) -> int:
        return sum(call == ("POST", "/responses") for call in self.calls)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")


@pytest.fixture(scope="module")
def footage(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("footage") / "footage.mp4"
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=12",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def found(*spans: tuple[float, float], source: str = "meta") -> list[dict[str, Any]]:
    return [{"start": start, "end": end, "reason": f"Seen at {start}s.", "label": f"Seen at {start}s.", "source": source, "score": 0.9 - 0.05 * rank}
            for rank, (start, end) in enumerate(spans)]


def ledger(work: Path) -> list[dict[str, Any]]:
    return json.loads((work / "meta-usage.json").read_text(encoding="utf-8"))["calls"]


# --- The noun phrase ---

@pytest.mark.parametrize(("query", "phrase"), [
    ("the red mug", "red mug"), ("the dog", "dog"), ("a dog", "dog"), ("hands sanding wood", "hands"),
    ("the wooden bowl", "wooden bowl"), ("Show me the wooden bowl", "wooden bowl"), ("the gouge", "gouge"),
    ("the moment the bowl appears", "bowl"), ("someone holding a red mug", "red mug"), ("find the bowl on the lathe", "bowl"),
    ("clips where the gouge is visible", "gouge"), ("wood shavings", "wood shavings"), ("where is the dog?", "dog"),
])
def test_a_visible_thing_becomes_a_short_noun_phrase(query: str, phrase: str) -> None:
    assert query_subject(query) == {"kind": "visual", "phrase": phrase, "reason": f"SAM looked for “{phrase}”."}


@pytest.mark.parametrize(("query", "kind"), [
    ("where I talk about pricing", "spoken"), ('when he says "thank you"', "spoken"), ("the part where she explains the lathe", "spoken"),
    ("applause", "spoken"), ("the best part", "abstract"), ("the intro", "abstract"), ("pricing", "abstract"), ("", "abstract"),
])
def test_a_spoken_or_abstract_search_names_nothing_for_sam(query: str, kind: str) -> None:
    subject = query_subject(query)
    assert subject["kind"] == kind and subject["phrase"] is None and "SAM" in subject["reason"]


# --- Windows, sampling and the score ---

def test_coverage_is_the_share_of_sampled_frames_with_a_big_enough_mask() -> None:
    assert coverage([0.0, 0.0005, 0.001, 0.2], 8) == 0.25  # specks under 0.1% of the picture do not count
    assert coverage([0.5] * 45, 45) == 1.0
    assert coverage([], 45) == 0.0 and coverage([0.5], 0) == 0.0
    assert coverage([0.5] * 50, 45) == 1.0  # never above every frame


def test_each_window_fits_inside_sams_minimum_bill() -> None:
    assert window_for(10.0, 20.0, 40.0) == (13.5, 16.5)  # the middle of a long moment
    assert window_for(0.0, 0.2, 40.0) == (0.0, 1.0)  # a blip grows to a second, inside the clip
    assert window_for(39.5, 40.0, 40.0) == (39.0, 40.0)
    assert window_for(0.0, 5.0, 2.0) == (0.0, 2.0)
    for seconds in (1.0, 2.0, 3.0, 4.0):
        fps = sample_rate(seconds, 30.0)
        assert seconds * fps <= verify_find.MAX_FRAMES and billed_usd(int(seconds * fps)) == 0.01
    assert sample_rate(3.0, 30.0) == 15.0 and sample_rate(3.0, 8.0) == 8.0
    assert billed_usd(100) == 0.0204


# --- One check, end to end on stubbed SAM ---

def test_a_seen_moment_is_verified_and_an_unseen_one_can_be_dropped(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The bowl is masked in 30 of the 45 sampled frames of the first window; the second window holds nothing.
    http = Http(lambda start, subject: bowl_text(range(0, 45, 3)) + "\n" + bowl_text(range(1, 45, 3)) if start == 0 else "")
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "meta"
    result = verify_moments(found((0.0, 3.0), (6.0, 9.0)), "the wooden bowl", footage, work, 0.05, unseen="drop")
    assert result["status"] == "verified" and result["phrase"] == "wooden bowl" and http.paid() == 2
    [kept] = result["moments"]
    assert kept["verified"] is True and kept["coverage"] == round(30 / 45, 4)
    assert kept["evidence"]["masked_frames"] == 30 and kept["evidence"]["sampled_frames"] == 45 and kept["evidence"]["measured"] == "mask"
    assert kept["evidence"]["peak_area"] == pytest.approx(BOWL_SHARE, abs=1e-4)
    [gone] = result["dropped"]
    assert gone["start"] == 6.0 and gone["verified"] is False and gone["coverage"] == 0.0 and gone["flagged"] is True
    assert "saw no “wooden bowl”" in gone["verification"]
    # The thumbnail is a real frame with the mask outline drawn on it.
    thumb = Path(kept["thumbnail"])
    assert thumb.is_file() and (probe(thumb).width, probe(thumb).height) == (640, 360)
    pixels = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(thumb), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                            check=True, capture_output=True).stdout
    green = sum(1 for at in range(0, len(pixels), 3) if pixels[at + 1] > 200 and pixels[at] < 110 and pixels[at + 2] < 110)
    assert green > 500
    assert Path(gone["thumbnail"]).is_file() and gone["thumbnail"].endswith("-unmasked.jpg")
    # Receipts and spend fit the project's evidence labels and ledger.
    receipt = result["receipt"]
    assert (receipt["basis"], receipt["by"], receipt["inputs"], receipt["passed"]) == ("derived", "sam-3.1", ["muse-spark"], True)
    assert "1 verified" in receipt["note"] and "Dropped 1" in receipt["note"]
    assert [item["cost_usd"] for item in result["spend"]] == [0.01, 0.01] and result["spent_usd"] == 0.02
    assert sorted((call["operation"], call["model"], call["status"]) for call in ledger(work)) == [("tracking", "sam-3.1", "completed")] * 2


def test_by_default_what_sam_never_saw_is_kept_flagged_and_ranked_last(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http(lambda start, subject: "" if start == 0 else bowl_text(range(45)))
    monkeypatch.setattr(meta, "_request", http)
    result = verify_moments(found((0.0, 3.0), (6.0, 9.0)), "the wooden bowl", footage, tmp_path / "meta", 0.05)  # flag is the default
    assert result["dropped"] == []
    assert [(moment["start"], moment["verified"], moment.get("flagged", False)) for moment in result["moments"]] == [(6.0, True, False), (0.0, False, True)]
    assert result["receipt"]["passed"] is False and result["receipt"]["at"] == 0.0 and "kept and flagged" in result["receipt"]["note"]
    # Ranking by score, as director.merge_moments does, keeps SAM's order.
    flagged = result["moments"][1]
    assert flagged["score"] == 0.45 and flagged["evidence"]["spark_score"] == 0.9 and result["moments"][0]["score"] == 0.85


def test_a_few_frames_is_kept_but_not_verified(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "_request", Http(bowl_text([4, 5, 6])))
    result = verify_moments(found((0.0, 3.0)), "the wooden bowl", footage, tmp_path / "meta", 0.05)
    [moment] = result["moments"]
    assert moment["verified"] is False and moment["coverage"] == round(3 / 45, 4) and "flagged" not in moment and result["dropped"] == []
    assert "only 3 of 45" in moment["verification"] and result["receipt"]["passed"] is False


def test_checking_again_is_free(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http(bowl_text(range(45)))
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "meta"
    first = verify_moments(found((0.0, 3.0)), "the wooden bowl", footage, work, 0.05)
    second = verify_moments(found((0.0, 3.0)), "the wooden bowl", footage, work, 0.05)
    assert http.paid() == 1 and first["spent_usd"] == 0.01 and second["spent_usd"] == 0.0
    assert second["spend"][0]["cached"] is True and second["moments"][0]["coverage"] == first["moments"][0]["coverage"] == 1.0


def test_only_found_moments_inside_the_cap_are_checked(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http(bowl_text(range(45)))
    monkeypatch.setattr(meta, "_request", http)
    moments = found((0.0, 3.0), (4.0, 7.0), (8.0, 10.0)) + found((1.0, 2.0), source="transcript")
    result = verify_moments(moments, "the wooden bowl", footage, tmp_path / "meta", 0.05, max_moments=2)
    assert http.paid() == 2 and len(result["moments"]) == 4
    unchecked = {moment["start"]: moment["verification"] for moment in result["moments"] if moment["verified"] is None}
    assert set(unchecked) == {8.0, 1.0} and "first 2" in unchecked[8.0] and "transcript" in unchecked[1.0]


def test_moments_that_share_a_window_share_one_paid_call(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http(bowl_text(range(45)))
    monkeypatch.setattr(meta, "_request", http)
    result = verify_moments(found((0.0, 3.0), (0.0, 3.0)), "the wooden bowl", footage, tmp_path / "meta", 0.05)
    assert http.paid() == 1 and result["spent_usd"] == 0.01 and [moment["verified"] for moment in result["moments"]] == [True, True]
    assert [item["cost_usd"] for item in result["spend"]] == [0.01, 0.0]


def test_a_failed_sam_call_never_drops_a_moment(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http(lambda start, subject: meta.MetaError("Meta request could not be completed.") if start == 0 else bowl_text(range(45)))
    monkeypatch.setattr(meta, "_request", http)
    result = verify_moments(found((0.0, 3.0), (6.0, 9.0)), "the wooden bowl", footage, tmp_path / "meta", 0.05)
    assert result["dropped"] == [] and [moment["verified"] for moment in result["moments"]] == [True, None]
    assert "SAM did not finish" in result["moments"][1]["verification"] and result["warnings"]
    assert result["receipt"]["passed"] is False


# --- What is never sent ---

def test_a_spoken_search_skips_sam_and_says_so(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http()
    monkeypatch.setattr(meta, "_request", http)
    result = verify_moments(found((0.0, 3.0)), "where I talk about pricing", footage, tmp_path / "meta", 0.05)
    assert http.calls == [] and result["status"] == "skipped" and result["kind"] == "spoken" and result["spend"] == []
    assert result["moments"][0]["verified"] is None and "something said" in result["moments"][0]["verification"]
    assert result["receipt"] == {"name": "sam: found moments checked on screen", "passed": True, "at": None,
                                 "note": "Not checked: " + result["reason"], "basis": "derived", "by": "sam-3.1", "inputs": ["muse-spark"]}
    assert not (tmp_path / "meta").exists()


def test_a_check_that_does_not_fit_the_budget_sends_nothing(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http(bowl_text(range(45)))
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "meta"
    result = verify_moments(found((0.0, 3.0), (6.0, 9.0)), "the wooden bowl", footage, work, 0.015)
    assert http.calls == [] and result["status"] == "refused" and "needs up to $0.0200" in result["reason"]
    assert [moment["verified"] for moment in result["moments"]] == [None, None] and result["dropped"] == []
    # A check that never ran is not a pass, and its receipt claims nothing about the phrase.
    assert result["receipt"]["passed"] is False and result["receipt"]["name"] == "sam: found moments checked on screen"
    assert result["receipt"]["note"].startswith("Not checked: ")
    # Spend already on the project's ledger counts too: the search's own Muse Spark call leaves too little.
    meta._reserve(work, "find", "muse-spark-1.3", 0.045, 0.05)
    result = verify_moments(found((0.0, 3.0)), "the wooden bowl", footage, work, 0.05)
    assert http.calls == [] and result["status"] == "refused" and "$0.0050 of the $0.0500" in result["reason"]


def test_without_meta_nothing_is_sent(footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    http = Http()
    monkeypatch.setattr(meta, "_request", http)
    result = verify_moments(found((0.0, 3.0)), "the wooden bowl", footage, tmp_path / "meta", 0.05)
    assert http.calls == [] and result["status"] == "unavailable" and result["moments"][0]["verified"] is None
    assert result["receipt"]["passed"] is False and result["receipt"]["name"] == "sam: found moments checked on screen"


def test_a_re_check_sam_already_answered_is_free_even_when_the_budget_is_spent(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http(bowl_text(range(45)))
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "meta"
    first = verify_moments(found((0.0, 3.0)), "the wooden bowl", footage, work, 0.05)
    assert first["status"] == "verified" and http.paid() == 1 and first["spent_usd"] == 0.01
    # The rest of the budget goes to the search itself. SAM's stored answer costs nothing to read again.
    meta._reserve(work, "find", "muse-spark-1.3", 0.04, 0.05)
    again = verify_moments(found((0.0, 3.0)), "the wooden bowl", footage, work, 0.05)
    assert again["status"] == "verified" and http.paid() == 1 and again["spent_usd"] == 0.0 and again["settings"]["planned_usd"] == 0.0
    assert again["moments"][0]["verified"] is True and again["spend"][0]["cached"] is True
    # A new window still has to be paid for, and is refused when it does not fit.
    third = verify_moments(found((0.0, 3.0), (6.0, 9.0)), "the wooden bowl", footage, work, 0.05)
    assert third["status"] == "refused" and http.paid() == 1 and "needs up to $0.0100" in third["reason"]


def test_a_billed_answer_sam_could_not_read_is_still_counted_as_spend(enabled: None, footage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http(lambda start, subject: "<0f>garbage that is not a box record" if start == 0 else bowl_text(range(45)))
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "meta"
    result = verify_moments(found((0.0, 3.0), (6.0, 9.0)), "the wooden bowl", footage, work, 0.05)
    assert http.paid() == 2 and [moment["verified"] for moment in result["moments"]] == [True, None]
    assert "SAM did not finish" in result["moments"][1]["verification"]
    assert [item["cost_usd"] for item in result["spend"]] == [0.01, 0.01] and result["spent_usd"] == 0.02
    assert result["spend"][0]["error"] and "malformed" in result["spend"][0]["error"]
    assert any(warning.startswith("SAM was billed $0.0100") and "could not be read" in warning for warning in result["warnings"])
    # The result's bill agrees with the project's ledger.
    assert result["spent_usd"] == pytest.approx(sum(call["usage"]["cost_usd"] for call in ledger(work)))
    assert result["receipt"]["passed"] is False


def test_only_the_public_decoder_is_used() -> None:
    # The SAM License bars reverse engineering: masks are read only through meta.read_mask.
    source = Path(verify_find.__file__).read_text(encoding="utf-8")
    assert "_mask_codec" not in source and "_decode_lossless_raster" not in source and "meta_sam_parser" not in source
    assert re.findall(r"\bmeta\._\w+", source) == []
    assert "meta.read_mask" in source
