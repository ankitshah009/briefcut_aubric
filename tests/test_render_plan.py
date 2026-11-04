"""A plan becomes compile ops in one fixed order, and each setting reaches its stage."""

from __future__ import annotations

from pathlib import Path

import pytest

from vedit import render_plan
from vedit.edit_spec import default_edit, merge_edit, validate_edit
from vedit.ir import Canvas
from vedit.render_plan import assembly_op, brief_sources, build_ops, context, delivery_preset, has_sound

CANVAS = Canvas(width=1080, height=1920)


def _plan(*ranges: tuple[float, float], **edit) -> dict:
    return {"title": "Bowl", "segments": [{"start": start, "end": end} for start, end in ranges],
            "edit": validate_edit(merge_edit(default_edit(), edit), duration=100.0, assets=None),
            "analysis": {"speech": [{"start": 0.0, "end": 1.0}, {"start": 3.0, "end": 8.0}]}}


def _context(plan: dict, tmp_path: Path, **extra):
    extra.setdefault("request", {"language": "en"})
    return context(plan, aspect="9:16", canvas=CANVAS, work=tmp_path, has_audio=extra.pop("has_audio", True), **extra)


def _names(ops: list[dict]) -> list[str]:
    return [op["op"] for op in ops]


def _asset(tmp_path: Path, name: str, kind: str) -> dict:
    path = tmp_path / name
    path.write_bytes(b"x")
    return {"kind": kind, "path": str(path), "name": name}


def test_a_plain_plan_keeps_the_fused_center_crop(tmp_path) -> None:
    ops = build_ops(_context(_plan((1.0, 2.0)), tmp_path))
    assert _names(ops) == ["reframe", "select_ranges", "loudnorm", "thumbnail", "export"]
    assert ops[0] == {"op": "reframe", "aspect": "9:16", "mode": "fill"}
    assert ops[1] == {"op": "select_ranges", "ranges": [{"start": 1.0, "end": 2.0}]}
    # The default rate matches the source, which only the compiler can read, so it always travels.
    assert ops[-1] == {"op": "export", "preset": "shorts_9x16", "path": str(tmp_path / "video.mp4"), "fps": "source"}
    assert ops[-2]["at"] == pytest.approx(1 / 3) and ops[-2]["path"].endswith("cover.jpg")


def test_every_stage_runs_in_the_contract_order(tmp_path) -> None:
    assets = {"a" * 8: _asset(tmp_path, "bed.wav", "audio"), "b" * 8: _asset(tmp_path, "logo.png", "image"),
              "c" * 8: _asset(tmp_path, "over.png", "image"), "d" * 8: _asset(tmp_path, "back.png", "image")}
    plan = _plan((0.0, 4.0), (5.0, 9.0),
                 look={"preset": "warm"}, transition={"type": "fade", "seconds": 0.3}, stabilize=True, denoise_picture=True,
                 audio={"enhance_voice": True}, zoom={"mode": "slow"}, texts=[{"text": "Hi", "start": 0, "duration": 1}],
                 logo={"asset": "b" * 8}, overlays=[{"asset": "c" * 8, "start": 1, "duration": 1}],
                 fade={"in": 0.2, "out": 0.3}, music={"asset": "a" * 8},
                 cutout={"mode": "image", "subject": "bowl", "asset": "d" * 8})
    subtitle, matte = tmp_path / "captions.ass", tmp_path / "matte.mkv"
    ctx = _context(plan, tmp_path, assets=assets, subtitle=subtitle, matte=matte, tracking=[{"time": 0, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}],
                   effect={"preset": "float", "intensity": 0.5, "start": 0, "duration": 2})
    ops = build_ops(ctx)
    assert _names(ops) == [
        "select_ranges", "cutout", "track_reframe", "stabilize", "denoise", "zoom", "spatial_effect", "look", "voice",
        "text", "logo", "overlay", "captions", "fade_in", "fade_out", "music", "loudnorm", "thumbnail", "export",
    ]
    assert ctx.warnings == []
    assert brief_sources(ctx, tmp_path / "source.mp4") == {
        "footage": str(tmp_path / "source.mp4"), "a" * 8: assets["a" * 8]["path"], "b" * 8: assets["b" * 8]["path"],
        "c" * 8: assets["c" * 8]["path"], "d" * 8: assets["d" * 8]["path"],
    }


