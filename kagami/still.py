"""See it before you pay for video: one frame of the clip, recast on an image model, approved.

  python -m kagami still copy --video clip.mp4 --image me.png [--at 0] [--model nbp|flux3|both] [--run|--fake]
  python -m kagami approve stills\\<folder> --pick nbp
  python -m kagami copy --video clip.mp4 --image me.png --still stills\\<folder> --run ...

The frame is Image 1 of the still request and the references follow; the approved still then
goes to H3 as Image 1 (H3 takes references or a first frame, never both, so it travels as a
reference), with the original references after it.

Models (Comfy Router):
  nbp    vertexai/gemini-3-pro-image  Gemini generateContent, images first then the text.
  flux3  bfl/flux-3-image             1 to 10 reference images; grounding (web search) turned OFF.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import engine, media

MODELS = {"nbp": "vertexai/gemini-3-pro-image", "flux3": "bfl/flux-3-image"}
NBP_RATIOS = {"21:9": 21 / 9, "16:9": 16 / 9, "3:2": 3 / 2, "4:3": 4 / 3, "5:4": 5 / 4, "1:1": 1.0,
              "4:5": 4 / 5, "3:4": 3 / 4, "2:3": 2 / 3, "9:16": 9 / 16}
FLUX_RATIOS = {"21:9": 21 / 9, "2:1": 2.0, "16:9": 16 / 9, "3:2": 3 / 2, "7:5": 7 / 5, "4:3": 4 / 3, "5:4": 5 / 4,
               "1:1": 1.0, "4:5": 4 / 5, "3:4": 3 / 4, "5:7": 5 / 7, "2:3": 2 / 3, "9:16": 9 / 16, "1:2": 0.5,
               "9:21": 9 / 21}
EACH_LIMIT = 20 * 1024 * 1024  # both models: 20 MB of base64 per image
FLUX_MAX_PIXELS = 16_000_000

COPY_STILL = (
    "Image 1 is one frame from a video. Recreate this exact frame as a photograph: the same camera angle, "
    "framing, lens, pose and body position, the same place, light and composition. Replace the person in "
    "Image 1 with the person in Image 2: their face, skin tone, hair, build and clothing exactly as in Image 2. "
    "Everything else stays as it is in Image 1."
)
# The frame sentence when the cast map names what changes (no "same place" or "same light": a place or a look may change them).
COPY_FRAME = ("Image 1 is one frame from a video. Recreate this exact frame as a photograph: the same camera angle, "
              "framing, lens, pose, body position and composition.")
SWAP_STILL = (
    "Image 1 is one frame from a video. Keep it exactly as it is, as a photograph: the same people, place, "
    "camera, framing and light. Change one thing only: {target} becomes the one in Image 2, matching Image 2 "
    "exactly in shape, colour, material and markings. Everything else stays as it is in Image 1."
)

# The sentence H3 gets when an approved still travels as its Image 1.
H3_STILL = ("Image 1 is the approved first frame of this shot: from the first frame to the last, the person, "
            "their clothing, the place and the light look exactly as in Image 1.")
H3_STILL_LATER = ("Image 1 is the approved look of this shot, recast from the frame at {at:.1f} seconds of Video 1. "
                  "It is a reference, not the opening frame: the video still begins as Video 1 begins and follows its "
                  "motion, and whenever the person is in view, they, their clothing, the place and the light look "
                  "exactly as in Image 1.")


def h3_still(at: float) -> str:
    """The sentence H3 gets for an approved still: a first frame, or a look taken from later in the clip."""
    return H3_STILL if at < 0.05 else H3_STILL_LATER.format(at=at)


def nearest(table: dict[str, float], w: int, h: int) -> str:
    r = w / h
    return min(table, key=lambda k: abs(table[k] - r))


def grab_frame(video: Path, at: float, out: Path) -> None:
    res = subprocess.run([media.ffmpeg_exe(), "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(video),
                          "-frames:v", "1", str(out)], capture_output=True, text=True)
    if res.returncode or not out.exists():
        raise engine.Refused(f"could not read a frame at {at} s from {video.name}: {res.stderr[-300:]}")


def b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def still_parts(mode: str, target: str, images: list[engine.Ref], extra: str, change: str = "",
                others: str = "keep") -> list[dict]:
    """The still's prompt from the same cast map as the video's, worded for one frame: the frame sentence,
    one sentence per image (yours are Image 2.. here, the frame is Image 1), the extras sentence, the change
    word for word, then --extra. With no roles set this is the prompt Kagami has always written."""
    images = [engine.Ref(Path(r[0]), *r[1:]) for r in images]
    engine.check_cast(mode, images, others)
    mapped = any(r.role for r in images)
    if mode == "copy":
        parts = [("frame", COPY_FRAME)] if mapped else [("copy", COPY_STILL)]
    else:
        if not target:
            raise engine.Refused("swap needs --target: what in the clip is replaced")
        parts = [("swap", SWAP_STILL.format(target=target))]
    parts += engine.cast_lines(images, 2, others, clip="Image 1", it="the frame", moves="pose")
    if change.strip():
        parts.append(("change", engine.rewrite_change(change, len(images), 2, clip="Image 1")))
    if extra.strip():
        parts.append(("extra", extra.strip()))
    return [{"part": k, "text": t} for k, t in parts]


def prompt_for(mode: str, target: str, images: list[engine.Ref], extra: str, change: str = "",
               others: str = "keep") -> str:
    return " ".join(p["text"] for p in still_parts(mode, target, images, extra, change, others))


def body_for(model: str, files: list[Path], prompt: str, w: int, h: int, seed: int | None) -> dict:
    if model == "nbp":
        parts = [{"inlineData": {"mimeType": media.mime(f), "data": b64(f)}} for f in files] + [{"text": prompt}]
        cfg: dict = {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": nearest(NBP_RATIOS, w, h), "imageSize": "2K"}}
        if seed is not None:
            cfg["seed"] = seed
        return {"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}
    if model == "flux3":
        return {"prompt": prompt, "images": [b64(f) for f in files], "aspect_ratio": nearest(FLUX_RATIOS, w, h),
                "resolution": "2k", "grounding": False, "safety_tolerance": 2}
    raise engine.Refused(f"unknown still model {model!r}: nbp or flux3")


def redact(body: dict, files: list[Path]) -> dict:
    r = json.loads(json.dumps(body))
    tags = [f"<base64 of {f.name}, sha256 {media.sha256(f)[:16]}..., {f.stat().st_size} bytes>" for f in files]
    if "contents" in r:
        n = 0
        for part in r["contents"][0]["parts"]:
            if "inlineData" in part:
                part["inlineData"]["data"] = tags[n]
                n += 1
    if "images" in r:
        r["images"] = tags
    return r


def check_image(path: Path, model: str) -> None:
    w, h = media.image_size(path)
    if min(w, h) < 256:
        raise engine.Refused(f"{path.name} is {w}x{h}; the still models want at least 256 px a side")
    if len(b64(path)) > EACH_LIMIT:
        raise engine.Refused(f"{path.name} is over 20 MB as base64; both still models refuse that")
    if model == "flux3" and w * h > FLUX_MAX_PIXELS:
        raise engine.Refused(f"{path.name} is {w * h / 1e6:.1f} MP; FLUX 3 takes at most 16 MP (export it smaller)")


def save_output(model: str, out: dict | object, folder: Path) -> tuple[Path | None, dict]:
    """Pull the image out of a model's native reply; keep it byte for byte."""
    import httpx

    info: dict = {}
    if model == "nbp":
        cands = (out or {}).get("candidates") or []
        parts = ((cands[0] if cands else {}).get("content") or {}).get("parts") or []
        info["finish_reason"] = (cands[0] if cands else {}).get("finishReason")
        info["usage"] = (out or {}).get("usageMetadata")
        info["model_version"] = (out or {}).get("modelVersion")
        for p in parts:
            if "inlineData" in p:
                ext = ".jpg" if "jpeg" in p["inlineData"].get("mimeType", "") else ".png"
                f = folder / f"still_nbp{ext}"
                f.write_bytes(base64.b64decode(p["inlineData"]["data"]))
                return f, info
            if "fileData" in p:
                ext = ".jpg" if "jpeg" in p["fileData"].get("mimeType", "") else ".png"
                f = folder / f"still_nbp{ext}"
                f.write_bytes(httpx.get(p["fileData"]["fileUri"], timeout=120).content)
                return f, info
            if "text" in p:
                info.setdefault("text", []).append(p["text"][:500])
        return None, info
    # flux3
    info["status"] = (out or {}).get("status")
    res = (out or {}).get("result") or {}
    info["provider_cost"] = res.get("cost", (out or {}).get("cost"))
    info["provider_cost_note"] = "BFL's own number, not the Comfy charge"
    url = res.get("sample")
    if not url:
        return None, info
    data = httpx.get(url, timeout=120, follow_redirects=True).content
    ext = ".jpg" if data[:3] == b"\xff\xd8\xff" else ".webp" if data[8:12] == b"WEBP" else ".png"
    f = folder / f"still_flux3{ext}"
    f.write_bytes(data)
    return f, info


