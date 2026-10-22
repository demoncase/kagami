"""Kagami probe: what can Comfy Router's H3 models take? Free, never runs a model.

What it does, in order:
  1. Lists the Router catalog and keeps every id that looks like H3 / MiniMax.
  2. Reads each candidate's input schema (openapi.json) and saves it to probe_out/.
  3. Prints, per model: the input kinds it accepts (x-comfy-router-input-modalities),
     the content roles, and the duration / resolution / ratio options.
  4. Asks Router for a price ESTIMATE of one small text-only request (nothing is
     charged; the estimate route may answer "not enabled", which is fine).

It never calls run/submit, so it never spends a credit.

Usage (PowerShell, from the repo root, venv active):
  python tools/probe.py --key-file E:\\keys\\comfy.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from comfy_sdk import Comfy

ROUTER = "https://api.comfy.org"
CANDIDATES = ["minimax/minimax-h3", "fal/h3-max", "fal/h3-max-turbo"]
OUT = Path("probe_out")


def read_key(key_file: str | None) -> str:
    if key_file:
        key = Path(key_file).read_text(encoding="utf-8").strip()
    else:
        key = os.environ.get("COMFY_API_KEY", "").strip()
    if not key:
        sys.exit("No key: pass --key-file <path> or set COMFY_API_KEY. Nothing was sent.")
    if not key.startswith("comfyui-"):
        print("warning: the key does not start with 'comfyui-'; carrying on anyway")
    return key


def find_all(node, name, found=None):
    """Every value stored under key `name`, anywhere in a JSON document."""
    found = [] if found is None else found
    if isinstance(node, dict):
        for k, v in node.items():
            if k == name:
                found.append(v)
            find_all(v, name, found)
    elif isinstance(node, list):
        for v in node:
            find_all(v, name, found)
    return found


def enums_for(doc, field):
    """The enum options of every property called `field`."""
    out = []
    for props in find_all(doc, "properties"):
        if isinstance(props, dict) and field in props and isinstance(props[field], dict):
            spec = props[field]
            opts = spec.get("enum") or [e for s in spec.get("anyOf", []) for e in s.get("enum", [])]
            rng = {k: spec[k] for k in ("minimum", "maximum", "default") if k in spec}
            out.append({"enum": opts, **rng, "description": spec.get("description", "")[:300]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key-file", help="text file holding the comfyui-... key (outside any repo)")
    ap.add_argument("--models", nargs="*", default=[], help="more model ids to read the schema of (e.g. vertexai/gemini-3-pro-image)")
    args = ap.parse_args()
    key = read_key(args.key_file)
    OUT.mkdir(exist_ok=True)
    client = Comfy(api_key=key)

    print("== 1. catalog ==")
    ids = []
    try:
        for m in client.models.list(limit=100):
            ids.append(m.id)
    except Exception as exc:  # noqa: BLE001 - report and carry on
        print(f"catalog read failed: {exc!r}")
    (OUT / "catalog.json").write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")
    h3ish = [i for i in ids if any(t in i.lower() for t in ("h3", "minimax", "hailuo"))]
    print(f"{len(ids)} models in the catalog; H3 / MiniMax ones: {h3ish or 'none found'}")
    models = list(dict.fromkeys(CANDIDATES + h3ish + args.models))

    print("\n== 2 + 3. schemas ==")
    for model in models:
        try:
            doc = client.models.schema(model).document
        except Exception as exc:  # noqa: BLE001
            print(f"\n{model}: schema read failed: {exc!r}")
            continue
        path = OUT / (model.replace("/", "__") + ".openapi.json")
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        print(f"\n{model}  (saved {path})")
        print("  input kinds:", find_all(doc, "x-comfy-router-input-modalities") or "not stated")
        for field in ("role", "type", "duration", "resolution", "ratio", "aspect_ratio",
                      "reference_video_urls", "reference_image_urls", "video_url", "image_url",
                      "image_urls", "input_image", "images", "image", "output_format", "num_images",
                      "image_size", "width", "height", "safety_tolerance", "image_config"):
            for e in enums_for(doc, field):
                print(f"  {field}: {e}")

    print("\n== 4. estimate (free; may be 'not enabled') ==")
    body = {
        "content": [{"type": "text", "text": "A paper boat drifts across a puddle, morning light."}],
        "duration": 5, "ratio": "9:16", "resolution": "768P",
    }
    try:
        r = httpx.post(f"{ROUTER}/v2/models/minimax/minimax-h3/estimate", json=body,
                       headers={"Authorization": f"Bearer {key}", "X-API-Key": key}, timeout=30)
        print(f"minimax/minimax-h3, 5 s text-only 768P: HTTP {r.status_code}")
        print("  ", r.text[:600])
        (OUT / "estimate_minimax-h3.json").write_text(r.text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"estimate failed: {exc!r}")

    print("\nDone. Nothing was run, nothing was charged. Send probe_out/ to Cowork.")


if __name__ == "__main__":
    main()
