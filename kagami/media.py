"""Reading the files a run sends: hashes, data URIs, and a video's length, size and fps.

The video facts come from the ffmpeg binary that the `imageio-ffmpeg` wheel ships
(Windows, macOS and Linux), so a fresh `pip install` is all a user needs.
"""

from __future__ import annotations

import base64
import math
import hashlib
import mimetypes
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def mime(path: Path) -> str:
    ext = path.suffix.lower()
    return VIDEO_TYPES.get(ext) or IMAGE_TYPES.get(ext) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def data_uri(path: Path) -> str:
    return f"data:{mime(path)};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


@dataclass(frozen=True)
class VideoFacts:
    seconds: float
    width: int
    height: int
    fps: float


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - fall back to one on PATH
        return "ffmpeg"


def video_facts(path: Path) -> VideoFacts:
    """Length, frame size and fps, read from `ffmpeg -i` (which prints them and exits)."""
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    dur = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    size = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    fps = re.search(r"(\d+(?:\.\d+)?) fps", out)
    if not (dur and size):
        raise ValueError(f"could not read {path.name} as a video:\n{out[-600:]}")
    h, m, s = dur.groups()
    return VideoFacts(
        seconds=int(h) * 3600 + int(m) * 60 + float(s),
        width=int(size.group(1)),
        height=int(size.group(2)),
        fps=float(fps.group(1)) if fps else 0.0,
    )


RATIOS = {"21:9": 21 / 9, "16:9": 16 / 9, "4:3": 4 / 3, "1:1": 1.0, "3:4": 3 / 4, "9:16": 9 / 16}


def nearest_ratio(width: int, height: int) -> str:
    r = width / height
    return min(RATIOS, key=lambda k: abs(RATIOS[k] - r))


# MiniMax H3's image limits (the H3 nodes' own tooltips): each side 256 to 5760 px,
# aspect between 2:5 and 5:2. A file outside them is refused by the provider with a
# bare "provider error", so Kagami checks first.
IMAGE_MIN, IMAGE_MAX = 256, 5760
ASPECT_MIN, ASPECT_MAX = 2 / 5, 5 / 2


def image_size(path: Path) -> tuple[int, int]:
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    if not m:
        raise ValueError(f"could not read {path.name} as an image:\n{out[-400:]}")
    return int(m.group(1)), int(m.group(2))


def image_problem(w: int, h: int) -> str:
    """Why H3 would refuse this size, or '' when it fits."""
    if min(w, h) < IMAGE_MIN:
        return f"{w}x{h}: a side is under {IMAGE_MIN} px"
    if max(w, h) > IMAGE_MAX:
        return f"{w}x{h}: a side is over {IMAGE_MAX} px"
    if not ASPECT_MIN <= w / h <= ASPECT_MAX:
        return f"{w}x{h}: aspect {w / h:.3f} is outside 2:5 to 5:2"
    return ""


def corner_colour(path: Path) -> str:
    """The top-left pixel as ffmpeg's 0xRRGGBB: the pad colour of a send copy."""
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-vf", "crop=1:1:0:0",
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    return "0x%02X%02X%02X" % tuple(raw[:3]) if len(raw) >= 3 else "0x808080"


def fit_copy(path: Path, folder: Path) -> tuple[Path, dict]:
    """A send copy inside H3's limits: padded (never cropped) with the image's own corner
    colour to reach the aspect, then scaled down (never up) to fit. The source is untouched;
    the copy is made once per source hash and reused."""
    w, h = image_size(path)
    pw, ph = w, h
    if w / h < ASPECT_MIN:
        pw = math.ceil(h * ASPECT_MIN)
    elif w / h > ASPECT_MAX:
        ph = math.ceil(w / ASPECT_MAX)
    scale = min(1.0, IMAGE_MAX / max(pw, ph))
    tw, th = max(IMAGE_MIN, int(pw * scale) // 2 * 2), max(IMAGE_MIN, int(ph * scale) // 2 * 2)
    colour = corner_colour(path)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{path.stem}_fit_{sha256(path)[:8]}_{tw}x{th}.png"
    if not out.exists():
        vf = f"pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:color={colour},scale={tw}:{th}:flags=lanczos"
        res = subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-i", str(path), "-vf", vf, "-frames:v", "1", str(out)],
                             capture_output=True, text=True)
        if res.returncode or not out.exists():
            raise ValueError(f"could not make a send copy of {path.name}: {res.stderr[-400:]}")
    return out, {"from": str(path), "from_size": f"{w}x{h}", "padded_to": f"{pw}x{ph}", "pad_colour": colour,
                 "size": f"{tw}x{th}", "how": "padded with the corner colour, then scaled down; never cropped"}
