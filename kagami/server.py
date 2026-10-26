"""Kagami's local app: one page, one flow, the same engine as the terminal.

  python -m kagami serve                      FAKE: every click is free, nothing is sent
  python -m kagami serve --live --key-file E:\\keys\\comfy-router.txt   LIVE: clicks cost credits

The mode is chosen when the server starts and shown on every screen; the page cannot switch it.
Files stay on this machine: uploads in work/, results in runs/ and stills/.

Only the page this server hands out can call it: a request must come to 127.0.0.1 or localhost on
this port, from that origin, with the token made when the server started (the page carries it).
Another site open in the same browser cannot start a paid run, or name a file for it to send.
"""

from __future__ import annotations

import io
import json
import secrets
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import edit, engine, finish, media, prices, still, workflow

ROOT = Path.cwd()
WORK, RUNS, STILLS = ROOT / "work", ROOT / "runs", ROOT / "stills"
SERVED = (WORK, RUNS, STILLS)
PAGE = Path(__file__).with_name("app.html")


class _ThreadOut(io.TextIOBase):
    """stdout that sends each job thread's prints to that job's log (the engine reports by print)."""

    def __init__(self, real):
        self.real, self.local = real, threading.local()

    def write(self, s):
        log = getattr(self.local, "log", None)
        if log is not None and s.strip():
            log.append({"at": time.strftime("%H:%M:%S"), "text": s.strip()})
        return self.real.write(s)

    def flush(self):
        self.real.flush()


OUT = _ThreadOut(sys.stdout)


class State:
    how = "fake"
    key: str | None = None
    jobs: dict[str, dict] = {}
    lock = threading.Lock()  # held while a still or video job runs: one paid job at a time
    token = secrets.token_urlsafe(24)  # made once per start; only the page served by this start has it


