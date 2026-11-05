"""Filter graphs that lay a SAM matte over the tape: cutout backdrops and the hero word's layers.

The compiler validates the brief and owns the argv. These functions build only
the graph, so each one can be read and tested on its own. Every stream is
normalized to the canvas rate first, so matte frame N lands on tape frame N
exactly. Compositing stays in Y'CbCr: the subject's color never takes a trip
through RGB and cannot pick up a matrix mismatch.
"""

from __future__ import annotations

from pathlib import Path

from vedit.edit_spec import CUTOUT_MODES as CUTOUT_ROWS
from vedit.media import filter_value, probe

CUTOUT_MODES = tuple(row[0] for row in CUTOUT_ROWS if row[0] != "none")
REQUIRED_CUTOUT_FILTERS = ("alphamerge", "scale", "overlay", "lutyuv", "boxblur", "lut", "premultiply", "unpremultiply")
CUTOUT_HOLE = 12
CUTOUT_DIM = 0.32
CUTOUT_DIM_CHROMA = 0.6
CUTOUT_EDGE_CENTER = 167
CUTOUT_EDGE_SPAN = 143
CUTOUT_EDGE_TIGHTEST = 32

REQUIRED_HERO_FILTERS = ("alphamerge", "overlay", "scale", "lut", "setpts", "fps", "ass", "split", "setparams")
# Wider than a cutout's edge: the subject layer is the frame's own picture, so
# covering a few pixels of background only hides the word a little early.
HERO_EDGE = "lut=y='clip((val-40)*255/120,0,255)'"
# Burned text and images take their color from the frame, and the export declares BT.709.
BT709_TAGS = "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"


def _chain(filters: list[str]) -> str:
    return ",".join(part for part in filters if part)


def limited_yuv(color: str) -> tuple[int, int, int]:
    """A ``#RRGGBB`` color as limited-range BT.709, the matrix every export is tagged with."""
    red, green, blue = (int(color[at : at + 2], 16) / 255 for at in (1, 3, 5))
    luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return round(16 + 219 * luma), round(128 + 224 * (blue - luma) / 1.8556), round(128 + 224 * (red - luma) / 1.5748)


def cutout_blur(blur: float) -> list[str]:
    """Blur the tape with the subject taken out, so its color cannot glow around it.

    Premultiplying by "not the subject" and blurring color and alpha alike is a
    normalized blur: each pixel averages only background. Inside the hole that
    average means nothing, and the subject covers it.
    """
    size = f"{0.004 + 0.04 * blur:.6f}"
    luma, chroma = f"'max(1,trunc(min(w,h)*{size}))'", f"'max(1,trunc(min(cw,ch)*{size}))'"
    return [
        "[sized]split[matte][outline]",
        f"[outline]lut=y='if(gt(val,{CUTOUT_HOLE}),0,255)'[hole]",
        "[tape]format=yuva444p[plate]",
        (
            "[plate][hole]alphamerge,premultiply=inplace=1,"
            f"boxblur=lr={luma}:lp=2:cr={chroma}:cp=2:ar={luma}:ap=2,unpremultiply=inplace=1,format=yuv444p[behind]"
        ),
    ]


def cutout_edge(tape: tuple[int, int] | None, matte: Path) -> str:
    """The curve from SAM's soft alpha to the finished edge.

    It starts above zero, which pulls the edge inside the subject so its outer
    pixels stop carrying the old background. A tape larger than the matte
    stretches SAM's soft band with it, so the curve steepens to match: a smooth
    ramp cut steeply stays a clean contour, and the edge stays a few pixels wide
    at any size. The base curve suits a tape 4/3 of the matte, the usual 720p clip.
    """
    info = probe(matte)
    scale = max(tape) / max(info.width, info.height) if tape and info.width and info.height else 1.0
    span = max(CUTOUT_EDGE_TIGHTEST, round(CUTOUT_EDGE_SPAN / max(1.0, scale * 0.75)))
    return f"lut=y='clip((val-{CUTOUT_EDGE_CENTER - span // 2})*255/{span},0,255)'"


