"""Kagami from the terminal.

  python -m kagami copy --video clip.mp4 --image me.png                 dry run (default): built, checked, nothing sent
  python -m kagami copy --video clip.mp4 --image me.png --fake          the whole path offline, free
  python -m kagami copy --video clip.mp4 --image me.png --run --key-file E:\\keys\\comfy-router.txt   THE PAID CALL
  python -m kagami swap --video clip.mp4 --image mug.png --target "the white mug on the desk"
  python -m kagami replay runs\\20261021-160000-copy --key-file ...       collect a run whose answer was lost (not billed twice)
  python -m kagami finish runs\\20261021-160000-copy                       the source's sound + before/after files (free, local)
  python -m kagami comfy runs\\20261021-160000-copy                        the run as a ComfyUI workflow (free, local)

An image can say what it is: --image "jacket.png::the jacket he wears" becomes "Image 2 is the jacket he wears."
Or take a role in the cast map (copy):
  --image "me.png::person:the man in the orange suit"    replaces him (just "::person" when there is one person)
  --image "beach.png::place"                             the setting; camera, ground and positions kept
  --image "ball.png::object:the red ball"                replaces that thing
  --image "grade.png::look"                              style and light only, nothing replaced
  --others keep|restyle    everyone else in the clip, when a person is mapped (default keep)
  --change "text"          the change in your words; @image1 / @video1 name your images and the clip
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import engine, finish, prices, still, workflow


def approx() -> str:
    """The "about" sign, or "~" on a console that cannot print it."""
    try:
        "≈".encode(sys.stdout.encoding or "ascii")
        return "≈"
    except (UnicodeEncodeError, LookupError):
        return "~"


def image_arg(text: str) -> engine.Ref:
    """path, path::what it is, or path::role[:who] with role person, place, object or look."""
    path, _, rest = text.partition("::")
    role, _, who = rest.partition(":")
    if role.strip().lower() in engine.ROLES:
        return engine.Ref(Path(path), role=role.strip().lower(), who=who.strip())
    return engine.Ref(Path(path), rest.strip())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kagami", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for mode in ("copy", "swap"):
        p = sub.add_parser(mode)
        p.add_argument("--video", required=True, type=Path, help="the source clip, 2 to 15 s")
        p.add_argument("--image", action="append", default=[], type=image_arg, help="a reference image (repeat; the first is Image 1)")
        p.add_argument("--target", default="", help="swap: what in the clip is replaced")
        p.add_argument("--prompt", default="", help="replace Kagami's prompt entirely")
        p.add_argument("--extra", default="", help="a sentence added after Kagami's prompt")
        p.add_argument("--change", default="", help="the change in your words (@image1, @video1)")
        p.add_argument("--others", choices=["keep", "restyle"], default="keep",
                       help="with a person mapped: everyone else in the clip kept as they are, or restyled to fit")
        p.add_argument("--resolution", default="768P", choices=["768P", "2K"])
        p.add_argument("--duration", type=int, help="output seconds, 4 to 15 (default: the clip's length)")
        p.add_argument("--ratio", choices=["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"])
        p.add_argument("--seed", type=int)
        p.add_argument("--still", type=Path, help="a stills folder whose approved still travels as Image 1")
        p.add_argument("--fit", action="store_true", help="send a padded, scaled copy of an image outside H3's limits (256-5760 px, 2:5 to 5:2)")
        how = p.add_mutually_exclusive_group()
        how.add_argument("--run", action="store_true", help="send it: the one paid call")
        how.add_argument("--fake", action="store_true", help="run the path offline, free")
        p.add_argument("--key-file")
        p.add_argument("--runs", type=Path, default=Path("runs"))
        p.add_argument("--wait", type=float, default=engine.WAIT_MINUTES, help="minutes to keep collecting (default 45)")
    r = sub.add_parser("replay")
    r.add_argument("folder", type=Path)
    r.add_argument("--key-file")
    r.add_argument("--wait", type=float, default=engine.WAIT_MINUTES, help="minutes to keep collecting (default 45)")
    st = sub.add_parser("still", help="recast one frame on an image model before paying for video")
    st.add_argument("mode", choices=["copy", "swap"])
    st.add_argument("--video", required=True, type=Path)
    st.add_argument("--image", action="append", default=[], type=image_arg)
    st.add_argument("--target", default="")
    st.add_argument("--extra", default="")
    st.add_argument("--change", default="", help="the change in your words (@image1, @video1)")
    st.add_argument("--others", choices=["keep", "restyle"], default="keep")
    st.add_argument("--at", type=float, default=0.0, help="the frame's time in seconds (default 0, the first frame)")
    st.add_argument("--model", choices=["nbp", "flux3", "both"], default="nbp")
    st.add_argument("--seed", type=int)
    sh = st.add_mutually_exclusive_group()
    sh.add_argument("--run", action="store_true", help="send it: one paid image call per model")
    sh.add_argument("--fake", action="store_true")
    st.add_argument("--key-file")
    st.add_argument("--stills", type=Path, default=Path("stills"))
    ap_ = sub.add_parser("approve", help="pick the still that goes to H3 as Image 1")
    ap_.add_argument("folder", type=Path)
    ap_.add_argument("--pick", required=True, choices=["nbp", "flux3"])
    sv = sub.add_parser("serve", help="the app: one page in your browser")
    sv.add_argument("--live", action="store_true", help="real calls, real credits (default: FAKE, free)")
    sv.add_argument("--key-file")
    sv.add_argument("--port", type=int, default=8777)
    sv.add_argument("--no-browser", action="store_true")
    fz = sub.add_parser("finish", help="the source's sound and the before/after files for a made run (free)")
    fz.add_argument("folder", type=Path)
    fz.add_argument("--no-sync", action="store_true", help="leave the recast's timing as H3 made it")
    cf = sub.add_parser("comfy", help="the run as a ComfyUI workflow on the H3 partner node (free, local)")
    cf.add_argument("folder", type=Path)
    cf.add_argument("--comfy-input", type=Path, help="ComfyUI's input folder: copy the run's files straight there")
    pr = sub.add_parser("price", help="compute an older run's price from Comfy's published rate (Router's numbers untouched)")
    pr.add_argument("folder", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        from . import server

        key = engine.read_key(a.key_file)
        if a.live and not key:
            print("refused: --live needs a key: --key-file or COMFY_API_KEY")
            return 2
        server.serve("live" if a.live else "fake", key if a.live else None, a.port, open_browser=not a.no_browser)
        return 0
    if a.cmd == "still":
        models = ("nbp", "flux3") if a.model == "both" else (a.model,)
        how = "run" if a.run else "fake" if a.fake else "dry"
        try:
            d, r = still.make(a.mode, a.video, a.image, target=a.target, extra=a.extra, change=a.change,
                              others=a.others, at=a.at, models=models,
                              how=how, api_key=engine.read_key(a.key_file) if a.run else None, root=a.stills, seed=a.seed)
        except engine.Refused as exc:
            print(f"refused: {exc}\nNothing was sent.")
            return 2
        print(f"\nSTILL {how.upper()}  {a.mode}  ->  {d}")
        print(f"  frame     {r['frame']['at']} s  {r['frame']['size']}")
        print(f"  prompt    {r['prompt']}")
        for m, res in r["results"].items():
            out = res.get("output", {}).get("path", "")
            print(f"  {m:9} {res['outcome']}  {out}  credits {res.get('credits_used', '-')}")
            if res.get("error"):
                print(f"            {res['error']['type']}: {res['error']['message'][:200]}")
        print(f"  next      python -m kagami approve {d} --pick {models[0]}")
        return 0
    if a.cmd == "approve":
        try:
            ok = still.approve(a.folder, a.pick)
        except engine.Refused as exc:
            print(f"refused: {exc}")
            return 2
        print(f"approved {ok['pick']}: {ok['path']} (sha256 {ok['sha256'][:12]})")
        print(f"next: python -m kagami copy|swap ... --still {a.folder}")
        return 0
    if a.cmd == "finish":
        try:
            done = finish.finish(a.folder, sync_it=not a.no_sync)
        except Exception as exc:  # noqa: BLE001
            print(f"could not finish: {exc}")
            return 1
        print_finishes(done)
        return 0
    if a.cmd == "comfy":
        try:
            done = workflow.export(a.folder, a.comfy_input)
        except (ValueError, OSError) as exc:
            print(f"could not export: {exc}")
            return 1
        print_comfy(done)
        return 0
    if a.cmd == "price":
        try:
            rec = prices.fill(a.folder)
        except (ValueError, OSError) as exc:
            print(f"no price: {exc}")
            return 1
        src = rec["price_source"]
        extra = f", + {src['extra_images']} extra images" if src["extra_images"] else ""
        said = prices.describe(rec) if prices.reported(rec) else "not reported"
        print(f"  reported  {said} (Router's numbers, untouched)")
        print(f"  computed  {rec['credits_computed']} credits, ${rec['usd_computed']} "
              f"({src['seconds_billed']:g} s x {src['rate']} {src['rate_unit']}{extra})")
        print(f"            seconds from {src['seconds_from']}")
        print(f"  price     {prices.describe(rec, approx())}")
        return 0

    try:
        if a.cmd == "replay":
            key = engine.read_key(a.key_file)
            if not key:
                raise engine.Refused("replay needs a key: --key-file or COMFY_API_KEY")
            d, rec = engine.replay(a.folder, key, wait_minutes=a.wait)
        else:
            i = engine.Inputs(mode=a.cmd, video=a.video, images=a.image, target=a.target, prompt=a.prompt,
                              extra=a.extra, change=a.change, others=a.others, resolution=a.resolution,
                              duration=a.duration, ratio=a.ratio, seed=a.seed, fit=a.fit, still=a.still)
            how = "run" if a.run else "fake" if a.fake else "dry"
            if how == "run":
                print("  sent; H3 can take many minutes. Kagami keeps collecting under one key (Ctrl+C is safe: replay later).")
            d, rec = engine.run(i, how, a.runs, api_key=engine.read_key(a.key_file) if a.run else None, wait_minutes=a.wait)
    except engine.Refused as exc:
        print(f"refused: {exc}\nNothing was sent.")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"failed: {type(exc).__name__}: {exc}\nThe receipt in the run folder has the details and the key for --replay.")
        return 1

    print(f"\n{rec['how'].upper()}  {rec['mode']}  ->  {d}")
    for f in rec["inputs"]:
        if f["role"] == "reference_video":
            extra = f" {f['seconds']} s {f['size']} {f['fps']} fps"
        else:
            extra = f" {f['size']}" + (f" ({f['what']})" if f.get("what") else "")
            if f.get("cast"):
                extra += f"  {f['cast']['role']}" + (f": {f['cast']['who']}" if f["cast"]["who"] else "")
            if f.get("send_copy"):
                extra += f"  -> sent as a copy {f['send_copy']['size']} ({f['send_copy']['how']})"
        print(f"  {f['label']:8} {f['name']}  {f['bytes']:,} bytes  sha256 {f['sha256'][:12]}{extra}")
    print(f"  settings  {json.dumps(rec['settings'])}   request {rec['request_bytes'] / 2**20:.1f} MiB of 100")
    print(f"  prompt    {rec['prompt']}")
    print(f"  outcome   {rec['outcome']}")
    if "credits_used" in rec:
        print(f"  price     {prices.describe(rec, approx())}")
    if rec.get("output"):
        print(f"  output    {rec['output']['path']}")
        try:
            print_finishes(finish.finish(d))
        except Exception as exc:  # noqa: BLE001 - the video is kept either way
            print(f"  finishing failed ({exc}); the video is kept. Try: python -m kagami finish {d}")
    if rec.get("prompt"):
        try:
            print_comfy(workflow.export(d))
        except Exception as exc:  # noqa: BLE001 - the run is kept either way
            print(f"  comfyui   not exported ({exc}). Try: python -m kagami comfy {d}")
    print(f"  receipt   {d / 'receipt.json'}")
    return 0


def print_comfy(done: dict) -> None:
    print(f"  comfyui   {done['workflow']['path']}")
    print(f"            {done['zip']['path']} (graph + the files it loads; drop input/ into ComfyUI's input folder)")
    if done.get("copied_to"):
        print(f"            copied to {done['copied_to']['folder']}: {', '.join(done['copied_to']['files']) or 'already there'}")


def print_finishes(done: dict) -> None:
    names = {"synced": "synced   ", "with_audio": "+ sound  ", "side": "side     ", "split": "split    "}
    for k, v in done.items():
        print(f"  {names.get(k, k)} {v.get('path') or v.get('skipped')}")
        if k == "synced":
            w = v["warp"]
            print(f"            {w['reads_as']}; corrected ({w['method']}, match {w['correlation']})")


if __name__ == "__main__":
    sys.exit(main())
