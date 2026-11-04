"""Generate the royalty-free music beds in ``src/vedit/assets/music``.

Everything is synthesized here from first principles, so the beds carry no
samples and no third-party rights. Run it with::

    uv run --with numpy python scripts/make_music_beds.py

numpy is only for this script. The beds are checked into the package and nothing
at runtime needs it. Each bed is a whole number of bars, and every tail wraps
around to the start, so a bed loops without a seam.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

RATE = 44_100
OUT = Path(__file__).resolve().parent.parent / "src" / "vedit" / "assets" / "music"


def _ffmpeg() -> str:
    """The same ffmpeg Briefcut renders with: VEDIT_FFMPEG, then ffmpeg-full, then PATH."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from vedit.media import resolve_ffmpeg

    return resolve_ffmpeg()


FFMPEG = _ffmpeg()
A4 = 440.0
_rng = np.random.default_rng(20261031)


def hz(note: int) -> float:
    """MIDI note to frequency."""
    return A4 * 2 ** ((note - 69) / 12)


def add(track: np.ndarray, sound: np.ndarray, at: float) -> None:
    """Mix ``sound`` in at ``at`` seconds, wrapping past the end so the loop has no seam."""
    start = int(at * RATE) % len(track)
    index = (start + np.arange(len(sound))) % len(track)
    np.add.at(track, index, sound)


def electric_piano(note: int, seconds: float, velocity: float = 1.0) -> np.ndarray:
    """Two-operator FM. The bright tine dies quickly into a soft sustained body."""
    t = np.arange(int(seconds * RATE)) / RATE
    f = hz(note)
    index = (1.2 + 2.6 * np.exp(-t * 5.0)) * velocity
    body = np.sin(2 * np.pi * f * t + index * np.sin(2 * np.pi * f * t))
    tine = 0.5 * np.sin(2 * np.pi * f * 7 * t + 0.8 * np.sin(2 * np.pi * f * 14 * t)) * np.exp(-t * 9) * velocity
    amp = np.exp(-t * 0.8) * (1 - np.exp(-t * 400))
    release = np.minimum(1, (seconds - t) / 0.08)
    return (body + tine) * amp * release * velocity * 0.3


def pluck(note: int, seconds: float, velocity: float = 1.0, brightness: float = 0.5) -> np.ndarray:
    """Karplus-Strong: a burst of noise in a damped delay line, which is why it sounds like a string."""
    f = hz(note)
    period = int(round(RATE / f))
    count = int(seconds * RATE)
    burst = _rng.uniform(-1, 1, period)
    # Softening the attack keeps it from reading as a harsh synthetic click.
    burst = np.convolve(burst, np.ones(3) / 3, mode="same")
    out = np.zeros(count)
    line = burst.copy()
    decay = 0.5 - (0.0035 * (1 - brightness))
    last = 0.0
    for i in range(count):
        sample = line[i % period]
        filtered = decay * (sample + last)
        last = sample
        line[i % period] = filtered
        out[i] = sample
    fade = np.minimum(1, (seconds - np.arange(count) / RATE) / 0.05)
    return out * fade * velocity * 0.32


def pad(notes: list[int], seconds: float, brightness: float = 0.5) -> np.ndarray:
    """Detuned voices with a slow swell. Harmonics fall away, so it stays warm."""
    t = np.arange(int(seconds * RATE)) / RATE
    out = np.zeros(len(t))
    for note in notes:
        for detune in (-0.07, 0.0, 0.08):
            f = hz(note + detune)
            phase = _rng.uniform(0, 2 * np.pi)
            for harmonic in range(1, 13):
                weight = 1.0 if harmonic == 1 else brightness * 0.9 / harmonic ** 1.15
                if f * harmonic < 14000:
                    out += weight * np.sin(2 * np.pi * f * harmonic * t + phase * harmonic)
    swell = np.minimum(1, t / (seconds * 0.3)) * np.minimum(1, (seconds - t) / (seconds * 0.35))
    return out * swell ** 1.5 * 0.05 / max(1, len(notes) / 3)


