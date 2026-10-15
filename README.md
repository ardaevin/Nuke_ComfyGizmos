# ComfyGizmos

ComfyUI workflows as Nuke nodes. Connect a plate, press **Render in ComfyUI**,
and the node's output becomes the result. ComfyUI only has to be running in
the background, so you never leave Nuke.

It ships with one workflow, **LTX Alpha Gen**: an alpha matte from a plate,
using Lightricks' LTX-2.5 Alpha Gen IC-LoRA. The output matches the plate's
resolution and frame numbers.

The node talks to ComfyUI's HTTP API directly. It is pure Python (stdlib only)
on the Nuke side.

## Requirements

**Nuke** 14 or later (tested on NukeX 14.1, Python 3.9).

**ComfyUI** running locally (default `http://127.0.0.1:8188`) with:

- [ComfyUI-OCIO](https://github.com/SlavaSexton/ComfyUI-OCIO): OCIO Read and
  OCIO Write, which load and write the plates. Its default config is ACES 1.2.
- [ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo)
- `ltx_plate_tools` from this repo (see Install)
- the LTX-2.5 models (the Lightricks repos on Hugging Face are gated, so accept
  the licence first):

  | Folder | File |
  |---|---|
  | `diffusion_models` | `ltx-2.5-22b-distilled-transformer-bf16.safetensors` |
  | `loras` | `ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors` |
  | `vae` | `ltx-2.5-video-vae-bf16.safetensors`, `ltx-2.5-audio-vae-bf16.safetensors` |
  | `text_encoders` | `gemma4-12b-with-proj-ltx-2.5-bf16.safetensors`, `gemma4_e2b_it_bf16.safetensors` |

## Install

1. **ComfyUI side**
   - Copy `comfyui_nodes/ltx_plate_tools` into `ComfyUI/custom_nodes/`.
   - Copy `comfyui_nodes/_ltx_no_image_placeholder.png` into `ComfyUI/input/`.
     The workflow's optional start-image input needs a file there; it is not
     used for the matte.
   - Restart ComfyUI.
2. **Nuke side**
   - Run `python install.py`. It copies the `comfygizmos` package to
     `~/.nuke/python/`, or pass another folder on your Nuke plugin path.
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

**Nodes > ComfyUI > LTX Alpha Gen**, connect it, press **Render in ComfyUI**.
The render runs in the background and progress shows in *status*, so Nuke stays
usable.

**Input**

- **source**
  - `auto`: a Read connected straight in is sent as is, with no render.
  - Anything else (a Grade, a merge, …), or `render input`: the input is
    rendered to EXR first, into `<output root>/_source/`.
- **reuse unchanged input**: skips that input render when nothing upstream has
  changed. That covers node settings, connections, frame range and Read file
  timestamps; moving or selecting nodes doesn't count. Changing only the
  workflow settings (LoRA strength, seed, …) therefore re-runs ComfyUI alone.
- **input range**: on uses the input's range; off uses **first** / **last**.
- **max frames**: the longest range the node will send. It defaults to 145,
  the model card's limit; above that, RGB detail can leak into the matte.
- **colorspace override**: leave it empty and the colorspace comes from the
  Read. A Read on `default` (or an OCIO role) counts as ACES2065-1. ACES 1.2,
  1.3 and 2.0 names, and nuke-default names, are matched to ComfyUI's config.

**Settings**: prompt (leave empty for Alpha Gen), seed, work scale and LoRA
strength.

**Output**

- **output root**: defaults to
  `[value root.project_directory]/Comp/Inputs/ComfyUI`. It follows the
  script's project directory, or one folder up from the .nk when that is
  empty. A plain path works too.
- **suffix**, **output colorspace** and **version**: files are written as
  `<plate>_<suffix>/<plate>_<suffix>_<colorspace>.####.exr`, using the plate's
  frame numbers.
  - The matte is written as ACEScg. The values are untouched and only the name
    and the Read carry the colorspace; the EXR has no colorspace metadata.
  - **version**, with **from input** on, comes from a directly connected Read's
    file name (`..._v002` gives 2). Anything else gives 1. **Version up** (or
    typing a number) fixes it. The version replaces the one in the plate name,
    so a v001 plate rendered at v002 writes `<shot>_v002_LTXAlpha/...`.
    Re-rendering the same version overwrites it, after asking.
- **output**: `matte` (rgba = matte) or `input + matte alpha`.
- **Reload result**, **Bring in output** (makes a plain Read of the result
  next to the node) and **Cancel**.

## Adding a workflow

1. Build it in ComfyUI with an **OCIO Read** in and an **OCIO Write** out. It
   can't contain subgraphs, so unpack them.
2. Export it in API format: *Save (API)*, or `app.graphToPrompt()` in the
   browser console.
3. Run
   `python tools/make_api.py export.json comfygizmos/workflows/<id>.api.json <write_node_id> [ids of preview-only nodes to drop]`
4. Write `comfygizmos/workflows/<id>.json`, using `ltx_alpha.json` as the
   template. It sets the read and write node ids, the suffix, max frames, the
   output kind (`matte` / `image`) and colorspace, and the knobs to expose
   (each one a `node` + `input` in the API graph).
5. Run `python install.py` and restart Nuke.

## Tests

These need ComfyUI running and do real renders.

- `tests/test_colorspace.py`: colorspace name matching against the live
  ComfyUI.
- `tests/nuke_test.py`, `nuke_test_reuse.py`, `nuke_test_version.py`:
  end-to-end in Nuke. Run each with
  `Nuke14.1.exe --nukex -i -t tests/<test>.py <repo_dir> <project_dir> <plate.%04d.exr> f<first> f<last>`.
  The `f` prefix stops Nuke from reading the numbers as a frame range.
