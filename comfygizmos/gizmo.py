"""ComfyUI workflows as Nuke nodes.

Each workflow in ./workflows (a manifest + an API-format graph) becomes a Group
node. "Render in ComfyUI" sends the upstream plate to ComfyUI, waits in a
background thread, then loads the result into the node's own Read - so the
node's output is the ComfyUI result and ComfyUI only has to be running."""
import copy
import glob
import json
import os
import re
import threading
import time
import uuid

import nuke

from . import client, colorspace

WORKFLOW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows")
PREFS_FILE = os.path.expanduser("~/.nuke/comfygizmos_prefs.json")
DEFAULT_PREFS = {
    "host": client.DEFAULT_HOST,
    "output_subpath": "Comp/Inputs/ComfyUI",   # under the shot root (project dir, else one up from the .nk)
}
MOVIE_EXT = {"mov", "mp4", "m4v", "avi", "mkv", "webm", "mxf"}
FRAME_TOKEN = re.compile(r"%0?\d*d|#+|@+")
POLL_SECONDS = 2.0
# Output root knob text that stands for the evaluated project directory.
PROJECT_TOKEN = "[value root.project_directory]"
# What a Read set to "default" (or to an OCIO role) means in our scripts.
DEFAULT_COLORSPACE = "ACES - ACES2065-1"

_jobs = {}   # node full name -> job dict, while a render is in flight
_syncing = False   # True while the version is set from the input (not typed by the user)


# --------------------------------------------------------------------------- workflows / prefs

def workflows():
    """{id: manifest} for every workflows/*.json that has its API graph next to it."""
    found = {}
    for path in sorted(glob.glob(os.path.join(WORKFLOW_DIR, "*.json"))):
        if path.endswith(".api.json"):
            continue
        with open(path, encoding="utf-8") as f:
            wf = json.load(f)
        if os.path.isfile(os.path.join(WORKFLOW_DIR, wf["api"])):
            found[wf["id"]] = wf
    return found


def _api_graph(wf):
    with open(os.path.join(WORKFLOW_DIR, wf["api"]), encoding="utf-8") as f:
        return json.load(f)


def prefs():
    data = dict(DEFAULT_PREFS)
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            data.update(json.load(f))
    except (OSError, ValueError):
        pass
    return data


def register_menu():
    """Nodes > ComfyUI > <one entry per workflow>."""
    menu = nuke.menu("Nodes").addMenu("ComfyUI")
    for wf_id, wf in sorted(workflows().items(), key=lambda kv: kv[1]["label"]):
        menu.addCommand(wf["label"], "comfygizmos.create({!r})".format(wf_id))


# --------------------------------------------------------------------------- building the node

def create(wf_id):
    wf = workflows()[wf_id]
    group = nuke.createNode("Group", inpanel=False)
    group.setName("Comfy_" + wf["suffix"])
    group["tile_color"].setValue(0x3F5F8AFF)
    _build_internals(group, wf)
    _add_knobs(group, wf)
    group["label"].setValue("v[format %03d [value cg_version]]")
    group["knobChanged"].setValue(
        "try:\n    import comfygizmos\n    comfygizmos.knob_changed(nuke.thisNode(), nuke.thisKnob())\n"
        "except ImportError:\n    pass\n")
    sync_version(group)
    group.showControlPanel()
    return group


def knob_changed(group, knob):
    """Group callback: keep the version in step with the input while 'from input' is on."""
    if knob.name() in ("inputChange", "cg_version_auto", "showPanel"):
        sync_version(group)
    elif knob.name() == "cg_version" and not _syncing:
        group["cg_version_auto"].setValue(False)        # typed by hand: keep it


