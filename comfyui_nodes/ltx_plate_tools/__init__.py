"""Helpers for running LTX IC-LoRAs on VFX plates (part of ComfyGizmos).

Plate Prep    : fit a plate inside the model's max size (aspect kept, never upscaled), edge-pad
                (not stretch) to multiples of 32, and pad the frame count to 8n+1 by holding
                the last frame, as the LTX-2.5 Alpha Gen model card asks.
Plate Restore : undo all of that on the result - drop pad frames and pad pixels, then scale
                back to the plate's original resolution - so the output lines up 1:1 with
                the source frames in comp.

web/plate_path_tokens.js : {plate_dir} / {plate_name} placeholders for OCIO Write's
                output_folder / filename, filled from the upstream OCIO Read at queue time.
"""
import torch
import torch.nn.functional as F


def _resize(images, w, h, hdr=False):
    if images.shape[1] == h and images.shape[2] == w:
        return images
    x = images.movedim(-1, 1)  # B,H,W,C -> B,C,H,W
    x = F.interpolate(x, size=(h, w), mode="bicubic", align_corners=False, antialias=True)
    x = x.movedim(1, -1)
    # bicubic overshoots; HDR keeps highlights above 1.0 and only drops negative light
    return x.clamp(min=0.0) if hdr else x.clamp(0.0, 1.0)


class LTXPlatePrep:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "max_width": ("INT", {"default": 1920, "min": 64, "max": 8192, "step": 32,
                                      "tooltip": "Largest working width. Alpha Gen: up to 1920."}),
                "max_height": ("INT", {"default": 1088, "min": 64, "max": 8192, "step": 32,
                                       "tooltip": "Largest working height. Alpha Gen: up to 1088."}),
                "scale": ("FLOAT", {"default": 1.0, "min": 0.1, "max": 1.0, "step": 0.05,
                                    "tooltip": "Extra downscale for speed / VRAM (1.0 = largest size that fits)."}),
                "max_frames": ("INT", {"default": 145, "min": 1, "max": 100000,
                                       "tooltip": "Refuse longer ranges. Alpha Gen leaks RGB into the matte above 145 frames: split the shot."}),
            }
        }

    RETURN_TYPES = ("IMAGE", "INT", "INT", "INT", "INT", "INT", "INT", "INT", "INT", "STRING")
    RETURN_NAMES = ("images", "width", "height", "length",
                    "orig_frames", "orig_width", "orig_height", "work_width", "work_height", "info")
    FUNCTION = "run"
    CATEGORY = "image/LTX plate"

    def run(self, images, max_width, max_height, scale, max_frames):
        n, h0, w0 = images.shape[0], images.shape[1], images.shape[2]
        if n > max_frames:
            raise ValueError(f"Plate Prep: {n} frames is over the {max_frames}-frame limit. "
                             f"Set a shorter start/end range and run the shot in parts.")
        s = min(1.0, max_width / w0, max_height / h0) * scale
        ww, wh = max(32, int(round(w0 * s / 2) * 2)), max(32, int(round(h0 * s / 2) * 2))
        x = _resize(images, ww, wh)
        pw, ph = (-ww) % 32, (-wh) % 32
        if pw or ph:  # replicate edges: no hard black border for the model to matte
            x = F.pad(x.movedim(-1, 1), (0, pw, 0, ph), mode="replicate").movedim(1, -1)
        pf = (1 - n) % 8
        if pf:
            x = torch.cat([x, x[-1:].repeat(pf, 1, 1, 1)], 0)
        info = (f"source {w0}x{h0} {n}f -> work {ww}x{wh}, padded {ww + pw}x{wh + ph} "
                f"{n + pf}f (8n+1)")
        print(f"[LTX Plate Prep] {info}")
        return (x, ww + pw, wh + ph, n + pf, n, w0, h0, ww, wh, info)


class LTXPlateRestore:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "orig_frames": ("INT", {"default": 1, "min": 1, "max": 100000, "forceInput": True}),
                "orig_width": ("INT", {"default": 64, "min": 1, "max": 16384, "forceInput": True}),
                "orig_height": ("INT", {"default": 64, "min": 1, "max": 16384, "forceInput": True}),
                "work_width": ("INT", {"default": 64, "min": 1, "max": 16384, "forceInput": True}),
                "work_height": ("INT", {"default": 64, "min": 1, "max": 16384, "forceInput": True}),
            },
            "optional": {
                "range": (["0-1 (matte / SDR)", "HDR (no upper clip)"],
                          {"default": "0-1 (matte / SDR)",
                           "tooltip": "HDR keeps scene-linear values above 1.0 when scaling back to plate size."}),
            },
        }

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("images", "matte")
    FUNCTION = "run"
    CATEGORY = "image/LTX plate"

    def run(self, images, orig_frames, orig_width, orig_height, work_width, work_height,
            range="0-1 (matte / SDR)"):
        x = images[:orig_frames, :work_height, :work_width, :3]
        x = _resize(x, orig_width, orig_height, hdr=range.startswith("HDR"))
        return (x, x[..., 0].clone())


NODE_CLASS_MAPPINGS = {"LTXPlatePrep": LTXPlatePrep, "LTXPlateRestore": LTXPlateRestore}
NODE_DISPLAY_NAME_MAPPINGS = {"LTXPlatePrep": "LTX Plate Prep (local)",
                              "LTXPlateRestore": "LTX Plate Restore (local)"}

WEB_DIRECTORY = "./web"
