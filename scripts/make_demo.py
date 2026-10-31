#!/usr/bin/env python3
"""Write local demo assets under examples/demo/.

``talk.mp4`` / ``bed.wav`` / ``logo.png`` are tiny lavfi fixtures for tests.
``tracking.mp4`` is a 12 s clip with a red square drifting across the frame, for
SAM subject tracking demos. ``workshop.mp4`` is a ~40 s, five-scene narrated
clip of a wooden bowl being made, built procedurally from ffmpeg filters (no
real footage, no baked-in titles) with real silences and hard scene changes so
silence, scene, subject tracking and transcript analysis have structure to
find. Narration uses the macOS ``say`` voice (Samantha, falling back to the
default voice); without ``say`` the clip is silent and the script says so.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from vedit.media import MediaInfo, probe, resolve_ffmpeg, resolve_font, run_command, run_ffmpeg

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DEST = REPO / "examples" / "demo"
SECONDS = 3.0
TALK_SIZE = "640x360"
LOGO_SIZE = "128x128"
WORKSHOP_SIZE = "1280x720"
WORKSHOP_SCENE_SECONDS = 8.0
NARRATION_VOICE = "Samantha"
NARRATION_RATE = "170"
# (label, narration). ``[[slnc 1000]]`` is a one-second pause for ``say``. The
# label is documentation only; nothing is drawn on the picture.
WORKSHOP_SCENES = (
    ("Finished bowl",
     "This is the finished oak bowl, oiled and ready for the table. [[slnc 1000]] It started as a rough blank from a fallen tree."),
    ("Raw oak",
     "Here is the raw oak blank, still wet and heavy. [[slnc 1000]] The grain tells me where the bowl wants to be."),
    ("Turning",
     "On the lathe the shape appears in long steady cuts. [[slnc 1000]] I keep the wall thickness even from rim to base."),
    ("Sanding",
     "Sanding goes from coarse to fine, with the lathe turning slowly. [[slnc 1000]] This is where the grain starts to glow."),
    ("Oil finish",
     "A coat of food safe oil brings out the color. [[slnc 1000]] Twenty four hours later, the bowl is ready to use."),
)


def write_talk(path: Path, *, seconds: float = SECONDS) -> Path:
    """640x360 color field + 440 Hz sine, ~3s."""
    path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x1e3a5f:s={TALK_SIZE}:r=30:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ]
    )
    return path


def write_bed(path: Path, *, seconds: float = SECONDS) -> Path:
    """Quieter 220 Hz sine bed as PCM WAV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:sample_rate=48000:duration={seconds}",
            "-filter:a",
            "volume=-20dB",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )
    return path


