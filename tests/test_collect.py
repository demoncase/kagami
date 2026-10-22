import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx

from kagami import engine, media
from kagami.engine import Inputs, Ref


class Collect(unittest.TestCase):
    """The paid path's waiting, against a fake Router and a fake clock: nothing is sent anywhere."""

    def run_with(self, replies, wait_minutes=45):
        """replies: per request, ("timeout",) waits out its timeout, ("busy",) is a 409 after 1 s, ("done",) answers."""
        clock = {"now": 1000.0}
        calls, sleeps = [], []

        def send(client, model, body, key, timeout):
            calls.append({"timeout": timeout, "key": key, "at": clock["now"] - 1000})
            kind = replies[len(calls) - 1] if len(calls) <= len(replies) else "timeout"
            if kind == "timeout":
                clock["now"] += timeout  # as every real first request has done
                raise httpx.ReadTimeout("The read operation timed out")
            clock["now"] += 1
            if kind == "busy":
                raise type("Busy", (Exception,), {"http_status": 409})()
            out = {"task": {"id": "t1", "status": "failed", "error": "fake: stops before any download"}}
            return types.SimpleNamespace(output=out, request_id="r1", replayed=True, credits_used=None), {}

        def sleep(s):
            sleeps.append(s)
            clock["now"] += s

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            ff = media.ffmpeg_exe()
            subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24", "-t", "3",
                            "-pix_fmt", "yuv420p", str(t / "clip.mp4")], check=True)
            subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:size=512x512", "-frames:v", "1",
                            str(t / "a.png")], check=True)
            i = Inputs(mode="copy", video=t / "clip.mp4", images=[Ref(t / "a.png")])
            with mock.patch.object(engine, "send", send), \
                    mock.patch.object(engine.time, "monotonic", lambda: clock["now"]), \
                    mock.patch.object(engine.time, "sleep", sleep), mock.patch("builtins.print"):
                try:
                    _, r = engine.run(i, "run", t / "runs", api_key="not-a-key", wait_minutes=wait_minutes)
                except httpx.ReadTimeout:
                    r = None
        return calls, sleeps, r

    def test_short_first_wait_then_a_check_every_90_s(self):
        calls, _, r = self.run_with(["timeout"] * 6 + ["done"])
        self.assertEqual([c["timeout"] for c in calls], [120, 60, 60, 60, 60, 60, 60])
        self.assertEqual(len({c["key"] for c in calls}), 1)  # every request collects under the one key
        self.assertEqual([c["at"] for c in calls], [0, 135, 225, 315, 405, 495, 585])  # 120 s + 15 s, then every 90 s
        self.assertEqual(r["seconds_waited"], 586)  # the check after it was ready, not a 25 min wait
        self.assertEqual(len(r["attempts"]), 6)
        self.assertEqual(r["waits"]["check_every"], 90)

    def test_a_quick_409_still_waits_out_the_interval(self):
        calls, sleeps, _ = self.run_with(["busy", "busy", "done"])
        self.assertEqual(sleeps, [89, 89])  # a request that fails at once does not re-send the body every 15 s

    def test_gives_up_after_the_wait(self):
        calls, _, r = self.run_with([], wait_minutes=5)
        self.assertIsNone(r)  # the timeout is raised; the receipt keeps the key for `replay`
        self.assertLess(calls[-2]["at"] + calls[-2]["timeout"], 5 * 60)  # the last check began before the deadline...
        self.assertLess(calls[-1]["at"], 5 * 60 + engine.CHECK_EVERY)  # ...so one more runs, no further


if __name__ == "__main__":
    unittest.main()
