import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from kagami import media, server


class App(unittest.TestCase):
    """The app in FAKE mode against a scratch folder: nothing is sent anywhere."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.patches = [mock.patch.object(server, k, v) for k, v in {
            "ROOT": root, "WORK": root / "work", "RUNS": root / "runs", "STILLS": root / "stills",
            "SERVED": (root / "work", root / "runs", root / "stills")}.items()]
        for p in cls.patches:
            p.start()
        for d in ("work", "runs", "stills", "elsewhere"):
            (root / d).mkdir()
        ff = media.ffmpeg_exe()
        subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24", "-t", "3",
                        "-pix_fmt", "yuv420p", str(root / "work" / "clip.mp4")], check=True)
        subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:size=512x512", "-frames:v", "1",
                        str(root / "work" / "a.png")], check=True)
        subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:size=512x512", "-frames:v", "1",
                        str(root / "elsewhere" / "secret.png")], check=True)
        cls.root = root
        server.State.how, server.State.key = "fake", None
        cls.c = TestClient(server.create_app(8777), base_url="http://127.0.0.1:8777")
        cls.t = {"x-kagami-token": server.State.token}

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def body(self, **kw):
        return {"mode": "copy", "video": "work/clip.mp4", "images": [{"path": "work/a.png"}], "fit": True, **kw}

    def wait(self, jid):
        for _ in range(600):
            j = self.c.get(f"/api/job/{jid}", headers=self.t).json()
            if j["state"] != "running":
                return j
            time.sleep(0.1)
        self.fail("the job never ended")

    def test_the_page_carries_the_token(self):
        page = self.c.get("/").text
        self.assertIn(server.State.token, page)
        self.assertNotIn("__KAGAMI_TOKEN__", page)

    def test_another_host_is_refused(self):
        r = self.c.get("/", headers={"host": "evil.example:8777"})  # a rebound DNS name
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.c.get("/", headers={"host": "127.0.0.1:9999"}).status_code, 403)

    def test_another_origin_is_refused(self):
        r = self.c.post("/api/run", json=self.body(), headers={**self.t, "origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.c.get("/", headers={"origin": "null"}).status_code, 403)

    def test_no_token_is_refused(self):
        self.assertEqual(self.c.get("/api/config").status_code, 403)
        self.assertEqual(self.c.get("/api/config", headers={"x-kagami-token": "guess"}).status_code, 403)
        self.assertEqual(self.c.post("/api/run", json=self.body()).status_code, 403)
        self.assertEqual(self.c.post("/api/upload?name=a.png", content=b"x").status_code, 403)
        self.assertEqual(self.c.get("/api/config", headers=self.t).status_code, 200)
        self.assertEqual(self.c.get(f"/api/config?token={server.State.token}").status_code, 200)  # an <img> GET
        self.assertEqual(self.c.post(f"/api/run?token={server.State.token}", json=self.body()).status_code, 403)  # a POST must use the header

    def test_json_only(self):
        # text/plain is what a cross-site form or no-cors fetch can send without a preflight
        r = self.c.post("/api/run", content='{"mode": "copy"}', headers={**self.t, "content-type": "text/plain"})
        self.assertEqual(r.status_code, 415)
        r = self.c.post("/api/still", content="{}", headers=self.t)
        self.assertEqual(r.status_code, 415)

    def test_paths_stay_inside_the_app(self):
        outside = str(self.root / "elsewhere" / "secret.png")
        for b in (self.body(video=str(self.root / "elsewhere" / "x.mp4")),
                  self.body(images=[{"path": outside}]),
                  self.body(images=[{"path": "work/../elsewhere/secret.png"}]),
                  self.body(still="elsewhere")):
            with self.subTest(b=b):
                r = self.c.post("/api/run", json=b, headers=self.t)
                self.assertEqual(r.status_code, 400, r.text)
        r = self.c.post("/api/approve", json={"folder": "elsewhere", "pick": "nbp"}, headers=self.t)
        self.assertEqual(r.status_code, 400)

    def test_one_paid_job_at_a_time(self):
        self.assertTrue(server.State.lock.acquire(blocking=False))  # as if a still or video job were running
        try:
            for route in ("/api/run", "/api/still"):
                r = self.c.post(route, json=self.body(), headers=self.t)
                self.assertEqual(r.status_code, 409, r.text)
        finally:
            server.State.lock.release()

    def test_a_fake_run_end_to_end(self):
        with mock.patch("builtins.print"):
            r = self.c.post("/api/run", json=self.body(), headers=self.t)
            self.assertEqual(r.status_code, 200, r.text)
            jid = r.json()["job"]
            again = self.c.post("/api/run", json=self.body(), headers=self.t)  # a second click while it runs
            j = self.wait(jid)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(j["state"], "done", j["error"])
        self.assertTrue(j["result"]["outcome"].startswith("simulated"))
        self.assertTrue(server.State.lock.acquire(blocking=False))  # let go when the job ended
        server.State.lock.release()


if __name__ == "__main__":
    unittest.main()
