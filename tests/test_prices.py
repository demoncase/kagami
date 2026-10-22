import json
import tempfile
import unittest
from pathlib import Path

from kagami import prices


def receipt(resolution="768P", duration=5, video_seconds=5.0, images=2, usage=None, credits="not reported",
            usd="not reported", how="run"):
    inputs = [{"role": "reference_video", "label": "Video 1", "seconds": video_seconds}]
    inputs += [{"role": "reference_image", "label": f"Image {n}"} for n in range(1, images + 1)]
    task = {"status": "succeeded", "usage": usage} if usage is not None else {"status": "succeeded"}
    return {"how": how, "model": "minimax/minimax-h3", "outcome": "made", "inputs": inputs, "task": task,
            "settings": {"duration": duration, "ratio": "16:9", "resolution": resolution},
            "credits_used": credits, "usd_reported": usd}


class Compute(unittest.TestCase):
    def test_768p_from_usage(self):
        p = prices.compute(receipt(usage={"total_seconds": 10, "output_seconds": 5, "input_seconds": 5}))
        self.assertEqual(p["credits_computed"], 271.56)  # 10 s x 27.1557
        self.assertEqual(p["usd_computed"], 1.287)  # / 211
        self.assertEqual(p["price_source"]["seconds_billed"], 10)
        self.assertIn("usage.total_seconds", p["price_source"]["seconds_from"])
        self.assertEqual(p["price_source"]["date"], "2026-10-22")
        self.assertEqual(p["price_source"]["url"], "https://docs.comfy.org/development/comfy-router/pricing")

    def test_2k_without_usage(self):
        p = prices.compute(receipt(resolution="2K", duration=6, video_seconds=5.5))
        self.assertEqual(p["price_source"]["seconds_billed"], 11.5)  # 6 s asked + 5.5 s of reference video
        self.assertEqual(p["credits_computed"], round(11.5 * 39.2249, 2))
        self.assertIn("requested duration", p["price_source"]["seconds_from"])

    def test_usage_wins_over_the_request(self):
        with_usage = prices.compute(receipt(duration=5, video_seconds=5.0, usage={"total_seconds": 12}))
        without = prices.compute(receipt(duration=5, video_seconds=5.0))
        self.assertEqual(with_usage["price_source"]["seconds_billed"], 12)
        self.assertEqual(without["price_source"]["seconds_billed"], 10)

    def test_images_past_five(self):
        five = prices.compute(receipt(images=5, usage={"total_seconds": 10}))
        seven = prices.compute(receipt(images=7, usage={"total_seconds": 10}))
        self.assertEqual(five["price_source"]["extra_images"], 0)
        self.assertEqual(seven["price_source"]["extra_images"], 2)
        self.assertAlmostEqual(seven["credits_computed"] - five["credits_computed"], 2 * 12.0692, places=2)

    def test_unknown_resolution_refused(self):
        with self.assertRaises(ValueError):
            prices.compute(receipt(resolution="4K"))


class Reported(unittest.TestCase):
    def test_describe_prefers_reported(self):
        r = receipt(credits=300, usd="1.42", usage={"total_seconds": 10})
        r.update(prices.compute(r))
        self.assertEqual(prices.describe(r), "300 credits · $1.420 (reported by Router)")

    def test_describe_computed(self):
        r = receipt(usage={"total_seconds": 10})
        r.update(prices.compute(r))
        self.assertEqual(prices.describe(r),
                         "≈ 272 credits ($1.29), computed from Comfy's published rate (2026-10-22)")

    def test_fill_never_overwrites_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "receipt.json").write_text(json.dumps(receipt(credits=300, usd="1.42", usage={"total_seconds": 10})))
            r = prices.fill(d)
            saved = json.loads((d / "receipt.json").read_text(encoding="utf-8"))
            for x in (r, saved):
                self.assertEqual(x["credits_used"], 300)
                self.assertEqual(x["usd_reported"], "1.42")
                self.assertEqual(x["credits_computed"], 271.56)

    def test_fill_refuses_unsent_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "receipt.json").write_text(json.dumps(receipt(how="fake")))
            with self.assertRaises(ValueError):
                prices.fill(d)


if __name__ == "__main__":
    unittest.main()
