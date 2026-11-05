"""Speech cleanup and music mixing, as filter strings and small pure helpers.

Levels in a recording are unknown until it is measured, and every useful speech
setting is relative to them. A noise floor of -30 dB is right for a loud room
and destroys a quiet phone recording, whose speech may sit near that level. So
``voice`` measures the tape during the render and writes the results to a small
command file. The filters read it through ``asendcmd``, which keeps the FFmpeg
command static and readable.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import subprocess
from pathlib import Path
from typing import Any

from vedit.errors import VeditError
from vedit.media import filter_value, has_filter, probe, resolve_ffmpeg

# afftdn holds half of its 50 ms analysis window and never accounts for it in
# timestamps, so audio would lag the picture by this much. See ``voice_filters``.
DENOISE_LATENCY = 0.025
_GATE_THRESHOLD = 0.02
# The expander engages only when the voice sits this far over the room. At 8 dB it cut
# 12.67% of loud windows on a long talk and 5.33% in a hall (docs/HANDOFF.md, defect 2).
_VOICE_GATE_MARGIN = 16.0
# Where the expander opens, as a share of the way from the cleaned room up to the voice.
# At the 16 dB margin this is about 10 dB under the voice, where quiet syllables still sit.
_GATE_SHARE = 0.62
# No threshold goes under this (-60 dB). A near-silent tape would otherwise round a level to 0,
# which acompressor refuses and which the gate's volume scale would divide by.
_LEVEL_FLOOR = 0.001
# The sidechain sees speech normalized to this level, whatever the recording's own.
_KEY_LEVEL = -20.0
# A bed is normalized here before ``volume_db``, so that number means the same for any track.
_BED_LEVEL = -16.0
DUCK_THRESHOLD = 0.03
_RATE = 16_000
_WINDOW = 480  # 30 ms at _RATE

# Used until the measurement lands, and when a tape is too short to measure.
_DEFAULTS: dict[str, float] = {"noise_floor": -42.0, "gate": 0.006, "compress": 0.06, "makeup": 1.85}


# Synthesized by scripts/make_music_beds.py, so they carry no third-party rights.
# Each is a whole number of bars and loops without a seam.
BUNDLED_MUSIC = (
    ("calm_keys", "Calm keys", "Warm electric piano over a soft pulse.", 84),
    ("bright_pluck", "Bright pluck", "A light plucked figure with a steady beat.", 110),
    ("slow_air", "Slow air", "Slow pads and a few bells, with no drums.", 70),
)


def bundled_music() -> list[dict[str, Any]]:
    """The music beds that ship with Briefcut and exist on this install."""
    folder = Path(__file__).with_name("assets") / "music"
    beds = [
        {"id": ident, "name": name, "description": description, "bpm": bpm, "path": str(folder / f"{ident}.m4a")}
        for ident, name, description, bpm in BUNDLED_MUSIC
    ]
    return [bed for bed in beds if Path(bed["path"]).is_file()]


def _clip(value: float) -> float:
    return min(1.0, max(0.0, value))


def level_stats(samples: array.array) -> dict[str, float]:
    """Noise floor and speech level, in dB, from 16-bit mono samples. Pure.

    Short-window RMS separates the two: the quietest windows are the room, the
    loud ones are the voice.
    """
    windows = []
    for start in range(0, len(samples) - _WINDOW, _WINDOW):
        chunk = samples[start : start + _WINDOW]
        windows.append(sum(value * value for value in chunk) / _WINDOW)
    if len(windows) < 10:
        raise ValueError("too little audio to measure")
    windows.sort()
    floor = 32768.0**2

    def db(fraction: float) -> float:
        return 10 * math.log10(max(windows[round(fraction * (len(windows) - 1))], 1e-3) / floor)

    return {"noise": db(0.05), "speech": db(0.85)}


def voice_settings(stats: dict[str, float]) -> dict[str, float]:
    """Filter settings relative to the measured room and voice. Pure."""
    noise, speech = float(stats["noise"]), float(stats["speech"])
    margin = speech - noise
    # The denoiser's floor sits a little above the measured room, and always
    # well under the voice, or it starts to remove the voice.
    noise_floor = min(noise + 8, speech - 14)
    # The expander opens ``_GATE_SHARE`` of the way from the cleaned room up to the voice.
    residual = noise - 10
    gate_db = residual + _GATE_SHARE * (speech - residual)
    # Compression lifts whatever sits under the voice along with it, so it only
    # works as hard as the gap allows, and backs right off in a noisy room.
    press = _clip((margin - 8) / 16)
    settings = {
        "noise_floor": max(-80.0, min(-20.0, noise_floor)),
        # A noisy room leaves no gap to find, so the expander stays out of the way.
        "gate": max(_LEVEL_FLOOR, 10 ** (gate_db / 20)) if margin >= _VOICE_GATE_MARGIN else _LEVEL_FLOOR,
        "compress": max(_LEVEL_FLOOR, min(1.0, 10 ** ((speech - 4 * press) / 20))) if press else 1.0,
        "makeup": 1 + 0.85 * press,
    }
    return {key: round(value, 5) for key, value in settings.items()}


def voice_commands(settings: dict[str, float], *, denoise: bool, enhance: bool) -> str:
    """``asendcmd`` file text. Commands apply before the first sample is processed."""
    lines = []
    if denoise:
        lines.append(f"0.0 afftdn@voice noise_floor {settings['noise_floor']}")
        # agate ignores a threshold change at runtime, so move the signal past
        # its fixed threshold instead, and undo the gain straight after.
        scale = _GATE_THRESHOLD / settings["gate"]
        lines.append(f"0.0 volume@voice_in volume {scale:.5f}")
        lines.append(f"0.0 volume@voice_out volume {1 / scale:.5f}")
    if enhance:
        lines.append(f"0.0 acompressor@voice threshold {settings['compress']}")
        lines.append(f"0.0 acompressor@voice makeup {settings['makeup']}")
    return ";\n".join(lines) + ";\n" if lines else ""


def voice_filters(
    *, denoise: bool, enhance: bool, commands: str | Path | None, rate: int = 48_000, warn: Any = None
) -> list[str]:
    """The speech chain, in signal order. Length and sync match the input exactly."""
    note = warn or (lambda message: None)
    chain: list[str] = []
    settings = _DEFAULTS
    if commands is not None:
        if has_filter("asendcmd"):
            chain.append(f"asendcmd=f={filter_value(commands)}")
        else:
            note("This FFmpeg cannot apply measured speech settings, so typical ones were used.")
    chain.append("highpass=f=80")
    if denoise:
        if has_filter("afftdn"):
            # Pad, filter, then drop the latency: nothing is lost at either end,
            # and speech stays on the same frame as the lips.
            late = round(DENOISE_LATENCY * rate)
            chain += [
                f"apad=pad_len={late}",
                f"afftdn@voice=nr=20:nf={settings['noise_floor']}",
                f"atrim=start_sample={late}",
                "asetpts=N/SR/TB",
            ]
        else:
            note("This FFmpeg has no afftdn filter, so background noise was not reduced.")
        if has_filter("agate"):
            scale = _GATE_THRESHOLD / settings["gate"]
            chain += [
                f"volume@voice_in=volume={scale:.5f}",
                f"agate=threshold={_GATE_THRESHOLD}:ratio=2.5:range=0.2:attack=6:release=200:knee=4",
                f"volume@voice_out=volume={1 / scale:.5f}",
            ]
    if enhance:
        # Take out the room boom, restore the consonants that denoising dulls.
        chain += ["equalizer=f=250:t=q:w=1.2:g=-2", "equalizer=f=3200:t=q:w=1:g=3", "highshelf=f=7000:g=1.5"]
        if has_filter("deesser"):
            chain.append("deesser=i=0.25")
        chain.append(
            f"acompressor@voice=threshold={settings['compress']}:ratio=2.5:attack=8:release=160:makeup={settings['makeup']}:knee=3"
        )
    return chain


def duck_filter() -> str:
    """Pull the music under speech and let it back slowly enough not to pump between words."""
    return f"sidechaincompress=threshold={DUCK_THRESHOLD}:ratio=6:attack=25:release=380:makeup=1:knee=4"


def music_commands(bed: dict[str, float], voice: dict[str, float] | None) -> str:
    """``asendcmd`` text that levels a bed and its ducking key from measurements. Pure."""
    gain = min(16.0, max(0.1, 10 ** ((_BED_LEVEL - bed["speech"]) / 20)))
    lines = [f"0.0 volume@music_level volume {gain:.5f}"]
    if voice is not None:
        # Room tone with no voice in it must not hold the music down.
        margin = voice["speech"] - voice["noise"]
        key = min(20.0, max(0.25, 10 ** ((_KEY_LEVEL - voice["speech"]) / 20))) if margin >= 8 else 0.25
        lines.append(f"0.0 volume@duck_key volume {key:.5f}")
    return ";\n".join(lines) + ";\n"


def decode_mono(
    path: str | Path, *, rate: int = _RATE, seconds: float | None = None, start: float = 0.0, track: int | None = None
) -> array.array:
    """16-bit mono samples of a file's sound, from ``start`` for ``seconds``.

    ``track`` is the position among the audio streams. By default it is the
    track the editor plays (``MediaInfo.audio_map``), not simply the first one,
    so a measurement hears what the render will use.
    """
    if track is None:
        try:
            track = probe(path).audio_map
        except VeditError:  # an unreadable file fails just below, in FFmpeg's own words
            track = 0
    argv = [resolve_ffmpeg(), "-hide_banner", "-v", "error"]
    if start > 0:
        argv += ["-ss", f"{start:.3f}"]
    argv += ["-i", str(path), "-map", f"0:a:{track}", "-vn"]
    if seconds is not None:
        argv += ["-t", f"{seconds:.3f}"]
    argv += ["-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
    done = subprocess.run(argv, capture_output=True, timeout=600, check=False)
    if done.returncode != 0:
        raise ValueError(f"could not decode audio from {path}: {done.stderr.decode('utf-8', 'replace')[-300:]}")
    samples = array.array("h")
    samples.frombytes(done.stdout[: len(done.stdout) // 2 * 2])
    return samples


def measure_voice(path: str | Path) -> dict[str, float]:
    return voice_settings(level_stats(decode_mono(path)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vedit.sound", description="Measure a tape and write its speech settings")
    parser.add_argument("--in", dest="source")
    parser.add_argument("--bed", help="measure this music track for mixing instead of cleaning speech")
    parser.add_argument("--out", required=True)
    parser.add_argument("--denoise", action="store_true")
    parser.add_argument("--enhance", action="store_true")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if args.bed:
        try:
            bed = level_stats(decode_mono(args.bed, seconds=120.0))
            voice = level_stats(decode_mono(args.source)) if args.source else None
        except ValueError:
            bed, voice = {"speech": _BED_LEVEL}, None
        out.write_text(music_commands(bed, voice), encoding="utf-8")
        return 0
    try:
        settings = measure_voice(args.source)
    except ValueError:
        settings = dict(_DEFAULTS)
    out.write_text(voice_commands(settings, denoise=args.denoise, enhance=args.enhance), encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
