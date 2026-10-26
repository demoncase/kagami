"""One Kagami run: build the H3 request, check it, send it (or not), keep the receipt.

Modes
  copy  the source clip's motion, camera and timing, recast with the images.
  swap  the source clip kept, one thing in it replaced by the thing in Image 1.

Both send the clip as `reference_video` (Video 1) and the images as
`reference_image` (Image 1, Image 2, ...) to `minimax/minimax-h3` on Comfy Router.
Only the prompt differs.

Three ways to run, and the receipt always says which:
  dry   (default) the request is built, checked and saved; nothing is sent.
  fake  the whole path runs offline; the "result" is a copy of the source clip.
  run   the one paid call. Never retried by Kagami: a lost answer is recovered
        with `--replay`, which resends the same body under the same
        Idempotency-Key (Router answers the first result, not billed again).
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import platform
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from . import media, prices

MODEL = "minimax/minimax-h3"
VERSION = "0.1.0"
BODY_LIMIT = 100 * 1024 * 1024  # Router's cap on the raw request body
# The first request has never brought the answer back: in every real run it waited out its timeout,
# and the next collect under the same key returned the finished video at once. So the first wait is
# short, and Kagami then checks about once a minute and a half: a run ends soon after H3 does.
FIRST_WAIT = 120       # seconds the first request waits
CHECK_WAIT = 60        # seconds each collect waits
CHECK_EVERY = 90       # a collect starts at most this often (each one re-sends the request body)
COLLECT_PAUSE = 15     # the least pause between two requests
WAIT_MINUTES = 45      # how long one Kagami call keeps collecting before it stops and says so


def send(client, model: str, body: dict, key: str, timeout: float):
    """One Router run. Returns the SDK's run result plus the price headers Router sent.

    The SDK's run_detailed keeps X-Comfy-Credits-Used but drops the USD price Router also sends
    (X-Litellm-Response-Cost), so the headers are read here. Both are absent on a replay and for
    models Router does not price per run: absent means "not reported", never free.
    """
    run = getattr(client.models, "_run", None)
    if run is None:  # an SDK without the private call: credits only
        res = client.models.run_detailed(model, body, idempotency_key=key, timeout=timeout)
        return res, {}
    from comfy_sdk.models import _run_result

    out, headers = run(model, body, idempotency_key=key, timeout=timeout)
    keep = {k: headers.get(k) for k in ("X-Comfy-Credits-Used", "X-Litellm-Response-Cost", "Idempotent-Replayed",
                                         "X-Comfy-Request-Id") if headers.get(k) is not None}
    return _run_result(out, headers), keep


def price(res, headers: dict) -> dict:
    """What the receipt says about cost: Router's numbers as sent, or why there are none."""
    usd = headers.get("X-Litellm-Response-Cost")
    note = ("a replay of an earlier answer: charged once, and Router does not repeat the price"
            if res.replayed else "Router sent no price for this model")
    return {"credits_used": res.credits_used if res.credits_used is not None else "not reported",
            "usd_reported": usd if usd is not None else "not reported",
            "price_note": None if (res.credits_used is not None or usd is not None) else note}


def collectable(exc: BaseException) -> bool:
    """A failure that means 'not finished yet', where a same-key resend collects the work."""
    import httpx

    if isinstance(exc, (httpx.ReadTimeout, httpx.RemoteProtocolError, TimeoutError)):
        return True
    if getattr(exc, "error_type", None) in ("deadline_exceeded", "provider_timeout"):
        return True
    return getattr(exc, "http_status", None) == 409  # the key's work is still running
MAX_IMAGES = 9

MOTION = (
    "Video 1 is the motion reference. Recreate it shot for shot: the same camera movement, "
    "the same framing, the same timing, and every movement of the body, beat for beat."
)
COPY_PROMPT = (
    MOTION + " Replace the person in Video 1 with the person in Image 1: their face, skin tone, hair, "
    "build and clothing exactly as in Image 1."
)

