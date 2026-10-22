"""A computed price, for the runs Router did not price (a replay never repeats the price).

From Comfy's published rates, kept as data in prices.json (the source URL and the date read are
in the file). The receipt keeps Router's numbers exactly as sent (credits_used, usd_reported, or
"not reported"); the computed ones sit beside them (credits_computed, usd_computed, price_source)
and are always shown as computed, never as reported.

  python -m kagami price runs\\<folder>     fills the computed fields on an older run
"""

from __future__ import annotations

import json
from pathlib import Path

FILE = Path(__file__).with_name("prices.json")
NOT_REPORTED = (None, "not reported", "none (simulated)")


def table() -> dict:
    return json.loads(FILE.read_text(encoding="utf-8"))


def compute(r: dict, t: dict | None = None) -> dict:
    """credits_computed, usd_computed and price_source for a video receipt."""
    t = t or table()
    model = t["models"].get(r.get("model"))
    if not model:
        raise ValueError(f"no published rate for {r.get('model')} in {FILE.name}")
    s = r.get("settings") or {}
    res = s.get("resolution", "768P")
    rate = model["credits_per_second"].get(res)
    if rate is None:
        raise ValueError(f"no published rate for {r.get('model')} at {res} in {FILE.name}")
    usage = (r.get("task") or {}).get("usage") or {}
    if usage.get("total_seconds") is not None:
        seconds = float(usage["total_seconds"])
        source = "the provider's task.usage.total_seconds"
    else:
        videos = [float(f.get("seconds") or 0) for f in r.get("inputs", []) if f.get("role") == "reference_video"]
        seconds = float(s.get("duration", 0)) + sum(videos)
        source = (f"no usage in the reply: the requested duration ({s.get('duration', 0)} s) plus the reference "
                  f"video{'s' if len(videos) != 1 else ''} ({' + '.join(f'{v:g}' for v in videos) or '0'} s)")
    images = sum(1 for f in r.get("inputs", []) if f.get("role") == "reference_image")
    extra = max(0, images - model["images_included"])
    credits = seconds * rate + extra * model["credits_per_extra_image"]
    return {"credits_computed": round(credits, 2), "usd_computed": round(credits / t["credits_per_usd"], 4),
            "price_source": {"file": f"kagami/{FILE.name}", "date": t["read"], "url": t["source"],
                             "rate": rate, "rate_unit": f"credits per second at {res}", "seconds_billed": seconds,
                             "seconds_from": source, "extra_images": extra,
                             "credits_per_extra_image": model["credits_per_extra_image"],
                             "credits_per_usd": t["credits_per_usd"]}}


def fill(folder: Path) -> dict:
    """Add the computed fields to an older run's receipt; Router's own numbers are left as they are."""
    path = folder / "receipt.json"
    r = json.loads(path.read_text(encoding="utf-8"))
    if r.get("how") != "run":
        raise ValueError(f"{folder.name} is a {r.get('how')} run: nothing was sent, so nothing was billed")
    if (r.get("task") or {}).get("status") != "succeeded":
        raise ValueError(f"{folder.name}: H3 made nothing ({r.get('outcome')}), so there is no video to price")
    r.update(compute(r))
    path.write_text(json.dumps(r, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return r


def reported(r: dict) -> bool:
    return r.get("credits_used") not in NOT_REPORTED or r.get("usd_reported") not in NOT_REPORTED


def describe(r: dict, approx: str = "≈") -> str:
    """One line: Router's price when it sent one, else the computed one, said as computed."""
    if reported(r):
        parts = []
        if r.get("credits_used") not in NOT_REPORTED:
            parts.append(f"{r['credits_used']} credits")
        if r.get("usd_reported") not in NOT_REPORTED:
            parts.append(f"${float(r['usd_reported']):.3f}")
        return " · ".join(parts) + " (reported by Router)"
    if r.get("credits_computed") is not None:
        return (f"{approx} {r['credits_computed']:.0f} credits (${r['usd_computed']:.2f}), "
                f"computed from Comfy's published rate ({r['price_source']['date']})")
    if r.get("how") == "fake":
        return "simulated, nothing charged"
    return str(r.get("credits_used") or "not reported")
