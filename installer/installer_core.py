"""ComfyGizmos installer - the work, without a window (installer_gui.py is the window).

Finds ComfyUI, works out what the picked gizmos need at the picked model size, and installs it:
the node packs (bundled ones copied, others cloned + their requirements pip-installed with ComfyUI's own
Python), the model files (downloaded from Hugging Face into ComfyUI's models folders), the Nuke gizmos (plus
the menu.py lines) and any optional add-ons for other apps found in the repo (see extras()). Only Python's
standard library is used here."""
import datetime
import glob
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "comfygizmos")
WORKFLOW_DIR = os.path.join(PKG, "workflows")
BUNDLED_NODES = os.path.join(REPO, "comfyui_nodes")
NUKE_DIR = os.path.expanduser("~/.nuke")
DEFAULT_HOST = "http://127.0.0.1:8188"
# where ComfyUI keeps a kind of model, besides models/<kind> (older folder names still loaded)
FOLDER_ALIASES = {"diffusion_models": ["diffusion_models", "unet"], "text_encoders": ["text_encoders", "clip"]}
MENU_START, MENU_END = "# >>> comfygizmos (added by the ComfyGizmos installer)", "# <<< comfygizmos"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


models = _load("comfygizmos_models", os.path.join(PKG, "models.py"))   # the package itself needs nuke
PREFS_FILE = models.PREFS_FILE


# --------------------------------------------------------------------------- what there is to install

def workflows():
    """[(id, label, API graph)] of every gizmo, in checklist order."""
    found = {}
    for fn in sorted(os.listdir(WORKFLOW_DIR)):
        if fn.endswith(".json") and not fn.endswith(".api.json"):
            with open(os.path.join(WORKFLOW_DIR, fn), encoding="utf-8") as f:
                wf = json.load(f)
            api = os.path.join(WORKFLOW_DIR, wf["api"])
            if os.path.isfile(api):
                with open(api, encoding="utf-8") as f:
                    found[wf["id"]] = (wf["label"], json.load(f))
    return found


def families():
    """[(family name, [workflow ids])], every gizmo in exactly one family."""
    wfs = workflows()
    out, seen = [], set()
    for fam in models.catalog()["families"]:
        ids = [i for i in fam["workflows"] if i in wfs]
        seen.update(ids)
        if ids:
            out.append((fam["name"], ids))
    rest = sorted(i for i in wfs if i not in seen)
    if rest:
        out.append(("Other", rest))
    return out


def requirements(wf_ids, tier, fp4):
    """{"packs": {pack: [ids]}, "models": {file: [ids]}} for the picked gizmos at this model size."""
    wfs = workflows()
    packs, files = {}, {}
    for wf_id in wf_ids:
        graph = models.swap(json.loads(json.dumps(wfs[wf_id][1])), tier, fp4)
        p, m = models.needs(graph)
        for name in p:
            packs.setdefault(name, []).append(wf_id)
        for name in m:
            files.setdefault(name, []).append(wf_id)
    return {"packs": packs, "models": files}


# --------------------------------------------------------------------------- finding ComfyUI

def _get(host, path, timeout=5):
    with urllib.request.urlopen(host.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def server_info(host=DEFAULT_HOST):
    """(system_stats, folder_paths) from a running ComfyUI, or (None, None)."""
    try:
        return _get(host, "/system_stats"), _get(host, "/internal/folder_paths")
    except (OSError, ValueError, urllib.error.URLError):
        return None, None


def is_comfyui(folder):
    return bool(folder) and os.path.isdir(os.path.join(folder, "custom_nodes")) and \
        os.path.isdir(os.path.join(folder, "models"))


def find_comfyui(folder_paths=None):
    """The ComfyUI folder (the one with custom_nodes and models): the running one, else a usual place."""
    if folder_paths:
        for path in folder_paths.get("custom_nodes") or []:
            root = os.path.dirname(os.path.normpath(path))
            if is_comfyui(root):
                return root
    home = os.path.expanduser("~")
    for cand in (r"C:\ComfyUI_windows_portable\ComfyUI", os.path.join(home, "ComfyUI_windows_portable", "ComfyUI"),
                 os.path.join(home, "ComfyUI"), os.path.join(home, "Documents", "ComfyUI"),
                 os.path.join(home, "comfy", "ComfyUI")):
        if is_comfyui(cand):
            return cand
    return ""


def find_python(root):
    """ComfyUI's own Python (node pack requirements go there), or ''."""
    if not root:
        return ""
    parent = os.path.dirname(os.path.normpath(root))
    for cand in (os.path.join(parent, "python_embeded", "python.exe"),     # Windows portable
                 os.path.join(root, ".venv", "Scripts", "python.exe"), os.path.join(root, "venv", "Scripts", "python.exe"),
                 os.path.join(root, ".venv", "bin", "python"), os.path.join(root, "venv", "bin", "python"),
                 os.path.join(parent, ".venv", "Scripts", "python.exe"), os.path.join(parent, "venv", "bin", "python")):
        if os.path.isfile(cand):
            return cand
    return ""


def comfy_version(root, stats=None):
    version = ((stats or {}).get("system") or {}).get("comfyui_version")
    if version:
        return version
    try:
        with open(os.path.join(root, "comfyui_version.py"), encoding="utf-8") as f:
            m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", f.read())
        return m.group(1) if m else ""
    except OSError:
        return ""


def gpu(stats=None):
    """(name, memory in bytes) of the GPU: from the running ComfyUI, else nvidia-smi, else ('', 0)."""
    name, vram = models.gpu_from_stats(stats)
    if name:
        return name, vram
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, **_no_window()).stdout
        first = out.strip().splitlines()[0]
        name, mib = [x.strip() for x in first.split(",")]
        return name, int(float(mib)) * 1024 ** 2
    except (OSError, IndexError, ValueError, subprocess.SubprocessError):
        return "", 0