# The cast map: one sentence per image with a role. {clip} is what is recast ("Video 1", or the
# frame, "Image 1", for a preview still), {it} the shot or the frame, {n} the image's number.
ROLES = ("person", "place", "object", "look")
CAST = {
    "person": "Replace {who} in {clip} with the person in Image {n}: their face, skin tone, hair, build and "
              "clothing exactly as in Image {n}.",
    "place": "The place in Image {n} replaces the setting of {clip}: {it} is set there, with {clip}'s camera, "
             "ground plane and where everyone stands kept as they are.",
    "object": "Replace {who} in {clip} with the object in Image {n}, matching Image {n} exactly in shape, colour, "
              "material and markings.",
    "look": "Image {n} sets the look only: match its style, colour and light, and take nothing else from it.",
}
OTHERS = {
    "keep": "Everyone else in {clip} stays exactly as they are.",
    "restyle": "Everyone else in {clip} keeps their place and {moves}, restyled to fit the new cast and setting.",
}
MENTION = re.compile(r"@(image|video)(\d+)", re.IGNORECASE)
SWAP_PROMPT = (
    "Keep Video 1 exactly as it is: the same people, place, camera movement, framing, timing "
    "and light, frame for frame. Change one thing only: {target} becomes the one in Image 1, "
    "matching Image 1 exactly in shape, colour, material and markings. Everything else stays "
    "as it is in Video 1."
)


SWAP_PROMPT_STILL = (
    "Keep Video 1 exactly as it is: the same people, place, camera movement, framing, timing "
    "and light, frame for frame. Change one thing only: {target} becomes the one in Image 2, "
    "matching Image 2 exactly in shape, colour, material and markings. Everything else stays "
    "as it is in Video 1."
)


class Refused(Exception):
    """The run cannot go: said before anything is sent."""


class Ref(NamedTuple):
    """One image you added: what it is in your words (as always), or its role in the cast map."""
    path: Path
    what: str = ""
    role: str = ""  # person | place | object | look, or "" (no role)
    who: str = ""   # person and object: who or what in the clip it replaces, in words


@dataclass
class Inputs:
    mode: str
    video: Path
    images: list[Ref]  # your images, in order; an approved still goes ahead of them as Image 1
    target: str = ""
    prompt: str = ""
    extra: str = ""
    change: str = ""     # "describe the change", word for word (@image1 / @video1 become Image 1 / Video 1)
    others: str = "keep"  # with a person mapped, everyone else in the clip: keep | restyle
    resolution: str = "768P"
    duration: int | None = None
    ratio: str | None = None
    seed: int | None = None
    fit: bool = False  # make send copies of images outside H3's limits instead of refusing
    copies: Path = Path("runs") / "_send_copies"
    still: Path | None = None  # a stills folder with an approved still: it travels as Image 1


@dataclass
class Built:
    body: dict
    redacted: dict
    prompt: str
    facts: media.VideoFacts
    files: list[dict] = field(default_factory=list)
    body_bytes: int = 0
    still: dict | None = None
    parts: list[dict] = field(default_factory=list)


def check_cast(mode: str, images: list[Ref], others: str = "keep") -> None:
    """Refuse a cast map that cannot be written as one clear prompt."""
    if others not in OTHERS:
        raise Refused(f"--others {others!r}: keep or restyle")
    if not any(r.role for r in images):
        return
    if mode != "copy":
        raise Refused("the cast map is for copy; swap replaces one thing (--target) and takes --change")
    for k, r in enumerate(images, 1):
        if r.role not in ("", *ROLES):
            raise Refused(f"@image{k}: no role {r.role!r} (person, place, object or look)")
        if r.role == "object" and not r.who.strip():
            raise Refused(f"@image{k} is an object: say what in the clip it replaces (path::object:the red ball)")
    people = [r for r in images if r.role == "person"]
    if len(people) > 1 and not all(r.who.strip() for r in people):
        raise Refused("two or more people are mapped: say who each one replaces (path::person:the man in the orange suit)")


def cast_lines(images: list[Ref], first: int, others: str, clip: str = "Video 1", it: str = "the shot",
               moves: str = "movement") -> list[tuple[str, str]]:
    """(part, sentence) per image in image order, then the extras sentence when a person is mapped.
    first is the number your first image travels under (2 when an approved still or the frame is Image 1)."""
    out = []
    for n, r in enumerate(images, first):
        if r.role:
            out.append((f"image {n}", CAST[r.role].format(n=n, clip=clip, it=it, who=r.who.strip() or "the person")))
        elif r.what:
            out.append((f"image {n}", f"Image {n} is {r.what}."))
    if any(r.role == "person" for r in images):
        out.append(("extras", OTHERS[others].format(clip=clip, moves=moves)))
    return out