def write_logo(path: Path) -> Path:
    """Single-frame solid PNG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=0xe11d48:s={LOGO_SIZE}:r=1:d=1",
            "-frames:v",
            "1",
            str(path),
        ]
    )
    return path


def _narration(text: str, dest: Path) -> Path | None:
    """Render one narration line to 48 kHz mono WAV with ``say``; None without it.

    Prefers the Samantha voice at 170 wpm and falls back to the system default
    voice when Samantha is not installed.
    """
    say = shutil.which("say")
    if say is None:
        return None
    aiff = dest.with_suffix(".aiff")
    for voice_args in (["-v", NARRATION_VOICE, "-r", NARRATION_RATE], ["-r", NARRATION_RATE]):
        try:
            subprocess.run([say, *voice_args, "-o", str(aiff), text], check=True, capture_output=True, timeout=120)
            break
        except subprocess.TimeoutExpired:
            return None
        except subprocess.CalledProcessError:
            continue
    else:
        return None
    run_ffmpeg(["-i", str(aiff), "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(dest)])
    return dest


# ---------------------------------------------------------------------------
# Procedural "footage". Every texture is one still rendered once with ``geq``;
# the scenes then animate those stills with cheap per-frame filters (rotate,
# overlay, crop, zoompan, noise). No text is baked into the picture.
# ---------------------------------------------------------------------------

BOWL_W, BOWL_H = 640, 410          # bowl layer (ellipse) size
DISC = 640                          # square grain disc that is squashed into the ellipse
PLATE_W, PLATE_H = 1408, 792        # oversize backgrounds so crops can drift


def _rgb_expr(prelude: str, tone: str, rgb: tuple[int, int, int]) -> dict[str, str]:
    """geq r/g/b expressions sharing a ``st``/``ld`` prelude and one tone multiplier."""
    return {ch: f"{prelude}{val}*({tone})" for ch, val in zip("rgb", rgb)}


def _texture(path: Path, size: str, exprs: dict[str, str], *, alpha: bool = True) -> Path:
    """Render one still with ``geq``; keys are r/g/b/a (or ``lum`` for a gray mask).

    Expressions may call ``random(9)``: ffmpeg keeps the generator state in the
    numbered variable, so slot 9 is reserved and ``st(0..8)`` stay intact.
    """
    fmt = "gbrap" if alpha else ("gray" if "lum" in exprs else "gbrp")
    geq = ":".join(f"{key}='{expr}'" for key, expr in exprs.items())
    run_ffmpeg(["-f", "lavfi", "-i", f"color=c=black:s={size}:r=1:d=1,format={fmt}",
                "-vf", f"geq={geq}", "-frames:v", "1", str(path)])
    return path


def _write_textures(work: Path) -> dict[str, Path]:
    """All stills the workshop scenes are assembled from."""
    tex: dict[str, Path] = {}
    c = DISC / 2
    # Grain disc: concentric rings with eccentric wobble, thin dark latewood
    # lines, fine grain and speckle. Alpha is the circle; the shade layer adds
    # the rim and hollow later.
    disc_prelude = (
        f"st(0,hypot(X-{c},Y-{c})/{c}+0.035*sin(X/53+1.3)+0.03*sin(Y/47)+0.015*sin((X+Y)/23));"
        "st(1,pow(0.5+0.5*sin(ld(0)*64+0.5*sin(ld(0)*9)),4));"
    )
    disc_tone = "1.06-0.34*ld(1)+0.05*sin(ld(0)*230+X/9)+0.08*(random(9)-0.5)"
    disc = _rgb_expr(disc_prelude, disc_tone, (190, 128, 64))
    disc["a"] = f"clip((1.01-hypot(X-{c},Y-{c})/{c})*4000,0,255)"
    tex["disc"] = _texture(work / "disc.png", f"{DISC}x{DISC}", disc)

    # Elliptical mask for the bowl layer (soft edge).
    ex, ey, rx, ry = BOWL_W / 2, BOWL_H / 2, BOWL_W / 2 - 4, BOWL_H / 2 - 4
    d = f"hypot((X-{ex})/{rx},(Y-{ey})/{ry})"
    tex["mask"] = _texture(work / "mask.png", f"{BOWL_W}x{BOWL_H}",
                           {"lum": f"clip((1-{d})*5000,0,255)"}, alpha=False)

    # Shade layers: a dark one (hollow with a lit far wall, inner rim shadow,
    # outer wall under the rim) and a light one (pale rim band, one soft
    # specular). Two layers so the edges blend instead of choosing a colour.
    shade_prelude = (
        f"st(0,{d});"
        f"st(1,if(lt(ld(0),0.80),clip(255*0.7*(1-ld(0)/0.80)*(0.5+0.5*(Y-{ey})/{ry}),0,255),0));"
        f"st(1,max(ld(1),if(between(ld(0),0.58,0.80),150*(1-abs(ld(0)-0.74)/0.16)"
        f"*clip(0.55-0.55*(Y-{ey})/{ry}-0.3*(X-{ex})/{rx},0,1),0)));"
        f"st(1,max(ld(1),if(gt(ld(0),0.92),140*clip((Y-{ey})/{ry},0,1)*(ld(0)-0.92)/0.08,0)));"
        "st(2,if(gt(ld(0),0.80),clip(120*(1-abs(ld(0)-0.90)/0.10),0,255),0));"
        f"st(3,170*exp(-(pow((X-{ex}+110)/105,2)+pow((Y-{ey}+85)/40,2))));"
    )
    tex["shade_dark"] = _texture(work / "shade_dark.png", f"{BOWL_W}x{BOWL_H}", {
        "r": "6", "g": "3", "b": "0", "a": f"{shade_prelude}if(lt(ld(0),1.0),ld(1),0)",
    })
    tex["shade_light"] = _texture(work / "shade_light.png", f"{BOWL_W}x{BOWL_H}", {
        "r": "255", "g": "238", "b": "205", "a": f"{shade_prelude}if(lt(ld(0),1.0),max(ld(2),ld(3)),0)",
    })

    # Contact shadow under the bowl.
    sw, sh = BOWL_W + 120, BOWL_H + 100
    sd = f"hypot((X-{sw / 2})/{sw / 2 - 2},(Y-{sh / 2})/{sh / 2 - 2})"
    tex["shadow"] = _texture(work / "shadow.png", f"{sw}x{sh}",
                             {"r": "0", "g": "0", "b": "0", "a": f"150/(1+exp(({sd}-0.92)*16))"})

    # Workbench: dark planks with along-the-board grain and seams.
    bench_prelude = (
        "st(0,floor(Y/110));"
        "st(1,(1+0.11*sin(X/19+2.2*sin(Y/13+ld(0)*7)+0.8*sin(X/3+ld(0)))+0.12*(random(9)-0.5))"
        "*(1-0.65*lt(mod(Y,110),4))"
        f"*(0.8+0.35*exp(-(pow((X-{PLATE_W / 2})/700,2)+pow((Y-300)/520,2)))));"
    )
    tex["bench"] = _texture(work / "bench.png", f"{PLATE_W}x{PLATE_H}",
                            _rgb_expr(bench_prelude, "ld(1)", (78, 48, 28)), alpha=False)

    # Bark plate: vertical fibrous grain in cool green-grey with deep fissures.
    bark_prelude = (
        "st(0,0.5+0.5*sin(X/4.5+1.8*sin(Y/33+X/120)+0.7*sin(Y/9)));"
        "st(1,0.5+0.5*sin(X/23+0.9*sin(Y/70)+0.5*sin(Y/19+X/40)+2.2*sin(X/210+Y/900)));"
        "st(2,0.5+0.5*sin(X/90+1.2*sin(Y/140)+1.5*sin(Y/31)));"
        "st(3,(0.42+0.28*ld(1)+0.3*ld(0)*ld(1)*(0.6+0.4*sin(Y/15+X/9))+0.15*ld(2)+0.2*(random(9)-0.5))"
        f"*(0.8+0.35*exp(-(pow((X-{PLATE_W / 2})/650,2)+pow((Y-350)/450,2)))));"
    )
    tex["bark"] = _texture(work / "bark.png", f"{PLATE_W}x{PLATE_H}",
                           _rgb_expr(bark_prelude, "ld(3)", (96, 112, 78)), alpha=False)

    # Raw blank: an end-grain slab (off-centre growth rings, saw marks) with a
    # dark fibrous bark band around the edge.
    bw, bh = 880, 560
    blank_prelude = (
        f"st(0,pow(abs(X-{bw / 2})/{bw / 2 - 6},3)+pow(abs(Y-{bh / 2})/{bh / 2 - 6},3));"
        f"st(1,hypot((X-{bw / 2 - 40})/{bw / 2},(Y-{bh / 2 + 20})/{bh / 2 + 20})+0.04*sin(X/37)+0.03*sin(Y/29));"
        "st(2,pow(0.5+0.5*sin(ld(1)*48+0.4*sin(ld(1)*7)),3));"
        "st(3,clip((ld(0)-0.42)*2.2,0,1));"
        "st(4,0.5+0.5*sin(X/5+2*sin(Y/25)));"
        "st(5,(1.0-0.32*ld(2)+0.06*sin(Y/3.2)+0.16*(random(9)-0.5))*(1-ld(3)));"
        "st(6,(0.5+0.3*ld(4)+0.16*(random(9)-0.5))*ld(3));"
    )
    tex["blank"] = _texture(work / "blank.png", f"{bw}x{bh}", {
        "r": f"{blank_prelude}138*ld(5)+58*ld(6)",
        "g": f"{blank_prelude}108*ld(5)+50*ld(6)",
        "b": f"{blank_prelude}72*ld(5)+40*ld(6)",
        "a": f"{blank_prelude}clip((1-ld(0))*2000,0,255)",
    })

    # Turning tool: steel shaft (left) and dark handle (right), as a capsule.
    tw, th = 560, 64
    tool_prelude = f"st(0,clip((26-abs(Y-{th / 2}))*60,0,255));st(1,0.55+0.5*(1-abs(Y-{th / 2}-8)/28));"
    tex["tool"] = _texture(work / "tool.png", f"{tw}x{th}", {
        "r": f"{tool_prelude}if(lt(X,300),200*ld(1),128*ld(1))",
        "g": f"{tool_prelude}if(lt(X,300),205*ld(1),76*ld(1))",
        "b": f"{tool_prelude}if(lt(X,300),210*ld(1),38*ld(1))",
        "a": f"{tool_prelude}ld(0)*lt(X,{tw - 8})*gt(X,8)",
    })

    # Particle gate: where chips / dust are allowed, around the tool contact at
    # the bottom of the bowl (inside the central 9:16 safe zone).
    tex["pmask"] = _texture(work / "pmask.png", "1280x720",
                            {"lum": "255*exp(-(pow((X-740)/200,2)+pow((Y-520)/150,2)))"}, alpha=False)

    # Gloss sweep: a soft diagonal cream band for the oil scene.
    tex["band"] = _texture(work / "band.png", f"400x{BOWL_H}", {
        "r": "255", "g": "246", "b": "222",
        "a": f"150*exp(-pow((X-200+0.45*(Y-{BOWL_H / 2}))/80,2))",
    })
    return tex


def _audio_args(voice: Path | None, scene_seconds: float) -> list[str]:
    if voice is not None:
        return ["-i", str(voice), "-af", "adelay=600|600,apad"]
    return ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={scene_seconds}"]


def _encode_scene(inputs: list[str], graph: str, audio_index: int, out: Path, scene_seconds: float) -> None:
    run_ffmpeg([*inputs, "-filter_complex", graph, "-map", "[v]", "-map", f"{audio_index}:a",
                "-t", f"{scene_seconds}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "1",
                str(out)])


def _loop(path: Path) -> list[str]:
    return ["-loop", "1", "-framerate", "30", "-i", str(path)]


FILM = "noise=alls=11:allf=t+u,vignette=angle=PI/5.5,format=yuv420p"


def _bowl_scene(tex: dict[str, Path], voice: Path | None, out: Path, scene_seconds: float, *,
                spin: str, bowl_x: str, bowl_y: str, zoom: str, tone: str = "",
                particles: tuple[int, int, str, str] | None = None, tool: bool = False,
                sweep: bool = False, blur: int = 0) -> None:
    """A wooden bowl on the bench: spinning grain, drifting position, push-in.

    ``particles`` is (density, blur_x, blur_y, tint) for chips or dust and
    ``tool`` rests a gouge against the rim. ``sweep`` runs a gloss highlight
    over the bowl; ``blur`` mixes that many frames for a motion-blur feel.
    """
    inputs = [*_loop(tex["bench"]), *_loop(tex["disc"]), *_loop(tex["mask"]),
              *_loop(tex["shade_dark"]), *_loop(tex["shade_light"]), *_loop(tex["shadow"])]
    n = 6
    extra: list[str] = []
    graph = [f"[1:v]rotate=a='{spin}':c=none:ow=iw:oh=ih,scale={BOWL_W}:{BOWL_H}[grain]"]
    if sweep:
        inputs += _loop(tex["band"])
        graph.append(f"[grain][{n}:v]overlay=x='-700+300*t':y=0:format=rgb[grain]")
        n += 1
    graph.append("[grain][2:v]alphamerge[el];[el][3:v]overlay=format=rgb[el];[el][4:v]overlay=format=rgb[bowl]")
    graph.append("[0:v]crop=1280:720:x='64+18*sin(t/3.1)':y='36+9*cos(t/2.3)'[bg]")
    graph.append(f"[bg][5:v]overlay=x='({bowl_x})-40':y='({bowl_y})-20'[bg]")
    graph.append(f"[bg][bowl]overlay=x='{bowl_x}':y='{bowl_y}'[comp]")
    if tool:
        inputs += _loop(tex["tool"])
        graph.append(f"[{n}:v]rotate=a=0.6:c=none:ow=600:oh=600[tool]")
        graph.append("[comp][tool]overlay=x='621+3*sin(t*9)':y='406+2*sin(t*13)'[comp]")
        n += 1
    if particles is not None:
        density, bx, by, tint = particles
        inputs += _loop(tex["pmask"])
        extra = ["-f", "lavfi", "-i", f"color=c=black:s=320x180:r=30:d={scene_seconds}",
                 "-f", "lavfi", "-i", f"color=c={tint}:s=1280x720:r=30:d={scene_seconds}"]
        graph.append(f"[{n + 1}:v]format=gray,geq=lum='255*gt(random(9),{1 - density})',"
                     f"scale=1280:720:flags=neighbor,avgblur=sizeX={bx}:sizeY={by},"
                     "lut=c0='clip(val*7,0,255)'[p]")
        graph.append(f"[{n}:v]format=gray[pm];[p][pm]blend=all_mode=multiply[p]")
        graph.append(f"[{n + 2}:v]format=rgba[tint];[tint][p]alphamerge[chips]")
        graph.append("[comp][chips]overlay=format=rgb[comp]")
        n += 3
    chain = [f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1280x720:fps=30"]
    if blur:
        chain.append(f"tmix=frames={blur}")
    if tone:
        chain.append(tone)
    chain.append(FILM)
    graph.append("[comp]" + ",".join(chain) + "[v]")
    inputs += extra
    inputs += _audio_args(voice, scene_seconds)
    _encode_scene(inputs, ";".join(graph), n, out, scene_seconds)


def _blank_scene(tex: dict[str, Path], voice: Path | None, out: Path, scene_seconds: float) -> None:
    """Raw oak: a rough-sawn blank on bark, handheld drift, cool tone."""
    inputs = [*_loop(tex["bark"]), *_loop(tex["blank"]), *_loop(tex["shadow"]),
              *_audio_args(voice, scene_seconds)]
    graph = (
        "[0:v]crop=1280:720:x='64+26*sin(t/1.7)+8*sin(t*2.9)':y='36+14*cos(t/1.3)+5*sin(t*3.7)'[bg];"
        "[2:v]scale=1000:680[shadow];[bg][shadow]overlay=x='120+10*sin(t/1.9)':y='40+6*cos(t/1.5)'[bg];"
        "[1:v]rotate=a='-0.08+0.01*sin(t/2)':c=none:ow=940:oh=640[blank];"
        "[bg][blank]overlay=x='170+10*sin(t/1.9)':y='50+6*cos(t/1.5)'[comp];"
        "[comp]zoompan=z='min(1.14,1.02+0.0005*on)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1280x720:fps=30,"
        "eq=saturation=0.85:contrast=1.08,colorbalance=rs=-0.06:gs=0.03:bs=0.04," + FILM + "[v]"
    )
    _encode_scene(inputs, graph, 3, out, scene_seconds)


def write_workshop(path: Path, *, scene_seconds: float = WORKSHOP_SCENE_SECONDS) -> tuple[Path, bool]:
    """Five procedural scenes of a bowl being made, each with narration padded to a fixed length.

    Returns the path and whether narration was recorded.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    narrated = True
    with tempfile.TemporaryDirectory(prefix="vedit-workshop-") as tmp:
        work = Path(tmp)
        tex = _write_textures(work)
        parts: list[Path] = []
        for index, (_label, line) in enumerate(WORKSHOP_SCENES, 1):
            voice = _narration(line, work / f"voice{index}.wav")
            if voice is None:
                narrated = False
            scene = work / f"scene{index}.mp4"
            if index == 1:
                _bowl_scene(tex, voice, scene, scene_seconds, spin="0.12*t",
                            bowl_x="322+24*sin(t/3.3)", bowl_y="152+10*sin(t/2.6)",
                            zoom="min(1.09,1+0.0004*on)")
            elif index == 2:
                _blank_scene(tex, voice, scene, scene_seconds)
            elif index == 3:
                _bowl_scene(tex, voice, scene, scene_seconds, spin="7*t", bowl_x="306+4*sin(t*8)",
                            bowl_y="156+2*sin(t*11)", zoom="min(1.1,1.02+0.0004*on)",
                            particles=(0.012, 10, 2, "0xF8DFA8"), tool=True, blur=3,
                            tone="eq=contrast=1.06")
            elif index == 4:
                _bowl_scene(tex, voice, scene, scene_seconds, spin="1.6*t", bowl_x="312+3*sin(t*5)",
                            bowl_y="158+2*sin(t*7)", zoom="min(1.08,1.01+0.0003*on)",
                            particles=(0.006, 6, 6, "0xE8B070"), tool=True, blur=2,
                            tone="colorbalance=rs=0.08:gs=0.02:bs=-0.08,eq=saturation=1.1")
            else:
                _bowl_scene(tex, voice, scene, scene_seconds, spin="0.05*t",
                            bowl_x="320+8*sin(t/5)", bowl_y="155+4*sin(t/4)",
                            zoom="min(1.12,1+0.0005*on)", sweep=True,
                            tone="eq=saturation='min(1.55,0.9+0.11*t)':brightness='min(0.06,-0.04+0.016*t)':"
                                 "contrast='min(1.12,1+0.02*t)'")
            parts.append(scene)
        manifest = work / "concat.txt"
        manifest.write_text("".join(f"file '{part}'\n" for part in parts), encoding="utf-8")
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(manifest), "-c", "copy", "-movflags", "+faststart", str(path)])
    return path, narrated