def make(mode: str, video: Path, images: list[engine.Ref], *, target: str = "", extra: str = "",
         change: str = "", others: str = "keep", at: float = 0.0, models: tuple[str, ...] = ("nbp",),
         how: str = "dry", api_key: str | None = None, root: Path = Path("stills"),
         seed: int | None = None) -> tuple[Path, dict]:
    images = [engine.Ref(Path(r[0]), *r[1:]) for r in images]
    if how == "run" and not api_key:
        raise engine.Refused("a real run needs a key: --key-file or COMFY_API_KEY")
    if mode not in ("copy", "swap"):
        raise engine.Refused(f"unknown mode {mode!r}")
    if not video.is_file():
        raise engine.Refused(f"no video at {video}")
    if not images:
        raise engine.Refused("at least one --image is needed")
    facts = media.video_facts(video)
    if not 0 <= at < facts.seconds:
        raise engine.Refused(f"--at {at} is outside the clip (0 to {facts.seconds:.2f} s)")
    parts = still_parts(mode, target, images, extra, change, others)
    prompt = " ".join(p["text"] for p in parts)

    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = root / f"{stamp}-{mode}"
    n = 1
    while d.exists():
        n += 1
        d = root / f"{stamp}-{mode}-{n}"
    d.mkdir()
    frame = d / "frame.png"
    grab_frame(video, at, frame)
    files = [frame] + [r.path for r in images]
    for m in models:
        for f in files:
            check_image(f, m)
    fw, fh = media.image_size(frame)

    r: dict = {
        "tool": f"kagami {engine.VERSION}", "kind": "still", "how": how, "mode": mode, "made_at": engine._now(),
        "video": {"path": str(video), "name": video.name, "sha256": media.sha256(video), "seconds": round(facts.seconds, 3)},
        "frame": {"at": at, "path": str(frame), "sha256": media.sha256(frame), "size": f"{fw}x{fh}",
                  "how": "ffmpeg, one frame at that time, PNG"},
        "images": [{"label": f"Image {n}", "path": str(r.path), "name": r.path.name, "sha256": media.sha256(r.path),
                    "what": r.what, **({"cast": {"role": r.role, "who": r.who or None}} if r.role else {})}
                   for n, r in enumerate(images, 2)],
        "prompt": prompt, "target": target, "extra": extra,
        **engine.cast_record(engine.Inputs(mode=mode, video=video, images=images, change=change, others=others), parts),
        "results": {}, "approved": None,
        "args": {"mode": mode, "video": str(video), "images": [[str(r.path), r.what, r.role, r.who] for r in images],
                 "target": target, "extra": extra, "change": change, "others": others, "at": at, "seed": seed},
    }
    path = d / "receipt.json"

    def save() -> None:
        path.write_text(json.dumps(r, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    client = None
    for m in models:
        body = body_for(m, files, prompt, fw, fh, seed)
        res: dict = {"model": MODELS[m], "request": redact(body, files),
                     "request_bytes": len(json.dumps(body).encode()), "idempotency_key": str(uuid.uuid4())}
        r["results"][m] = res
        if how == "dry":
            res["outcome"] = "dry_run: built and checked, nothing sent, nothing charged"
            continue
        if how == "fake":
            f = d / f"still_{m}.png"
            shutil.copyfile(frame, f)
            res.update(outcome="simulated: the frame copied, no model ran", credits_used="none (simulated)",
                       output={"path": str(f), "sha256": media.sha256(f)})
            continue
        res["outcome"] = "sent, no answer recorded yet"
        save()
        if client is None:
            from comfy_sdk import Comfy

            client = Comfy(api_key=api_key)
        t0 = time.monotonic()
        try:
            for attempt in range(1, 4):  # collects under the same key only; never a second generation
                try:
                    rr, headers = engine.send(client, MODELS[m], body, res["idempotency_key"], 300)
                    break
                except Exception as exc:  # noqa: BLE001
                    if engine.collectable(exc) and attempt < 3:
                        print(f"  ... {m} still working; collecting under the same key")
                        time.sleep(engine.COLLECT_PAUSE)
                        continue
                    raise
        except Exception as exc:  # noqa: BLE001
            res.update(outcome="failed", seconds_waited=round(time.monotonic() - t0, 1),
                       error={"type": type(exc).__name__, "message": str(exc)[:1500],
                              "error_type": getattr(exc, "error_type", None),
                              "http_status": getattr(exc, "http_status", None),
                              "details": getattr(exc, "details", None)})
            save()
            continue
        res.update(seconds_waited=round(time.monotonic() - t0, 1), request_id=rr.request_id, replayed=rr.replayed,
                   serving_provider=rr.serving_provider or "the one asked for",
                   dropped_params=rr.dropped_params,
                   **engine.price(rr, headers), router_headers=headers)
        f, info = save_output(m, rr.output if isinstance(rr.output, dict) else {}, d)
        res["reply"] = info
        if f is None:
            res["outcome"] = "nothing_made"
        else:
            try:
                w, h = media.image_size(f)
                size = f"{w}x{h}"
            except ValueError:
                size = None  # kept anyway: the file is what the model returned
            res.update(outcome="made", output={"path": str(f), "sha256": media.sha256(f), "bytes": f.stat().st_size,
                                               "size": size, "note": "the model's file, kept byte for byte"})
        save()
    save()
    return d, r


def approve(folder: Path, pick: str) -> dict:
    path = folder / "receipt.json"
    r = json.loads(path.read_text(encoding="utf-8"))
    res = r.get("results", {}).get(pick)
    if not res or not res.get("output"):
        made = [k for k, v in r.get("results", {}).items() if v.get("output")]
        raise engine.Refused(f"no made still called {pick!r} in {folder.name}; made: {made or 'none'}")
    f = Path(res["output"]["path"])
    if media.sha256(f) != res["output"]["sha256"]:
        raise engine.Refused(f"{f.name} changed since it was made; approve only the file the model returned")
    r["approved"] = {"pick": pick, "at": engine._now(), "path": str(f), "sha256": res["output"]["sha256"],
                     "model": res.get("model")}
    path.write_text(json.dumps(r, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return r["approved"]


def approved(folder: Path) -> tuple[Path, dict]:
    """The approved still of a stills folder, checked byte for byte, ready to be H3's Image 1."""
    r = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    a = r.get("approved")
    if not a:
        raise engine.Refused(f"nothing approved in {folder.name}: python -m kagami approve {folder} --pick nbp")
    f = Path(a["path"])
    if not f.exists() or media.sha256(f) != a["sha256"]:
        raise engine.Refused(f"the approved still {f.name} is missing or changed")
    return f, {"folder": str(folder), **a, "frame_at": r["frame"]["at"], "still_prompt": r["prompt"]}
