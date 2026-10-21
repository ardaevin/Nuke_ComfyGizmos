"""Helpers for running LTX IC-LoRAs on VFX plates (part of ComfyGizmos).

Plate Prep    : fit a plate inside the model's max size (aspect kept, never upscaled), edge-pad
                (not stretch) to multiples of 32, and pad the frame count to 8n+1 by holding
                the last frame, as the LTX-2.5 Alpha Gen model card asks.
Plate Restore : undo all of that on the result - drop pad frames and pad pixels, then scale
                back to the plate's original resolution - so the output lines up 1:1 with
                the source frames in comp.
Plate Aspect Pad : for models with a fixed aspect-ratio list (Gemini / Nano Banana 2): pad the
                plate (right / bottom, edge-replicate) up to the nearest listed ratio and hand that
                ratio to the model, so Plate Restore crops the pad off instead of stretching.

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
            },
            "optional": {
                "spatial_pad": ("BOOLEAN", {"default": True,
                                            "tooltip": "Pad width/height to multiples of 32. Turn OFF for Tiled Fusion "
                                                       "workflows: they fit the plate to their own canvas, and a padded "
                                                       "aspect makes them pick a different (larger) canvas."}),
            },
        }

    RETURN_TYPES = ("IMAGE", "INT", "INT", "INT", "INT", "INT", "INT", "INT", "INT", "STRING")
    RETURN_NAMES = ("images", "width", "height", "length",
                    "orig_frames", "orig_width", "orig_height", "work_width", "work_height", "info")
    FUNCTION = "run"
    CATEGORY = "image/LTX plate"

    def run(self, images, max_width, max_height, scale, max_frames, spatial_pad=True):
        n, h0, w0 = images.shape[0], images.shape[1], images.shape[2]
        if n > max_frames:
            raise ValueError(f"Plate Prep: {n} frames is over the {max_frames}-frame limit. "
                             f"Set a shorter start/end range and run the shot in parts.")
        s = min(1.0, max_width / w0, max_height / h0) * scale
        ww, wh = max(32, int(round(w0 * s / 2) * 2)), max(32, int(round(h0 * s / 2) * 2))
        x = _resize(images, ww, wh)
        pw, ph = ((-ww) % 32, (-wh) % 32) if spatial_pad else (0, 0)
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
                "padded_width": ("INT", {"default": 0, "min": 0, "max": 16384, "forceInput": True,
                                         "tooltip": "Plate Prep's width output. Unconnected = assume pad to /32."}),
                "padded_height": ("INT", {"default": 0, "min": 0, "max": 16384, "forceInput": True,
                                          "tooltip": "Plate Prep's height output. Unconnected = assume pad to /32."}),
                "output_size": (["plate size", "keep upscale"],
                                {"default": "plate size",
                                 "tooltip": "keep upscale: when the model returns a larger image than Plate Prep "
                                            "sent (e.g. Tiled Fusion's 2x/4x), output the plate aspect at that "
                                            "larger size instead of scaling back down."}),
            },
        }

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("images", "matte")
    FUNCTION = "run"
    CATEGORY = "image/LTX plate"

    def run(self, images, orig_frames, orig_width, orig_height, work_width, work_height,
            range="0-1 (matte / SDR)", output_size="plate size", padded_width=0, padded_height=0):
        # Plate Prep padded work_* up to the next multiple of 32. The model may hand back that
        # canvas at another size (Tiled Fusion upscales, and may stretch to its own canvas), so
        # crop the pad off proportionally per axis rather than by pixel count.
        pad_w = padded_width or work_width + (-work_width) % 32
        pad_h = padded_height or work_height + (-work_height) % 32
        crop_w = max(1, round(work_width * images.shape[2] / pad_w))
        crop_h = max(1, round(work_height * images.shape[1] / pad_h))
        x = images[:orig_frames, :crop_h, :crop_w, :3]
        w, h = orig_width, orig_height
        if output_size == "keep upscale":
            k = crop_w / work_width  # model scale factor relative to the plate-sized work area
            w, h = max(2, int(round(orig_width * k / 2) * 2)), max(2, int(round(orig_height * k / 2) * 2))
        x = _resize(x, w, h, hdr=range.startswith("HDR"))
        return (x, x[..., 0].clone())


# Same list (same order) as the Google GenMedia pack's GEMINI_31_FLASH_IMAGE_ASPECT_RATIO, so the
# aspect_ratio output validates against Gemini31FlashImage's aspect_ratio combo.
GEMINI_31_ASPECT_RATIOS = ["1:1", "1:4", "4:1", "1:8", "8:1", "2:3", "3:2", "3:4", "4:3",
                           "4:5", "5:4", "9:16", "16:9", "21:9"]


class PlateAspectPad:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",)}}

    RETURN_TYPES = ("IMAGE", GEMINI_31_ASPECT_RATIOS, "INT", "INT", "STRING")
    RETURN_NAMES = ("images", "aspect_ratio", "padded_width", "padded_height", "info")
    FUNCTION = "run"
    CATEGORY = "image/LTX plate"

    def run(self, images):
        h, w = images.shape[1], images.shape[2]
        best = None
        for name in GEMINI_31_ASPECT_RATIOS:
            a, b = (int(v) for v in name.split(":"))
            # only ever pad (grow one axis), never crop the plate
            if a * h >= b * w:
                pw, ph = -(-h * a // b), h
            else:
                pw, ph = w, -(-w * b // a)
            if best is None or pw * ph < best[1] * best[2]:
                best = (name, pw, ph)
        name, pw, ph = best
        x = images
        if pw > w or ph > h:
            x = F.pad(x.movedim(-1, 1), (0, pw - w, 0, ph - h), mode="replicate").movedim(1, -1)
        info = f"plate {w}x{h} ({w / h:.3f}) -> {name}, padded {pw}x{ph}"
        print(f"[Plate Aspect Pad] {info}")
        return (x, name, pw, ph, info)


NODE_CLASS_MAPPINGS = {"LTXPlatePrep": LTXPlatePrep, "LTXPlateRestore": LTXPlateRestore,
                       "PlateAspectPad": PlateAspectPad}
NODE_DISPLAY_NAME_MAPPINGS = {"LTXPlatePrep": "LTX Plate Prep (local)",
                              "LTXPlateRestore": "LTX Plate Restore (local)",
                              "PlateAspectPad": "Plate Aspect Pad (local)"}

WEB_DIRECTORY = "./web"