def write_tracking(path: Path, *, seconds: float = 12.0) -> Path:
    """A red square drifting across a slate field: a subject SAM can follow deterministically."""
    ffmpeg = resolve_ffmpeg()
    font = resolve_font()
    text = f",drawtext=fontfile='{font}':text='red square demo':x=40:y=40:fontsize=40:fontcolor=white" if font else ""
    run_command([
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0x8aa0b8:s=1280x720:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"color=c=red:s=140x140:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-filter_complex",
        "[0:v][1:v]overlay=x='(W-w)*(0.5+0.42*sin(t*0.9))':y='(H-h)*(0.5+0.3*cos(t*0.7))':shortest=1"
        + text + ",format=yuv420p[v]",
        "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(path),
    ])
    return path


def make_demo(dest: Path | None = None, *, seconds: float = SECONDS, workshop: bool = True) -> dict[str, Path]:
    root = Path(dest) if dest is not None else DEFAULT_DEST
    root.mkdir(parents=True, exist_ok=True)
    assets = {
        "talk": write_talk(root / "talk.mp4", seconds=seconds),
        "bed": write_bed(root / "bed.wav", seconds=seconds),
        "logo": write_logo(root / "logo.png"),
    }
    _verify(assets, seconds=seconds)
    if workshop:
        clip, narrated = write_workshop(root / "workshop.mp4")
        assets["workshop"] = clip
        _verify_workshop(clip, narrated=narrated)
        if not narrated:
            print("workshop.mp4: macOS `say` unavailable; the clip has no narration.")
        assets["tracking"] = write_tracking(root / "tracking.mp4")
    return assets