def bass(note: int, seconds: float, velocity: float = 1.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    f = hz(note)
    tone = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t) * np.exp(-t * 5)
    amp = (1 - np.exp(-t * 180)) * np.exp(-t * 2.2) * np.minimum(1, (seconds - t) / 0.03)
    return tone * amp * velocity * 0.42


def kick(velocity: float = 1.0) -> np.ndarray:
    t = np.arange(int(0.28 * RATE)) / RATE
    sweep = 2 * np.pi * (48 * t + (120 - 48) / 28 * (1 - np.exp(-t * 28)))
    return np.sin(sweep) * np.exp(-t * 13) * (1 - np.exp(-t * 900)) * velocity * 0.6


def hat(velocity: float = 1.0, seconds: float = 0.06) -> np.ndarray:
    count = int(seconds * RATE)
    t = np.arange(count) / RATE
    noise = np.diff(_rng.normal(0, 1, count + 1))
    return noise * np.exp(-t * 90) * velocity * 0.16


def clap(velocity: float = 1.0) -> np.ndarray:
    count = int(0.16 * RATE)
    t = np.arange(count) / RATE
    noise = _rng.normal(0, 1, count)
    noise = np.convolve(noise, [1, 0, -1], mode="same")
    return noise * np.exp(-t * 26) * velocity * 0.1


def room(track: np.ndarray, seconds: float, wet: float) -> np.ndarray:
    """Circular convolution with decaying noise: reverb that also wraps around the loop."""
    size = len(track)
    tail = int(seconds * RATE)
    t = np.arange(tail) / RATE
    impulse = np.zeros(size)
    impulse[:tail] = _rng.normal(0, 1, tail) * np.exp(-t * 6.9 / seconds)
    # Highs die first in a real room.
    spectrum = np.fft.rfft(impulse)
    freqs = np.fft.rfftfreq(size, 1 / RATE)
    spectrum *= 1 / (1 + (freqs / 3500) ** 2)
    spectrum[freqs < 180] *= 0.2
    wetness = np.fft.irfft(np.fft.rfft(track) * spectrum, size)
    wetness /= max(1e-9, np.sqrt(np.mean(wetness ** 2))) / max(1e-9, np.sqrt(np.mean(track ** 2)))
    return track + wet * wetness


def warmth(track: np.ndarray, cutoff: float) -> np.ndarray:
    """A gentle top-end roll-off on the whole bus, in the frequency domain so it stays circular."""
    size = len(track)
    freqs = np.fft.rfftfreq(size, 1 / RATE)
    response = 1 / np.sqrt(1 + (freqs / cutoff) ** 4)
    response[freqs < 30] = 0
    return np.fft.irfft(np.fft.rfft(track) * response, size)


def calm_keys() -> tuple[np.ndarray, np.ndarray]:
    """Unhurried electric piano over a soft pulse. 84 BPM, A minor."""
    bpm, bars = 84, 8
    beat = 60 / bpm
    length = int(bars * 4 * beat * RATE)
    left, right = np.zeros(length), np.zeros(length)
    drums = np.zeros(length)
    low = np.zeros(length)
    chords = [(45, [57, 60, 64, 67]), (41, [57, 60, 65, 64]), (48, [55, 60, 64, 71]), (43, [55, 59, 62, 69])]
    for bar in range(bars):
        root, notes = chords[bar % len(chords)]
        when = bar * 4 * beat
        for index, note in enumerate(notes):
            voice = electric_piano(note, beat * 3.8, 0.75 + 0.12 * (index == 3))
            add(left, voice * (0.85 - 0.12 * index), when + index * 0.018)
            add(right, voice * (0.55 + 0.12 * index), when + index * 0.018)
        for index, offset in enumerate((2.5, 3.0)):
            note = notes[-1 - index] + 12 * (bar % 2 == 1)
            voice = electric_piano(note, beat * 1.4, 0.42)
            add(left, voice * 0.7, when + offset * beat)
            add(right, voice * 0.8, when + offset * beat)
        under = pad([root + 12, *notes[:3]], beat * 4.6, 0.5) * 0.55
        add(left, under, when - beat * 0.2)
        add(right, np.roll(under, 271), when - beat * 0.2)
        add(low, bass(root - 12 + 12, beat * 1.8, 0.8), when)
        add(low, bass(root - 12 + 12, beat * 1.2, 0.6), when + 2 * beat)
        for step in (0, 2):
            add(drums, kick(0.42), when + step * beat)
        for step in range(8):
            swing = 0.012 if step % 2 else 0.0
            add(drums, hat(0.8 if step % 2 == 0 else 0.5), when + step * beat / 2 + swing)
    left, right = room(left, 1.4, 0.32), room(right, 1.4, 0.32)
    low *= 0.55
    return warmth(left + low + drums, 12000), warmth(right + low + drums, 12000)


