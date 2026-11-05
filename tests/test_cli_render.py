"""`vedit render` and `vedit execute` control flow: a dry run writes no report, and --resume needs a work dir."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vedit import cli
from vedit.cli import main


class _Step:
    def __init__(self, name: str) -> None:
        self.name, self.description, self.outputs = name, f"{name} step", []


def _fake_compiler(work: Path) -> SimpleNamespace:
    pipeline = SimpleNamespace(
        steps=[_Step("concat"), _Step("export")], work_dir=work, meta={"finals": [str(work / "out.mp4")]}
    )
    return SimpleNamespace(pipeline=pipeline, work_dir=work, finals=[work / "out.mp4"])


@pytest.fixture
def stubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """The compiler and executor replaced, so these tests prove the CLI's own decisions and nothing else."""
    work = tmp_path / "work"
    work.mkdir()
    calls: dict[str, list] = {"execute": [], "write_report": []}
    brief = SimpleNamespace(hw=None)
    monkeypatch.setattr(cli, "load_brief", lambda path: brief)
    monkeypatch.setattr(cli, "brief_dumps", lambda brief: "name: x\n")
    monkeypatch.setattr(cli, "compile_brief", lambda brief, work_dir=None: _fake_compiler(work))
    monkeypatch.setattr(
        cli, "write_codegen", lambda pipeline: {"project": work / "project.json", "render": work / "render.sh"}
    )
    monkeypatch.setattr(cli, "load_pipeline", lambda root: _fake_compiler(work).pipeline)

    def execute(pipeline, *, dry_run=False, resume=False, **_kwargs):
        calls["execute"].append({"dry_run": dry_run, "resume": resume})
        return [SimpleNamespace(name=step.name, ok=True, duration_ms=0) for step in pipeline.steps]

    def write_report(pipeline, reports, *, finals):
        calls["write_report"].append(finals)
        path = work / "report.json"
        path.write_text(
            json.dumps({"steps": [{"name": report.name, "ok": True} for report in reports]}), encoding="utf-8"
        )
        return path

    monkeypatch.setattr(cli, "execute", execute)
    monkeypatch.setattr(cli, "write_report", write_report)
    (tmp_path / "brief.yaml").write_text("name: x\n", encoding="utf-8")
    (work / "project.json").write_text("{}", encoding="utf-8")
    calls["work"] = [work]
    calls["brief"] = [tmp_path / "brief.yaml"]
    return calls


def test_a_dry_run_writes_no_report_and_names_no_outputs(
    stubbed: dict[str, list], capsys: pytest.CaptureFixture[str]
) -> None:
    work, brief = stubbed["work"][0], stubbed["brief"][0]
    real = work / "report.json"
    real.write_text('{"steps": [{"name": "export", "ok": false}]}', encoding="utf-8")
    assert main(["render", str(brief), "--dry-run", "--work-dir", str(work)]) == 0
    out = capsys.readouterr().out
    assert stubbed["execute"] == [{"dry_run": True, "resume": False}]
    assert stubbed["write_report"] == [], "a dry run must not write a report that says every step passed in 0 ms"
    assert json.loads(real.read_text(encoding="utf-8"))["steps"][0]["ok"] is False, (
        "the real render's report is untouched"
    )
    assert "dry run  : nothing executed" in out
    assert "output   :" not in out and "report   :" not in out
    assert "work dir : " in out and "steps    : 2" in out


def test_a_dry_run_of_a_work_dir_writes_no_report(stubbed: dict[str, list], capsys: pytest.CaptureFixture[str]) -> None:
    work = stubbed["work"][0]
    assert main(["execute", str(work), "--dry-run"]) == 0
    assert stubbed["write_report"] == [] and stubbed["execute"] == [{"dry_run": True, "resume": False}]
    out = capsys.readouterr().out
    assert "dry run  : nothing executed" in out and "output   :" not in out
    assert not (work / "report.json").exists()


def test_a_real_run_still_writes_the_report_and_lists_outputs(
    stubbed: dict[str, list], capsys: pytest.CaptureFixture[str]
) -> None:
    work, brief = stubbed["work"][0], stubbed["brief"][0]
    assert main(["render", str(brief), "--work-dir", str(work)]) == 0
    out = capsys.readouterr().out
    assert len(stubbed["write_report"]) == 1
    assert f"report   : {work / 'report.json'}" in out and f"output   : {work / 'out.mp4'}" in out


def test_resume_without_a_work_dir_is_refused_for_a_brief(
    stubbed: dict[str, list], capsys: pytest.CaptureFixture[str]
) -> None:
    brief = stubbed["brief"][0]
    assert main(["render", str(brief), "--resume"]) == 2
    assert "vedit render: --resume needs --work-dir DIR" in capsys.readouterr().err
    assert stubbed["execute"] == [], "nothing was re-rendered behind a flag that was ignored"


@pytest.mark.parametrize("target", ["dir", "project.json"])
def test_a_work_dir_argument_resumes_only_with_the_flag(stubbed: dict[str, list], target: str) -> None:
    work = stubbed["work"][0]
    argument = str(work) if target == "dir" else str(work / "project.json")
    assert main(["render", argument]) == 0
    assert main(["render", argument, "--resume"]) == 0
    assert stubbed["execute"] == [{"dry_run": False, "resume": False}, {"dry_run": False, "resume": True}]


def test_resume_with_a_work_dir_runs_that_dir(stubbed: dict[str, list]) -> None:
    work = stubbed["work"][0]
    assert main(["render", "--resume", "--work-dir", str(work)]) == 0
    assert stubbed["execute"] == [{"dry_run": False, "resume": True}]


def test_a_missing_brief_is_one_line_not_a_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plan", str(tmp_path / "missing.yaml")]) == 2
    assert capsys.readouterr().err.startswith("vedit: ")


def test_the_expect_help_says_how_long_a_prefix_must_be() -> None:
    parser = cli._parser()
    verify = next(
        action
        for action in parser._subparsers._group_actions[0].choices["verify"]._actions
        if "--expect" in action.option_strings
    )
    assert "16 hex" in verify.help
