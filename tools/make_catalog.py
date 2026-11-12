"""Write comfygizmos/catalog.json: where every model file the workflows load comes from, its lighter
'medium' alternative, the non-Blackwell alternative of NVFP4 files, and which node pack each custom node
is in. Sizes and the gated flag are read from Hugging Face, so re-run this after changing SOURCES.

    python tools/make_catalog.py

The installer and "Check setup" read the catalog; the gizmos use its swaps at render time. Which files a
gizmo needs is not listed here - it is read from the gizmo's own API graph."""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "comfygizmos", "catalog.json")
WORKFLOWS = os.path.join(HERE, "..", "comfygizmos", "workflows")

LTX = "Lightricks/LTX-2.5"
QWEN = "Comfy-Org/Qwen-Image-2.1"
H3 = "Comfy-Org/MiniMax-H3"
FLUX = "black-forest-labs/flux-3-image-klein-comfy"

# file name the workflows use -> (models sub-folder, HF repo, path in the repo). repo None = no public source
# known: the installer lists it to get by hand. "auto" = the node pack downloads it on first use.
SOURCES = {
    # LTX-2.5
    "ltx-2.5-22b-distilled-transformer-bf16.safetensors":
        ("diffusion_models", LTX, "diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors"),
    "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors":
        ("diffusion_models", LTX, "diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors"),
    "gemma4-12b-with-proj-ltx-2.5-bf16.safetensors":
        ("text_encoders", LTX, "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors"),
    "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors":
        ("text_encoders", LTX, "text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors"),
    "gemma4_e2b_it_bf16.safetensors":
        ("text_encoders", "Comfy-Org/gemma-4", "text_encoders/gemma4_e2b_it_bf16.safetensors"),
    "gemma4_e2b_it_int8_convrot.safetensors":
        ("text_encoders", "Comfy-Org/gemma-4", "text_encoders/gemma4_e2b_it_int8_convrot.safetensors"),
    "ltx-2.5-video-vae-bf16.safetensors": ("vae", LTX, "vae/ltx-2.5-video-vae-bf16.safetensors"),
    "ltx-2.5-audio-vae-bf16.safetensors": ("vae", LTX, "vae/ltx-2.5-audio-vae-bf16.safetensors"),
    "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors":
        ("latent_upscale_models", LTX, "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"),
    "ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors":
        ("loras", "Lightricks/LTX-2.5-22b-IC-LoRA-Alpha-Gen", "ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors"),
    "ltx-2.5-22b-ic-lora-layout-to-render-1.0.safetensors":
        ("loras", "Lightricks/LTX-2.5-22b-IC-LoRA-Layout-To-Render", "ltx-2.5-22b-ic-lora-layout-to-render-1.0.safetensors"),
    "ltx-2.5-22b-ic-lora-refine-details-1.0.safetensors":
        ("loras", "Lightricks/LTX-2.5-22b-IC-LoRA-Refine-Details", "ltx-2.5-22b-ic-lora-refine-details-1.0.safetensors"),
    "ltx-2.5-22b-ic-lora-sdr-to-hdr-1.0.safetensors":
        ("loras", "Lightricks/LTX-2.5-22b-IC-LoRA-SDR-To-HDR", "ltx-2.5-22b-ic-lora-sdr-to-hdr-1.0.safetensors"),
    "ltx-2.5-22b-ic-lora-sdr-to-hdr-scene-emb.safetensors":
        ("embeddings", "Lightricks/LTX-2.5-22b-IC-LoRA-SDR-To-HDR", "ltx-2.5-22b-ic-lora-sdr-to-hdr-scene-emb.safetensors"),
    # Qwen-Image 2.1
    "qwen_image_2.1_int8_convrot.safetensors": ("diffusion_models", QWEN, "diffusion_models/qwen_image_2.1_int8_convrot.safetensors"),
    "qwen_image_2.1_turbo_int8_convrot.safetensors":
        ("diffusion_models", QWEN, "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors"),
    "qwen_image_2.1_vae_bf16.safetensors": ("vae", QWEN, "vae/qwen_image_2.1_vae_bf16.safetensors"),
    "qwen3vl_8b_int8_convrot.safetensors": ("text_encoders", QWEN, "text_encoders/qwen3vl_8b_int8_convrot.safetensors"),
    "qwen3vl_8b_w4a8.safetensors": ("text_encoders", QWEN, "text_encoders/qwen3vl_8b_w4a8.safetensors"),
    "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors":
        ("text_encoders", QWEN, "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors"),
    "qwen3.5_9b_qwen_image_2.1_pe_i2i_w4a8.safetensors":
        ("text_encoders", QWEN, "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_i2i_w4a8.safetensors"),
    "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors":
        ("text_encoders", QWEN, "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors"),
    "qwen3.5_9b_qwen_image_2.1_pe_t2i_w4a8.safetensors":
        ("text_encoders", QWEN, "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i_w4a8.safetensors"),
    "qwen-image-2.1-outpaint-v2.safetensors":
        ("loras", "ausboss/Qwen-Image-2.1-Outpaint-LoRA", "qwen-image-2.1-outpaint-v2.safetensors"),
    "qwen-image-2.1-consistency.safetensors":
        ("loras", "ausboss/Qwen-Image-2.1-Consistency-LoRA", "qwen-image-2.1-consistency.safetensors"),
    "qwen-image-2.1-outfit-swap.safetensors":
        ("loras", "ausboss/Qwen-Image-2.1-Outfit-Swap-Consistency-LoRA", "qwen-image-2.1-outfit-swap.safetensors"),
    "Qwenimag21_c2-st2000.safetensors":
        ("loras", "RunningHubAI/rh-qwen-image-2.1-lora-2104918997757157378", "Qwenimag21_c2-st2000.safetensors"),
    # MiniMax H3 / FastH3
    "minimax_h3_fl2va_pruned_int8_convrot.safetensors":
        ("diffusion_models", H3, "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors"),
    "minimax_h3_fl2va_pruned_w6a8.safetensors": ("diffusion_models", H3, "diffusion_models/minimax_h3_fl2va_pruned_w6a8.safetensors"),
    "minimax_h3_ref2va_pruned_int8_convrot.safetensors":
        ("diffusion_models", H3, "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors"),
    "minimax_h3_ref2va_pruned_w6a8.safetensors": ("diffusion_models", H3, "diffusion_models/minimax_h3_ref2va_pruned_w6a8.safetensors"),
    "fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors":
        ("diffusion_models", "FastVideo/FastVideo-FastH3-Comfy", "diffusion_models/fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors"),
    "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors": ("text_encoders", H3, "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"),
    "qwen3vl_32b_minimax_h3_int8_convrot.safetensors": ("text_encoders", H3, "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors"),
    "minimax_h3_video_vae_int8_convrot.safetensors": ("vae", H3, "vae/minimax_h3_video_vae_int8_convrot.safetensors"),
    "minimax_h3_audio_vae_fp32.safetensors": ("vae", H3, "vae/minimax_h3_audio_vae_fp32.safetensors"),
    "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors":
        ("loras", H3, "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"),
    "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors":
        ("loras", H3, "loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"),
    "minimax_h3_vfx_edit_v1.0_r128_ffp.safetensors":
        ("loras", "Alissonerdx/Minimax-H3-ComfyUI", "loras/minimax_h3_vfx_edit_v1.0_r128_ffp.safetensors"),
    # Flux 3 Klein (the repo's own names use dashes; saved under the names the workflows load)
    "flux3_image_klein_base_int8_convrot.safetensors":
        ("diffusion_models", FLUX, "diffusion_models/flux3-image-klein-base-int8-convrot.safetensors"),
    "flux3_image_klein_sd_int8_convrot.safetensors":
        ("diffusion_models", FLUX, "diffusion_models/flux3-image-klein-int8-convrot.safetensors"),
    "qwen3vl_4b_int8_convrot.safetensors": ("text_encoders", FLUX, "text_encoders/qwen3vl_4b_int8_convrot.safetensors"),
    "qwen3.5_9b_flux3_prompt_upsampler_w4a8.safetensors":
        ("text_encoders", FLUX, "text_encoders/qwen3.5_9b_flux3_prompt_upsampler_w4a8.safetensors"),
    "flux2-vae.safetensors": ("vae", "Comfy-Org/flux2-dev", "split_files/vae/flux2-vae.safetensors"),
    # downloaded by their node pack on first use
    "video_depth_anything_vitl.pth": ("videodepthanything", "auto", None),
}