def bright_pluck() -> tuple[np.ndarray, np.ndarray]:
    """A plucked figure with a steady four-on-the-floor. 110 BPM, C major."""
    bpm, bars = 110, 8
    beat = 60 / bpm
    length = int(bars * 4 * beat * RATE)
    left, right = np.zeros(length), np.zeros(length)
    drums = np.zeros(length)
    low = np.zeros(length)
    chords = [(36, [60, 64, 67, 72]), (43, [59, 62, 67, 74]), (45, [60, 64, 69, 72]), (41, [60, 65, 69, 72])]
    pattern = [0, 2, 1, 3, 2, 1, 3, 2]
    for bar in range(bars):
        root, notes = chords[bar % len(chords)]
        when = bar * 4 * beat
        for step, pick in enumerate(pattern):
            note = notes[pick]
            velocity = 0.95 if step % 2 == 0 else 0.7
            voice = pluck(note, beat * 1.6, velocity * 3.2, brightness=0.8)
            pan = 0.5 + 0.28 * np.sin(step * 2.4)
            add(left, voice * (1 - pan), when + step * beat / 2)
            add(right, voice * pan, when + step * beat / 2)
        for step in range(4):
            add(drums, kick(0.42), when + step * beat)
            add(low, bass(root + 12, beat * 0.55, 0.34), when + (step + 0.5) * beat)
            add(drums, hat(1.0), when + (step + 0.5) * beat)
        for step in (1, 3):
            add(drums, clap(0.85), when + step * beat)
    left, right = room(left, 1.0, 0.24), room(right, 1.0, 0.24)
    return warmth(left + low + drums, 13000), warmth(right + low + drums, 13000)


def slow_air() -> tuple[np.ndarray, np.ndarray]:
    """Slow pads with a few bells and no drums, for voice to sit on. 70 BPM, D major."""
    bpm, bars = 70, 8
    beat = 60 / bpm
    length = int(bars * 4 * beat * RATE)
    left, right = np.zeros(length), np.zeros(length)
    chords = [[50, 57, 62, 66, 69], [47, 54, 62, 66, 69], [43, 55, 59, 62, 66], [45, 52, 61, 64, 69]]
    for bar in range(bars):
        notes = chords[bar % len(chords)]
        when = bar * 4 * beat
        wash = pad(notes, beat * 5.2, 0.9)
        add(left, wash, when - beat * 0.4)
        add(right, np.roll(wash, 311), when - beat * 0.4)
        if bar % 2 == 0:
            for index, note in enumerate(notes[-3:]):
                bell = electric_piano(note + 12, beat * 3, 0.9)
                add(left, bell * (0.8 - 0.3 * index), when + (index * 1.5 + 0.5) * beat)
                add(right, bell * (0.3 + 0.3 * index), when + (index * 1.5 + 0.5) * beat)
    left, right = room(left, 2.6, 0.5), room(right, 2.6, 0.5)
    return warmth(left, 11000), warmth(right, 11000)


BEDS = {"calm_keys": calm_keys, "bright_pluck": bright_pluck, "slow_air": slow_air}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in BEDS.items():
        left, right = build()
        stereo = np.stack((left, right), axis=1)
        stereo -= stereo.mean(axis=0)
        stereo /= np.abs(stereo).max() / 0.7
        pcm = (stereo * 32767).astype("<i2").tobytes()
        dest = OUT / f"{name}.m4a"
        subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-y", "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "-",
                        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dest)], input=pcm, check=True)
        print(name, f"{len(left) / RATE:.2f}s", dest.stat().st_size // 1024, "KB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