def _verify_workshop(path: Path, *, narrated: bool) -> None:
    info = probe(path)
    expected = WORKSHOP_SCENE_SECONDS * len(WORKSHOP_SCENES)
    if not info.has_video or not info.has_audio:
        raise SystemExit(f"workshop.mp4 missing streams: video={info.has_video} audio={info.has_audio}")
    if (info.width, info.height) != (1280, 720):
        raise SystemExit(f"workshop.mp4 expected 1280x720, got {info.width}x{info.height}")
    if not (expected - 0.5) < info.duration < (expected + 0.5):
        raise SystemExit(f"workshop.mp4 duration {info.duration:.3f}s not ~{expected}s")
    if info.size_bytes > 6_000_000:
        raise SystemExit(f"workshop.mp4 is {info.size_bytes} bytes; keep it under 6 MB")


def _verify(assets: dict[str, Path], *, seconds: float) -> None:
    talk = probe(assets["talk"])
    bed = probe(assets["bed"])
    if not talk.has_video or not talk.has_audio:
        raise SystemExit(f"talk.mp4 missing streams: video={talk.has_video} audio={talk.has_audio}")
    if talk.width != 640 or talk.height != 360:
        raise SystemExit(f"talk.mp4 expected 640x360, got {talk.width}x{talk.height}")
    if not (seconds - 0.25) < talk.duration < (seconds + 0.4):
        raise SystemExit(f"talk.mp4 duration {talk.duration:.3f}s not ~{seconds}s")
    if not bed.has_audio or bed.has_video:
        raise SystemExit(f"bed.wav expected audio-only, video={bed.has_video} audio={bed.has_audio}")
    if not (seconds - 0.25) < bed.duration < (seconds + 0.4):
        raise SystemExit(f"bed.wav duration {bed.duration:.3f}s not ~{seconds}s")
    logo = assets["logo"]
    if not logo.is_file() or logo.stat().st_size < 32:
        raise SystemExit(f"logo.png missing or empty: {logo}")


def _describe(path: Path) -> str:
    try:
        info: MediaInfo = probe(path)
    except Exception:
        return f"{path}  ({path.stat().st_size} bytes)"
    bits = [f"duration={info.duration:.2f}s", f"video={info.has_video}", f"audio={info.has_audio}"]
    if info.has_video and info.width and info.height:
        bits.append(f"{info.width}x{info.height}")
    return f"{path}  " + "  ".join(bits)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate tiny local demo media for vedit examples.")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="output directory")
    parser.add_argument("--seconds", type=float, default=SECONDS, help="talk/bed duration")
    parser.add_argument("--no-workshop", action="store_true", help="skip the narrated workshop.mp4")
    args = parser.parse_args()
    assets = make_demo(args.dest, seconds=args.seconds, workshop=not args.no_workshop)
    for name, path in assets.items():
        print(f"{name}: {_describe(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
