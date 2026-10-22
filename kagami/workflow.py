"""A run's receipt as a ComfyUI workflow: the same files, prompt and settings on ComfyUI's
"MiniMax H3 Reference to Video" partner node (MinimaxHailuo03ReferenceNode), so anyone can open
the run in ComfyUI and make it again there. Free and local: nothing is sent anywhere.

Beside the receipt, per run:
  comfyui_workflow.json       the graph: drop it on ComfyUI's canvas
  comfyui_workflow_api.json   the same graph in ComfyUI's API format (POST /prompt)
  comfyui_input/              the exact files the run sent, named the way the graph names them
  comfyui.zip                 all of the above in one file, for the app's download

The graph names its files by hash (kagami_<sha256 first 10>.png), never by the names they had
on your disk. Copy comfyui_input/ into ComfyUI's input folder (or pass --comfy-input and Kagami
copies them there), then open the graph.

Not identical, and said in the receipt: Kagami calls H3 through Comfy Router, the graph calls it
through the partner node. Same model, two doors. A run sent with no seed gets the seed written
here, so ComfyUI's result will differ from the run's.
"""

from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from pathlib import Path

from . import media

NODE = "MinimaxHailuo03ReferenceNode"
FRONTEND = "1.49.6"  # the ComfyUI frontend the graph was exported from (workflows/h3_reference.json)
DEFAULT_SEED = 42
MAX_IMAGES, MAX_VIDEOS = 9, 3

WORKFLOW = "comfyui_workflow.json"
WORKFLOW_API = "comfyui_workflow_api.json"
INPUT_DIR = "comfyui_input"
ZIP = "comfyui.zip"

README = """Kagami run {run}, as a ComfyUI workflow.

1. Copy the files in input/ into ComfyUI's input folder.
2. Drop workflow.json on ComfyUI's canvas (workflow_api.json is the API format).
3. Run. It is a partner node: it needs a Comfy account with credits, like the run itself.

Same files, prompt and settings as the run. Kagami called H3 through Comfy Router; this graph calls it
through the partner node. {seed_note}
"""


def _file_for(f: dict) -> tuple[Path, str]:
    """The file this input really sent (a padded send copy when there was one) and its hash, checked."""
    src = f.get("send_copy") or f
    p, sha = Path(src["path"]), src["sha256"]
    if not p.exists():
        raise ValueError(f"{f['label']}: {p} is gone; the workflow needs the file the run sent")
    if media.sha256(p) != sha:
        raise ValueError(f"{f['label']}: {p.name} changed since the run (hash differs); refusing to export another file")
    return p, sha


def _name(p: Path, sha: str) -> str:
    return f"kagami_{sha[:10]}{p.suffix.lower()}"


def _settings(r: dict) -> tuple[dict, str]:
    s = r.get("settings") or {}
    if "seed" in s:
        seed, note = int(s["seed"]), f"The run sent seed {s['seed']}; the graph uses the same."
    else:
        seed = DEFAULT_SEED
        note = (f"The run sent no seed (H3 picked one), so the graph uses {DEFAULT_SEED} "
                "and its result will differ from the run's.")
    return {"model": "MiniMax H3", "model.prompt": r.get("prompt") or "",
            "model.resolution": s.get("resolution", "768P"), "model.ratio": s.get("ratio", "adaptive"),
            "model.duration": int(s.get("duration", 5)), "seed": seed, "watermark": False}, note


