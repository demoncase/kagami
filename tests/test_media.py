import subprocess
import tempfile
import unittest
from pathlib import Path

from kagami import media


class FitSize(unittest.TestCase):
    """A send copy is padded, never stretched, and lands inside H3's limits after the rounding."""

    def check(self, w, h):
        sw, sh, tw, th = media.fit_size(w, h)
        self.assertEqual(media.image_problem(tw, th), "", (w, h, tw, th))
        self.assertEqual((tw % 2, th % 2), (0, 0))
        self.assertTrue(sw <= tw and sh <= th)  # the canvas holds the whole image: padded, not cropped
        self.assertTrue(sw <= w and sh <= h)  # scaled down only
        s = min(1.0, media.IMAGE_MAX / max(w, h))  # one scale for both sides, each within half a pixel: not stretched
        for got, exact in ((sw, w * s), (sh, h * s)):
            self.assertTrue(abs(got - exact) <= 0.5 or (exact < 1 and got == 1), (w, h, sw, sh))
        return sw, sh, tw, th

    def test_a_small_face_is_padded_not_stretched(self):
        self.assertEqual(self.check(100, 240), (100, 240, 256, 256))
        self.assertEqual(self.check(200, 450), (200, 450, 256, 450))

    def test_the_aspect_holds_after_rounding(self):
        self.assertEqual(self.check(100, 1002), (100, 1002, 402, 1002))  # 401 floored to 400 was 0.399
        self.assertEqual(self.check(1002, 100), (1002, 100, 1002, 402))  # and 2.505 the other way

    def test_too_big_is_scaled_down_as_a_whole(self):
        self.assertEqual(self.check(7000, 200), (5760, 165, 5760, 2304))
        self.assertEqual(self.check(6000, 6000), (5760, 5760, 5760, 5760))

    def test_many_sizes(self):
        for w in (1, 7, 99, 255, 256, 257, 401, 999, 1001, 2049, 5759, 5761, 9000, 20000):
            for h in (1, 3, 100, 255, 256, 641, 1003, 2303, 5760, 6001, 15000):
                with self.subTest(w=w, h=h):
                    self.check(w, h)


class FitCopy(unittest.TestCase):
    """Real copies made by ffmpeg from PNG and JPEG (4:2:0) inputs."""

    def test_copies(self):
        ff = media.ffmpeg_exe()
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            for w, h, ext in ((100, 240, "png"), (101, 1003, "jpg"), (1002, 100, "png"), (7000, 200, "jpg")):
                with self.subTest(size=(w, h, ext)):
                    src = t / f"in_{w}x{h}.{ext}"
                    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}", "-frames:v", "1",
                                    str(src)], check=True)
                    out, info = media.fit_copy(src, t / "copies")
                    tw, th = media.image_size(out)
                    self.assertEqual(media.image_problem(tw, th), "")
                    self.assertEqual(f"{tw}x{th}", info["size"])
                    sw, sh = map(int, info["scaled_to"].split("x"))
                    self.assertAlmostEqual(sw / sh, w / h, delta=(w / h) * 0.01 + 1 / sh)


PHONE = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'IMG_0001.MOV':
  Duration: 00:01:07.43, start: 0.000000, bitrate: 15862 kb/s
  Stream #0:0[0x1](und): Video: hevc (Main) (hvc1 / 0x31637668), yuv420p(tv, bt709), 1920x1080, 15705 kb/s, 29.98 fps, 30 tbr, 600 tbn (default)
      Metadata:
        handler_name    : Core Media Video
      Side data:
        displaymatrix: rotation of -90.00 degrees
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 157 kb/s (default)
"""
OLD_FFMPEG = """  Duration: 00:00:05.00, start: 0.000000, bitrate: 900 kb/s
    Stream #0:0(und): Video: h264 (High) (avc1 / 0x31637661), yuv420p, 1280x720, 800 kb/s, 30 fps, 30 tbr, 90k tbn (default)
    Metadata:
      rotate          : 270
      handler_name    : VideoHandler
"""


def size(f):
    return f.width, f.height


class Rotation(unittest.TestCase):
    """A portrait phone clip is stored landscape with a rotation: its facts give the size shown."""

    def test_displaymatrix(self):
        f = media.read_facts(PHONE)
        self.assertEqual((f.width, f.height, f.seconds, f.fps), (1080, 1920, 67.43, 29.98))  # minutes kept too
        self.assertEqual(media.nearest_ratio(f.width, f.height), "9:16")

    def test_rotate_tag(self):
        self.assertEqual(size(media.read_facts(OLD_FFMPEG)), (720, 1280))

    def test_a_half_turn_or_none_keeps_the_size(self):
        self.assertEqual(size(media.read_facts(PHONE.replace("-90.00", "180.00"))), (1920, 1080))
        self.assertEqual(size(media.read_facts(PHONE.replace("displaymatrix: rotation of -90.00 degrees", ""))), (1920, 1080))

    def test_another_streams_rotation_is_not_read(self):
        two = PHONE.replace("      Side data:\n        displaymatrix: rotation of -90.00 degrees\n", "") + \
            "      Side data:\n        displaymatrix: rotation of 90.00 degrees\n"  # under the audio stream
        self.assertEqual(size(media.read_facts(two)), (1920, 1080))

    def test_a_real_rotated_clip(self):
        ff = media.ffmpeg_exe()
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24", "-t", "2.5",
                            "-pix_fmt", "yuv420p", str(t / "land.mp4")], check=True)
            subprocess.run([ff, "-v", "error", "-display_rotation:v:0", "90", "-i", str(t / "land.mp4"), "-c", "copy",
                            str(t / "portrait.mp4")], check=True)
            self.assertEqual(size(media.video_facts(t / "land.mp4")), (640, 360))
            self.assertEqual(size(media.video_facts(t / "portrait.mp4")), (360, 640))


if __name__ == "__main__":
    unittest.main()
