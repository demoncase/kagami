import contextlib
import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx

from kagami import engine, media
from kagami.engine import Inputs, Ref, Refused


class Folders(unittest.TestCase):
    """Run folders made dry or fake from a scratch clip; nothing here is ever sent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = Path(self.tmp.name)
        ff = media.ffmpeg_exe()
        subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24", "-t", "3",
                        "-pix_fmt", "yuv420p", str(t / "clip.mp4")], check=True)
        subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:size=512x512", "-frames:v", "1",
                        str(t / "a.png")], check=True)
        self.i = Inputs(mode="copy", video=t / "clip.mp4", images=[Ref(t / "a.png")])

    def tearDown(self):
        self.tmp.cleanup()

    def folder(self, made="dry", **receipt):
        """A run folder made dry or fake (never sent), its receipt then changed as a test needs."""
        assert made in ("dry", "fake")
        d, r = engine.run(self.i, made, self.t / "runs")
        r.update(receipt)
        engine.save(d, r)
        return d

    def lost(self, **receipt):
        """A real send whose answer never came back: what the receipt says right after sending."""
        return self.folder("dry", **{"how": "run", "outcome": "sent, no answer recorded yet (if this stays, use --replay "
                                                               "with this folder)", **receipt})

    def replay(self, d):
        with mock.patch.object(engine, "run", return_value=(d, {})) as run, mock.patch("builtins.print"):
            engine.replay(d, "not-a-key")
        return run


class Replay(Folders):
    """replay() on folders made here; the send itself is never reached (engine.run is stubbed)."""

    def test_a_dry_folder_is_refused(self):
        with self.assertRaisesRegex(Refused, "dry run"):
            self.replay(self.folder("dry"))

    def test_a_fake_folder_is_refused(self):
        d = self.folder("fake")
        before = (d / "output.mp4").read_bytes()
        with self.assertRaisesRegex(Refused, "fake run"):
            self.replay(d)
        self.assertEqual((d / "output.mp4").read_bytes(), before)

    def test_a_made_run_is_refused(self):
        d = self.folder("fake", how="run", outcome="made")  # a run with its video, finishes and edits
        with self.assertRaisesRegex(Refused, "already has its video"):
            self.replay(d)

    def test_an_answered_run_with_nothing_made_is_refused(self):
        with self.assertRaisesRegex(Refused, "was answered"):
            self.replay(self.lost(outcome="nothing_made", task={"status": "failed", "error": "moderation"}))

    def test_a_lost_answer_is_collected_under_its_own_key(self):
        d = self.lost()
        key = json.loads((d / "receipt.json").read_text(encoding="utf-8"))["idempotency_key"]
        run = self.replay(d)
        self.assertEqual(run.call_args.kwargs["key"], key)
        self.assertEqual(run.call_args.args[1], "run")

    def test_a_reworded_prompt_is_refused(self):
        d = self.lost()  # sent by this Kagami; then an update rewords the prompt
        with mock.patch.object(engine, "COPY_PROMPT", engine.COPY_PROMPT + " Keep it sharp."):
            with self.assertRaisesRegex(Refused, "the prompt is not the same"):
                self.replay(d)

    def test_other_settings_or_files_are_refused(self):
        def changed(edit):
            d = self.lost()
            r = json.loads((d / "receipt.json").read_text(encoding="utf-8"))
            edit(r["request"])
            engine.save(d, r)
            return d

        with self.assertRaisesRegex(Refused, "duration"):
            self.replay(changed(lambda q: q.update(duration=q["duration"] + 1)))
        with self.assertRaisesRegex(Refused, "reference_image"):  # e.g. a send copy rebuilt with other bytes
            self.replay(changed(lambda q: q["content"][2]["image_url"].update(url="<data URI of a_fit.png, ...>")))
        with self.assertRaisesRegex(Refused, "no request"):
            self.replay(changed(lambda q: q.clear()))


class LostDownload(Folders):
    """Router answers and bills, then the download fails: a fake Router and a fake CDN, nothing is sent anywhere."""

    def answer(self, replayed=False, credits=272):
        out = {"task": {"id": "t1", "status": "succeeded", "content": {"url": "https://cdn.example/out.mp4"}}}
        res = types.SimpleNamespace(output=out, request_id="req-1", replayed=replayed, credits_used=credits)
        return lambda *a, **k: (res, {} if replayed else {"X-Litellm-Response-Cost": "1.29"})

    @staticmethod
    def cdn(fail_with=None, data=b""):
        @contextlib.contextmanager
        def stream(*a, **k):
            if fail_with:
                raise fail_with
            yield types.SimpleNamespace(raise_for_status=lambda: None, iter_bytes=lambda: iter([data]))
        return stream

    def send(self, answer, cdn, run=None):
        with mock.patch.object(engine, "send", answer), mock.patch.object(httpx, "stream", cdn), \
                mock.patch.dict("sys.modules", {"comfy_sdk": types.SimpleNamespace(Comfy=lambda **k: None)}), \
                mock.patch("builtins.print"):
            return run() if run else engine.run(self.i, "run", self.t / "runs", api_key="not-a-key")

    def test_the_answer_is_kept_when_the_download_fails(self):
        with self.assertRaises(httpx.ConnectError):
            self.send(self.answer(), self.cdn(httpx.ConnectError("CDN dropped")))
        d = next((self.t / "runs").glob("2*"))
        r = json.loads((d / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual((r["request_id"], r["credits_used"], r["usd_reported"]), ("req-1", 272, "1.29"))
        self.assertEqual(r["task"]["status"], "succeeded")
        self.assertIn("could not be downloaded", r["outcome"])
        self.assertEqual([p.name for p in d.iterdir() if "output" in p.name], [])  # no half file
        engine.replayable(r, d)  # a lost answer: replay may collect it

        # The replay: Router hands back the same answer with no price; the first one's numbers stay.
        clip = (self.t / "clip.mp4").read_bytes()
        _, r2 = self.send(self.answer(replayed=True, credits=None), self.cdn(data=clip),
                          run=lambda: engine.replay(d, "not-a-key"))
        self.assertEqual((r2["outcome"], r2["credits_used"], r2["usd_reported"]), ("made", 272, "1.29"))
        self.assertIn("first answer", r2["price_note"])
        self.assertEqual((d / "output.mp4").read_bytes(), clip)


if __name__ == "__main__":
    unittest.main()