def _build_internals(group, wf):
    group.begin()
    try:
        inp = nuke.nodes.Input(name="Input")
        src = nuke.nodes.Write(name="SourceWrite", inputs=[inp], file_type="exr", disable=True)
        src["channels"].setValue("rgb")
        src["datatype"].setValue("16 bit half")
        _set_enum(src, "compression", "Zip (1 scanline)")
        res = nuke.nodes.Read(name="Result")

        if wf["output"]["kind"] == "matte":
            # matte -> rgba (alpha = red), or the input with the matte as its alpha
            matte = nuke.nodes.Copy(name="MatteToAlpha", inputs=[res, res])
            input_matte = nuke.nodes.Copy(name="InputWithMatte", inputs=[inp, res])
            for c in (matte, input_matte):
                c["from0"].setValue("rgba.red")
                c["to0"].setValue("rgba.alpha")
            choices = [inp, matte, input_matte]           # 0 = no result yet (pass through)
        else:
            choices = [inp, res]
        switch = nuke.nodes.Switch(name="OutputSwitch")
        for i, node in enumerate(choices):
            switch.setInput(i, node)
        switch["which"].setValue(0)
        nuke.nodes.Output(inputs=[switch])
    finally:
        group.end()


def _set_enum(node, knob, value):
    try:
        node[knob].setValue(value)
    except Exception:
        pass


def _add_knobs(group, wf):
    k = group.addKnob
    k(nuke.Tab_Knob("comfy", "ComfyUI"))
    t = nuke.Text_Knob("cg_title", "", "<b>{}</b><br>{}".format(wf["label"], wf.get("description", "")))
    k(t)
    wid = nuke.String_Knob("cg_workflow", "workflow", wf["id"])
    wid.setFlag(nuke.INVISIBLE)
    k(wid)

    k(nuke.Text_Knob("cg_div_in", "input"))
    src = nuke.Enumeration_Knob("cg_source", "source", ["auto", "render input"])
    src.setTooltip("auto: a Read connected straight in is used as-is (no render). Anything else - or "
                   "'render input' - is rendered to EXR first and that is sent.")
    k(src)
    reuse = nuke.Boolean_Knob("cg_reuse_input", "reuse unchanged input", True)
    reuse.setTooltip("When the input has to be rendered: skip that render if nothing upstream changed since "
                     "the last one (node settings, connections, frame range, Read files on disk) and the "
                     "_source frames are still there. Untick to always re-render the input.")
    reuse.clearFlag(nuke.STARTLINE)
    k(reuse)
    use_in = nuke.Boolean_Knob("cg_input_range", "input range", True)
    use_in.setFlag(nuke.STARTLINE)
    k(use_in)
    first = nuke.Int_Knob("cg_first", "first")
    last = nuke.Int_Knob("cg_last", "last")
    first.clearFlag(nuke.STARTLINE)
    last.clearFlag(nuke.STARTLINE)
    root = nuke.root()
    first.setValue(int(root.firstFrame()))
    last.setValue(int(root.lastFrame()))
    k(first)
    k(last)
    if wf.get("max_frames"):
        mf = nuke.Int_Knob("cg_max_frames", "max frames")
        mf.setValue(int(wf["max_frames"]))
        mf.setTooltip("Longest range this node will send (default {}). {}".format(
            wf["max_frames"], wf.get("max_frames_input", {}).get("tooltip", "")).strip())
        k(mf)
    cs = nuke.String_Knob("cg_colorspace", "colorspace override")
    cs.setTooltip("Leave empty: worked out from the Read / the working space. Otherwise a ComfyUI-OCIO "
                  "colorspace name, e.g. 'ACES - ACEScg'.")
    k(cs)

    if wf.get("knobs"):
        k(nuke.Text_Knob("cg_div_wf", "settings"))
    for spec in wf.get("knobs", []):
        k(_make_knob(spec))

    k(nuke.Text_Knob("cg_div_out", "output"))
    out_root = nuke.File_Knob("cg_out_root", "output root")
    out_root.setTooltip("Follows the script: {} is the evaluated project directory (one folder up from the "
                        ".nk when it is empty). A plain path works too.".format(PROJECT_TOKEN))
    k(out_root)
    out_root.setValue(_default_output_root())
    suffix = nuke.String_Knob("cg_suffix", "suffix", wf["suffix"])
    suffix.setTooltip("Output = <plate name>_<suffix>/<plate name>_<suffix>_<colorspace>.####.exr")
    k(suffix)
    out_cs = nuke.String_Knob("cg_out_cs", "output colorspace", _default_out_cs(wf))
    out_cs.setTooltip("Colorspace of the written EXRs and of the Read that loads them. 'raw' = untagged."
                      + ("" if wf["output"].get("convert", True) else
                         " This workflow's output is data (a matte): values are written untouched, only "
                         "labelled with this colorspace."))
    k(out_cs)
    ver = nuke.Int_Knob("cg_version", "version")
    ver.setValue(1)
    ver.setTooltip("Output version. From input: a Read connected straight in gives its version (v002 -> 2); "
                   "anything else gives 1. It replaces the version in the plate name: <plate ..._v002>_<suffix>.")
    k(ver)
    auto = nuke.Boolean_Knob("cg_version_auto", "from input", True)
    auto.clearFlag(nuke.STARTLINE)
    auto.setTooltip("On: version follows the input. Version up (or typing a version) turns it off.")
    k(auto)
    up_btn = nuke.PyScript_Knob("cg_version_up", "Version up", "comfygizmos.version_up(nuke.thisNode())")
    up_btn.clearFlag(nuke.STARTLINE)
    k(up_btn)
    if wf["output"]["kind"] == "matte":
        mode = nuke.Enumeration_Knob("cg_output", "output", ["matte", "input + matte alpha"])
        mode.setTooltip("matte: rgba = the matte. input + matte alpha: the input with the matte as its alpha.")
        k(mode)
    host = nuke.String_Knob("cg_host", "ComfyUI", prefs()["host"])
    k(host)

    k(nuke.Text_Knob("cg_div_run", ""))
    render = nuke.PyScript_Knob("cg_render", "Render in ComfyUI", "comfygizmos.render(nuke.thisNode())")
    render.setFlag(nuke.STARTLINE)
    k(render)
    k(nuke.PyScript_Knob("cg_cancel", "Cancel", "comfygizmos.cancel(nuke.thisNode())"))
    k(nuke.PyScript_Knob("cg_reload", "Reload result", "comfygizmos.load_result(nuke.thisNode())"))
    k(nuke.PyScript_Knob("cg_bring_in", "Bring in output", "comfygizmos.read_output(nuke.thisNode())"))
    k(nuke.Text_Knob("cg_status", "status", "not rendered"))
    for name in ("cg_prompt_id", "cg_result", "cg_result_first", "cg_result_last", "cg_result_cs",
                 "cg_source_fp", "cg_source_cs", "cg_version_pad"):
        hidden = nuke.String_Knob(name, name)
        hidden.setFlag(nuke.INVISIBLE)
        k(hidden)