def cutout_backdrop(mode: str, color: str) -> str:
    """The filter that turns the tape into what stands behind the subject."""
    if mode == "dim":
        keep = f"128+(val-128)*{CUTOUT_DIM_CHROMA}"
        return f"lutyuv=y='16+(val-16)*{CUTOUT_DIM}':u='{keep}':v='{keep}'"
    if mode == "mono":
        return "lutyuv=u=128:v=128"
    luma, blue, red = limited_yuv(color)
    return f"lutyuv=y={luma}:u={blue}:v={red}"


def cutout_graph(
    *,
    video: str,
    fps: float,
    frame: tuple[int, int],
    mode: str,
    blur: float,
    color: str,
    matte: Path,
    image: Path | None,
) -> list[str]:
    """Input 0 is the tape, 1 the matte, 2 the optional background still; the finished picture is ``[v]``.

    The tape stays the length authority: a matte that runs short holds its last
    frame and one that runs long is cut off.
    """
    frame_width, frame_height = frame
    rate = f"fps={fps:.8f}"
    graph = [
        f"[0:v:0]{_chain([video, rate, 'setsar=1', 'format=yuv444p'])},split[picture][tape]",
        f"[1:v:0]{rate},format=gray[raw]",
        f"[raw]scale={frame_width}:{frame_height}:flags=bicubic[sized]",
        "[picture]format=yuva444p[solid]",
    ]
    if mode == "blur":
        graph += cutout_blur(blur)
    else:
        graph.append("[sized]null[matte]")
    graph += [f"[matte]{cutout_edge(frame, matte)}[alpha]", "[solid][alpha]alphamerge[cut]"]
    if image is not None:
        # One still, covering the frame: the first overlay holds it for the whole tape.
        graph += [
            "[2:v:0]setsar=1,scale=out_color_matrix=bt709:out_range=tv,format=yuv444p[still]",
            f"[still]scale=w='ceil(max({frame_width},{frame_height}*iw/ih))+2':h='ceil(max({frame_height},{frame_width}*ih/iw))+2':flags=lanczos[cover]",
            "[tape]null[frame]",
            "[frame][cover]overlay=x=(W-w)/2:y=(H-h)/2:format=yuv444[behind]",
        ]
    elif mode != "blur":
        graph.append(f"[tape]{cutout_backdrop(mode, color)}[behind]")
    graph.append("[behind][cut]overlay=format=yuv444:shortest=1,format=yuv420p[v]")
    return graph


def hero_graph(
    *,
    video: str,
    fps: float,
    frame: tuple[int, int],
    first: int,
    shown: float,
    hidden: float,
    subtitle: Path,
    fonts: Path,
) -> list[str]:
    """Input 0 is the tape, 1 the matte for the word's window; the finished picture is ``[v]``.

    The background, then the word, then the subject. The subject layer is the
    tape's own picture with the matte as its alpha, so laying it back on top
    changes nothing except where the word was drawn. The matte is placed at the
    window's first frame, and outside the window the overlay is switched off, so
    those frames pass through as they were.
    """
    frame_width, frame_height = frame
    rate = f"fps={fps:.8f}"
    return [
        # Both layers carry the same color tags, so FFmpeg never converts one of them on the way into the overlay.
        f"[0:v:0]{_chain([video, rate, 'setsar=1', 'format=yuv420p', BT709_TAGS])},split[plate][body]",
        f"[1:v:0]setpts=PTS-STARTPTS+{first / fps:.6f}/TB,{rate},format=gray,scale={frame_width}:{frame_height}:flags=bicubic,{HERO_EDGE}[alpha]",
        "[body]format=yuva420p[solid]",
        "[solid][alpha]alphamerge[subject]",
        f"[plate]ass={filter_value(subtitle)}:fontsdir={filter_value(fonts)}[worded]",
        f"[worded][subject]overlay=format=yuv420:enable='between(t,{shown:.6f},{hidden:.6f})'[v]",
    ]
