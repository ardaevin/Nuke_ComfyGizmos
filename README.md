# ComfyGizmos

ComfyUI workflows as Nuke nodes. Connect a plate, press **Render in ComfyUI**,
and the node's output becomes the result. ComfyUI only has to be running in
the background, so you never leave Nuke.

The node talks to ComfyUI's HTTP API directly. The Nuke side is pure Python
(stdlib only).

## Gizmos

| Node (Nodes > ComfyUI) | Does | Frames | Output |
|---|---|---|---|
| **LTX Alpha Gen** | Alpha matte from a plate (LTX-2.5 Alpha Gen IC-LoRA) | range, up to 145 | ACEScg-labelled matte |
| **LTX SDR to HDR** | SDR plate to scene-linear HDR (LTX-2.5 IC-LoRA) | range, up to 241 | ACES2065-1 |
| **LTX SDR to HDR (Tiled)** | Same, two stages with a latent x2 upscale | range, up to 241 | ACES2065-1, upscaled |
| **Qwen Image Edit** | Edits one frame from a text prompt, with up to 3 reference images (Qwen-Image 2.1) | single | ACES2065-1 |
| **Qwen Image Edit (Turbo)** | Same, with the fixed 8-step Turbo model | single | ACES2065-1 |
| **DSINE Normals** | Camera-space surface normals | single | raw EXR, decoded to -1..1 in the node |

## Requirements

**Nuke** 14 or later (tested on NukeX 14.1, Python 3.9).