def _default_out_cs(wf):
    out = wf["output"]
    return "raw" if out.get("raw") else out.get("colorspace", DEFAULT_COLORSPACE)


def _make_knob(spec):
    name, label, kind = "wf_" + spec["name"], spec.get("label", spec["name"]), spec["type"]
    if kind == "multiline":
        knob = nuke.Multiline_Eval_String_Knob(name, label, str(spec.get("default", "")))
    elif kind == "int":
        knob = nuke.Int_Knob(name, label)
        knob.setValue(int(spec.get("default", 0)))
    elif kind == "float":
        knob = nuke.Double_Knob(name, label)
        knob.setRange(float(spec.get("min", 0.0)), float(spec.get("max", 1.0)))
        knob.setValue(float(spec.get("default", 0.0)))
    elif kind == "bool":
        knob = nuke.Boolean_Knob(name, label, bool(spec.get("default", False)))
        knob.setFlag(nuke.STARTLINE)
    else:
        knob = nuke.String_Knob(name, label, str(spec.get("default", "")))
    if spec.get("tooltip"):
        knob.setTooltip(spec["tooltip"])
    return knob


# --------------------------------------------------------------------------- paths / plate

def _abs(path):
    path = (path or "").replace("\\", "/")
    if not path or os.path.isabs(path) or re.match(r"^[A-Za-z]:/", path) or path.startswith("//"):
        return path
    base = _project_dir() or _script_dir()
    return os.path.normpath(os.path.join(base, path)).replace("\\", "/") if base else ""


def _project_dir():
    try:
        return (nuke.root()["project_directory"].evaluate() or "").replace("\\", "/").rstrip("/")
    except Exception:
        return ""


