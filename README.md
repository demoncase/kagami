# Kagami

Re-shoot any clip with your own cast. Kagami (鏡, "mirror") keeps a video's motion and camera and recasts it from your images, or swaps one thing in the shot and keeps the rest. Built on the Comfy Developer Platform (Comfy Router + Comfy SDK) with MiniMax H3.

Work in progress for Comfy's Open Source Higgsfield challenge (October 2026).

## Modes

- **Copy**: the source clip's motion, timing and camera, recast with your characters, products and places.
- **Swap**: replace one thing in the clip (a person, an outfit, a product), keep everything else.

## What makes it different

- **Approve the still before you pay for video.** Kagami recasts one frame first; the video is made only from a frame you approved.
- **Every result has a receipt.** Each run writes `receipt.json`: every input with its hash, the exact request sent, the model, the seed, the request id, and the cost Router reported (or "not reported", never a guessed zero).

## Setup (Windows, PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

ffmpeg comes with the `imageio-ffmpeg` wheel; nothing else to install.

Make a key at platform.comfy.org and keep it in a text file outside the repo.

## The app

```powershell
python -m kagami serve
python -m kagami serve --live --key-file E:\keys\comfy-router.txt
```

One page in your browser, top to bottom: the clip, your cast (or the new thing, in Swap), an optional preview frame to approve, the video, and the result as a synced before/after with downloads and the receipt. `serve` alone is FAKE: every click is free and the page says so. `--live` sends real requests and the page says that too; it cannot be switched from the page.

## The terminal

```powershell
python -m kagami copy --video clip.mp4 --image me.png
python -m kagami copy --video clip.mp4 --image me.png --fake
python -m kagami copy --video clip.mp4 --image me.png --run --key-file E:\keys\comfy-router.txt
python -m kagami swap --video clip.mp4 --image mug.png --target "the white mug on the desk" --run --key-file E:\keys\comfy-router.txt
```

See it before you pay for video: recast one frame on Nano Banana Pro or FLUX 3 (cents, seconds), approve it, and the approved still goes to H3 as Image 1:

```powershell
python -m kagami still copy --video clip.mp4 --image me.png --model both --run --key-file E:\keys\comfy-router.txt
python -m kagami approve stills\<folder> --pick nbp
python -m kagami copy --video clip.mp4 --image me.png --still stills\<folder> --run --key-file E:\keys\comfy-router.txt
```

Every made video also gets `output_source_audio.mp4` (the recast with the clip's own sound), `before_after_side.mp4` and `before_after_split.mp4` (`python -m kagami finish runs\<folder>` makes them for older runs).

The first line is a dry run (the default): the request is built, checked against H3's limits and saved, nothing is sent. `--fake` runs the whole path offline. `--run` is the one paid call; Kagami never retries it. If an answer is lost, `python -m kagami replay runs\<folder> --key-file ...` resends the same request under the same Idempotency-Key and Router hands back the first result instead of billing again.

Each run gets a folder under `runs\` with `receipt.json` and, when a video was made, `output.mp4` (the provider's file, byte for byte).

## Tools

- `tools/probe.py`: reads the H3 model schemas from Comfy Router. Free.
- `tools/probe_gate.py`: checks the Router run route is open with an invalid-on-purpose request. Free.
- `tools/probe_local.py`: lists the MiniMax H3 nodes in a running ComfyUI and their inputs. Free.

## License

MIT