**ComfyUI** running locally (default `http://127.0.0.1:8188`) with
[ComfyUI-OCIO](https://github.com/SlavaSexton/ComfyUI-OCIO). Every gizmo
loads and writes through its OCIO Read and OCIO Write, using an ACES 1.2
config by default.

Per gizmo:

| Gizmo | Node packs | Models (ComfyUI `models/` folder) |
|---|---|---|
| LTX Alpha Gen | [ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo), `ltx_plate_tools` (this repo) | `diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors`, `loras/ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors`, `vae/ltx-2.5-video-vae-bf16.safetensors`, `vae/ltx-2.5-audio-vae-bf16.safetensors`, `text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors`, `text_encoders/gemma4_e2b_it_bf16.safetensors` |
| LTX SDR to HDR | ComfyUI-LTXVideo, `ltx_plate_tools` | the LTX-2.5 transformer and VAEs above, `loras/ltx-2.5-22b-ic-lora-sdr-to-hdr-1.0.safetensors`, `embeddings/ltx-2.5-22b-ic-lora-sdr-to-hdr-scene-emb.safetensors` |
| LTX SDR to HDR (Tiled) | ComfyUI-LTXVideo, `ltx_plate_tools` | as above, plus `latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors` |
| Qwen Image Edit | `ltx_plate_tools` | `diffusion_models/qwen_image_2.1_int8_convrot.safetensors`, `vae/qwen_image_2.1_vae_bf16.safetensors`, `text_encoders/qwen3vl_8b_int8_convrot.safetensors`, `text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors` (prompt enhancer) |
| Qwen Image Edit (Turbo) | `ltx_plate_tools` | as above, with `diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors` |
| DSINE Normals | [comfyui_controlnet_aux](https://github.com/Fannovel16/comfyui_controlnet_aux) | DSINE (downloaded by the pack on first use) |

The Lightricks repos on Hugging Face are gated, so accept the licence first.
ComfyUI only lists `embeddings/` at startup, so restart it after adding the
scene embedding.

## Install

1. **ComfyUI side**
   - Copy `comfyui_nodes/ltx_plate_tools` into `ComfyUI/custom_nodes/`.
   - Copy `comfyui_nodes/_ltx_no_image_placeholder.png` into `ComfyUI/input/`.
     LTX Alpha Gen's optional start-image input needs it; the image isn't
     used for the matte.
   - Restart ComfyUI.
2. **Nuke side**
   - Run `python install.py`. It copies `comfygizmos` to `~/.nuke/python/`,
     or pass another folder on your Nuke plugin path.
   - Add to `menu.py`:

     ```python
     try:
         import comfygizmos
         comfygizmos.register_menu()   # Nodes > ComfyUI > one entry per workflow
     except Exception as _err:
         nuke.tprint('comfygizmos failed to load: %s' % _err)
     ```
   - Restart Nuke.

## Use

Create the node from **Nodes > ComfyUI**, connect it, fill in **Name** if
needed, and press **Render in ComfyUI**. The render runs in the background and
**status** reports progress, so Nuke stays usable.

### Inputs

The **input** (or source) is whatever is connected to the gizmo. On the Qwen
gizmos the `ref2`-`ref4` reference inputs count as inputs too.

- **A plain Read**, connected straight in with no frame offset, is sent to
  ComfyUI as it is. Nothing is rendered.
- **Anything else** (a Grade, Merge, Crop, ...) is rendered to EXR first,
  into the `sources` folder, and that render is sent.

### Name

**Name**, at the top, names everything the node writes. It's required unless
the input is a plain Read; with a plain Read and Name left empty, the plate's
file name is used. The gizmo name is fixed per gizmo: `LTXAlpha`, `LTXHDR`,
`LTXHDRTiled`, `QwenEdit`, `QwenEditTurbo` or `DSINENormal`.

### Folders and versions

Everything goes under **output root**, which defaults to
`[value root.project_directory]/Comp/Inputs/ComfyUI`. It follows the script's
project directory, or one folder up from the .nk when that is empty.

```
<output root>/
├── sources/<Name>_<Gizmo>/<Name>_<Gizmo>_Source_v001.####.exr     (rendered inputs)
│                          <Name>_<Gizmo>_Source_ref2_v001.####.exr (Qwen references)
└── outputs/<Name>_<Gizmo>/<Name>_<Gizmo>_v001_<colorspace>.####.exr
```

The **source** and **output** versions are separate:

- **source** + **Version up source**: Render in ComfyUI uses
  `_Source_v###` if it is already on disk, and renders it first if not. If
  only some frames are missing, only those are rendered. Changed something
  upstream? Press **Version up source** and the input is rendered fresh at the
  next render. Typing an older number goes back to that render. Both controls
  are greyed out when every connected input is a plain Read.
- **version** + **Version up output**: the output version, starting at 001.
  To try again from the same source (a new seed, other settings), press
  **Version up output**. The source is reused and earlier outputs are kept.
  Rendering over an existing output version asks before overwriting.
- The node's **label** shows the Name and the output version, e.g.
  `WallFix` / `v003`.

Output frames keep the plate's frame numbers. The colorspace tag in the file
name comes from OCIO Write.

### Other controls

- **input range** / **first** / **last** (range gizmos): input range on uses
  the input's range; off uses first and last. **max frames** caps the range
  and is also passed to ComfyUI.
- **frame** + **Set to current frame** (single-frame gizmos): the frame to
  process. It doesn't follow the playhead. **hold on all frames** shows the
  result on every frame.
- **colorspace override**: leave it empty and the colorspace comes from the
  Read; a Read on `default` (or an OCIO role) counts as ACES2065-1. ACES 1.2,
  1.3 and 2.0 names, and nuke-default names, are matched to ComfyUI's config.
- **output colorspace**: the written files' colorspace, which the node's Read
  also uses.
- **Reload result**, **Bring in output** (a Read of the result next to the
  node, then a Crop; plus a red-to-alpha Shuffle for mattes) and **Cancel**.

### Gizmo notes

- **LTX Alpha Gen**: seed, work scale and LoRA strength. **output** is
  `matte` (rgba = matte) or `input + matte alpha`. The matte is written
  untouched and labelled ACEScg.
- **LTX SDR to HDR (Tiled)**: seed and LoRA strength drive both stages. The
  output is at the upscaled size.
- **Qwen Image Edit / Turbo**:
  - **prompt** is the edit instruction. The plate is `<image1>`; the refs are
    `<image2>`-`<image4>`.
  - **refine prompt** lets the Qwen prompt enhancer rewrite the instruction
    first. **Run enhancer only** runs just the enhancer and shows its rewrite
    in **refined prompt**. **Use refined prompt** copies the rewrite into
    **prompt** and turns refining off.
  - Qwen works on display-referred images, so values above 1.0 are clipped.
    Comp the edit back over the plate only where it changed.
- **DSINE Normals**: fov, iterations and resolution. **output** is
  `normals -1..1` or `encoded 0..1`.

## Adding a workflow

1. Build it in ComfyUI with an **OCIO Read** in and an **OCIO Write** out.
   Subgraphs are fine; the API export flattens them.
2. Export it in API format: *Save (API)*, or `app.graphToPrompt()` in the
   browser console. Clear the OCIO Write's folder and filename first.
3. Run
   `python tools/make_api.py export.json comfygizmos/workflows/<id>.api.json <write_node_id> [ids of preview-only nodes to drop]`
4. Write `comfygizmos/workflows/<id>.json`, using an existing manifest as the
   template. It holds:
   - the read and write node ids
   - `suffix`, which becomes the gizmo name
   - `frames: single` or a range with `max_frames`
   - the output kind (`matte` / `image` / `normals`) and colorspace
   - optional `refs` and `enhancer`
   - the knobs to expose: each one is a `node` + `input` in the API graph, or
     a list of `targets` when one knob drives several nodes
5. Run `python install.py` and restart Nuke.

## Tests

These need ComfyUI running and do real renders. Run each with
`Nuke14.1.exe --nukex -i -t tests/<test>.py <repo_dir> <project_dir> ...`
(see each file's docstring). Prefix frame numbers with `f`, as in `f1001`,
because Nuke reads bare numbers as a frame range.

- `nuke_test_naming.py`: Name, sources/outputs, and the source and output
  versions.
- `nuke_test.py`, `nuke_test_workflow.py`, `nuke_test_qwen.py`,
  `nuke_test_normals.py`: end-to-end per gizmo.
- `test_colorspace.py`: colorspace name matching against the live ComfyUI.
