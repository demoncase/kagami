import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kagami import engine, media
from kagami.engine import Inputs, Ref, Refused


class Replay(unittest.TestCase):
    """replay() on dry and fake folders made here; the send itself is never reached (engine.run is stubbed)."""

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


if __name__ == "__main__":
    unittest.main()
