import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kagami import engine, media, still
from kagami.engine import Inputs, Ref, Refused

V = Path("clip.mp4")

# The prompts Kagami wrote before the cast map, captured from that code: with no roles set they must not move a byte.
BEFORE = {
    'copy_one': 'Video 1 is the motion reference. Recreate it shot for shot: the same camera movement, the same framing, the same timing, and every movement of the body, beat for beat. Replace the person in Video 1 with the person in Image 1: their face, skin tone, hair, build and clothing exactly as in Image 1.',
    'copy_whats_extra': 'Video 1 is the motion reference. Recreate it shot for shot: the same camera movement, the same framing, the same timing, and every movement of the body, beat for beat. Replace the person in Video 1 with the person in Image 1: their face, skin tone, hair, build and clothing exactly as in Image 1. Image 2 is the jacket he wears. Image 3 is his left side. Keep it dusk.',
    'copy_still0': 'Video 1 is the motion reference. Recreate it shot for shot: the same camera movement, the same framing, the same timing, and every movement of the body, beat for beat. Replace the person in Video 1 with the person in Image 1: their face, skin tone, hair, build and clothing exactly as in Image 1. Image 3 is the jacket he wears. Image 1 is the approved first frame of this shot: from the first frame to the last, the person, their clothing, the place and the light look exactly as in Image 1. The other images show the same person, for reference.',
    'copy_still_later_extra': 'Video 1 is the motion reference. Recreate it shot for shot: the same camera movement, the same framing, the same timing, and every movement of the body, beat for beat. Replace the person in Video 1 with the person in Image 1: their face, skin tone, hair, build and clothing exactly as in Image 1. Image 1 is the approved look of this shot, recast from the frame at 1.3 seconds of Video 1. It is a reference, not the opening frame: the video still begins as Video 1 begins and follows its motion, and whenever the person is in view, they, their clothing, the place and the light look exactly as in Image 1. The other images show the same person, for reference. Keep it dusk.',
    'copy_custom': 'Make it a dance. Image 1 is a dancer. Slow.',
    'swap': 'Keep Video 1 exactly as it is: the same people, place, camera movement, framing, timing and light, frame for frame. Change one thing only: the white mug on the desk becomes the one in Image 1, matching Image 1 exactly in shape, colour, material and markings. Everything else stays as it is in Video 1.',
    'swap_still_what': 'Keep Video 1 exactly as it is: the same people, place, camera movement, framing, timing and light, frame for frame. Change one thing only: the white mug on the desk becomes the one in Image 2, matching Image 2 exactly in shape, colour, material and markings. Everything else stays as it is in Video 1. Image 2 is a red mug. Image 1 is the approved look of this shot, recast from the frame at 2.0 seconds of Video 1. It is a reference, not the opening frame: the video still begins as Video 1 begins and follows its motion, and whenever the person is in view, they, their clothing, the place and the light look exactly as in Image 1.',
    'still_copy': 'Image 1 is one frame from a video. Recreate this exact frame as a photograph: the same camera angle, framing, lens, pose and body position, the same place, light and composition. Replace the person in Image 1 with the person in Image 2: their face, skin tone, hair, build and clothing exactly as in Image 2. Everything else stays as it is in Image 1. Image 3 is the jacket he wears.',
    'still_swap_extra': 'Image 1 is one frame from a video. Keep it exactly as it is, as a photograph: the same people, place, camera, framing and light. Change one thing only: the white mug becomes the one in Image 2, matching Image 2 exactly in shape, colour, material and markings. Everything else stays as it is in Image 1. Keep it dusk.',
}


def h3(mode="copy", images=(), still_at=None, **kw):
    return engine.write_prompt(Inputs(mode=mode, video=V, images=list(images), **kw), still_at)


def parts(mode="copy", images=(), still_at=None, **kw):
    return engine.write_parts(Inputs(mode=mode, video=V, images=list(images), **kw), still_at)


A, B, C, D = (Ref(Path(f"{x}.png")) for x in "abcd")