def model_dirs(root, folder_paths=None):
    """{models sub-folder: [folders ComfyUI looks in]}."""
    dirs = {}
    for folder in {m["folder"] for m in models.catalog()["models"].values()}:
        found = list((folder_paths or {}).get(folder) or [])
        for alias in FOLDER_ALIASES.get(folder, [folder]):
            path = os.path.join(root, "models", alias)
            if path not in found:
                found.append(path)
        dirs[folder] = found
    return dirs


def present(root, folder_paths=None):
    """Model file names already in ComfyUI's model folders (any sub-folder)."""
    names = set()
    for paths in model_dirs(root, folder_paths).values():
        for path in paths:
            for _, _, files in os.walk(path):
                names.update(files)
    return names


def packs_present(root):
    folder = os.path.join(root, "custom_nodes")
    have = {n.lower() for n in os.listdir(folder)} if os.path.isdir(folder) else set()
    out = set()
    for name, pack in models.catalog()["packs"].items():
        repo = pack.get("url", "").rstrip("/").rsplit("/", 1)[-1].lower()
        if name.lower() in have or (repo and repo in have) or ("local_" + name).lower() in have:
            out.add(name)
    return out


# --------------------------------------------------------------------------- installing

def _no_window():
    return {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}   # no console flashing up


def copy_bundled_pack(name, root, log):
    src = os.path.join(BUNDLED_NODES, name)
    dst = os.path.join(root, "custom_nodes", name)
    local = os.path.join(root, "custom_nodes", "local_" + name)      # an earlier hand-made copy: leave it be
    if os.path.isdir(local) and not os.path.isdir(dst):
        log("{}: already installed as {}".format(name, os.path.basename(local)))
        return
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=IGNORE)
    log("{}: copied to {}".format(name, dst))


def install_pack(name, root, python, log):
    """Clone a node pack into custom_nodes (git, else GitHub's zip) and pip-install its requirements."""
    url = models.catalog()["packs"][name]["url"]
    dst = os.path.join(root, "custom_nodes", url.rstrip("/").rsplit("/", 1)[-1])
    if not os.path.isdir(dst):
        git = shutil.which("git")
        if git:
            log("{}: git clone {}".format(name, url))
            r = subprocess.run([git, "clone", "--depth", "1", url, dst], capture_output=True, text=True, **_no_window())
            if r.returncode:
                raise RuntimeError("git clone failed: " + (r.stderr or r.stdout).strip()[-400:])
        else:
            owner_repo = url.split("github.com/", 1)[1].strip("/")
            branch = _get("https://api.github.com", "/repos/" + owner_repo, timeout=30)["default_branch"]
            log("{}: downloading {} ({})".format(name, url, branch))
            data = urllib.request.urlopen("https://github.com/{}/archive/refs/heads/{}.zip".format(owner_repo, branch),
                                          timeout=120).read()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                top = z.namelist()[0].split("/")[0]
                z.extractall(os.path.join(root, "custom_nodes"))
            os.rename(os.path.join(root, "custom_nodes", top), dst)
    req = os.path.join(dst, "requirements.txt")
    if os.path.isfile(req):
        if not python:
            log("{}: pip-install {} with ComfyUI's Python yourself (its Python wasn't found)".format(name, req))
            return
        log("{}: installing its Python requirements".format(name))
        r = subprocess.run([python, "-s", "-m", "pip", "install", "-r", req], capture_output=True, text=True,
                           **_no_window())
        if r.returncode:
            raise RuntimeError("pip install failed: " + (r.stderr or r.stdout).strip()[-600:])


