"""Turn an intent and a probed inventory into an editorial brief."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from vedit.agent.model import Decision, Intent, Inventory, SourceShot
from vedit.agent.policy import PlatformContract, canvas_for, contract_for
from vedit.errors import BriefError
from vedit.ir import Brief, Canvas

_ASPECT_SIZE: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "4:5": (1080, 1350),
}

_SPEECH_PLATFORMS = frozenset({"youtube", "shorts", "podcast", "linkedin", "square"})


def decide(intent: Intent, inventory: Inventory) -> Decision:
    shots = tuple(inventory.shots)
    if not shots:
        raise BriefError("inventory is empty")
    primary = inventory.primary_shot
    if primary is None:
        raise BriefError("inventory is empty")

    contract = contract_for(intent.platform)
    canvas = _canvas(intent, contract)
    sources = _sources(intent, shots, primary)
    images = tuple(shot for shot in shots if _kind(shot) == "image")
    videos = tuple(shot for shot in shots if _kind(shot) == "video")
    slideshow = not videos and bool(images)
    has_music = "music" in sources
    has_logo = "logo" in sources
    shorts = contract.name == "shorts"
    preview = contract.name == "preview"
    aspect = _requested_aspect(intent, contract)
    native = _native_aspect(primary.width, primary.height)
    matches = aspect is not None and native is not None and native == aspect
    reframe = (not slideshow) and aspect is not None and primary.has_video and not matches
    cut = (
        (not slideshow)
        and intent.cut_silence
        and primary.has_audio
        and (primary.silence_ratio is None or primary.silence_ratio > 0.08)
    )
    captions = (not slideshow) and intent.captions and primary.has_audio
    highpass = (not slideshow) and intent.highpass and primary.has_audio
    denoise = (not slideshow) and intent.denoise and primary.has_video
    title_text = _text(intent.title)
    cta_text = _text(intent.cta)
    card = title_text or cta_text
    use_title = (not slideshow) and bool(card)
    loud = (not slideshow) and intent.loudnorm and (primary.has_audio or has_music)
    fade = (not slideshow) and intent.fade
    mark = (not slideshow) and has_logo
    duck_music = (not slideshow) and has_music and primary.has_audio
    replace_music = has_music and not duck_music
    crf = 23 if preview else 18
    presets = [str(item) for item in contract.presets]
    style = "boxed" if shorts else "lower-third"

    ops: list[dict[str, Any]] = []
    if slideshow:
        ops.append(
            {
                "op": "slideshow",
                "images": [shot.name for shot in images],
                "duration": 2.5,
                "fade": 0.3,
            }
        )
        if replace_music:
            ops.append({"op": "replace_audio", "source": "music"})
    else:
        if cut:
            if shorts:
                ops.append(
                    {"op": "cut_silence", "threshold_db": -34, "min_silence": 0.28, "pad": 0.08}
                )
            else:
                ops.append(
                    {"op": "cut_silence", "threshold_db": -34, "min_silence": 0.45, "pad": 0.12}
                )
        if highpass:
            ops.append({"op": "highpass", "hz": 80})
        if denoise:
            ops.append({"op": "denoise"})
        if reframe and aspect is not None:
            ops.append({"op": "reframe", "aspect": aspect, "mode": "fill"})
        if captions:
            ops.append({"op": "captions", "burn": True, "language": "en", "style": style})
        if use_title and card is not None:
            ops.append({"op": "title", "text": card})
        if duck_music:
            ops.append({"op": "music", "source": "music", "duck": True, "volume_db": -18})
        elif replace_music:
            ops.append({"op": "replace_audio", "source": "music"})
        if loud:
            ops.append({"op": "loudnorm", "i": contract.target_lufs})
        if fade:
            ops.append({"op": "fade_in", "duration": 0.25})
            ops.append({"op": "fade_out", "duration": 0.4})
        if mark:
            ops.append({"op": "watermark", "source": "logo"})
    export: dict[str, Any] = {"op": "export", "presets": list(presets)}
    if shorts:
        export["reframe"] = "fill"
    ops.append(export)

    brief = Brief(
        name=_text(intent.name) or "edit",
        sources=sources,
        ops=ops,
        canvas=canvas,
        output_dir="out",
        presets=list(presets),
        hw=None,
        crf=crf,
    )
    rationale = _rationale(
        intent=intent,
        contract=contract,
        primary=primary,
        canvas=canvas,
        images=images,
        slideshow=slideshow,
        shorts=shorts,
        aspect=aspect,
        matches=matches,
        reframe=reframe,
        cut=cut,
        captions=captions,
        style=style,
        highpass=highpass,
        denoise=denoise,
        title_text=title_text,
        cta_text=cta_text,
        use_title=use_title,
        has_music=has_music,
        duck_music=duck_music,
        replace_music=replace_music,
        loud=loud,
        fade=fade,
        mark=mark,
        crf=crf,
        presets=presets,
    )
    risks = _risks(
        intent=intent,
        contract=contract,
        primary=primary,
        images=images,
        slideshow=slideshow,
        cut=cut,
        has_music=has_music,
        canvas=canvas,
    )
    return Decision(rationale=rationale, risks=risks, brief=brief)


def _canvas(intent: Intent, contract: PlatformContract) -> Canvas:
    canvas = canvas_for(contract)
    aspect = _text(intent.reframe)
    if aspect is None:
        return canvas
    size = _ASPECT_SIZE.get(aspect)
    if size is None:
        return canvas
    width, height = size
    return replace(canvas, width=width, height=height)


def _sources(intent: Intent, shots: tuple[SourceShot, ...], primary: SourceShot) -> dict[str, str]:
    sources = {shot.name: shot.path for shot in shots}
    music = _text(intent.music)
    if music and "music" not in sources:
        sources["music"] = music
    logo = _text(intent.watermark)
    if logo and "logo" not in sources:
        sources["logo"] = logo
    if "music" not in sources:
        for shot in shots:
            if shot.name != primary.name and _kind(shot) == "audio":
                sources["music"] = shot.path
                break
    return sources


def _requested_aspect(intent: Intent, contract: PlatformContract) -> str | None:
    return _text(intent.reframe) or _text(contract.reframe)


def _native_aspect(width: int, height: int) -> str | None:
    if width <= 0 or height <= 0:
        return None
    if height > width * 1.2:
        return "9:16"
    if abs(width - height) < 8:
        return "1:1"
    if width > height * 1.2:
        return "16:9"
    return None


def _target_aspect(intent: Intent, contract: PlatformContract, canvas: Canvas) -> str | None:
    requested = _requested_aspect(intent, contract)
    if requested in _ASPECT_SIZE:
        return requested
    return _native_aspect(canvas.width, canvas.height)


def _kind(shot: SourceShot) -> str:
    return shot.kind.strip().lower()


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _secs(value: float) -> str:
    number = float(value)
    if number.is_integer():
        return f"{int(number)}s"
    return f"{number:.1f}s"


def _ratio(value: float | None) -> str:
    if value is None:
        return "unmeasured"
    return f"{value:.2f}"


def _lufs(value: float) -> str:
    return f"{float(value):g}"


def _period(text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned.endswith("."):
        cleaned += "."
    return cleaned


def _frame_for_risk(
    primary: SourceShot,
    images: tuple[SourceShot, ...],
    slideshow: bool,
) -> SourceShot:
    if primary.width > 0 and primary.height > 0:
        return primary
    if slideshow:
        for shot in images:
            if shot.width > 0 and shot.height > 0:
                return shot
    return primary


def _rationale(
    *,
    intent: Intent,
    contract: PlatformContract,
    primary: SourceShot,
    canvas: Canvas,
    images: tuple[SourceShot, ...],
    slideshow: bool,
    shorts: bool,
    aspect: str | None,
    matches: bool,
    reframe: bool,
    cut: bool,
    captions: bool,
    style: str,
    highpass: bool,
    denoise: bool,
    title_text: str | None,
    cta_text: str | None,
    use_title: bool,
    has_music: bool,
    duck_music: bool,
    replace_music: bool,
    loud: bool,
    fade: bool,
    mark: bool,
    crf: int,
    presets: list[str],
) -> tuple[str, ...]:
    dur = _secs(primary.duration)
    ratio = _ratio(primary.silence_ratio)
    frame = f"{primary.width}x{primary.height}"
    platform = contract.name
    must = [
        (
            f"Platform {platform} uses a {canvas.width}x{canvas.height} canvas "
            f"for the {frame} primary, which runs {dur}."
        ),
        _silence_line(
            contract=contract,
            primary=primary,
            slideshow=slideshow,
            shorts=shorts,
            cut=cut,
            asked=intent.cut_silence,
            dur=dur,
            ratio=ratio,
        ),
        _structure_line(
            contract=contract,
            primary=primary,
            images=images,
            slideshow=slideshow,
            aspect=aspect,
            matches=matches,
            reframe=reframe,
            frame=frame,
        ),
    ]
    if use_title and title_text and cta_text:
        must.append(
            f'Title card text is "{title_text}" because both title and CTA are set; '
            f'CTA "{cta_text}" stays off the {canvas.width}x{canvas.height} card.'
        )
    must.append(
        f"Export uses {', '.join(presets)} at CRF {crf} for the {platform} delivery of this {dur} source."
    )

    optional: list[str] = [
        _caption_line(
            contract=contract,
            slideshow=slideshow,
            captions=captions,
            style=style,
            dur=dur,
            has_audio=primary.has_audio,
        ),
        _music_line(
            contract=contract,
            images=images,
            slideshow=slideshow,
            has_music=has_music,
            duck_music=duck_music,
            replace_music=replace_music,
            dur=dur,
        ),
    ]
    if loud:
        optional.append(
            f"Loudnorm targets {_lufs(contract.target_lufs)} LUFS, the {platform} level, "
            f"because this {dur} program has audio."
        )
    elif not slideshow:
        optional.append(
            f"Loudnorm stays off on the {dur} {platform} cut "
            f"(intent loudnorm is {str(intent.loudnorm).lower()})."
        )
    if use_title and _one_card(title_text, cta_text):
        shown = title_text or cta_text or ""
        if title_text and not cta_text:
            optional.append(f'Title card uses "{shown}" on the {canvas.width}x{canvas.height} {platform} frame.')
        else:
            optional.append(
                f'Title card uses the CTA "{shown}" because no title was set for this {platform} edit.'
            )
    if highpass:
        optional.append(f"Highpass at 80 Hz is on because the {dur} primary has audio.")
    if mark:
        optional.append(f"Watermark uses source logo on the {frame} frame.")
    if fade:
        optional.append(f"Fade in is 0.25 seconds and fade out is 0.4 seconds on the {dur} {platform} cut.")
    if denoise:
        optional.append(f"Denoise is on because the {frame} primary is video and denoise was requested.")
    hook = _text(intent.hook)
    if hook:
        optional.append(f'Hook "{hook}" is not burned in; the {platform} card only uses title or CTA.')

    room = max(0, 10 - len(must))
    lines = must[:-1] + optional[:room] + must[-1:]
    if len(lines) > 10:
        lines = lines[:9] + lines[-1:]
    return tuple(_period(line) for line in lines)


def _one_card(title_text: str | None, cta_text: str | None) -> bool:
    return bool(title_text) ^ bool(cta_text)


def _silence_line(
    *,
    contract: PlatformContract,
    primary: SourceShot,
    slideshow: bool,
    shorts: bool,
    cut: bool,
    asked: bool,
    dur: str,
    ratio: str,
) -> str:
    if slideshow:
        return (
            f"Silence cuts are skipped because there is no video shot; "
            f"the primary runs {dur} and the platform is {contract.name}."
        )
    if cut:
        window = (
            "min_silence 0.28 seconds and pad 0.08 seconds"
            if shorts
            else "min_silence 0.45 seconds and pad 0.12 seconds"
        )
        return (
            f"Cut silence is on (silence ratio {ratio} on a {dur} source), "
            f"so the {contract.name} pass uses threshold -34 dB, {window}."
        )
    if not primary.has_audio:
        return (
            f"Cut silence stays off because {primary.name} has no audio; "
            f"duration is {dur} on {contract.name}."
        )
    if not asked:
        return (
            f"Cut silence stays off because the intent set it false; "
            f"silence ratio is {ratio} on a {dur} {contract.name} source."
        )
    return (
        f"Cut silence stays off because silence ratio {ratio} is not above 0.08 "
        f"on the {dur} {contract.name} source."
    )


def _structure_line(
    *,
    contract: PlatformContract,
    primary: SourceShot,
    images: tuple[SourceShot, ...],
    slideshow: bool,
    aspect: str | None,
    matches: bool,
    reframe: bool,
    frame: str,
) -> str:
    if slideshow:
        names = ", ".join(shot.name for shot in images)
        return (
            f"The inventory has {len(images)} image shot(s) ({names}) and no video, "
            f"so the tape is a slideshow at 2.5 seconds per still with a 0.3 second fade."
        )
    if reframe and aspect is not None:
        return (
            f"Reframe to {aspect} fill is on because the frame is {frame}, "
            f"which is not already {aspect} for {contract.name}."
        )
    if matches and aspect is not None:
        return f"Reframe is skipped because {frame} already reads as {aspect} on {contract.name}."
    if not primary.has_video:
        return (
            f"Reframe is skipped because {primary.name} has no video frame "
            f"({frame}) on {contract.name}."
        )
    return (
        f"Reframe is skipped because {contract.name} does not ask for a new aspect "
        f"on this {frame} frame."
    )


def _caption_line(
    *,
    contract: PlatformContract,
    slideshow: bool,
    captions: bool,
    style: str,
    dur: str,
    has_audio: bool,
) -> str:
    if captions:
        return (
            f"Captions burn in as {style} in en because the {dur} primary has audio "
            f"and captions were requested for {contract.name}."
        )
    if slideshow:
        return (
            f"Captions are skipped because the slideshow has no speech track "
            f"on {contract.name}."
        )
    if not has_audio:
        return f"Captions are skipped because the {dur} primary has no audio on {contract.name}."
    return f"Captions stay off because the intent set captions false for this {dur} {contract.name} edit."


def _music_line(
    *,
    contract: PlatformContract,
    images: tuple[SourceShot, ...],
    slideshow: bool,
    has_music: bool,
    duck_music: bool,
    replace_music: bool,
    dur: str,
) -> str:
    if duck_music:
        return (
            f"Music from source music ducks to -18 dB under speech "
            f"on the {dur} {contract.name} cut."
        )
    if replace_music and slideshow:
        return (
            f"Music replaces the silent slideshow track from source music "
            f"because these {len(images)} stills have no speech."
        )
    if replace_music:
        return (
            f"Music replaces audio from source music because the {dur} primary "
            f"on {contract.name} has no speech track to duck against."
        )
    if slideshow:
        return f"No music source is available, so the slideshow of {len(images)} stills exports without a bed."
    return (
        f"No music bed is added because {contract.name} has no music path "
        f"and no audio shot besides the {dur} primary."
    )


def _risks(
    *,
    intent: Intent,
    contract: PlatformContract,
    primary: SourceShot,
    images: tuple[SourceShot, ...],
    slideshow: bool,
    cut: bool,
    has_music: bool,
    canvas: Canvas,
) -> tuple[str, ...]:
    risks: list[str] = []
    if intent.captions:
        risks.append("Captions were requested, and whisper may be missing, so burn-in can fail.")
    if cut and primary.silence_ratio is None:
        risks.append(
            f"Silence ratio on {primary.name} ({_secs(primary.duration)}) is not measured yet, "
            "so cut_silence may remove the wrong spans."
        )
    if intent.max_duration is not None and primary.duration > float(intent.max_duration):
        risks.append(
            f"Primary duration {_secs(primary.duration)} is over max_duration "
            f"{_secs(float(intent.max_duration))}, so the silence cut may not be enough."
        )
    if not primary.has_audio and contract.name in _SPEECH_PLATFORMS:
        risks.append(f"{primary.name} has no audio on {contract.name}, which expects speech.")
    frame = _frame_for_risk(primary, images, slideshow)
    crop = _crop_risk(frame, _target_aspect(intent, contract, canvas))
    if crop is not None:
        risks.append(crop)
    if slideshow and not has_music:
        risks.append(f"Slideshow of {len(images)} stills has no music.")
    return tuple(risks)


def _crop_risk(frame: SourceShot, target: str | None) -> str | None:
    width, height = frame.width, frame.height
    if width <= 0 or height <= 0 or target is None:
        return None
    native = _native_aspect(width, height)
    if native is not None and native == target:
        return None
    if height > width and target == "16:9":
        return f"Frame {width}x{height} is vertical and the delivery is 16:9, so faces may be cropped."
    if width > height and target == "9:16":
        return f"Frame {width}x{height} is landscape and the delivery is 9:16, so faces may be cropped."
    return None