class NoRolesIsToday(unittest.TestCase):
    def test_byte_for_byte(self):
        now = {
            "copy_one": h3(images=[A]),
            "copy_whats_extra": h3(images=[A, Ref(Path("b.png"), "the jacket he wears"), Ref(Path("c.png"), "his left side")],
                                   extra="Keep it dusk."),
            "copy_still0": h3(images=[A, Ref(Path("b.png"), "the jacket he wears")], still_at=0.0),
            "copy_still_later_extra": h3(images=[A], extra="Keep it dusk.", still_at=1.3026),
            "copy_custom": h3(images=[Ref(Path("a.png"), "a dancer")], prompt="Make it a dance.", extra="Slow."),
            "swap": h3("swap", [A], target="the white mug on the desk"),
            "swap_still_what": h3("swap", [Ref(Path("m.png"), "a red mug")], target="the white mug on the desk", still_at=2.0),
            "still_copy": still.prompt_for("copy", "", [A, Ref(Path("b.png"), "the jacket he wears")], ""),
            "still_swap_extra": still.prompt_for("swap", "the white mug", [A], "Keep it dusk."),
        }
        for k, text in BEFORE.items():
            with self.subTest(k):
                self.assertEqual(now[k], text)

    def test_plain_pairs_still_work(self):
        # Older callers and receipts pass (path, what) pairs.
        r = engine.cast_record(Inputs(mode="copy", video=V, images=[(Path("a.png"), "")]), [])
        self.assertEqual(r["cast"], [{"image": "@image1", "name": "a.png", "role": None, "who": None}])
        self.assertIsNone(r["extras"])


class Roles(unittest.TestCase):
    def test_person(self):
        text = h3(images=[A._replace(role="person", who="the man in the orange suit")])
        self.assertEqual(text, engine.MOTION + " Replace the man in the orange suit in Video 1 with the person in Image 1: "
                               "their face, skin tone, hair, build and clothing exactly as in Image 1. "
                               "Everyone else in Video 1 stays exactly as they are.")

    def test_person_without_who(self):
        self.assertIn("Replace the person in Video 1 with the person in Image 1:", h3(images=[A._replace(role="person")]))

    def test_place(self):
        text = h3(images=[A._replace(role="place")])
        self.assertEqual(text, engine.MOTION + " The place in Image 1 replaces the setting of Video 1: the shot is set "
                               "there, with Video 1's camera, ground plane and where everyone stands kept as they are.")
        self.assertNotIn("Everyone else", text)  # no person mapped, no extras sentence

    def test_object(self):
        self.assertTrue(h3(images=[A._replace(role="object", who="the red ball")]).endswith(
            "Replace the red ball in Video 1 with the object in Image 1, matching Image 1 exactly in shape, colour, "
            "material and markings."))

    def test_look(self):
        self.assertTrue(h3(images=[A._replace(role="look")]).endswith(
            "Image 1 sets the look only: match its style, colour and light, and take nothing else from it."))

    def test_two_people_a_place_and_an_object(self):
        cast = [A._replace(role="person", who="the man in the orange suit"),
                B._replace(role="person", who="the woman on the left"),
                C._replace(role="place"), D._replace(role="object", who="the red ball")]
        p = parts(images=cast, others="restyle", change="Make it night, and @image1 waves at @Image2.")
        self.assertEqual([x["part"] for x in p], ["motion", "image 1", "image 2", "image 3", "image 4", "extras", "change"])
        self.assertEqual(" ".join(x["text"] for x in p), (
            engine.MOTION + " Replace the man in the orange suit in Video 1 with the person in Image 1: their face, skin "
            "tone, hair, build and clothing exactly as in Image 1. Replace the woman on the left in Video 1 with the person "
            "in Image 2: their face, skin tone, hair, build and clothing exactly as in Image 2. The place in Image 3 replaces "
            "the setting of Video 1: the shot is set there, with Video 1's camera, ground plane and where everyone stands "
            "kept as they are. Replace the red ball in Video 1 with the object in Image 4, matching Image 4 exactly in shape, "
            "colour, material and markings. Everyone else in Video 1 keeps their place and movement, restyled to fit the new "
            "cast and setting. Make it night, and Image 1 waves at Image 2."))

    def test_same_inputs_same_text(self):
        cast = [A._replace(role="place"), B._replace(role="person", who="the dancer")]
        self.assertEqual(h3(images=cast, change="x"), h3(images=list(cast), change="x"))

    def test_order_with_the_still(self):
        cast = [A._replace(role="person", who="the dancer"), B._replace(role="look")]
        p = parts(images=cast, still_at=0.0, change="@image2 is warm.", extra="Slow.")
        self.assertEqual([x["part"] for x in p], ["motion", "image 2", "image 3", "extras", "still", "change", "extra"])
        self.assertIn("with the person in Image 2", p[1]["text"])  # the still is Image 1, yours follow
        self.assertEqual(p[4]["text"], still.H3_STILL)  # no "other images show the same person" with a cast map
        self.assertEqual(p[5]["text"], "Image 3 is warm.")

    def test_unmapped_images_keep_their_words(self):
        p = parts(images=[A._replace(role="person"), Ref(Path("b.png"), "his jacket"), C])
        self.assertEqual([x["part"] for x in p], ["motion", "image 1", "image 2", "extras"])
        self.assertEqual(p[2]["text"], "Image 2 is his jacket.")

    def test_others(self):
        person = [A._replace(role="person")]
        self.assertTrue(h3(images=person).endswith("Everyone else in Video 1 stays exactly as they are."))
        self.assertTrue(h3(images=person, others="restyle").endswith(
            "Everyone else in Video 1 keeps their place and movement, restyled to fit the new cast and setting."))
        with self.assertRaises(Refused):
            h3(images=person, others="dance")