def test_a_cutout_moves_the_crop_after_the_matte(tmp_path) -> None:
    plan = _plan((0.0, 4.0), cutout={"mode": "blur", "subject": "bowl"})
    ops = build_ops(_context(plan, tmp_path, matte=tmp_path / "matte.mkv"))
    assert _names(ops)[:3] == ["select_ranges", "cutout", "reframe"]
    assert ops[1]["matte"] == str(tmp_path / "matte.mkv") and ops[1]["mode"] == "blur"
    # Without a matte the crop stays fused and the creator is told why nothing changed.
    ctx = _context(plan, tmp_path)
    assert _names(build_ops(ctx))[:3] == ["reframe", "select_ranges", "loudnorm"]
    assert any("cutout" in warning for warning in ctx.warnings)


def test_assembly_carries_keep_speed_volume_and_the_real_transition(tmp_path) -> None:
    plan = _plan((0.0, 8.0), (10.0, 10.5), trim_pauses=True, transition={"type": "wipeleft", "seconds": 2.0})
    plan["segments"][0].update(speed=1.5, volume=0.5)
    ops = build_ops(_context(plan, tmp_path))
    assembled = next(op for op in ops if op["op"] == "select_ranges")
    first, second = assembled["ranges"]
    assert first["keep"] and first["speed"] == 1.5 and first["volume"] == 0.5
    assert second == {"start": 10.0, "end": 10.5}
    # The op holds what the clock really uses, never the wish that was clamped.
    assert assembled["transition"] == {"type": "wipeleft", "seconds": 0.2}


def test_mute_source_silences_every_range_and_skips_loudness_without_music(tmp_path) -> None:
    plan = _plan((0.0, 4.0), (5.0, 6.0), audio={"mute_source": True, "enhance_voice": True})
    ops = build_ops(_context(plan, tmp_path))
    assembled = next(op for op in ops if op["op"] == "select_ranges")
    assert [part["volume"] for part in assembled["ranges"]] == [0.0, 0.0]
    assert "loudnorm" not in _names(ops) and "voice" not in _names(ops)


def test_no_audio_means_no_audio_stages(tmp_path) -> None:
    plan = _plan((0.0, 4.0), audio={"enhance_voice": True, "denoise": True})
    assert _names(build_ops(_context(plan, tmp_path, has_audio=False))) == ["reframe", "select_ranges", "thumbnail", "export"]


def test_keep_loudness_skips_normalization(tmp_path) -> None:
    assert "loudnorm" not in _names(build_ops(_context(_plan((0.0, 4.0), audio={"keep_loudness": True}), tmp_path)))


def test_zoom_moves_land_on_the_output_clock(tmp_path) -> None:
    plan = _plan((0.0, 4.0), (5.0, 7.0), (8.0, 10.0), zoom={"mode": "punch", "strength": 0.5}, transition={"type": "fade", "seconds": 0.5})
    plan["segments"][2]["motion"] = {"from": {"scale": 1.2, "x": 0.3, "y": 0.4}, "to": {"scale": 1.0, "x": 0.5, "y": 0.5}}
    ops = build_ops(_context(plan, tmp_path))
    moves = next(op for op in ops if op["op"] == "zoom")["moves"]
    assert [(move["start"], move["end"]) for move in moves] == [(3.5, 5.5), (5.0, 7.0)]
    assert moves[0]["from"] == moves[0]["to"] and moves[0]["from"]["scale"] == 1.15  # punch: alternate moments, held
    assert moves[1]["from"]["scale"] == 1.2  # an explicit move wins over the zoom mode