def rewrite_change(text: str, images: int, first: int, clip: str = "Video 1") -> str:
    """The change in your words, with @imageK (your Kth image) and @video1 named as the request names them."""
    def name(m: re.Match) -> str:
        kind, k = m.group(1).lower(), int(m.group(2))
        if kind == "video":
            if k != 1:
                raise Refused(f"@video{k} is not attached: there is one clip, @video1")
            return clip
        if not 1 <= k <= images:
            have = f"@image1 to @image{images}" if images > 1 else "@image1" if images else "none"
            raise Refused(f"@image{k} is not attached: {images} image{'' if images == 1 else 's'} added ({have})")
        return f"Image {k + first - 1}"

    return MENTION.sub(name, text.strip())


def write_parts(i: Inputs, still_at: float | None = None) -> list[dict]:
    """The prompt part by part, in a fixed order, so the same inputs always give the same text: the motion
    sentence, one sentence per image in image order, the extras sentence, the approved still's sentence, the
    change word for word, then --extra. i.images are yours; an approved still (still_at: its frame's time)
    is Image 1 ahead of them. With no roles set this is the prompt Kagami has always written."""
    from . import still as stills

    check_cast(i.mode, i.images, i.others)
    first = 1 if still_at is None else 2
    mapped = any(r.role for r in i.images)
    if i.prompt:
        parts = [("yours", i.prompt)]
    elif i.mode == "copy":
        parts = [("motion", MOTION)] if mapped else [("copy", COPY_PROMPT)]
    else:
        if not i.target:
            raise Refused("swap needs --target: what in the clip is replaced (e.g. 'the red mug on the table')")
        parts = [("swap", (SWAP_PROMPT if still_at is None else SWAP_PROMPT_STILL).format(target=i.target))]
    parts += cast_lines(i.images, first, i.others)
    if still_at is not None:
        more = " The other images show the same person, for reference." if i.mode == "copy" and i.images and not mapped else ""
        parts.append(("still", stills.h3_still(still_at) + more))
    if i.change.strip():
        parts.append(("change", rewrite_change(i.change, len(i.images), first)))
    if i.extra:
        parts.append(("extra", i.extra))
    return [{"part": k, "text": t} for k, t in parts]


def write_prompt(i: Inputs, still_at: float | None = None) -> str:
    return " ".join(p["text"] for p in write_parts(i, still_at))