def download(name, root, token, log, progress, cancelled):
    """One model file into models/<folder>/<name> (a .part file until it is complete)."""
    url, page, gated, folder = models.source(name)
    dst_dir = os.path.join(root, "models", folder)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, name)
    part = dst + ".part"
    headers = {"User-Agent": "ComfyGizmos-installer"}
    if token:
        headers["Authorization"] = "Bearer " + token
    done = os.path.getsize(part) if os.path.isfile(part) else 0
    if done:
        headers["Range"] = "bytes={}-".format(done)                    # carry on an interrupted download
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("Hugging Face refused it (HTTP {}). Accept the licence at {} while signed in, "
                               "and give the installer a Hugging Face token.".format(e.code, page))
        if e.code == 416:                                              # the .part is already whole
            os.replace(part, dst)
            return
        raise
    total = models.file_size(name) or (done + int(resp.headers.get("Content-Length") or 0))
    mode = "ab" if done and resp.status == 206 else "wb"
    done = done if mode == "ab" else 0
    log("{}: downloading {:.1f} GB".format(name, total / 1e9))
    with resp, open(part, mode) as f:
        while True:
            if cancelled():
                raise RuntimeError("cancelled - run the installer again to carry on")
            chunk = resp.read(4 * 1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            progress(name, done, total)
    os.replace(part, dst)


def install_nuke(target, log):
    """The gizmos into <target>/comfygizmos, and the lines in ~/.nuke/menu.py that load them."""
    dst = os.path.join(target, "comfygizmos")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(PKG, dst, ignore=IGNORE)
    log("Nuke: gizmos copied to " + dst)
    menu = os.path.join(NUKE_DIR, "menu.py")
    text = ""
    if os.path.isfile(menu):
        with open(menu, encoding="utf-8", errors="replace") as f:
            text = f.read()
    if "comfygizmos.register_menu" in text and MENU_START not in text:
        log("Nuke: menu.py already loads comfygizmos - left as it is")
        return
    block = "\n".join([
        MENU_START,
        "try:",
        "    import sys as _sys",
        "    if {!r} not in _sys.path:".format(os.path.normpath(target)),
        "        _sys.path.append({!r})".format(os.path.normpath(target)),
        "    import comfygizmos",
        "    comfygizmos.register_menu()   # Nodes > ComfyUI",
        "except Exception as _err:",
        "    nuke.tprint('comfygizmos failed to load: %s' % _err)",
        MENU_END])
    if MENU_START in text:
        text = re.sub(re.escape(MENU_START) + r".*?" + re.escape(MENU_END), lambda m: block, text, flags=re.S)
    else:
        if os.path.isfile(menu):
            backup = menu + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(menu, backup)
            log("Nuke: menu.py backed up to " + backup)
        if "import nuke" not in text:
            text = "import nuke\n" + text
        text = text.rstrip("\n") + "\n\n" + block + "\n"
    os.makedirs(NUKE_DIR, exist_ok=True)
    with open(menu, "w", encoding="utf-8") as f:
        f.write(text)
    log("Nuke: menu.py loads the gizmos")


def extras():
    """Optional installers for other host apps: <repo>/<folder>/install_<name>.py with a LABEL and install().
    They live in their own folders (and branches), so this installer never needs editing for them."""
    found = []
    for path in sorted(glob.glob(os.path.join(REPO, "*", "install_*.py"))):
        if os.path.basename(os.path.dirname(path)) in ("installer", "tools", "tests"):
            continue
        try:
            mod = _load("comfygizmos_" + os.path.splitext(os.path.basename(path))[0], path)
        except Exception:                                               # a broken hook must not stop the installer
            continue
        if hasattr(mod, "install") and hasattr(mod, "LABEL"):
            found.append(mod)
    return found


def install_extra(mod, log):
    log("{}: installed to {}".format(mod.LABEL.split(" (")[0].replace("Also install the ", ""), mod.install()))


def read_prefs():
    return models.read_prefs()


def write_prefs(wf_ids, tier, fp4, log):
    """Keeps every other setting (host, output folder, knob defaults)."""
    data = read_prefs()
    data.update({"gizmos": sorted(wf_ids), "known": sorted(workflows()), "tier": tier, "fp4": bool(fp4)})
    os.makedirs(os.path.dirname(PREFS_FILE), exist_ok=True)
    with open(PREFS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log("Settings saved to " + PREFS_FILE)