def test_slow_zoom_covers_every_moment(tmp_path) -> None:
    plan = _plan((0.0, 2.0), (3.0, 4.0), zoom={"mode": "slow", "strength": 1.0})
    moves = next(op for op in build_ops(_context(plan, tmp_path)) if op["op"] == "zoom")["moves"]
    assert len(moves) == 2 and moves[1]["from"]["scale"] == 1.0 and moves[1]["to"]["scale"] == pytest.approx(1.18)


def test_a_look_at_its_defaults_adds_nothing(tmp_path) -> None:
    assert "look" not in _names(build_ops(_context(_plan((0.0, 4.0)), tmp_path)))
    adjusted = build_ops(_context(_plan((0.0, 4.0), look={"contrast": 0.2}), tmp_path))
    assert next(op for op in adjusted if op["op"] == "look")["contrast"] == 0.2


def test_a_missing_asset_file_warns_instead_of_failing(tmp_path) -> None:
    plan = _plan((0.0, 4.0), music={"asset": "a" * 8}, logo={"asset": "b" * 8}, overlays=[{"asset": "c" * 8, "start": 0, "duration": 1}])
    ctx = _context(plan, tmp_path, assets={"a" * 8: {"kind": "audio", "path": str(tmp_path / "gone.wav")}})
    assert _names(build_ops(ctx)) == ["reframe", "select_ranges", "loudnorm", "thumbnail", "export"]
    assert len(ctx.warnings) == 3


def test_music_gives_a_silent_clip_sound_and_loudness(tmp_path) -> None:
    plan = _plan((0.0, 4.0), music={"asset": "a" * 8})
    assets = {"a" * 8: _asset(tmp_path, "bed.wav", "audio")}
    ctx = _context(plan, tmp_path, has_audio=False, assets=assets)
    assert has_sound(ctx) and _names(build_ops(ctx)) == ["reframe", "select_ranges", "music", "loudnorm", "thumbnail", "export"]
    gone = _context(plan, tmp_path, has_audio=False)
    assert not has_sound(gone) and "loudnorm" not in _names(build_ops(gone))


def test_a_muted_clip_keeps_only_its_music_bed(tmp_path) -> None:
    plan = _plan((0.0, 4.0), music={"asset": "a" * 8}, audio={"mute_source": True})
    ops = build_ops(_context(plan, tmp_path, assets={"a" * 8: _asset(tmp_path, "bed.wav", "audio")}))
    assert next(op for op in ops if op["op"] == "music")["mute_source"] is True
    assert "loudnorm" in _names(ops)


def test_delivery_reads_the_export_choice() -> None:
    untouched = delivery_preset("shorts_9x16", default_edit())
    assert untouched.name == "export_1080x1920_source_mp4" and untouched.canvas.fps == 0  # 0: the compiler sets the source's rate
    chosen = delivery_preset("shorts_9x16", merge_edit(default_edit(), {"export": {"quality": "4k", "fps": 60}}))
    assert (chosen.canvas.width, chosen.canvas.height, chosen.canvas.fps) == (2160, 3840, 60)


def test_export_options_are_only_what_changed(tmp_path) -> None:
    ops = build_ops(_context(_plan((0.0, 4.0), export={"quality": "720p", "fps": 24}), tmp_path))
    assert ops[-1]["quality"] == "720p" and ops[-1]["fps"] == 24 and "format" not in ops[-1]


def test_the_same_assembly_serves_renders_and_tracking(tmp_path) -> None:
    plan = _plan((0.0, 8.0), trim_pauses=True)
    ctx = _context(plan, tmp_path)
    assert assembly_op(ctx.view, ctx.edit) == next(op for op in build_ops(ctx) if op["op"] == "select_ranges")


def test_stages_cover_every_step_of_the_contract() -> None:
    names = [stage.__name__ for stage in render_plan.STAGES]
    for name in ("assembly", "cutout", "stabilize", "denoise", "zoom", "spatial_effect", "look", "voice", "texts", "logo", "overlays", "captions", "fades", "music", "loudness", "thumbnail", "export"):
        assert name in names
    assert names.index("assembly") < names.index("cutout") < names.index("zoom") < names.index("look") < names.index("captions") < names.index("fades") < names.index("music")