def build(i: Inputs) -> Built:
    still_meta = None
    images = [Ref(Path(r[0]), *r[1:]) for r in i.images]  # a plain (path, what) pair still works
    i = dataclasses.replace(i, images=images)
    if i.still is not None:
        from . import still as stills

        f, still_meta = stills.approved(i.still)
        images = [Ref(f)] + images  # the approved still travels as Image 1
    if len(images) > MAX_IMAGES:
        raise Refused(f"H3 takes at most {MAX_IMAGES} images; {len(images)}" + (" with the still" if still_meta else " given"))
    if i.mode not in ("copy", "swap"):
        raise Refused(f"unknown mode {i.mode!r}")
    if not i.video.is_file():
        raise Refused(f"no video at {i.video}")
    if not images:
        raise Refused("at least one --image is needed (Image 1)")
    sent: list[tuple[Path, dict | None]] = []  # what travels for each image, and the copy's facts
    for p, *_ in images:
        if not p.is_file():
            raise Refused(f"no image at {p}")
        if p.suffix.lower() not in media.IMAGE_TYPES:
            raise Refused(f"{p.name}: PNG, JPEG or WebP only")
        problem = media.image_problem(*media.image_size(p))
        if problem and not i.fit:
            raise Refused(f"{p.name} is {problem}; H3 takes 256 to 5760 px a side, aspect 2:5 to 5:2. "
                          f"Add --fit to send a padded, scaled copy (the file itself is never changed).")
        sent.append(media.fit_copy(p, i.copies) if problem else (p, None))
    if i.video.suffix.lower() not in media.VIDEO_TYPES:
        raise Refused(f"{i.video.name}: MP4, MOV or WebM only")

    facts = media.video_facts(i.video)
    if facts.seconds < 2:
        raise Refused(f"the clip is {facts.seconds:.2f} s; H3 takes reference videos of 2 to 15 s")
    if facts.seconds > 15.05:
        raise Refused(f"the clip is {facts.seconds:.2f} s; H3 takes at most 15 s (cut it, or wait for chunking)")
    if facts.fps and not (23.9 <= facts.fps <= 60.5):
        raise Refused(f"the clip is {facts.fps} fps; H3 takes 23.976 to 60 fps")

    duration = i.duration or max(4, min(15, math.ceil(facts.seconds - 0.05)))
    if not 4 <= duration <= 15:
        raise Refused(f"duration {duration}: Router takes 4 to 15 s")
    ratio = i.ratio or media.nearest_ratio(facts.width, facts.height)
    parts = write_parts(i, still_meta["frame_at"] if still_meta else None)
    prompt = " ".join(p["text"] for p in parts)

    files = [{"role": "reference_video", "label": "Video 1", "path": str(i.video), "name": i.video.name,
              "bytes": i.video.stat().st_size, "sha256": media.sha256(i.video),
              "seconds": round(facts.seconds, 3), "size": f"{facts.width}x{facts.height}", "fps": facts.fps}]
    for n, (r, (q, copy)) in enumerate(zip(images, sent), 1):
        p = r.path
        w, h = media.image_size(p)
        f = {"role": "reference_image", "label": f"Image {n}", "path": str(p), "name": p.name,
             "bytes": p.stat().st_size, "sha256": media.sha256(p), "size": f"{w}x{h}", "what": r.what}
        if r.role:
            f["cast"] = {"role": r.role, "who": r.who or None}
        if copy:
            f["send_copy"] = {**copy, "path": str(q), "bytes": q.stat().st_size, "sha256": media.sha256(q)}
        files.append(f)

    content = [{"type": "text", "text": prompt},
               {"type": "video_url", "role": "reference_video", "video_url": {"url": media.data_uri(i.video)}}]
    for q, _ in sent:
        content.append({"type": "image_url", "role": "reference_image", "image_url": {"url": media.data_uri(q)}})
    body = {"content": content, "duration": duration, "ratio": ratio, "resolution": i.resolution}
    if i.seed is not None:
        body["seed"] = i.seed

    redacted = json.loads(json.dumps(body))
    for item, f in zip(redacted["content"][1:], files):
        key = "video_url" if item["type"] == "video_url" else "image_url"
        src = f.get("send_copy") or f
        name = Path(src["path"]).name if "send_copy" in f else f["name"]
        item[key]["url"] = f"<data URI of {name}, sha256 {src['sha256'][:16]}..., {src['bytes']} bytes>"

    size = len(json.dumps(body).encode("utf-8"))
    if size > BODY_LIMIT:
        raise Refused(f"the request is {size / 2**20:.1f} MiB; Router's cap is 100 MiB (base64 counts). Use a shorter or smaller clip.")
    if still_meta:
        files[1]["approved_still"] = still_meta
    return Built(body=body, redacted=redacted, prompt=prompt, facts=facts, files=files, body_bytes=size, still=still_meta,
                 parts=parts)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_dir(root: Path, mode: str) -> Path:
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    for n in range(100):
        d = root / (f"{stamp}-{mode}" if n == 0 else f"{stamp}-{mode}-{n + 1}")
        try:
            d.mkdir(parents=True, exist_ok=False)
            return d
        except FileExistsError:
            continue
    raise Refused(f"could not make a run folder under {root}")


def cast_record(i: Inputs, parts: list[dict]) -> dict:
    """What the receipt keeps of the cast map, beside the full prompt: per image its role and who, the
    extras switch (when a person is mapped), the change as typed, and each part of the prompt as written."""
    refs = [Ref(Path(r[0]), *r[1:]) for r in i.images]
    return {"cast": [{"image": f"@image{k}", "name": r.path.name, "role": r.role or None, "who": r.who or None}
                     for k, r in enumerate(refs, 1)],
            "extras": i.others if any(r.role == "person" for r in refs) else None,
            "change": i.change or None, "prompt_parts": parts}


def receipt_base(i: Inputs, b: Built, how: str, key: str) -> dict:
    return {
        "tool": f"kagami {VERSION}", "how": how, "mode": i.mode, "model": MODEL, "made_at": _now(),
        "machine": platform.platform(), "inputs": b.files, "prompt": b.prompt, **cast_record(i, b.parts),
        "settings": {k: b.body[k] for k in ("duration", "ratio", "resolution", "seed") if k in b.body},
        "request": b.redacted, "request_bytes": b.body_bytes, "idempotency_key": key, "still": b.still,
        "args": {"mode": i.mode, "video": str(i.video), "images": [[str(r[0]), *r[1:]] for r in i.images],
                 "target": i.target, "prompt": i.prompt, "extra": i.extra, "change": i.change, "others": i.others,
                 "resolution": i.resolution,
                 "duration": i.duration, "ratio": i.ratio, "seed": i.seed, "fit": i.fit,
                 "still": str(i.still) if i.still else None},
    }


