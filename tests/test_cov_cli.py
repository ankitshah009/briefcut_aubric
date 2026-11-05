"""The `vedit` command line: every subcommand's exit code, printed lines, and refusals, through main()."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from vedit import settings
from vedit.cli import briefcut_main, main
from vedit.media import resolve_ffmpeg
from vedit.presets import PRESETS
from vedit.recipes import list_recipes

KEY = "test-meta-key-0123456789"


def _have_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


render = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg required")


def _clip(path: Path, *, seconds: float = 1.0) -> Path:
    subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=s=320x180:r=30:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture
def no_checkout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """This checkout may hold a real key in .env.local; the CLI must not read it during a test."""
    monkeypatch.setattr(settings, "load_checkout_env", list)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)


def test_no_subcommand_prints_help_and_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    out = capsys.readouterr().out
    assert "usage: vedit" in out and "render" in out and "doctor" in out


def test_an_unknown_subcommand_is_an_argparse_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2


def test_presets_lists_every_export_preset_with_its_description(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["presets"]) == 0
    out = capsys.readouterr().out
    for name, preset in PRESETS.items():
        assert name in out and preset.description in out


def test_recipe_list_names_every_recipe(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["recipe", "list"]) == 0
    out = capsys.readouterr().out
    for name, description in list_recipes().items():
        assert name in out and description in out


def test_recipe_apply_without_an_input_fails_with_the_tool_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["recipe", "apply", "youtube"]) == 2
    assert capsys.readouterr().err.strip() == "vedit: recipe youtube requires --in"
    assert main(["recipe", "apply", "karaoke", "--in", "a.mp4"]) == 2
    assert "vedit: unknown recipe 'karaoke'" in capsys.readouterr().err


def test_briefcut_with_arguments_is_vedit_under_its_own_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert briefcut_main(["recipe", "apply", "youtube"]) == 2
    assert capsys.readouterr().err.strip() == "briefcut: recipe youtube requires --in"


def test_probe_of_a_missing_file_is_a_clean_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["probe", str(tmp_path / "gone.mp4")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("vedit: ") and "file does not exist" in captured.err


@render
def test_probe_prints_the_file_as_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clip = _clip(tmp_path / "clip.mp4")
    assert main(["probe", str(clip)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(clip.resolve())
    assert (payload["width"], payload["height"]) == (320, 180)
    assert payload["has_video"] is True and payload["has_audio"] is True
    assert 0.9 <= payload["duration"] <= 1.2
    kinds = {stream["type"] for stream in payload["streams"]}
    assert kinds == {"video", "audio"}
    audio = next(stream for stream in payload["streams"] if stream["type"] == "audio")
    assert audio["sample_rate"] == 48000 and audio["codec"] == "aac"


@render
@pytest.mark.parametrize("verb", ["plan", "compile"])
def test_plan_writes_the_program_and_renders_nothing(
    verb: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clip = _clip(tmp_path / "clip.mp4")
    brief = tmp_path / "brief.yaml"
    brief.write_text(
        "name: planned\nsources:\n  main: clip.mp4\nops:\n  - op: trim\n    start: 0\n    end: 0.5\n"
        "  - op: export\n    presets: [web_preview]\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    assert main([verb, str(brief), "--work-dir", str(work), "--no-hw"]) == 0
    out = capsys.readouterr().out
    assert f"work dir : {work}" in out and "steps    :" in out
    assert "report   :" not in out and "dry run" not in out, "plan neither renders nor pretends to"
    assert (work / "project.json").is_file() and (work / "render.sh").is_file()
    saved = (work / "brief.yaml").read_text(encoding="utf-8")
    assert "hw: false" in saved, "--no-hw is written into the brief the program was compiled from"
    assert str(clip.resolve()) in saved, "relative sources resolve against the brief file"
    assert not list(work.glob("*.mp4")), "nothing was rendered"


@render
def test_hw_flag_is_recorded_in_the_planned_brief(tmp_path: Path) -> None:
    _clip(tmp_path / "clip.mp4")
    brief = tmp_path / "brief.yaml"
    brief.write_text(
        "name: x\nsources:\n  main: clip.mp4\nops:\n  - op: export\n    presets: [web_preview]\n", encoding="utf-8"
    )
    work = tmp_path / "work"
    assert main(["plan", str(brief), "--work-dir", str(work), "--hw"]) == 0
    assert "hw: true" in (work / "brief.yaml").read_text(encoding="utf-8")


def test_plan_of_a_brief_that_is_not_a_mapping_fails_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    brief = tmp_path / "list.yaml"
    brief.write_text("- op: trim\n", encoding="utf-8")
    assert main(["plan", str(brief)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("vedit: ") and "expected a mapping at the top level" in err


def test_do_with_nothing_to_do_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["do"]) == 2
    assert "pass an input file and flags, or a quoted English edit" in capsys.readouterr().err
    assert main(["do", "--", "--dry-run", "--plan"]) == 2, "flags alone are not an edit"


def test_do_with_an_unparseable_edit_names_the_clause(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["do", "--", "make it pop; captions"]) == 2
    assert "could not parse clause" in capsys.readouterr().err


@render
def test_do_plan_writes_scripts_from_flags_and_does_not_render(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    work = tmp_path / "work"
    assert main(["do", str(clip), "--trim", "0-0.5", "--preset", "web_preview", "--plan", "--work-dir", str(work)]) == 0
    out = capsys.readouterr().out
    assert f"work dir : {work}" in out and "trim" in out and "export" in out
    assert "report   :" not in out
    project = json.loads((work / "project.json").read_text(encoding="utf-8"))
    assert project, "a program was written"
    assert not list(work.glob("*.mp4"))


@render
def test_do_plan_accepts_english_given_as_separate_words(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    work = tmp_path / "work"
    words = f"trim 0 to 0.5 of {clip}; render a preview".split()
    assert main(["do", "--work-dir", str(work), "--plan", "--", *words]) == 0
    assert "trim" in capsys.readouterr().out
    assert "web_preview" in (work / "brief.yaml").read_text(encoding="utf-8")


@render
def test_do_dry_run_executes_nothing_and_writes_no_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    work = tmp_path / "work"
    assert (
        main(["do", str(clip), "--trim", "0-0.5", "--preset", "web_preview", "--dry-run", "--work-dir", str(work)]) == 0
    )
    out = capsys.readouterr().out
    assert "dry run  : nothing executed" in out and "report   :" not in out
    assert not (work / "report.json").exists() and not list(work.glob("*.mp4"))


@render
def test_recipe_apply_dry_run_plans_the_recipe_on_a_real_clip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clip = _clip(tmp_path / "talk.mp4")
    work = tmp_path / "work"
    assert main(["recipe", "apply", "shorts", "--in", str(clip), "--dry-run", "--work-dir", str(work), "--no-hw"]) == 0
    out = capsys.readouterr().out
    assert "dry run  : nothing executed" in out
    saved = (work / "brief.yaml").read_text(encoding="utf-8")
    assert "shorts_9x16" in saved and "reframe" in saved and "hw: false" in saved


def test_meta_status_without_a_key_exits_1_and_says_how_to_add_one(
    no_checkout_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["meta", "status"]) == 1
    out = capsys.readouterr().out
    assert "configured  no (add a key in Settings, or set MODEL_API_KEY)" in out
    assert "enabled     no" in out
    assert "Add your Meta API key in Settings" in out
    assert "transcribe" in out and "story" in out, "every feature's model is listed"


def test_meta_status_with_a_key_and_calls_allowed_exits_0_without_printing_the_key(
    no_checkout_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")
    assert main(["meta", "status"]) == 0
    out = capsys.readouterr().out
    assert "configured  yes" in out and "enabled     yes" in out
    assert KEY not in out and KEY[-4:] not in out


def test_meta_check_without_a_key_exits_1_and_makes_no_call(
    no_checkout_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["meta", "check"]) == 1
    out = capsys.readouterr().out
    assert "Add your Meta API key in Settings" in out
    assert "models visible" not in out


def test_meta_check_with_a_key_but_calls_off_exits_1(
    no_checkout_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("VEDIT_META_ENABLED", "0")
    assert main(["meta", "check"]) == 1
    assert "Turn on Meta in Settings" in capsys.readouterr().out


def test_doctor_without_a_key_fails_the_required_meta_row(
    no_checkout_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("vedit doctor\n")
    assert "NO  meta" in out and "[required]" in out
    assert "no Meta API key" in out and "https://dev.meta.ai" in out
    assert "Host is not ready" in out


def test_tools_serves_json_lines_over_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO('{"tool":"specs"}\nnot json\n{"tool":"parse_intent","arguments":{"goal":"shorts of a.mp4"}}\n'),
    )
    assert main(["tools"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 3
    assert lines[0]["ok"] is True and any(spec["name"] == "render" for spec in lines[0]["result"])
    assert lines[1]["ok"] is False and "Expecting value" in lines[1]["error"]
    assert lines[2]["ok"] is True and lines[2]["result"]["platform"] == "shorts"


def test_mcp_without_the_mcp_package_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "vedit.mcp_server", None)
    assert main(["mcp"]) == 2
    err = capsys.readouterr().err
    assert "needs the `mcp` package" in err and "uv sync" in err


def test_studio_passes_host_port_and_browser_choice_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import vedit.studio.server as server

    seen: list[tuple] = []
    monkeypatch.setattr(server, "serve", lambda host, port, open_browser=False: seen.append((host, port, open_browser)))
    monkeypatch.setattr(settings, "enter_workspace", lambda default=None: tmp_path)
    assert main(["studio", "--host", "0.0.0.0", "--port", "9001", "--open"]) == 0
    assert seen == [("0.0.0.0", 9001, True)]
    assert main(["studio"]) == 0
    assert seen[-1] == ("127.0.0.1", 8765, False)


def test_package_dmg_reports_when_python_is_left_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import vedit.packaging as packaging

    staged: list[tuple[Path, bool]] = []
    built: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        packaging, "stage_app", lambda app_dir, bundle_python=True: staged.append((app_dir, bundle_python))
    )
    monkeypatch.setattr(packaging, "create_dmg", lambda app_dir, out: built.append((app_dir, out)))
    app = tmp_path / "Briefcut.app"
    out = tmp_path / "Briefcut.dmg"
    assert main(["package", "dmg", "--no-python", "--app-dir", str(app), "--out", str(out)]) == 0
    assert staged == [(app, False)] and built == [(app, out)]
    printed = capsys.readouterr().out
    assert f"app      : {app}" in printed and f"dmg      : {out}" in printed
    assert "python   : not bundled" in printed


def test_package_dmg_is_quiet_about_python_when_it_is_bundled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import vedit.packaging as packaging

    app = tmp_path / "Briefcut.app"
    (app / "Contents" / "Resources" / "python").mkdir(parents=True)
    monkeypatch.setattr(packaging, "stage_app", lambda app_dir, bundle_python=True: None)
    monkeypatch.setattr(packaging, "create_dmg", lambda app_dir, out: None)
    assert main(["package", "dmg", "--app-dir", str(app), "--out", str(tmp_path / "x.dmg")]) == 0
    assert "not bundled" not in capsys.readouterr().out


def test_agent_with_no_media_anywhere_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "session"
    assert main(["agent", "edit the board meeting", "--work-dir", str(root), "--plan"]) == 3
    out = capsys.readouterr().out
    assert f"session  : {root}" in out and "status   : failed" in out
    assert "fail missing_output: no media paths" in out
    assert (root / "session.json").is_file()


@render
def test_agent_plan_prints_the_decisions_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("vedit.agent.loop.transcriber_ready", lambda: False)
    clip = _clip(tmp_path / "talk.mp4")
    root = tmp_path / "session"
    assert main(["agent", "preview of the talk", str(clip), "--work-dir", str(root), "--plan", "--no-analyze"]) == 0
    out = capsys.readouterr().out
    assert "status   : planned" in out
    assert f"brief    : {root / 'brief.yaml'}" in out
    assert "Pauses will be" in out and "Clip: talk.mp4." in out
    assert "output   :" not in out and "probe    :" not in out, "a plan delivers no file and proves none"
    assert (root / "work" / "render.sh").is_file()