def build(r: dict, names: list[tuple[dict, str]], run_name: str) -> tuple[dict, dict]:
    """(API-format graph, canvas graph) for this receipt; names = [(input, comfy file name)]."""
    images = [(f, n) for f, n in names if f["role"] == "reference_image"]
    videos = [(f, n) for f, n in names if f["role"] == "reference_video"]
    if not videos:
        raise ValueError("the receipt has no reference video")
    if len(images) > MAX_IMAGES or len(videos) > MAX_VIDEOS:
        raise ValueError(f"the node takes {MAX_IMAGES} images and {MAX_VIDEOS} videos")
    w, seed_note = _settings(r)
    prefix = f"kagami/{run_name}"

    # API format: loaders numbered first, then the H3 node, then Save Video.
    api: dict[str, dict] = {}
    nid = 0
    h3_inputs = dict(w)
    for k, (_, n) in enumerate(images, 1):
        nid += 1
        api[str(nid)] = {"inputs": {"image": n}, "class_type": "LoadImage", "_meta": {"title": f"Image {k}"}}
        h3_inputs[f"model.reference_images.image_{k}"] = [str(nid), 0]
    for k, (_, n) in enumerate(videos, 1):
        nid += 1
        api[str(nid)] = {"inputs": {"file": n, "video-preview": ""}, "class_type": "LoadVideo",
                         "_meta": {"title": f"Video {k}"}}
        h3_inputs[f"model.reference_videos.video_{k}"] = [str(nid), 0]
    h3 = nid + 1
    api[str(h3)] = {"inputs": h3_inputs, "class_type": NODE, "_meta": {"title": "MiniMax H3 Reference to Video"}}
    api[str(h3 + 1)] = {"inputs": {"filename_prefix": prefix, "format": "auto", "codec": "auto", "video": [str(h3), 0]},
                        "class_type": "SaveVideo", "_meta": {"title": "Save Video"}}

    # Canvas format, shaped like the graph exported from ComfyUI (workflows/h3_reference.json):
    # the node's inputs grow one empty slot past the last connected one.
    nodes, links = [], []
    slots: list[dict] = []
    y = 0.0
    nid = 0

    def link(src: int, dst: int, slot: int, typ: str) -> int:
        links.append([len(links) + 1, src, 0, dst, slot, typ])
        return len(links)

    loaders = []
    for k, (_, n) in enumerate(images, 1):
        nid += 1
        loaders.append(("IMAGE", k, nid))
        nodes.append({"id": nid, "type": "LoadImage", "pos": [0, y], "size": [300, 314], "flags": {}, "order": nid - 1,
                      "mode": 0, "inputs": [], "title": f"Image {k}",
                      "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}, {"name": "MASK", "type": "MASK", "links": None}],
                      "properties": {"Node name for S&R": "LoadImage"}, "widgets_values": [n, "image"]})
        y += 350
    for k, (_, n) in enumerate(videos, 1):
        nid += 1
        loaders.append(("VIDEO", k, nid))
        nodes.append({"id": nid, "type": "LoadVideo", "pos": [0, y], "size": [300, 560], "flags": {}, "order": nid - 1,
                      "mode": 0, "inputs": [], "title": f"Video {k}",
                      "outputs": [{"name": "VIDEO", "type": "VIDEO", "links": []}],
                      "properties": {"Node name for S&R": "LoadVideo"}, "widgets_values": [n, "image"]})
        y += 600
    h3_id, save_id = nid + 1, nid + 2
    groups = (("IMAGE", "image", "model.reference_images.image_", len(images), MAX_IMAGES),
              ("VIDEO", "video", "model.reference_videos.video_", len(videos), MAX_VIDEOS),
              ("AUDIO", "audio", "model.reference_audios.audio_", 0, 3))
    for typ, label, name, used, cap in groups:
        for k in range(1, min(used + 1, cap) + 1):
            slots.append({"label": f"{label}_{k}", "name": f"{name}{k}", "shape": 7, "type": typ, "link": None})
    for typ, k, src in loaders:
        prefix_name = "model.reference_images.image_" if typ == "IMAGE" else "model.reference_videos.video_"
        slot = next(i for i, s in enumerate(slots) if s["name"] == f"{prefix_name}{k}")
        lid = link(src, h3_id, slot, typ)
        slots[slot]["link"] = lid
        nodes[src - 1]["outputs"][0]["links"].append(lid)
    out_link = link(h3_id, save_id, 0, "VIDEO")
    nodes.append({"id": h3_id, "type": NODE, "pos": [420, 0], "size": [400, 336], "flags": {}, "order": nid, "mode": 0,
                  "inputs": slots, "outputs": [{"name": "VIDEO", "type": "VIDEO", "links": [out_link]}],
                  "properties": {"Node name for S&R": NODE},
                  "widgets_values": [w["model"], w["model.prompt"], w["model.resolution"], w["model.ratio"],
                                     w["model.duration"], w["seed"], "fixed", False],
                  "color": "#432", "bgcolor": "#653"})
    nodes.append({"id": save_id, "type": "SaveVideo", "pos": [900, 0], "size": [270, 106], "flags": {}, "order": nid + 1,
                  "mode": 0, "inputs": [{"name": "video", "type": "VIDEO", "link": out_link}],
                  "outputs": [{"name": "video", "type": "VIDEO", "links": None}], "properties": {},
                  "widgets_values": [prefix, "auto", "auto"]})
    canvas = {"id": str(uuid.uuid4()), "revision": 0, "last_node_id": save_id, "last_link_id": len(links),
              "nodes": nodes, "links": links, "groups": [], "config": {},
              "extra": {"ds": {"scale": 0.8, "offset": [80, 80]}, "frontendVersion": FRONTEND,
                        "kagami": {"run": run_name, "note": seed_note}},
              "version": 0.4}
    return api, canvas