# Medium tier (about 24 GB cards): the same model in a smaller official quantisation.
MEDIUM = {
    "ltx-2.5-22b-distilled-transformer-bf16.safetensors": "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors",
    "gemma4-12b-with-proj-ltx-2.5-bf16.safetensors": "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
    "gemma4_e2b_it_bf16.safetensors": "gemma4_e2b_it_int8_convrot.safetensors",
    "qwen3vl_8b_int8_convrot.safetensors": "qwen3vl_8b_w4a8.safetensors",
    "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors": "qwen3.5_9b_qwen_image_2.1_pe_i2i_w4a8.safetensors",
    "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors": "qwen3.5_9b_qwen_image_2.1_pe_t2i_w4a8.safetensors",
    "minimax_h3_fl2va_pruned_int8_convrot.safetensors": "minimax_h3_fl2va_pruned_w6a8.safetensors",
    "minimax_h3_ref2va_pruned_int8_convrot.safetensors": "minimax_h3_ref2va_pruned_w6a8.safetensors",
}

# NVFP4 files run fast only on RTX 50 (Blackwell) cards; other cards get this instead.
NO_FP4 = {
    "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
}

# Custom node packs. "bundled" = shipped in this repo's comfyui_nodes folder.
PACKS = {
    "ComfyUI-OCIO": {"url": "https://github.com/SlavaSexton/ComfyUI-OCIO", "nodes": ["OCIORead", "OCIOWrite"]},
    "ComfyUI-LTXVideo": {"url": "https://github.com/Lightricks/ComfyUI-LTXVideo", "nodes": [
        "GemmaAPITextEncode", "LTXAddVideoICLoRAGuide", "LTXICLoRALoaderModelOnly", "LTXVGetTilingSizes",
        "LTXVHDRDecodePostprocess", "LTXVLoadConditioning", "LTXVSDRToHDRWorkingSpace", "LTXVSetAudioRefTokens",
        "LTXVTiledFusionSampler", "LTXVVAEForceFloat32"]},
    "ComfyUI-Video-Depth-Anything": {"url": "https://github.com/yuvraj108c/ComfyUI-Video-Depth-Anything", "nodes": [
        "LoadVideoDepthAnythingModel", "VideoDepthAnythingOutput", "VideoDepthAnythingProcess"]},
    "MiniMax-H3-Prompt-Rewriter-ComfyUI": {"url": "https://github.com/pytraveler/MiniMax-H3-Prompt-Rewriter-ComfyUI",
                                           "nodes": ["MiniMaxH3PromptWriter8B", "MiniMaxH3PromptWriterOmni"]},
    "comfyui-google-genmedia-custom-nodes": {
        "url": "https://github.com/GoogleCloudPlatform/comfyui-google-genmedia-custom-nodes",
        "nodes": ["Gemini31FlashImage", "Gemini3ProImage", "GeminiNode31", "Veo3ImageToVideoNode", "Veo3ReferenceToVideo"]},
    "comfyui_controlnet_aux": {"url": "https://github.com/Fannovel16/comfyui_controlnet_aux",
                               "nodes": ["DSINE-NormalMapPreprocessor"]},
    "ltx_plate_tools": {"bundled": True, "nodes": ["LTXPlatePrep", "LTXPlateRestore", "PlateAspectPad",
                                                     "PlateAspectPadGeminiPro", "VeoVideoFrames"]},
    "audio_fallback": {"bundled": True, "nodes": ["AudioOrSilence"]},
}

