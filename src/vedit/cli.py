"""vedit — tell it the edit, it writes the program, then it renders."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vedit import __version__
from vedit.compile import compile_brief
from vedit.doctor import format_doctor, run_doctor, suggest_whisper_model
from vedit.dsl import parse_do
from vedit.errors import VeditError
from vedit.ir import brief_dumps, load_brief
from vedit.media import probe
from vedit.pipeline import execute, load_pipeline, write_codegen, write_report
from vedit.presets import PRESETS
from vedit.recipes import apply_recipe, list_recipes


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    try:
        return func(args)
    except VeditError as exc:
        print(f"vedit: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vedit",
        description="Local agent-native video editor. Intent → inspectable FFmpeg program → render.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"vedit {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("doctor", help="check local toolchain")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("probe", help="print media info as JSON")
    p.add_argument("file")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("plan", help="compile a brief to scripts without rendering")
    p.add_argument("brief")
    p.add_argument("--work-dir")
    _add_hw_flags(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("compile", help="alias of plan")
    p.add_argument("brief")
    p.add_argument("--work-dir")
    _add_hw_flags(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("render", help="compile + execute a brief, or run an existing work dir")
    p.add_argument("brief", nargs="?", help="brief YAML/JSON, a work dir with project.json, or omit with --resume --work-dir")
    p.add_argument("--work-dir")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume", action="store_true", help="execute existing project.json; skip finished steps")
    _add_hw_flags(p)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("execute", help="run an existing planned work dir (no recompile)")
    p.add_argument("work_dir")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.set_defaults(func=cmd_execute)

    p = sub.add_parser("do", help="one-shot: flags or constrained English")
    p.add_argument("--work-dir")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--plan", action="store_true", help="write scripts only; do not render")
    _add_hw_flags(p)
    p.add_argument(
        "spec",
        nargs=argparse.REMAINDER,
        help="input + flags (talk.mp4 --cut-silence --title Hi) or a quoted English edit",
    )
    p.set_defaults(func=cmd_do)

    p = sub.add_parser("recipe", help="named end-to-end jobs")
    rsub = p.add_subparsers(dest="recipe_cmd", required=True)
    r = rsub.add_parser("list")
    r.set_defaults(func=cmd_recipe_list)
    r = rsub.add_parser("apply")
    r.add_argument("name")
    r.add_argument("--in", dest="inp")
    r.add_argument("--music")
    r.add_argument("--logo")
    r.add_argument("--title")
    r.add_argument("--images", nargs="+")
    r.add_argument("--work-dir")
    r.add_argument("--dry-run", action="store_true")
    _add_hw_flags(r)
    r.set_defaults(func=cmd_recipe_apply)

    p = sub.add_parser("presets", help="list export presets")
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("agent", help="editorial session")
    p.add_argument("goal")
    p.add_argument("paths", nargs="*")
    p.add_argument("--work-dir")
    p.add_argument("--plan", action="store_true", help="do not render")
    p.add_argument("--no-analyze", action="store_true")
    p.add_argument("--revise", type=int, default=1)
    _add_hw_flags(p)
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("tools", help="stdio tool protocol")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("studio", help="local review server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="open a browser")
    p.set_defaults(func=cmd_studio)

    p = sub.add_parser("package", help="package the desktop app")
    psub = p.add_subparsers(dest="package_cmd", required=True)
    d = psub.add_parser("dmg", help="build a macOS disk image")
    d.add_argument("--out", default="dist/Briefcut.dmg")
    d.add_argument("--app-dir")
    d.set_defaults(func=cmd_package_dmg)
    return parser


def cmd_doctor(_args: argparse.Namespace) -> int:
    checks = run_doctor()
    sys.stdout.write(format_doctor(checks))
    if not any(c.name == "whisper-cli" and c.ok for c in checks):
        sys.stdout.write("\n" + suggest_whisper_model())
    return 0 if all(c.ok for c in checks if c.required) else 1


def cmd_probe(args: argparse.Namespace) -> int:
    info = probe(args.file)
    payload = {
        "path": str(info.path),
        "duration": info.duration,
        "format": info.format_name,
        "size_bytes": info.size_bytes,
        "width": info.width,
        "height": info.height,
        "fps": info.fps,
        "has_video": info.has_video,
        "has_audio": info.has_audio,
        "streams": [
            {
                "index": s.index,
                "type": s.codec_type,
                "codec": s.codec_name,
                "width": s.width,
                "height": s.height,
                "fps": s.fps,
                "sample_rate": s.sample_rate,
                "channels": s.channels,
            }
            for s in info.streams
        ],
    }
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _add_hw_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--hw", dest="hw_flag", action="store_const", const=True, help="force VideoToolbox")
    group.add_argument("--no-hw", dest="hw_flag", action="store_const", const=False, help="force libx264")
    parser.set_defaults(hw_flag=None)


def _run_brief(
    brief_path_or_obj,
    *,
    work_dir: str | None,
    dry_run: bool,
    execute_it: bool,
    hw_flag: bool | None = None,
) -> int:
    if isinstance(brief_path_or_obj, (str, Path)):
        brief = load_brief(brief_path_or_obj)
    else:
        brief = brief_path_or_obj
    if hw_flag is not None:
        brief.hw = hw_flag
    compiler = compile_brief(brief, work_dir=Path(work_dir) if work_dir else None)
    artifacts = write_codegen(compiler.pipeline)
    (compiler.work_dir / "brief.yaml").write_text(brief_dumps(brief), encoding="utf-8")
    print(f"work dir : {compiler.work_dir}")
    print(f"program  : {artifacts['project']}")
    print(f"script   : {artifacts['render']}")
    if artifacts.get("otio"):
        print(f"otio     : {artifacts['otio']}")
    print(f"steps    : {len(compiler.pipeline.steps)}")
    for step in compiler.pipeline.steps:
        print(f"  - {step.name}: {step.description}")
    if not execute_it:
        return 0
    reports = execute(compiler.pipeline, dry_run=dry_run)
    report = write_report(compiler.pipeline, reports, finals=compiler.finals)
    print(f"report   : {report}")
    for path in compiler.finals:
        print(f"output   : {path}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    return _run_brief(
        args.brief,
        work_dir=args.work_dir,
        dry_run=True,
        execute_it=False,
        hw_flag=getattr(args, "hw_flag", None),
    )


def cmd_render(args: argparse.Namespace) -> int:
    if args.resume and args.work_dir:
        return _execute_dir(Path(args.work_dir), dry_run=args.dry_run, resume=True)
    if not args.brief:
        print("vedit render: pass a brief, a work dir, or --resume --work-dir DIR", file=sys.stderr)
        return 2
    target = Path(args.brief)
    if target.is_dir() and (target / "project.json").is_file():
        return _execute_dir(target, dry_run=args.dry_run, resume=True)
    if target.is_file() and target.name == "project.json":
        return _execute_dir(target.parent, dry_run=args.dry_run, resume=True)
    return _run_brief(
        args.brief,
        work_dir=args.work_dir,
        dry_run=args.dry_run,
        execute_it=True,
        hw_flag=getattr(args, "hw_flag", None),
    )


def cmd_execute(args: argparse.Namespace) -> int:
    return _execute_dir(Path(args.work_dir), dry_run=args.dry_run, resume=bool(getattr(args, "resume", False)))


def _execute_dir(root: Path, *, dry_run: bool, resume: bool) -> int:
    pipe = load_pipeline(root)
    print(f"work dir : {pipe.work_dir}")
    print(f"steps    : {len(pipe.steps)}")
    for step in pipe.steps:
        print(f"  - {step.name}: {step.description}")
    reports = execute(pipe, dry_run=dry_run, resume=resume)
    finals = [Path(p) for p in (pipe.meta.get("finals") or [])]
    report = write_report(pipe, reports, finals=finals)
    print(f"report   : {report}")
    for path in finals:
        print(f"output   : {path}")
    return 0


def _peel_do_flags(tokens: list[str]) -> tuple[list[str], str | None, bool, bool, bool | None]:
    """Allow --dry-run / --work-dir / --plan / --hw anywhere in `vedit do …`."""
    work_dir: str | None = None
    dry_run = False
    plan = False
    hw_flag: bool | None = None
    kept: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--dry-run":
            dry_run = True
            i += 1
            continue
        if tok == "--plan":
            plan = True
            i += 1
            continue
        if tok == "--hw":
            hw_flag = True
            i += 1
            continue
        if tok == "--no-hw":
            hw_flag = False
            i += 1
            continue
        if tok == "--work-dir" and i + 1 < len(tokens):
            work_dir = tokens[i + 1]
            i += 2
            continue
        kept.append(tok)
        i += 1
    return kept, work_dir, dry_run, plan, hw_flag


def cmd_do(args: argparse.Namespace) -> int:
    tokens = list(args.spec)
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]
    tokens, peeled_work, peeled_dry, peeled_plan, peeled_hw = _peel_do_flags(tokens)
    if not tokens:
        print("vedit do: pass an input file and flags, or a quoted English edit", file=sys.stderr)
        return 2
    brief = parse_do(tokens)
    hw_flag = peeled_hw if peeled_hw is not None else getattr(args, "hw_flag", None)
    return _run_brief(
        brief,
        work_dir=args.work_dir or peeled_work,
        dry_run=bool(args.dry_run or peeled_dry),
        execute_it=not (peeled_plan or getattr(args, "plan", False)),
        hw_flag=hw_flag,
    )


def cmd_recipe_list(_args: argparse.Namespace) -> int:
    for name, desc in list_recipes().items():
        print(f"{name:16} {desc}")
    return 0


def cmd_recipe_apply(args: argparse.Namespace) -> int:
    brief = apply_recipe(
        args.name,
        inp=args.inp,
        music=args.music,
        images=args.images,
        logo=args.logo,
        title=args.title,
    )
    return _run_brief(
        brief,
        work_dir=args.work_dir,
        dry_run=args.dry_run,
        execute_it=True,
        hw_flag=getattr(args, "hw_flag", None),
    )


def cmd_presets(_args: argparse.Namespace) -> int:
    for name, preset in PRESETS.items():
        print(f"{name:20} {preset.description}")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    from vedit.agent.loop import run_session

    goal = args.goal
    paths = args.paths
    work_dir = args.work_dir
    plan = args.plan
    no_analyze = args.no_analyze
    hw_flag = args.hw_flag
    revise = args.revise
    session = run_session(
        goal,
        list(paths),
        root=Path(work_dir) if work_dir else None,
        render=not plan,
        analyze=not no_analyze,
        hw=hw_flag,
        max_revisions=revise,
    )
    root = session.root
    print(f"session  : {root}")
    print(f"status   : {session.status}")
    print(f"work dir : {session.work_dir}")
    print(f"brief    : {root / 'brief.yaml'}")
    decisions = session.decisions or {}
    for key in ("pauses_sentence", "shape_sentence", "captions_sentence", "clip_sentence"):
        line = decisions.get(key)
        if isinstance(line, str) and line:
            print(line)
    if session.primary_output:
        print(f"output   : {session.primary_output}")
    proof = session.proof
    if isinstance(proof, dict) and proof:
        print(
            f"probe    : {proof.get('duration')} {proof.get('width')}x{proof.get('height')} "
            f"audio={proof.get('has_audio')} video={proof.get('has_video')}"
        )
    if session.critique is not None:
        for finding in session.critique.findings:
            print(f"{finding.severity} {finding.code}: {finding.message}")
    if session.status in {"planned", "verified"}:
        return 0
    if session.status == "failed":
        return 3
    return 1


def cmd_tools(_args: argparse.Namespace) -> int:
    from vedit.agent.tools import serve_stdio

    return serve_stdio()


def cmd_studio(args: argparse.Namespace) -> int:
    try:
        from vedit.studio.server import serve
    except ImportError:
        from vedit.studio import serve

    serve(args.host, args.port, open_browser=args.open)
    return 0


def cmd_package_dmg(args: argparse.Namespace) -> int:
    from vedit.packaging import create_dmg, stage_app

    app_dir = Path(args.app_dir) if args.app_dir else Path("dist/Briefcut.app")
    out = args.out
    stage_app(app_dir)
    create_dmg(app_dir, Path(out))
    print(f"app      : {app_dir}")
    print(f"dmg      : {Path(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