def _job(kind: str, fn, *args, lock: threading.Lock | None = None, **kw) -> str:
    """Run fn in a thread. lock, when given, is already held and is let go when the job ends."""
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "kind": kind, "state": "running", "started": time.time(), "log": [], "result": None, "error": None}
    State.jobs[jid] = job

    def work():
        OUT.local.log = job["log"]
        try:
            job["result"] = fn(*args, **kw)
            job["state"] = "done"
        except engine.Refused as exc:
            job.update(state="refused", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - shown on the page, the receipt has the rest
            job.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
        finally:
            job["ended"] = time.time()
            OUT.local.log = None
            if lock is not None:
                lock.release()

    try:
        threading.Thread(target=work, daemon=True).start()
    except BaseException:
        if lock is not None:
            lock.release()
        raise
    return jid


def _paid_job(kind: str, fn) -> str:
    """A still or a video: refused while another one runs, so a second click never sends a second request."""
    if not State.lock.acquire(blocking=False):
        running = next((j["kind"] for j in State.jobs.values() if j["state"] == "running" and j["kind"] in ("still", "video")), "a job")
        raise HTTPException(409, f"the {running} job is still running; wait for it to end")
    return _job(kind, fn, lock=State.lock)


def _inside(path: str, base: Path) -> Path:
    """A path the page sent, kept inside base (work/ for clips and images, stills/ for previews)."""
    f = (ROOT / path).resolve()
    if not f.is_relative_to(base.resolve()):
        raise HTTPException(400, f"{path}: not a file this app made")
    return f


def rel(p: str | Path) -> str:
    """A path the page can ask /files for (relative to the app's folder, forward slashes)."""
    p = Path(p)
    try:
        return p.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _image_list(items: list[dict]) -> list[engine.Ref]:
    """The page's image cards: what each is, or its role in the cast map and who it replaces."""
    out = []
    for i in items:
        role = i.get("role") or ""
        out.append(engine.Ref(_inside(i["path"], WORK), (i.get("what") or "").strip() if not role else "", role,
                              (i.get("who") or "").strip() if role in ("person", "object") else ""))
    return out


JSON_POSTS = ("/api/still", "/api/approve", "/api/run", "/api/edit")  # /api/upload takes the file's own bytes


def create_app(port: int = 8777) -> FastAPI:
    app = FastAPI(title="Kagami", docs_url=None, redoc_url=None)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    @app.middleware("http")
    async def only_this_page(request: Request, call_next):
        # Host stops a rebound DNS name; Origin stops another site's form or fetch; the token stops both,
        # and a custom header cannot be sent cross-origin without a preflight this server never answers.
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "Kagami answers on 127.0.0.1 or localhost only"}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin not in origins:
            return JSONResponse({"detail": f"requests from {origin} are refused"}, status_code=403)
        path = request.url.path
        if path.startswith("/api/"):
            # An <img> cannot send a header, so a GET may carry the token in the address instead.
            given = request.headers.get("x-kagami-token") or (request.query_params.get("token") if request.method == "GET" else None)
            if not given or not secrets.compare_digest(given, State.token):
                return JSONResponse({"detail": "no token: open Kagami from the address the server printed"}, status_code=403)
        if request.method == "POST" and path in JSON_POSTS:
            if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return JSONResponse({"detail": "JSON only"}, status_code=415)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def page():
        return PAGE.read_text(encoding="utf-8").replace("__KAGAMI_TOKEN__", State.token)

    @app.get("/api/config")
    def config():
        h3 = prices.table()["models"][engine.MODEL]
        return {"how": State.how, "key": bool(State.key), "version": engine.VERSION,
                "limits": {"images": engine.MAX_IMAGES, "video_seconds": [2, 15], "image_px": [media.IMAGE_MIN, media.IMAGE_MAX]},
                "images_included": h3["images_included"], "credits_per_extra_image": h3["credits_per_extra_image"]}

    @app.post("/api/upload")
    async def upload(request: Request, name: str):
        data = await request.body()
        if not data:
            raise HTTPException(400, "empty upload")
        ext = Path(name).suffix.lower()
        if ext not in media.VIDEO_TYPES and ext not in media.IMAGE_TYPES:
            raise HTTPException(400, f"{name}: MP4, MOV, WebM, PNG, JPEG or WebP only")
        WORK.mkdir(exist_ok=True)
        import hashlib

        sha = hashlib.sha256(data).hexdigest()
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(name).stem)[:60]
        f = WORK / f"{sha[:10]}_{safe}{ext}"
        if not f.exists():
            f.write_bytes(data)  # kept byte for byte, named by its hash
        out = {"path": rel(f), "name": name, "sha256": sha, "bytes": len(data)}
        try:
            if ext in media.VIDEO_TYPES:
                v = media.video_facts(f)
                out.update(kind="video", seconds=round(v.seconds, 3), size=f"{v.width}x{v.height}", fps=v.fps,
                           ratio=media.nearest_ratio(v.width, v.height),
                           problem=("" if 2 <= v.seconds <= 15.05 else f"{v.seconds:.1f} s: H3 takes 2 to 15 s")
                           or ("" if not v.fps or 23.9 <= v.fps <= 60.5 else f"{v.fps} fps: H3 takes 24 to 60"))
            else:
                w, h = media.image_size(f)
                out.update(kind="image", size=f"{w}x{h}", problem=media.image_problem(w, h))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return out

    @app.get("/api/frames")
    def frames(video: str, n: int = 10):
        """A strip of small frames across the clip, to pick the one the preview recasts."""
        import subprocess

        f = (ROOT / video).resolve()
        if not f.is_relative_to(WORK.resolve()) or not f.is_file():
            raise HTTPException(404, "no such clip")
        secs = media.video_facts(f).seconds
        n = max(2, min(n, 16))
        folder = WORK / "frames" / f.stem
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        for k in range(n):
            at = round(min(secs - 0.05, k * secs / (n - 1)), 2) if n > 1 else 0.0
            at = max(0.0, at)
            t = folder / f"t_{at:06.2f}.jpg"
            if not t.exists():
                subprocess.run([media.ffmpeg_exe(), "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(f),
                                "-frames:v", "1", "-vf", "scale=-2:240", "-q:v", "4", str(t)], capture_output=True)
            if t.exists():
                out.append({"at": at, "file": rel(t)})
        return {"seconds": secs, "frames": out}

    @app.get("/api/frame")
    def frame(video: str, at: float):
        """The exact frame a preview would recast (same ffmpeg seek as still.grab_frame), as a JPEG."""
        import subprocess

        f = (ROOT / video).resolve()
        if not f.is_relative_to(WORK.resolve()) or not f.is_file():
            raise HTTPException(404, "no such clip")
        at = max(0.0, round(at, 3))
        folder = WORK / "frames" / f.stem
        folder.mkdir(parents=True, exist_ok=True)
        t = folder / f"big_{at:08.3f}.jpg"
        if not t.exists():
            subprocess.run([media.ffmpeg_exe(), "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(f),
                            "-frames:v", "1", "-vf", "scale=-2:720", "-q:v", "3", str(t)], capture_output=True)
        if not t.exists():
            raise HTTPException(404, "could not read that frame")
        return FileResponse(t)

    @app.post("/api/still")
    async def make_still(request: Request):
        b = await request.json()
        models = tuple(m for m in b.get("models", ["nbp"]) if m in still.MODELS) or ("nbp",)
        video, images = _inside(b["video"], WORK), _image_list(b.get("images", []))

        def go():
            d, r = still.make(b["mode"], video, images, target=b.get("target", ""),
                              change=b.get("change", ""), others=b.get("others", "keep"), at=float(b.get("at", 0)),
                              models=models, how=State.how, api_key=State.key, root=STILLS)
            return _still_view(d, r)

        return {"job": _paid_job("still", go)}

    @app.post("/api/approve")
    async def approve(request: Request):
        b = await request.json()
        try:
            ok = still.approve(_inside(b["folder"], STILLS), b["pick"])
        except engine.Refused as exc:
            raise HTTPException(409, str(exc)) from None
        return {"approved": ok["pick"], "path": rel(ok["path"])}

    @app.post("/api/run")
    async def make_video(request: Request):
        b = await request.json()
        i = engine.Inputs(mode=b["mode"], video=_inside(b["video"], WORK), images=_image_list(b.get("images", [])),
                          target=b.get("target", ""), change=b.get("change", ""), others=b.get("others", "keep"),
                          resolution=b.get("resolution", "768P"), fit=bool(b.get("fit", True)),
                          still=_inside(b["still"], STILLS) if b.get("still") else None, copies=RUNS / "_send_copies")

        def go():
            if State.how == "live":
                print("sent to H3; this can take many minutes. Kagami collects under one key, so a wait never pays twice.")
            d, r = engine.run(i, "run" if State.how == "live" else "fake", RUNS, api_key=State.key)
            if r.get("output"):
                print("made. syncing to the source's beat and building the before/after...")
                try:
                    finish.finish(d)
                except Exception as exc:  # noqa: BLE001 - the video is kept either way, as in the terminal
                    print(f"finishing failed ({exc}); the video is kept. Try: python -m kagami finish {d}")
            try:
                workflow.export(d)
            except Exception as exc:  # noqa: BLE001 - the video is kept either way
                print(f"ComfyUI workflow not exported: {exc}")
            return _run_view(d)

        return {"job": _paid_job("video", go)}

    @app.post("/api/edit")
    async def make_edit(request: Request):
        b = await request.json()
        d = RUNS / Path(b["folder"]).name
        if not (d / "receipt.json").exists():
            raise HTTPException(404, "no such run")

        def go():
            try:
                edit.apply(d, float(b["in"]), float(b["out"]), int(b.get("nudge", 0)))
            except ValueError as exc:
                raise engine.Refused(str(exc)) from None
            return _run_view(d)

        return {"job": _job("edit", go)}

    @app.get("/api/job/{jid}")
    def job(jid: str):
        j = State.jobs.get(jid)
        if not j:
            raise HTTPException(404, "no such job")
        return {**j, "elapsed": round((j.get("ended") or time.time()) - j["started"], 1)}

    @app.get("/api/runs")
    def runs():
        out = []
        for d in sorted(RUNS.glob("2*"), reverse=True)[:30]:
            try:
                out.append(_run_view(d))
            except Exception:  # noqa: BLE001 - a half-written folder is skipped, never fatal
                continue
        return out

    @app.get("/api/run/{folder}")
    def one_run(folder: str):
        d = RUNS / folder
        if not (d / "receipt.json").exists():
            raise HTTPException(404, "no such run")
        return _run_view(d, full=True)

    @app.get("/files/{path:path}")
    def files(path: str):
        f = (ROOT / path).resolve()
        if not any(f.is_relative_to(s.resolve()) for s in SERVED) or not f.is_file():
            raise HTTPException(404, "not served")
        return FileResponse(f)

    @app.exception_handler(engine.Refused)
    async def refused(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    return app


def _still_view(d: Path, r: dict) -> dict:
    res = {}
    for m, v in r["results"].items():
        res[m] = {"model": v["model"], "outcome": v["outcome"], "credits": v.get("credits_used"), "usd": v.get("usd_reported"),
                  "file": rel(v["output"]["path"]) if v.get("output") else None,
                  "error": (v.get("error") or {}).get("message"), "seconds": v.get("seconds_waited"),
                  "served_by": v.get("serving_provider")}
    return {"folder": rel(d), "frame": rel(r["frame"]["path"]), "at": r["frame"]["at"], "prompt": r["prompt"],
            "how": r["how"], "results": res}


def _run_view(d: Path, full: bool = False) -> dict:
    r = json.loads((d / "receipt.json").read_text(encoding="utf-8"))
    fz = r.get("finishes") or {}
    files = {k: rel(v["path"]) for k, v in fz.items() if isinstance(v, dict) and v.get("path")}
    if r.get("output"):
        files["raw"] = rel(r["output"]["path"])
    if (r.get("comfyui") or {}).get("zip"):
        files["comfyui"] = rel(r["comfyui"]["zip"]["path"])
    view = {"folder": d.name, "path": rel(d), "mode": r.get("mode"), "how": r.get("how"), "made_at": r.get("made_at"),
            "outcome": r.get("outcome"), "credits": r.get("credits_used"), "usd": r.get("usd_reported"),
            "price_note": r.get("price_note"), "credits_computed": r.get("credits_computed"),
            "usd_computed": r.get("usd_computed"), "price_date": (r.get("price_source") or {}).get("date"),
            "prompt": r.get("prompt"), "change": r.get("change"), "extras": r.get("extras"),
            "settings": r.get("settings"), "files": files, "warp": (fz.get("synced") or {}).get("warp"),
            "still": (r.get("still") or {}).get("pick"), "inputs": [
                {k: f.get(k) for k in ("label", "name", "sha256", "size", "seconds", "what", "cast")} for f in r.get("inputs", [])],
            "error": (r.get("error") or {}).get("message"), "seconds_waited": r.get("seconds_waited"),
            "length": ((fz.get("synced") or {}).get("seconds")), "fps": (r.get("output") or {}).get("fps"),
            "edits": [{"version": e["version"], "trim": e["trim"], "nudge": e["nudge_frames"],
                       "files": {k: rel(f["path"]) for k, f in e["files"].items()}} for e in r.get("edits", [])]}
    if full:
        view["receipt"] = r
    return view


def serve(how: str, key: str | None, port: int, open_browser: bool = True) -> None:
    import uvicorn

    State.how, State.key = how, key
    sys.stdout = OUT
    for d in (WORK, RUNS, STILLS):
        d.mkdir(exist_ok=True)
    url = f"http://127.0.0.1:{port}"
    print(f"Kagami {engine.VERSION}: {'LIVE, clicks cost credits' if how == 'live' else 'FAKE, every click is free'}  ->  {url}")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(port), host="127.0.0.1", port=port, log_level="warning")
