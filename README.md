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

Make a key at platform.comfy.org and keep it in a text file outside the repo.

## Status

- `tools/probe.py`: reads the H3 model schemas from Comfy Router and asks for a price estimate. Free: it never runs a model.

## License

MIT
