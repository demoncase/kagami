"""Kagami probe 2: what do the MiniMax H3 partner nodes in YOUR ComfyUI take? Free.

Reads /system_stats and /object_info from a running ComfyUI (default
http://127.0.0.1:8188), keeps every node whose class name mentions MiniMax /
Hailuo / H3, saves them to probe_out/local_nodes.json and prints each input
(name, type, options, min/max, required or optional). Never queues a prompt,
so it never spends a credit.

Usage (PowerShell, kagami venv active, ComfyUI running):
  python tools/probe_local.py
  python tools/probe_local.py --comfy http://127.0.0.1:8000   (Comfy Desktop's port)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

OUT = Path("probe_out")
WORDS = ("minimax", "hailuo", "h3")


def short(spec) -> str:
    """One input spec from object_info, in a line."""
    if not isinstance(spec, list) or not spec:
        return repr(spec)[:200]
    kind, opts = spec[0], (spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {})
    if isinstance(kind, list):  # an old-style combo: the options are the type
        return f"COMBO {kind}"
    if kind == "COMFY_COMBO_V3" or "options" in opts:
        return f"{kind} options={opts.get('options')} default={opts.get('default')!r}"
    keep = {k: opts[k] for k in ("default", "min", "max", "step", "multiline", "tooltip") if k in opts}
    if "tooltip" in keep:
        keep["tooltip"] = str(keep["tooltip"])[:160]
    return f"{kind} {keep}" if keep else str(kind)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--comfy", default="http://127.0.0.1:8188", help="ComfyUI address")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    try:
        stats = httpx.get(f"{args.comfy}/system_stats", timeout=10).json()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"No ComfyUI at {args.comfy} ({exc!r}). Start it, or pass --comfy. Nothing was sent.")
    system = stats.get("system", {})
    print(f"ComfyUI {system.get('comfyui_version', '?')}, Python {system.get('python_version', '?')[:6]}")

    info = httpx.get(f"{args.comfy}/object_info", timeout=60).json()
    hits = {k: v for k, v in info.items() if any(w in k.lower() for w in WORDS)}
    (OUT / "local_nodes.json").write_text(json.dumps({"system": system, "nodes": hits}, indent=2), encoding="utf-8")
    if not hits:
        print("No MiniMax / H3 node found: update ComfyUI (the H3 nodes landed in 0.29.2, H3 Max on the Reference node in 0.34.3).")
        return
    for name, node in hits.items():
        print(f"\n{name}  ({node.get('display_name', '')})")
        for part in ("required", "optional"):
            for inp, spec in (node.get("input", {}).get(part) or {}).items():
                print(f"  [{part[:3]}] {inp}: {short(spec)}")
        print(f"  outputs: {node.get('output')}")
    print(f"\nSaved {OUT / 'local_nodes.json'}. Nothing was queued, nothing was charged.")


if __name__ == "__main__":
    main()
