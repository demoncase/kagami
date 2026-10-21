"""Kagami from the terminal.

  python -m kagami copy --video clip.mp4 --image me.png                 dry run (default): built, checked, nothing sent
  python -m kagami copy --video clip.mp4 --image me.png --fake          the whole path offline, free
  python -m kagami copy --video clip.mp4 --image me.png --run --key-file E:\\keys\\comfy-router.txt   THE PAID CALL
  python -m kagami swap --video clip.mp4 --image mug.png --target "the white mug on the desk"
  python -m kagami replay runs\\20261021-160000-copy --key-file ...       collect a run whose answer was lost (not billed twice)

An image can say what it is: --image "jacket.png::the jacket he wears" becomes "Image 2 is the jacket he wears."
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import engine


def image_arg(text: str) -> tuple[Path, str]:
    path, _, what = text.partition("::")
    return Path(path), what.strip()


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
        p.add_argument("--resolution", default="768P", choices=["768P", "2K"])
        p.add_argument("--duration", type=int, help="output seconds, 4 to 15 (default: the clip's length)")
        p.add_argument("--ratio", choices=["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"])
        p.add_argument("--seed", type=int)
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
    a = ap.parse_args(argv)

    try:
        if a.cmd == "replay":
            key = engine.read_key(a.key_file)
            if not key:
                raise engine.Refused("replay needs a key: --key-file or COMFY_API_KEY")
            d, rec = engine.replay(a.folder, key, wait_minutes=a.wait)
        else:
            i = engine.Inputs(mode=a.cmd, video=a.video, images=a.image, target=a.target, prompt=a.prompt,
                              extra=a.extra, resolution=a.resolution, duration=a.duration, ratio=a.ratio, seed=a.seed, fit=a.fit)
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
            if f.get("send_copy"):
                extra += f"  -> sent as a copy {f['send_copy']['size']} ({f['send_copy']['how']})"
        print(f"  {f['label']:8} {f['name']}  {f['bytes']:,} bytes  sha256 {f['sha256'][:12]}{extra}")
    print(f"  settings  {json.dumps(rec['settings'])}   request {rec['request_bytes'] / 2**20:.1f} MiB of 100")
    print(f"  prompt    {rec['prompt']}")
    print(f"  outcome   {rec['outcome']}")
    if "credits_used" in rec:
        print(f"  credits   {rec['credits_used']}")
    if rec.get("output"):
        print(f"  output    {rec['output']['path']}")
    print(f"  receipt   {d / 'receipt.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