def _script_dir():
    name = nuke.root().name()
    if not name or name == "Root":
        return ""
    return os.path.dirname(os.path.abspath(name)).replace("\\", "/")


def _default_output_root():
    """What the knob shows: a reference to the project directory, not a fixed path."""
    return "{}/{}".format(PROJECT_TOKEN, prefs()["output_subpath"].strip("/"))


def _shot_root():
    """Evaluated project directory, else one folder up from the .nk ('' if neither)."""
    return _project_dir() or (os.path.dirname(_script_dir()) if _script_dir() else "")


def _output_root(group):
    """The knob's folder, resolved. An empty knob gets the default reference back."""
    knob = group["cg_out_root"]
    text = knob.value().strip()
    if not text:
        text = _default_output_root()
        knob.setValue(text)
    if PROJECT_TOKEN in text:
        # project_directory often holds an expression itself - use its evaluated value
        base = _shot_root()
        if not base:
            return ""
        text = text.replace(PROJECT_TOKEN, base)
        return _abs(text).rstrip("/") if "[" not in text else _abs(nuke.tcl("subst", text)).rstrip("/")
    return _abs(knob.evaluate().strip()).rstrip("/")


def _read_path(read):
    """A Read's file with expressions expanded but the frame token kept."""
    raw = (read["file"].value() or "").strip().replace("\\", "/")
    if "[" in raw or "$" in raw:
        try:
            expanded = (nuke.filename(read) or "").replace("\\", "/")
            if expanded and (not FRAME_TOKEN.search(raw) or FRAME_TOKEN.search(expanded)):
                raw = expanded
        except Exception:
            pass
    return _abs(raw)


_VERSION = re.compile(r"(?<![A-Za-z0-9])([vV])(\d{1,4})(?![A-Za-z0-9])")


def find_version(name):
    """Last 'v###' token in a name -> (number, digits) or None. 'shot_v002' -> (2, 3)."""
    hits = list(_VERSION.finditer(name or ""))
    return (int(hits[-1].group(2)), len(hits[-1].group(2))) if hits else None


def versioned(name, version, pad=3):
    """Put version into name: replaces its last v### (keeping that padding), else appends _v###."""
    hits = list(_VERSION.finditer(name or ""))
    if hits:
        m = hits[-1]
        return name[:m.start(2)] + str(version).zfill(len(m.group(2))) + name[m.end(2):]
    return "{}_v{}".format(name, str(version).zfill(pad)) if name else "v" + str(version).zfill(pad)


def _input_version(group):
    """(version, padding) of the input: a Read's file (name, else folder) - anything else is 1."""
    up = group.input(0)
    if up is not None and up.Class() == "Read":
        path = _read_path(up)
        found = find_version(plate_name(path)) or find_version(os.path.basename(os.path.dirname(path)))
        if found:
            return found
    return 1, 3


def sync_version(group):
    if "cg_version_auto" not in group.knobs() or not group["cg_version_auto"].value():
        return
    global _syncing
    version, pad = _input_version(group)
    _syncing = True
    try:
        group["cg_version"].setValue(version)
        group["cg_version_pad"].setValue(str(pad))
    finally:
        _syncing = False


def version_up(group):
    group["cg_version_auto"].setValue(False)
    group["cg_version"].setValue(int(group["cg_version"].value()) + 1)   # knob_changed: auto already off
    _set_status(group, "version up -> v{:03d} (no longer follows the input)".format(int(group["cg_version"].value())))


def plate_name(path):
    """'.../SH0400_plate_v002.%04d.exr' -> 'SH0400_plate_v002' (movies keep their whole name)."""
    name = os.path.basename(path.replace("\\", "/"))
    name = re.sub(r"\.[^.]+$", "", name)
    return re.sub(r"[._-]?(%0?\d*d|#+|@+)$", "", name)


def _upstream_read(node):
    """Nearest Read above node (for naming when the input has to be rendered)."""
    seen, todo = set(), [node]
    while todo:
        n = todo.pop(0)
        if n is None or n.fullName() in seen:
            continue
        seen.add(n.fullName())
        if n.Class() == "Read":
            return n
        todo.extend(n.dependencies(nuke.INPUTS | nuke.HIDDEN_INPUTS))
    return None