def export(run_folder: Path, comfy_input: Path | None = None) -> dict:
    """Write the run's ComfyUI files and record them in its receipt. comfy_input: ComfyUI's input
    folder, to copy the files straight there (a file already there under the same name is the same
    file, since the name is its hash; checked anyway)."""
    path = run_folder / "receipt.json"
    r = json.loads(path.read_text(encoding="utf-8"))
    ins = [f for f in r.get("inputs", []) if f.get("role") in ("reference_image", "reference_video")]
    if not ins or not r.get("prompt"):
        raise ValueError(f"{run_folder.name}: the receipt has no inputs or prompt to export")
    files = []
    for f in ins:
        p, sha = _file_for(f)
        files.append((f, p, sha, _name(p, sha)))
    in_dir = run_folder / INPUT_DIR
    in_dir.mkdir(exist_ok=True)
    for _, p, sha, n in files:
        dst = in_dir / n
        if not dst.exists():
            shutil.copy2(p, dst)
    copied = []
    if comfy_input is not None:
        if not comfy_input.is_dir():
            raise ValueError(f"no such folder: {comfy_input} (ComfyUI's input folder)")
        for _, p, sha, n in files:  # check every name first, so a refusal copies nothing
            dst = comfy_input / n
            if dst.exists() and media.sha256(dst) != sha:
                raise ValueError(f"{dst} exists with other bytes; nothing copied, nothing overwritten")
        for _, p, sha, n in files:
            dst = comfy_input / n
            if not dst.exists():
                shutil.copy2(p, dst)
                copied.append(n)
    api, canvas = build(r, [(f, n) for f, _, _, n in files], run_folder.name)
    _, seed_note = _settings(r)
    wf, wfa = run_folder / WORKFLOW, run_folder / WORKFLOW_API
    wf.write_text(json.dumps(canvas, indent=2, ensure_ascii=False), encoding="utf-8")
    wfa.write_text(json.dumps(api, indent=2, ensure_ascii=False), encoding="utf-8")
    z = run_folder / ZIP
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(wf, "workflow.json")
        zf.write(wfa, "workflow_api.json")
        for _, p, _, n in files:
            zf.write(p, f"input/{n}")
        zf.writestr("README.txt", README.format(run=run_folder.name, seed_note=seed_note))
    done = {
        "note": "the run as a ComfyUI graph on the MiniMax H3 Reference to Video partner node; same files, "
                "prompt and settings. Kagami used Comfy Router, the graph uses the partner node: same model. " + seed_note,
        "node": NODE, "frontend": FRONTEND,
        "workflow": {"path": str(wf), "sha256": media.sha256(wf)},
        "workflow_api": {"path": str(wfa), "sha256": media.sha256(wfa)},
        "zip": {"path": str(z), "sha256": media.sha256(z), "bytes": z.stat().st_size},
        "inputs": [{"label": f["label"], "comfy_name": n, "sha256": sha, "sent_copy": "send_copy" in f}
                   for f, _, sha, n in files],
    }
    if comfy_input is not None:
        done["copied_to"] = {"folder": str(comfy_input), "files": copied}
    r["comfyui"] = done
    path.write_text(json.dumps(r, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return done