# Installer checklist groups, in order. A workflow not listed here goes under "Other".
FAMILIES = [
    ("LTX-2.5", ["ltx_alpha", "ltx_hdr", "ltx_hdr_tiled", "ltx_i2v_hdr", "ltx_refine", "ltx_layout"]),
    ("Qwen-Image 2.1", ["qwen_edit", "qwen_edit_turbo", "qwen_t2i", "qwen_t2i_turbo", "qwen_outpaint",
                        "qwen_bg_removal", "qwen_light_blend", "qwen_outfit_swap"]),
    ("MiniMax H3", ["minimax_h3_i2v", "fasth3_i2v", "minimax_h3_r2v", "minimax_h3_vfx_edit"]),
    ("Flux 3 Klein", ["flux3_klein", "flux3_klein_distilled"]),
    ("Google (Vertex AI, no local models)", ["nano_banana2", "nano_banana_pro", "veo31_i2v", "veo31_r2v", "gemini31_pro"]),
    ("Depth and normals", ["vda_depth", "dsine_normals"]),
]

MIN_COMFYUI = "0.41.0"


def _repo_files(repo, cache={}):
    if repo not in cache:
        url = "https://huggingface.co/api/models/{}/tree/main?recursive=1".format(repo)
        files = json.load(urllib.request.urlopen(url, timeout=60))
        info = json.load(urllib.request.urlopen("https://huggingface.co/api/models/" + repo, timeout=60))
        cache[repo] = ({f["path"]: f["size"] for f in files if f.get("type") == "file"}, bool(info.get("gated")))
    return cache[repo]


def main():
    models = {}
    for name, (folder, repo, path) in SOURCES.items():
        entry = {"folder": folder}
        if repo == "auto":
            entry["auto"] = True
        elif repo:
            sizes, gated = _repo_files(repo)
            if path not in sizes:
                sys.exit("{}: {} not in {}".format(name, path, repo))
            entry.update({"url": "https://huggingface.co/{}/resolve/main/{}".format(repo, path),
                          "page": "https://huggingface.co/" + repo, "size": sizes[path], "gated": gated})
        models[name] = entry
    for table in (MEDIUM, NO_FP4):
        for a, b in table.items():
            assert a in models and b in models, (a, b)

    # every model file and custom node the workflows use must be covered
    ext = (".safetensors", ".gguf", ".pth", ".ckpt", ".pt", ".bin")
    known_nodes = {n for p in PACKS.values() for n in p["nodes"]}
    for fn in sorted(os.listdir(WORKFLOWS)):
        if not fn.endswith(".api.json"):
            continue
        with open(os.path.join(WORKFLOWS, fn), encoding="utf-8") as f:
            graph = json.load(f)
        for node in graph.values():
            for val in node["inputs"].values():
                if isinstance(val, str) and val.endswith(ext) and val not in models:
                    sys.exit("{}: model {} has no entry in SOURCES".format(fn, val))
    catalog = {"min_comfyui": MIN_COMFYUI, "models": models, "medium": MEDIUM, "no_fp4": NO_FP4,
               "packs": PACKS, "families": [{"name": n, "workflows": ids} for n, ids in FAMILIES]}
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(catalog, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print("wrote", os.path.normpath(OUT), len(models), "models,", len(known_nodes), "custom nodes")


if __name__ == "__main__":
    main()
