"""Put the recast back on the source's beat. Free, local.

H3 spreads the reference motion over its own clip length (it is asked for whole seconds and
returns a little more), so the dance plays a few percent slower than the source and drifts
off the song. Here we measure that warp and undo it.

How it is measured: both clips are read small and grey at 48 fps, and each frame's motion
energy (the mean change from the frame before) makes a curve over time. The recast's look is
different but its motion follows the source, so the two curves match once the recast's curve
is stretched by the right factor and slid by the right offset. We try every pair in a grid
and keep the one with the highest correlation. A weak match is not trusted: the fallback is
the plain ratio of the two lengths, and the receipt says which one was used.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from . import media

W, H, FPS = 36, 64, 48
TRUST = 0.45  # minimum correlation to trust a measured warp


def energy(path: Path) -> tuple[np.ndarray, np.ndarray]:
    raw = subprocess.run([media.ffmpeg_exe(), "-v", "error", "-i", str(path), "-vf",
                          f"fps={FPS},scale={W}:{H},format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.float32)
    e = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    e = np.convolve(e, np.ones(5) / 5, mode="same")
    return (np.arange(len(e)) + 0.5) / FPS, e


def _z(x: np.ndarray) -> np.ndarray:
    return (x - x.mean()) / (x.std() + 1e-9)


def measure(source: Path, output: Path) -> dict:
    """How the recast's time relates to the source's: output_time = source_time * scale + offset."""
    ts, es = energy(source)
    to, eo = energy(output)
    best = (-2.0, 1.0, 0.0)
    for s in np.arange(0.85, 1.25, 0.005):
        for off in np.arange(-1.0, 1.01, 0.02):
            tq = ts * s + off
            m = (tq >= to[0]) & (tq <= to[-1])
            if m.sum() < len(ts) * 0.6:
                continue
            c = float(np.corrcoef(_z(es[m]), _z(np.interp(tq[m], to, eo)))[0, 1])
            if np.isfinite(c) and c > best[0]:
                best = (c, float(s), float(off))
    src_len, out_len = media.video_facts(source).seconds, media.video_facts(output).seconds
    corr, scale, offset = best
    if es.std() < 1e-6 or eo.std() < 1e-6:
        corr = 0.0  # a clip with no motion at all: nothing to match
    trusted = corr >= TRUST
    if not trusted:
        scale, offset = out_len / src_len, 0.0
    return {"scale": round(scale, 4), "offset": round(offset, 3), "correlation": round(corr, 3),
            "trusted": trusted, "source_seconds": round(src_len, 3), "output_seconds": round(out_len, 3),
            "method": "motion-energy match" if trusted else f"length ratio (motion match too weak: {corr:.2f} < {TRUST})",
            "reads_as": f"the recast plays {abs(scale - 1) * 100:.1f}% {'slower' if scale > 1 else 'faster'} "
                        f"and starts {abs(offset):.2f} s {'late' if offset > 0 else 'early'}"}


def synced(output: Path, source: Path, out: Path, warp: dict, fps: float) -> dict:
    """The recast retimed onto the source clock, with the source's sound, cut at the shorter."""
    scale, offset = warp["scale"], warp["offset"]
    fps = fps or 24
    if offset >= 0:
        head = f"trim=start={offset:.3f},setpts=(PTS-STARTPTS)/{scale:.4f}"
    else:  # the recast starts early: hold its first frame for that long, then retime
        head = f"tpad=start_duration={-offset:.3f}:start_mode=clone,setpts=(PTS-STARTPTS)/{scale:.4f}"
    vf = f"{head},fps={fps}"
    args = ["-i", str(output), "-i", str(source), "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]"]
    from .finish import has_audio  # local import: finish imports sync

    if has_audio(source):
        args += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    args += ["-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p", "-shortest",
             "-movflags", "+faststart", str(out)]
    res = subprocess.run([media.ffmpeg_exe(), "-v", "error", "-y", *args], capture_output=True, text=True)
    if res.returncode:
        raise RuntimeError(res.stderr[-800:])
    return {"how": f"trimmed/held {offset:+.3f} s, retimed by 1/{scale:.4f}, {fps} fps, H.264 CRF 16, the source's sound"}