class Refusals(unittest.TestCase):
    def test_object_needs_words(self):
        with self.assertRaisesRegex(Refused, "@image1 is an object"):
            h3(images=[A._replace(role="object")])

    def test_two_people_need_words(self):
        with self.assertRaisesRegex(Refused, "say who each one replaces"):
            h3(images=[A._replace(role="person", who="him"), B._replace(role="person")])

    def test_roles_are_for_copy(self):
        with self.assertRaisesRegex(Refused, "cast map is for copy"):
            h3("swap", [A._replace(role="place")], target="the mug")


class Change(unittest.TestCase):
    def test_mentions_any_case(self):
        self.assertTrue(h3(images=[A, B], change="Put @IMAGE2's hat on him in @Video1.").endswith(
            " Put Image 2's hat on him in Video 1."))

    def test_word_for_word(self):
        text = "  Rain, heavy; neon signs (pink) — keep the dog.  "
        self.assertEqual(parts(images=[A], change=text)[-1], {"part": "change", "text": text.strip()})

    def test_not_attached(self):
        with self.assertRaisesRegex(Refused, r"^@image3 is not attached: 2 images added \(@image1 to @image2\)$"):
            h3(images=[A, B], change="use @image3")
        with self.assertRaisesRegex(Refused, r"^@video2 is not attached"):
            h3(images=[A], change="like @video2")

    def test_swap_gains_the_change(self):
        text = h3("swap", [A], target="the white mug on the desk", change="It steams.")
        self.assertEqual(text, BEFORE["swap"] + " It steams.")


class StillFromTheMap(unittest.TestCase):
    def test_worded_for_one_frame(self):
        cast = [A._replace(role="person", who="the man in the orange suit"), B._replace(role="place")]
        p = still.still_parts("copy", "", cast, "", change="@image1 smiles at @video1.")
        self.assertEqual([x["part"] for x in p], ["frame", "image 2", "image 3", "extras", "change"])
        self.assertEqual(" ".join(x["text"] for x in p), (
            still.COPY_FRAME + " Replace the man in the orange suit in Image 1 with the person in Image 2: their face, skin "
            "tone, hair, build and clothing exactly as in Image 2. The place in Image 3 replaces the setting of Image 1: the "
            "frame is set there, with Image 1's camera, ground plane and where everyone stands kept as they are. Everyone "
            "else in Image 1 stays exactly as they are. Image 2 smiles at Image 1."))

    def test_restyle_in_a_frame(self):
        p = still.still_parts("copy", "", [A._replace(role="person")], "", others="restyle")
        self.assertEqual(p[-1]["text"], "Everyone else in Image 1 keeps their place and pose, restyled to fit the new cast and setting.")


@unittest.skipUnless(shutil.which(media.ffmpeg_exe()) or Path(media.ffmpeg_exe()).exists(), "needs ffmpeg")
class DryRun(unittest.TestCase):
    """The whole build on real files, dry: the receipt keeps the map, the parts and the full prompt."""

    def test_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            ff = media.ffmpeg_exe()
            subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24", "-t", "3",
                            "-pix_fmt", "yuv420p", str(t / "clip.mp4")], check=True)
            for n in "ab":
                subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:size=512x512", "-frames:v", "1",
                                str(t / f"{n}.png")], check=True)
            i = Inputs(mode="copy", video=t / "clip.mp4", others="restyle", change="@image2 at dusk",
                       images=[Ref(t / "a.png", role="person", who="the runner"), Ref(t / "b.png", role="place")])
            d, r = engine.run(i, "dry", t / "runs")
            self.assertEqual(r["prompt"], " ".join(p["text"] for p in r["prompt_parts"]))
            self.assertEqual(r["cast"], [{"image": "@image1", "name": "a.png", "role": "person", "who": "the runner"},
                                         {"image": "@image2", "name": "b.png", "role": "place", "who": None}])
            self.assertEqual((r["extras"], r["change"]), ("restyle", "@image2 at dusk"))
            self.assertEqual(r["request"]["content"][0]["text"], r["prompt"])
            self.assertEqual(r["args"]["images"][0], [str(t / "a.png"), "", "person", "the runner"])
            saved = json.loads((d / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["prompt_parts"], r["prompt_parts"])


if __name__ == "__main__":
    unittest.main()