def save(d: Path, receipt: dict) -> Path:
    path = d / "receipt.json"
    path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def run(i: Inputs, how: str, root: Path, api_key: str | None = None, key: str | None = None,
        out_dir: Path | None = None, wait_minutes: float = WAIT_MINUTES,
        earlier: dict | None = None) -> tuple[Path, dict]:
    """Build, then dry / fake / run. Returns the run folder and the receipt.
    earlier: on a replay, the run's receipt before it (its reported price is kept)."""
    if how == "run" and not api_key:
        raise Refused("a real run needs a key: --key-file or COMFY_API_KEY")
    i.copies = root / "_send_copies"
    b = build(i)
    key = key or str(uuid.uuid4())
    d = out_dir or new_run_dir(root, i.mode)
    r = receipt_base(i, b, how, key)

    if how == "dry":
        r["outcome"] = "dry_run: built and checked, nothing sent, nothing charged"
        save(d, r)
        return d, r

    if how == "fake":
        out = d / "output.mp4"
        shutil.copyfile(i.video, out)
        r.update(outcome="simulated: the source clip copied, no model ran", credits_used="none (simulated)",
                 output={"path": str(out), "sha256": media.sha256(out), "bytes": out.stat().st_size})
        save(d, r)
        return d, r

    # The paid call. The receipt is written BEFORE sending, so a crash still leaves the key.
    r["outcome"] = "sent, no answer recorded yet (if this stays, use --replay with this folder)"
    save(d, r)

    from comfy_sdk import Comfy  # imported here so dry and fake runs need no SDK

    client = Comfy(api_key=api_key)
    t0 = time.monotonic()
    give_up = t0 + wait_minutes * 60
    r.setdefault("attempts", [])
    r["waits"] = {"first_wait": FIRST_WAIT, "check_wait": CHECK_WAIT, "check_every": CHECK_EVERY,
                  "give_up_minutes": wait_minutes}
    attempt = 0
    while True:
        attempt += 1
        started = time.monotonic()
        try:
            res, headers = send(client, MODEL, b.body, key, FIRST_WAIT if attempt == 1 else CHECK_WAIT)
            break
        except Exception as exc:  # noqa: BLE001 - every failure is recorded
            err = {"type": type(exc).__name__, "message": str(exc)[:2000],
                   "error_type": getattr(exc, "error_type", None),
                   "request_id": getattr(exc, "request_id", None),
                   "http_status": getattr(exc, "http_status", None),
                   "details": getattr(exc, "details", None),
                   "field_errors": [str(e) for e in getattr(exc, "errors", ())]}
            r["attempts"].append({"at": _now(), "attempt": attempt, "seconds": round(time.monotonic() - started, 1),
                                  "error": err})
            if collectable(exc) and time.monotonic() < give_up:
                # Still generating. The same body under the same key COLLECTS that generation
                # (Router's contract), it never starts a second one, so this is not a retry.
                pause = max(COLLECT_PAUSE, CHECK_EVERY - (time.monotonic() - started))
                waited = round((time.monotonic() - t0) / 60, 1)
                print(f"  ... still generating after {waited} min; checking again in {pause:.0f} s under the same key (check {attempt + 1})")
                save(d, r)
                time.sleep(pause)
                continue
            r.update(outcome="failed" if not collectable(exc) else "not collected yet (still generating?)",
                     seconds_waited=round(time.monotonic() - t0, 1), error=err,
                     note="An invalid body is refused before the provider is called (not charged). "
                          "A timeout means the generation may still be running and may be billed: "
                          "`python -m kagami replay <this folder>` collects it under the same key (no second charge), "
                          "for up to 24 hours.")
            save(d, r)
            raise
    r.update(seconds_waited=round(time.monotonic() - t0, 1), request_id=res.request_id,
             replayed=res.replayed, **price(res, headers), router_headers=headers)
    if earlier and prices.reported(earlier) and not prices.reported(r):
        # Router reported the price on the first answer and does not repeat it on a replay: keep its numbers.
        r.update(credits_used=earlier.get("credits_used"), usd_reported=earlier.get("usd_reported"),
                 price_note=f"reported by Router on the first answer ({earlier.get('request_id')}); not repeated on this replay")
    task = (res.output or {}).get("task", {}) if isinstance(res.output, dict) else {}
    r["task"] = {k: task.get(k) for k in ("id", "status", "duration", "ratio", "resolution", "model", "task_type", "usage", "error")}
    url = (task.get("content") or {}).get("url")
    if task.get("status") != "succeeded" or not url:
        r["outcome"] = "nothing_made"
        save(d, r)
        return d, r
    # Router's answer (the request id and the price it reported, which a replay never repeats) is on disk
    # before the download, so a failed download loses only the file.
    r["outcome"] = "answered, the video not downloaded yet (if this stays, use --replay with this folder)"
    save(d, r)

    import httpx

    out = d / "output.mp4"
    part = d / "output.mp4.part"
    try:
        with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
            resp.raise_for_status()
            with part.open("wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        facts = media.video_facts(part)
        part.replace(out)
    except Exception as exc:  # noqa: BLE001 - recorded, then raised
        part.unlink(missing_ok=True)
        r.update(outcome="answered, but the video could not be downloaded",
                 error={"type": type(exc).__name__, "message": str(exc)[:2000]},
                 note="H3 made the video and Router's answer is recorded above. "
                      "`python -m kagami replay <this folder>` asks for the same answer again under the same key "
                      "(no second charge), for up to 24 hours.")
        save(d, r)
        raise
    r.update(outcome="made", output={"path": str(out), "sha256": media.sha256(out), "bytes": out.stat().st_size,
                                     "seconds": round(facts.seconds, 3), "size": f"{facts.width}x{facts.height}",
                                     "fps": facts.fps, "note": "the provider's file, kept byte for byte (it carries H3's own audio)"})
    try:  # beside Router's numbers, never over them
        r.update(prices.compute(r))
    except ValueError as exc:
        r["price_source"] = {"error": str(exc)}
    save(d, r)
    return d, r


def replayable(old: dict, run_folder: Path) -> None:
    """Refuse a replay unless the folder is a real send whose answer was lost. A dry or fake folder was
    never sent (its key would start a new, paid generation), and a run with its video is done."""
    if old.get("how") != "run":
        raise Refused(f"{run_folder.name} is a {old.get('how', 'unknown')} run: nothing was sent under its key, "
                      "so a replay would be a new paid run. Make one with --run instead.")
    if old.get("output") or (run_folder / "output.mp4").exists():
        raise Refused(f"{run_folder.name} already has its video; a replay is only for an answer that was lost")
    if old.get("outcome") == "nothing_made":
        raise Refused(f"{run_folder.name} was answered (nothing was made: {(old.get('task') or {}).get('error')}); "
                      "a replay would bring back the same answer")


def replay(run_folder: Path, api_key: str, wait_minutes: float = WAIT_MINUTES) -> tuple[Path, dict]:
    """Resend a run's exact request under its own key: Router answers the first result."""
    old = json.loads((run_folder / "receipt.json").read_text(encoding="utf-8"))
    replayable(old, run_folder)
    a = old["args"]
    i = Inputs(mode=a["mode"], video=Path(a["video"]), images=[Ref(Path(x[0]), *x[1:]) for x in a["images"]],
               target=a["target"], prompt=a["prompt"], extra=a["extra"], change=a.get("change", ""),
               others=a.get("others", "keep"), resolution=a["resolution"],
               duration=a["duration"], ratio=a["ratio"], seed=a["seed"], fit=a.get("fit", False),
               copies=run_folder.parent / "_send_copies", still=Path(a["still"]) if a.get("still") else None)
    b = build(i)
    for now, then in zip(b.files, old["inputs"]):
        if now["sha256"] != then["sha256"]:
            raise Refused(f"{now['name']} changed since the run; a replay must send the same bytes")
    keep = run_folder / f"receipt.before-replay-{datetime.now().strftime('%H%M%S')}.json"
    shutil.copyfile(run_folder / "receipt.json", keep)
    print(f"  collecting under the run's own key {old['idempotency_key']} (same body, so no second charge)")
    return run(i, "run", run_folder.parent, api_key=api_key, key=old["idempotency_key"], out_dir=run_folder,
               wait_minutes=wait_minutes, earlier=old)


def read_key(key_file: str | None) -> str | None:
    if key_file:
        return Path(key_file).read_text(encoding="utf-8").strip()
    return os.environ.get("COMFY_API_KEY", "").strip() or None
