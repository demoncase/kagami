"""Kagami probe 3: is Comfy Router's RUN route open for this key? Free by construction.

Sends minimax/minimax-h3 a body that is invalid TWICE over (duration 3, below
Router's minimum of 4, and an empty prompt). Router refuses an invalid body
before the provider is called, so nothing can be generated or charged.

  HTTP 400 / invalid_input  -> the gate is open: Router validated our body.
  HTTP 403 / not_enabled    -> Router is still switched off for this account.

Usage (PowerShell, kagami venv active):
  python tools/probe_gate.py --key-file E:\\keys\\comfy-router.txt
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

import httpx

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--key-file", required=True)
key = Path(ap.parse_args().key_file).read_text(encoding="utf-8").strip()

body = {"content": [{"type": "text", "text": ""}], "duration": 3, "ratio": "9:16", "resolution": "768P"}
r = httpx.post(
    "https://api.comfy.org/v2/models/minimax/minimax-h3",
    json=body,
    headers={"Authorization": f"Bearer {key}", "Idempotency-Key": str(uuid.uuid4())},
    timeout=60,
)
Path("probe_out").mkdir(exist_ok=True)
Path("probe_out/gate.json").write_text(
    json.dumps({"status": r.status_code, "headers": dict(r.headers), "body": r.text}, indent=2), encoding="utf-8"
)
print(f"HTTP {r.status_code}: {r.text[:500]}")
print("credits header:", r.headers.get("X-Comfy-Credits-Used", "not reported"))
