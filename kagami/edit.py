"""Trim and nudge a made run. Free, local, ffmpeg only.

Every finished file of a run is on the source clip's clock (the synced recast, and the
before/after built from it), so one trim and one nudge apply to all of them:

  trim   in and out points, in seconds of that clock.
  nudge  whole frames of the recast's timing against the source, on top of the measured sync:
         + moves the recast later (it was early), - moves it earlier (it was late).

Each edit is a new numbered version under the run's edits/ folder (v001, v002, ...), made
again from output.mp4 and the source clip, never from an earlier export, and recorded in the
receipt with its values and every file's hash. output.mp4 is never touched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import finish, media, sync
from .engine import _now

MIN_LENGTH = 0.5  # seconds


def _ff(args: list[str]) -> None:
    res = subprocess.run([media.ffmpeg_exe(), "-v", "error", "-y", *args], capture_output=True, text=True)
    if res.returncode:
        raise RuntimeError(res.stderr[-800:])


def _cut(src: Path, out: Path, t_in: float, t_out: float, audio: bool) -> None:
    """A frame-accurate cut: re-encoded, so it starts on the exact frame asked for."""
    args = ["-ss", f"{t_in:.3f}", "-i", str(src), "-t", f"{t_out - t_in:.3f}", "-map", "0:v:0"]
    if audio:
        args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"]
    args += ["-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(out)]
    _ff(args)


def apply(run_folder: Path, t_in: float, t_out: float, nudge_frames: int = 0) -> dict:
    path = run_folder / "receipt.json"
    r = json.loads(path.read_text(encoding="utf-8"))
    out = run_folder / "output.mp4"
    if not out.exists():
        raise ValueError(f"{run_folder.name} has no made video to edit")
    source = Path(r["inputs"][0]["path"])
    if not source.exists() or media.sha256(source) != r["inputs"][0]["sha256"]:
        raise ValueError(f"the source clip {source.name} is missing or changed since the run")

    fps = media.video_facts(out).fps or 24
    measured = ((r.get("finishes") or {}).get("synced") or {}).get("warp") or sync.measure(source, out)
    warp = {**measured, "offset": round(measured["offset"] - nudge_frames / fps, 4),
            "nudge_frames": nudge_frames, "nudge_seconds": round(nudge_frames / fps, 4)}

    edits = run_folder / "edits"
    edits.mkdir(exist_ok=True)
    n = len([d for d in edits.iterdir() if d.is_dir() and d.name.startswith("v")]) + 1
    d = edits / f"v{n:03d}"
    d.mkdir()

    full = d / "_synced_full.mp4"
    sync.synced(out, source, full, warp, fps)
    length = media.video_facts(full).seconds
    t_out = min(t_out, length)
    if not 0 <= t_in < t_out or t_out - t_in < MIN_LENGTH:
        full.unlink(missing_ok=True)
        d.rmdir()
        raise ValueError(f"trim {t_in:.2f}-{t_out:.2f} s is not a cut of at least {MIN_LENGTH} s inside 0-{length:.2f} s")

    has_sound = finish.has_audio(source)
    synced_cut = d / "output_synced.mp4"
    _cut(full, synced_cut, t_in, t_out, has_sound)
    src_cut = d / "_source_cut.mp4"
    _cut(source, src_cut, t_in, t_out, has_sound)
    files = {"synced": synced_cut}
    for layout in finish.LAYOUTS:
        f = d / f"before_after_{layout}.mp4"
        finish.before_after(synced_cut, src_cut, f, layout, fps)
        files[layout] = f
    for tmp in (full, src_cut):
        tmp.unlink(missing_ok=True)

    rec = {"version": d.name, "at": _now(),
           "trim": [round(t_in, 3), round(t_out, 3)], "nudge_frames": nudge_frames, "warp": warp,
           "how": "made again from output.mp4 and the source clip: retimed with the warp, cut frame-accurately, "
                  "before/after rebuilt; output.mp4 untouched", "files": {}}
    for k, f in files.items():
        facts = media.video_facts(f)
        rec["files"][k] = {"path": str(f), "sha256": media.sha256(f), "bytes": f.stat().st_size,
                           "seconds": round(facts.seconds, 3), "size": f"{facts.width}x{facts.height}"}
    r.setdefault("edits", []).append(rec)
    path.write_text(json.dumps(r, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return rec
