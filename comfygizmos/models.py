"""Model tiers, the shared settings file and the setup check. No nuke in here: the installer uses it, and so
can other host apps (they copy PORTABLE and use read_prefs / is_picked / swap like the Nuke gizmos do).

catalog.json (written by tools/make_catalog.py) says where each model file comes from, its lighter
'medium' alternative and which pack each custom node is in. Which files a gizmo needs is read from its own
API graph, so the catalog never goes out of step with the workflows.

Settings, in ~/.nuke/comfygizmos_prefs.json (the installer writes them; without them nothing changes):
    "tier": "full" | "medium"   medium = smaller quantisations of the big models, for about 24 GB cards
    "fp4": true | false         false = the card can't run NVFP4 fast (older than RTX 50): use int8 instead
    "gizmos": [workflow ids]    only these appear in the menu (missing = all)
    "known": [workflow ids]     every workflow there was when the installer ran: ones added later are shown
                                (and ticked in the installer) until someone unticks them"""
import json
import os
import re

CATALOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.json")
# The one settings file every host app and the installer read (host, output folder, knob defaults, tier...).
PREFS_FILE = os.path.expanduser("~/.nuke/comfygizmos_prefs.json")
# The parts of this package that need no nuke: another host app copies these next to its own code.
PORTABLE = ("client.py", "colorspace.py", "models.py", "catalog.json", "workflows")
MODEL_EXT = (".safetensors", ".gguf", ".pth", ".ckpt", ".pt", ".bin", ".sft")
TIERS = ("full", "medium")
BLACKWELL = re.compile(r"RTX\s*(PRO\s*)?50\d\d|Blackwell|\bB[12]\d0\b|GB[12]\d0|RTX\s*PRO\s*\d+\s*Blackwell", re.I)

_catalog = None


def catalog():
    global _catalog
    if _catalog is None:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            _catalog = json.load(f)
    return _catalog


def read_prefs():
    """The settings file as a dict ({} if there is none or it doesn't parse)."""
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_new(prefs, wf_id):
    """A workflow added after the installer last ran (it didn't exist then, so nobody unticked it)."""
    known = prefs.get("known")
    return known is not None and wf_id not in known


def is_picked(prefs, wf_id):
    """Whether a workflow is shown: ticked in the installer, or new since (all are, until it has been run)."""
    picked = prefs.get("gizmos")
    return picked is None or wf_id in picked or is_new(prefs, wf_id)


def settings(prefs):
    """(tier, fp4) from a prefs dict; defaults = the files the workflows were built with."""
    tier = str(prefs.get("tier") or "full").lower()
    return (tier if tier in TIERS else "full"), bool(prefs.get("fp4", True))


def swap_table(tier="full", fp4=True):
    """{file the workflow loads: file to load instead} for this tier."""
    cat = catalog()
    table = {}
    if tier == "medium":
        table.update(cat["medium"])
    if not fp4:
        table.update(cat["no_fp4"])
    return table


def swap(prompt, tier="full", fp4=True):
    """Point the graph's model loaders at the tier's files (in place). Returns the prompt."""
    table = swap_table(tier, fp4)
    if not table:
        return prompt
    for node in prompt.values():
        inputs = node.get("inputs", {})
        for key, val in inputs.items():
            if isinstance(val, str) and val in table:
                inputs[key] = table[val]
    return prompt


def model_files(prompt):
    """[(node class, input name, file)] for every model file the graph loads."""
    out = []
    for node in prompt.values():
        for key, val in node.get("inputs", {}).items():
            if isinstance(val, str) and val.lower().endswith(MODEL_EXT):
                out.append((node["class_type"], key, val))
    return out


def pack_of(node_class):
    for name, pack in catalog()["packs"].items():
        if node_class in pack["nodes"]:
            return name
    return None                                          # part of ComfyUI itself


def needs(prompt):
    """(sorted pack names, sorted model files) a graph needs - pass the swapped graph."""
    packs = {pack_of(n["class_type"]) for n in prompt.values()} - {None}
    files = sorted({f for _, _, f in model_files(prompt)})
    return sorted(packs), files


def file_size(name):
    return catalog()["models"].get(name, {}).get("size", 0)


def version_tuple(text):
    return tuple(int(x) for x in re.findall(r"\d+", str(text or ""))[:3]) or (0,)


def version_ok(version):
    return version_tuple(version) >= version_tuple(catalog()["min_comfyui"])


def gpu_advice(name, vram_bytes):
    """(suggested tier, fp4 ok) for a GPU name and its memory in bytes."""
    fp4 = bool(BLACKWELL.search(name or ""))
    tier = "full" if (vram_bytes or 0) >= 30 * 1024 ** 3 else "medium"
    return tier, fp4


def gpu_from_stats(stats):
    """(name, vram bytes) of the first GPU in ComfyUI's /system_stats, or ('', 0)."""
    for dev in (stats or {}).get("devices") or []:
        if dev.get("type") in ("cuda", "xpu", "mps") or "cuda" in str(dev.get("name", "")):
            name = re.sub(r"^\S+:\d+\s*", "", str(dev.get("name", ""))).split(" : ")[0].strip()
            return name, int(dev.get("vram_total") or 0)
    return "", 0


def _combo(spec):
    """The allowed values of a combo input spec from /object_info, or None if it isn't a list."""
    if not spec:
        return None
    if isinstance(spec[0], list):
        return spec[0]
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("options")
    return None


def check(object_info, stats, graphs, tier="full", fp4=True, aces_ok=None):
    """What's missing in a running ComfyUI for each workflow.

    object_info: ComfyUI's /object_info. stats: its /system_stats. graphs: {workflow id: API graph}.
    aces_ok: whether its OCIO config has ACES2065-1 (None = not checked).
    Returns {"version", "version_ok", "gpu", "vram", "advice": (tier, fp4), "aces_ok",
             "workflows": {id: {"packs": [missing pack], "models": [missing file], "ready": bool}}}."""
    system = (stats or {}).get("system") or {}
    version = system.get("comfyui_version", "")
    gpu, vram = gpu_from_stats(stats)
    result = {"version": version, "version_ok": version_ok(version), "gpu": gpu, "vram": vram,
              "advice": gpu_advice(gpu, vram), "aces_ok": aces_ok, "workflows": {}}
    for wf_id, graph in graphs.items():
        graph = swap(json.loads(json.dumps(graph)), tier, fp4)
        packs, models = set(), []
        for node in graph.values():
            cls = node["class_type"]
            if cls not in object_info:
                packs.add(pack_of(cls) or "ComfyUI {} or newer ({})".format(catalog()["min_comfyui"], cls))
        for cls, key, name in model_files(graph):
            info = object_info.get(cls)
            if not info:
                continue                                 # its pack is missing: reported above
            spec = (info.get("input", {}).get("required", {}).get(key)
                    or info.get("input", {}).get("optional", {}).get(key))
            allowed = _combo(spec)
            if allowed is not None and name not in allowed and name not in models:
                if not catalog()["models"].get(name, {}).get("auto"):
                    models.append(name)
        result["workflows"][wf_id] = {"packs": sorted(packs), "models": models,
                                      "ready": not packs and not models}
    return result


def source(name):
    """(download url or None, page url or None, gated, models sub-folder) for a model file."""
    m = catalog()["models"].get(name, {})
    return m.get("url"), m.get("page"), bool(m.get("gated")), m.get("folder", "")
