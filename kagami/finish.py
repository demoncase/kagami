"""What Kagami does to a result after it lands: free, local, ffmpeg only.

  synced       the recast retimed onto the source's clock (H3 plays the motion a few
               percent slower than the source), with the SOURCE clip's sound. See sync.py.
  with_audio   the recast as H3 made it (not retimed) with the source's sound, for comparison.
  side         source and recast side by side, the source's sound: the before/after to post.
  split        one frame, left half source and right half recast, the source's sound.

The provider's output.mp4 is never touched: every file here is a new one, named in the
receipt with its hash and how it was made.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import media

LAYOUTS = ("side", "split")
HEIGHT = 1280  # each panel's height in the before/after files


def has_audio(path: Path) -> bool:
    out = subprocess.run([media.ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    return "Audio:" in out


def _run(args: list[str]) -> None:
    res = subprocess.run([media.ffmpeg_exe(), "-v", "error", "-y", *args], capture_output=True, text=True)
    if res.returncode:
        raise RuntimeError(res.stderr[-800:])


def with_audio(output: Path, source: Path, out: Path) -> dict:
    """The recast's picture (copied as is) with the source's sound, cut at the shorter one."""
    _run(["-i", str(output), "-i", str(source), "-map", "0:v:0", "-map", "1:a:0",
          "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)])
    return {"how": "the recast's video stream copied, the source clip's audio (AAC 192k), cut at the shorter"}


def before_after(output: Path, source: Path, out: Path, layout: str, fps: float) -> dict:
    """Source and recast in one frame, both at the recast's frame rate, the source's sound when it has one."""
    fps = fps or 24
    common = f"fps={fps},scale=-2:{HEIGHT}:flags=lanczos,setsar=1"
    if layout == "side":
        graph = f"[0:v]{common}[a];[1:v]{common},scale=-2:{HEIGHT}[b];[a][b]hstack=inputs=2:shortest=1[v]"
    elif layout == "split":
        # Both panels at the recast's size; the left half from the source, the right half from the recast.
        w, h = media.video_facts(output).width, media.video_facts(output).height
        w2 = w // 2 * 2
        graph = (f"[0:v]fps={fps},scale={w2}:{h}:force_original_aspect_ratio=increase,crop={w2}:{h},setsar=1[src];"
                 f"[1:v]fps={fps},scale={w2}:{h},setsar=1[rec];"
                 f"[src]crop={w2 // 2}:{h}:0:0[l];[rec]crop={w2 - w2 // 2}:{h}:{w2 // 2}:0[r];"
                 f"[l][r]hstack=inputs=2:shortest=1[v]")
    else:
        raise ValueError(f"unknown layout {layout!r}; use one of {LAYOUTS}")
    args = ["-i", str(source), "-i", str(output), "-filter_complex", graph, "-map", "[v]"]
    if has_audio(source):
        args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"]
    args += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p", "-shortest",
             "-movflags", "+faststart", str(out)]
    _run(args)
    return {"how": f"{layout}: source left, recast right, {fps} fps, {HEIGHT} px panels (split: halves of one frame), "
                   "H.264 CRF 18, the source's sound"}


def finish(run_folder: Path, layouts: tuple[str, ...] = LAYOUTS, sync_it: bool = True) -> dict:
    """Make the finishing files for a run that made a video; record them in its receipt."""
    path = run_folder / "receipt.json"
    r = json.loads(path.read_text(encoding="utf-8"))
    out = run_folder / "output.mp4"
    if not out.exists() or (r.get("outcome") != "made" and r.get("how") != "fake"):
        raise ValueError(f"{run_folder.name} has no made video to finish")
    source = Path(r["inputs"][0]["path"])
    if not source.exists():
        raise ValueError(f"the source clip is gone: {source}")
    if media.sha256(source) != r["inputs"][0]["sha256"]:
        raise ValueError(f"{source.name} changed since the run; finishing would pair the recast with another clip")
    fps = media.video_facts(out).fps
    done: dict[str, dict] = {}
    picture = out  # what the before/after files show: the synced recast when there is one
    if sync_it:
        from . import sync

        warp = sync.measure(source, out)
        f = run_folder / "output_synced.mp4"
        done["synced"] = {"path": str(f), "warp": warp, **sync.synced(out, source, f, warp, fps)}
        picture = f
    if has_audio(source):
        f = run_folder / "output_source_audio.mp4"
        done["with_audio"] = {"path": str(f), **with_audio(out, source, f)}
    else:
        done["with_audio"] = {"skipped": "the source clip has no sound"}
    for layout in layouts:
        f = run_folder / f"before_after_{layout}.mp4"
        done[layout] = {"path": str(f), "recast": picture.name, **before_after(picture, source, f, layout, fps)}
    for v in done.values():
        if "path" in v:
            p = Path(v["path"])
            facts = media.video_facts(p)
            v.update(sha256=media.sha256(p), bytes=p.stat().st_size, seconds=round(facts.seconds, 3),
                     size=f"{facts.width}x{facts.height}")
    r["finishes"] = {"note": "made locally from output.mp4 and the source clip; output.mp4 is untouched", **done}
    path.write_text(json.dumps(r, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return done