def _direct_read(group):
    """The connected Read when it can be sent as-is: file numbers = Nuke frame numbers."""
    up = group.input(0)
    if up is None or up.Class() != "Read" or up["disable"].value():
        return None
    if (up["frame"].value() or "").strip() not in ("", "0"):
        return None                                  # frame offset / expression -> numbers differ
    return up


# --------------------------------------------------------------------------- render

def render(group, wait=False):
    """Send the input to ComfyUI. wait=True blocks until done (scripts / tests)."""
    try:
        job = _prepare(group)
    except _Abort as e:
        if str(e):
            _set_status(group, "not sent: " + str(e))
            if nuke.GUI:
                nuke.message("{}:\n\n{}".format(group.name(), e))
        return None
    except client.ComfyError as e:
        _set_status(group, "ComfyUI error")
        if nuke.GUI:
            nuke.message("{} - ComfyUI error:\n\n{}".format(group.name(), e))
        raise
    if wait:
        _poll(job, in_main=True)
        return job
    threading.Thread(target=_poll, args=(job,), daemon=True).start()
    return job


class _Abort(Exception):
    pass


def _prepare(group):
    wf = workflows()[group["cg_workflow"].value()]
    host = group["cg_host"].value().strip() or client.DEFAULT_HOST
    if group.fullName() in _jobs:
        raise _Abort("a render is already running - Cancel it first")
    if group.input(0) is None:
        raise _Abort("nothing connected to the input")
    if not client.is_up(host):
        raise _Abort("ComfyUI is not running at " + host)

    # frame range
    if group["cg_input_range"].value():
        first, last = int(group.input(0).firstFrame()), int(group.input(0).lastFrame())
    else:
        first, last = int(group["cg_first"].value()), int(group["cg_last"].value())
    count = last - first + 1
    if count < 1:
        raise _Abort("empty frame range {}-{}".format(first, last))
    max_frames = _max_frames(group, wf)
    if max_frames and count > max_frames:
        raise _Abort("{} frames ({}-{}) - 'max frames' is {}. Raise it, split the shot or set a "
                     "shorter range.".format(count, first, last, max_frames))

    # source: the Read itself, or a render of the input
    read = _direct_read(group) if group["cg_source"].value() == "auto" else None
    if read is not None:
        src_path = _read_path(read)
        if not src_path:
            raise _Abort("the Read's path could not be made absolute - save the script first")
        raw = bool(read["raw"].value())
        nuke_cs = "" if raw else read["colorspace"].value()
        name = plate_name(src_path)
    else:
        up = _upstream_read(group.input(0))
        name = plate_name(_read_path(up)) if up is not None else group.name()
        src_path, nuke_cs, raw = None, None, False

    suffix = group["cg_suffix"].value().strip() or wf["suffix"]
    out_root = _output_root(group)
    if not out_root:
        raise _Abort("no output root: set the project directory, save the script, or type a path in "
                     "'output root'")
    sync_version(group)
    version = int(group["cg_version"].value()) if "cg_version" in group.knobs() else 1
    pad = int(group["cg_version_pad"].value() or 3) if "cg_version_pad" in group.knobs() else 3
    stem = "_".join(p for p in (versioned(name, version, pad), suffix) if p)
    out_dir = "{}/{}".format(out_root, stem)

    reused = False
    if read is None:
        src_path = "{0}/_source/{1}_{2}/{1}_source.%04d.exr".format(out_root, name, suffix)
        fp = _input_fingerprint(group, src_path, first, last)
        reusable = ("cg_reuse_input" in group.knobs() and group["cg_reuse_input"].value()
                    and group["cg_source_fp"].value() == fp
                    and all(os.path.isfile(src_path % f) for f in range(first, last + 1)))
        if reusable:
            reused = True
            nuke_cs = group["cg_source_cs"].value()
            nuke.tprint("[ComfyGizmos] {}: input unchanged - reusing {}".format(group.name(), src_path))
        else:
            group["cg_source_fp"].setValue("")
            src_path, nuke_cs = _render_source(group, src_path, first, last)
            group["cg_source_fp"].setValue(fp)
            group["cg_source_cs"].setValue(nuke_cs)

    # colorspace -> ComfyUI name
    available = client.colorspaces(host)
    comfy_cs = group["cg_colorspace"].value().strip()
    if not raw and not comfy_cs:
        if colorspace.is_default(nuke_cs):
            nuke_cs = DEFAULT_COLORSPACE
        comfy_cs = colorspace.to_comfy(nuke_cs, available)
        if not comfy_cs:
            raise _Abort("colorspace '{}' has no match in ComfyUI's OCIO config - set 'colorspace "
                         "override'".format(nuke_cs))

    # output colorspace
    out_cs = group["cg_out_cs"].value().strip() if "cg_out_cs" in group.knobs() else ""
    out_cs = out_cs or _default_out_cs(wf)
    out_raw = out_cs.lower() == "raw"
    if not out_raw:
        mapped = colorspace.to_comfy(out_cs, available)
        if not mapped:
            raise _Abort("output colorspace '{}' has no match in ComfyUI's OCIO config".format(out_cs))
        out_cs = mapped

    prompt = _fill_graph(wf, group, src_path, raw, comfy_cs, first, last, out_dir, stem, out_cs, out_raw)

    # output naming = whatever OCIO Write will produce (<stem>_<tag>.####.exr)
    first_file = client.write_first_path(host, prompt[wf["write"]["node"]]["inputs"])
    result = re.sub(r"\.(\d+)(\.[^./]+)$", lambda m: ".%0{}d{}".format(len(m.group(1)), m.group(2)),
                    first_file)
    existing = os.path.isfile(first_file)
    if existing and nuke.GUI and not nuke.ask("{} already exists.\n\nOverwrite?".format(first_file)):
        raise _Abort("")

    prompt_id = client.submit(host, prompt)
    job = {"group": group.fullName(), "host": host, "prompt_id": prompt_id, "result": result,
           "first": first, "last": last, "colorspace": "raw" if out_raw else out_cs, "started": time.time()}
    _jobs[group.fullName()] = job
    group["cg_prompt_id"].setValue(prompt_id)
    _set_status(group, "queued - {} frames{}, {}".format(count, " (input unchanged, reused)" if reused else "",
                                                       os.path.basename(result)))
    return job


_LAYOUT_KNOBS = re.compile(r"^\s*(xpos|ypos|selected|note_font\w*|tile_color|gl_color|hide_input|"
                           r"postage_stamp\w*|label|cached|dope_sheet|bookmark|icon|indicators)\b.*$", re.M)


def _input_fingerprint(group, src_path, first, last):
    """Everything that decides the rendered input: upstream node settings + wiring, Read files' timestamps,
    frame range, target path. Layout-only knobs (position, selection, colour, label) are left out."""
    import hashlib
    parts = ["{}|{}-{}".format(src_path, first, last)]
    seen, todo = set(), [group.input(0)]
    while todo:
        n = todo.pop()
        if n is None or n.fullName() in seen:
            continue
        seen.add(n.fullName())
        deps = n.dependencies(nuke.INPUTS | nuke.HIDDEN_INPUTS | nuke.EXPRESSIONS)
        knobs = _LAYOUT_KNOBS.sub("", n.writeKnobs(nuke.WRITE_NON_DEFAULT_ONLY | nuke.TO_SCRIPT))
        wiring = ",".join((n.input(i).fullName() if n.input(i) else "-") for i in range(n.inputs()))
        parts.append("{}|{}|{}|{}".format(n.fullName(), n.Class(), wiring, knobs.strip()))
        if n.Class() == "Read":
            path = re.sub(r"#+", lambda m: "%0{}d".format(len(m.group())), _read_path(n))
            stamps = []
            for f in range(first, last + 1):
                fpath = path % f if re.search(r"%0?\d*d", path) else path
                try:
                    stamps.append("{:.0f}".format(os.path.getmtime(fpath)))
                except (OSError, TypeError):
                    stamps.append("?")
            parts.append(",".join(stamps))
        todo.extend(deps)
    return hashlib.sha1("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


def _max_frames(group, wf):
    if "cg_max_frames" in group.knobs():
        return int(group["cg_max_frames"].value())
    return int(wf.get("max_frames") or 0)


def _render_source(group, path, first, last):
    """Render the group's input to EXR (ACES2065-1 when the config has it); returns (pattern, colorspace)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write = group.node("SourceWrite")
    write["file"].setValue(path)
    try:
        write["colorspace"].setValue(DEFAULT_COLORSPACE)
    except Exception:
        pass                                         # not in this config - keep the Write's default
    write["disable"].setValue(False)
    try:
        _set_status(group, "rendering input {}-{}...".format(first, last))
        nuke.execute(write, first, last, 1)
    finally:
        write["disable"].setValue(True)
    return path, colorspace.unwrap_default(write["colorspace"].value())


def _fill_graph(wf, group, src_path, raw, comfy_cs, first, last, out_dir, stem, out_cs, out_raw):
    prompt = copy.deepcopy(_api_graph(wf))
    ext = os.path.splitext(src_path)[1].lstrip(".").lower()
    if ext in MOVIE_EXT:
        frame_mode = "video"
    elif FRAME_TOKEN.search(os.path.basename(src_path)):
        frame_mode = "sequence"
    else:
        frame_mode = "single"

    rd = prompt[wf["read"]["node"]]["inputs"]
    rd.update({
        "source": src_path, "frame_mode": frame_mode, "raw_data": raw,
        "start_frame": first, "end_frame": last, "frame_shift": first,
        "fps": float(nuke.root()["fps"].value()),
        "missing_frames": "black", "edge_mode": "hold",
    })
    if not raw:
        rd["input_colorspace"] = comfy_cs

    wr = prompt[wf["write"]["node"]]["inputs"]
    wr.update({
        "output_folder": out_dir, "filename": stem, "container": "sequence", "still_format": "exr",
        "first_frame": first, "last_frame": last, "start_number": first, "source_start": first,
        "colorspace_in_name": True, "render_nonce": uuid.uuid4().hex,   # always re-run the Write
        "profile": "none", "auto_colorspace": False, "raw_data": out_raw,
    })
    if not out_raw:
        wr["output_colorspace"] = out_cs
        if not wf["output"].get("convert", True):
            wr["from_colorspace"] = out_cs          # data (a matte): label only, values untouched

    mfi = wf.get("max_frames_input")
    if mfi and _max_frames(group, wf):
        prompt[mfi["node"]]["inputs"][mfi["input"]] = _max_frames(group, wf)

    for spec in wf.get("knobs", []):
        val = group["wf_" + spec["name"]].value()
        if spec["type"] == "int":
            val = int(val)
        elif spec["type"] == "float":
            val = float(val)
        prompt[spec["node"]]["inputs"][spec["input"]] = val
    return prompt


def _poll(job, in_main=False):
    def ui(fn, *args):
        if in_main or not nuke.GUI:
            fn(*args)
        else:
            nuke.executeInMainThread(fn, args)

    try:
        while True:
            try:
                state, info = client.status(job["host"], job["prompt_id"])
            except client.ComfyError as e:
                state, info = "error", str(e)
            elapsed = int(time.time() - job["started"])
            if state == "queued":
                ui(_status_by_name, job, "queued (position {}) {}s".format(info, elapsed))
            elif state == "running":
                ui(_status_by_name, job, "rendering in ComfyUI... {}s".format(elapsed))
            elif state == "done":
                ui(_finish, job, None)
                return
            elif state in ("error", "missing"):
                ui(_finish, job, info or "the job disappeared from ComfyUI's queue")
                return
            time.sleep(POLL_SECONDS)
    finally:
        _jobs.pop(job["group"], None)


def _node(job):
    return nuke.toNode(job["group"])


def _status_by_name(job, text):
    node = _node(job)
    if node is not None:
        _set_status(node, text)


def _finish(job, error):
    node = _node(job)
    if node is None:
        return
    if error:
        _set_status(node, "failed: " + error)
        nuke.tprint("[ComfyGizmos] {}: {}".format(node.name(), error))
        return
    missing = [f for f in range(job["first"], job["last"] + 1) if not os.path.isfile(job["result"] % f)]
    if missing:
        _set_status(node, "finished, but {} frame(s) are missing in {}".format(
            len(missing), os.path.dirname(job["result"])))
        return
    node["cg_result"].setValue(job["result"])
    node["cg_result_first"].setValue(str(job["first"]))
    node["cg_result_last"].setValue(str(job["last"]))
    node["cg_result_cs"].setValue(job["colorspace"])
    got = load_result(node)
    _set_status(node, "done in {}s - {} - frames {}-{}".format(
        int(time.time() - job["started"]), os.path.basename(job["result"]), *got))


def load_result(group):
    """Point the internal Read at the last result and switch the output to it."""
    path = group["cg_result"].value()
    if not path:
        _set_status(group, "nothing rendered yet")
        return
    first, last = int(group["cg_result_first"].value()), int(group["cg_result_last"].value())
    res = group.node("Result")
    _point_read(res, path, first, last)
    _set_read_colorspace(res, _result_cs(group))
    res["on_error"].setValue("black")
    res["reload"].execute()
    sw = group.node("OutputSwitch")
    if "cg_output" in group.knobs():
        sw["which"].setValue(1 + group["cg_output"].getValue())
    else:
        sw["which"].setValue(1)
    return int(res["first"].value()), int(res["last"].value())


def _point_read(read, path, first, last):
    """File + range in one go, like Nuke's file browser. Setting file, then first/last one by one lets
    Nuke's own re-scan and first<=last clamping win in the GUI."""
    read["file"].fromUserText("{} {}-{}".format(path, first, last))
    for knob, val in (("origfirst", first), ("origlast", last), ("last", last), ("first", first),
                      ("last", last)):   # last twice: first can't go above the old last
        read[knob].setValue(val)


def _result_cs(group):
    return group["cg_result_cs"].value() if "cg_result_cs" in group.knobs() else "raw"


def _set_read_colorspace(read, cs):
    """'raw' -> raw; otherwise the colorspace, trying this config's spelling (ACES 1.2 vs 1.3/2.0 names)."""
    if not cs or cs.lower() == "raw":
        read["raw"].setValue(True)
        return
    read["raw"].setValue(False)
    for name in (cs, cs.split(" - ")[-1]):          # 'ACES - ACEScg' -> 'ACEScg'
        try:
            read["colorspace"].setValue(name)
        except Exception:
            continue
        if colorspace.unwrap_default(read["colorspace"].value()) == name:
            return
    nuke.tprint("[ComfyGizmos] {}: colorspace '{}' not in this config - set it by hand".format(read.name(), cs))


def read_output(group):
    """Bring in output: a plain Read next to this node, on the rendered EXRs."""
    path = group["cg_result"].value()
    if not path:
        nuke.message("{}: nothing rendered yet.".format(group.name()))
        return None
    first, last = int(group["cg_result_first"].value()), int(group["cg_result_last"].value())
    wf = workflows().get(group["cg_workflow"].value(), {})
    parent = nuke.toNode(group.fullName().rpartition(".")[0]) if "." in group.fullName() else nuke.root()
    with parent:
        for n in nuke.selectedNodes():
            n.setSelected(False)
        read = nuke.nodes.Read()
        _point_read(read, path, first, last)
        _set_read_colorspace(read, _result_cs(group))
        read.setName("Read_" + re.sub(r"\W", "_", os.path.basename(os.path.dirname(path))))
        read.setXYpos(group.xpos() + 150, group.ypos())
        read.setSelected(True)
    return read


def cancel(group):
    job = _jobs.get(group.fullName())
    prompt_id = job["prompt_id"] if job else group["cg_prompt_id"].value()
    if not prompt_id:
        return
    try:
        client.cancel(group["cg_host"].value().strip() or client.DEFAULT_HOST, prompt_id)
        _set_status(group, "cancelled")
    except client.ComfyError as e:
        _set_status(group, "cancel failed: {}".format(e))


def _set_status(group, text):
    try:
        group["cg_status"].setValue(text)
    except Exception:
        pass
