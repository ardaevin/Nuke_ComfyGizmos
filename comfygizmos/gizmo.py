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
    group["knobChanged"].setValue(
        "try:\n    import comfygizmos\n    comfygizmos.knob_changed(nuke.thisNode(), nuke.thisKnob())\n"
        "except ImportError:\n    pass\n")
    _update_source_knobs(group)
    _update_label(group)
    group.showControlPanel()
    return group


def knob_changed(group, knob):
    """Group callback: grey out the source version when a plain Read is connected; refresh the result view."""
    if knob.name() in ("inputChange", "showPanel"):
        _update_source_knobs(group)
        _update_label(group)
    elif knob.name() in ("cg_name", "cg_version"):
        _update_label(group)
    elif knob.name() in ("cg_hold", "cg_output") and group["cg_result"].value():
        load_result(group)


def _build_internals(group, wf):
    group.begin()
    try:
        inp = nuke.nodes.Input(name=wf.get("input_name", "Input"))
        inp["number"].setValue(0)
        _source_write("SourceWrite", inp)
        for i, ref in enumerate(wf.get("refs", []), 1):          # extra arrows: reference images
            ref_in = nuke.nodes.Input(name=ref["name"])
            ref_in["number"].setValue(i)
            _source_write("SourceWrite_" + ref["name"], ref_in)
        res = nuke.nodes.Read(name="Result")

        if wf["output"]["kind"] == "matte":
            # matte -> rgba (alpha = red), or the input with the matte as its alpha
            matte = nuke.nodes.Copy(name="MatteToAlpha", inputs=[res, res])
            input_matte = nuke.nodes.Copy(name="InputWithMatte", inputs=[inp, res])
            for c in (matte, input_matte):
                c["from0"].setValue("rgba.red")
                c["to0"].setValue("rgba.alpha")
            choices = [inp, matte, input_matte]           # 0 = no result yet (pass through)
        elif wf["output"]["kind"] == "normals":
            # 0-1 encoded normals -> -1..1 vectors, or the file as written
            decoded = _decode_normals(res)
            decoded.setName("DecodeNormals")
            choices = [inp, decoded, res]
        else:
            choices = [inp, res]
        switch = nuke.nodes.Switch(name="OutputSwitch")
        for i, node in enumerate(choices):
            switch.setInput(i, node)
        switch["which"].setValue(0)
        nuke.nodes.Output(inputs=[switch])
    finally:
        group.end()


def _decode_normals(inp):
    """Expression n*2-1 on rgb: normal maps store -1..1 vectors as 0..1."""
    expr = nuke.nodes.Expression(inputs=[inp], expr0="r*2-1", expr1="g*2-1", expr2="b*2-1")
    expr["label"].setValue("normals 0-1 -> -1..1")
    return expr


def _source_write(name, inp):
    """Disabled EXR Write used to render an input that can't be sent as-is."""
    src = nuke.nodes.Write(name=name, inputs=[inp], file_type="exr", disable=True)
    src["channels"].setValue("rgb")
    src["datatype"].setValue("16 bit half")
    _set_enum(src, "compression", "Zip (1 scanline)")
    return src


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
    name = nuke.String_Knob("cg_name", "Name", "")
    name.setTooltip("Names the source and output: <Name>_{0}_v###. Required unless the input is a plain Read "
                    "(then empty = the plate's file name).".format(wf["suffix"]))
    k(name)

    k(nuke.Text_Knob("cg_div_in", "input"))
    sv = nuke.Int_Knob("cg_src_version", "source")
    sv.setValue(1)
    sv.setTooltip("Source version. Render in ComfyUI uses sources/<Name>_{0}/<Name>_{0}_Source_v###; it is "
                  "rendered only if it isn't on disk yet. Changed something upstream? 'Version up source'. "
                  "Greyed out for a plain Read, which is sent as it is.".format(wf["suffix"]))
    k(sv)
    sv_up = nuke.PyScript_Knob("cg_src_version_up", "Version up source", "comfygizmos.version_up_source(nuke.thisNode())")
    sv_up.setTooltip("Next source version - the input is rendered again at the next 'Render in ComfyUI'.")
    sv_up.clearFlag(nuke.STARTLINE)
    k(sv_up)
    if _single(wf):
        fr = nuke.Int_Knob("cg_frame", "frame")
        fr.setFlag(nuke.STARTLINE)
        fr.setValue(int(nuke.frame()))
        fr.setTooltip("The frame to edit. Stays put when the playhead moves; change it here or with "
                      "'Set to current frame'.")
        k(fr)
        cur = nuke.PyScript_Knob("cg_frame_set", "Set to current frame", "comfygizmos.set_frame(nuke.thisNode())")
        cur.clearFlag(nuke.STARTLINE)
        k(cur)
        hold = nuke.Boolean_Knob("cg_hold", "hold on all frames", True)
        hold.clearFlag(nuke.STARTLINE)
        hold.setTooltip("Show the edited frame on every frame (off: only on its own frame, black elsewhere).")
        k(hold)
    else:
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
    enh = wf.get("enhancer")
    for spec in wf.get("knobs", []):
        k(_make_knob(spec))
        if enh and spec["name"] == enh.get("after"):
            run = nuke.PyScript_Knob("cg_enhance", "Run enhancer only", "comfygizmos.enhance(nuke.thisNode())")
            run.setTooltip("Only the prompt enhancer runs (no image is made). Its rewrite lands in 'refined prompt'.")
            run.setFlag(nuke.STARTLINE)
            k(run)
            use = nuke.PyScript_Knob("cg_use_refined", "Use refined prompt", "comfygizmos.use_refined(nuke.thisNode())")
            use.setTooltip("Copy the refined prompt into 'prompt' and turn 'refine prompt' off.")
            k(use)
            k(nuke.Multiline_Eval_String_Knob("cg_refined", "refined prompt", ""))

    k(nuke.Text_Knob("cg_div_out", "output"))
    out_root = nuke.File_Knob("cg_out_root", "output root")
    out_root.setTooltip("Follows the script: {} is the evaluated project directory (one folder up from the "
                        ".nk when it is empty). A plain path works too.".format(PROJECT_TOKEN))
    k(out_root)
    out_root.setValue(_default_output_root())
    out_cs = nuke.String_Knob("cg_out_cs", "output colorspace", _default_out_cs(wf))
    out_cs.setTooltip("Colorspace of the written EXRs and of the Read that loads them. 'raw' = untagged."
                      + ("" if wf["output"].get("convert", True) else
                         " This workflow's output is data (a matte): values are written untouched, only "
                         "labelled with this colorspace.")
                      + (" " + wf["output"]["tooltip"] if wf["output"].get("tooltip") else ""))
    k(out_cs)
    ver = nuke.Int_Knob("cg_version", "version")
    ver.setValue(1)
    ver.setTooltip("Output version: outputs/<Name>_{0}/<Name>_{0}_v###. Separate from the source version."
                   .format(wf["suffix"]))
    k(ver)
    up_btn = nuke.PyScript_Knob("cg_version_up", "Version up output", "comfygizmos.version_up(nuke.thisNode())")
    up_btn.clearFlag(nuke.STARTLINE)
    k(up_btn)
    if wf["output"]["kind"] == "matte":
        mode = nuke.Enumeration_Knob("cg_output", "output", ["matte", "input + matte alpha"])
        mode.setTooltip("matte: rgba = the matte. input + matte alpha: the input with the matte as its alpha.")
        k(mode)
    elif wf["output"]["kind"] == "normals":
        mode = nuke.Enumeration_Knob("cg_output", "output", ["normals -1..1", "encoded 0..1"])
        mode.setTooltip("normals -1..1: decoded vectors (n*2-1), ready for Relight. encoded 0..1: the EXR as "
                        "written. If lighting comes out flipped, negate that channel (e.g. green).")
        k(mode)
    host =nuke.String_Knob("cg_host", "ComfyUI", prefs()["host"])
    k(host)

    k(nuke.Text_Knob("cg_div_run", ""))
    render = nuke.PyScript_Knob("cg_render", "Render in ComfyUI", "comfygizmos.render(nuke.thisNode())")
    render.setFlag(nuke.STARTLINE)
    k(render)
    k(nuke.PyScript_Knob("cg_cancel", "Cancel", "comfygizmos.cancel(nuke.thisNode())"))
    k(nuke.PyScript_Knob("cg_reload", "Reload result", "comfygizmos.load_result(nuke.thisNode())"))
    k(nuke.PyScript_Knob("cg_bring_in", "Bring in output", "comfygizmos.read_output(nuke.thisNode())"))
    k(nuke.Text_Knob("cg_status", "status", "not rendered"))
    for name in ("cg_prompt_id", "cg_result", "cg_result_first", "cg_result_last", "cg_result_cs"):
        hidden = nuke.String_Knob(name, name)
        hidden.setFlag(nuke.INVISIBLE)
        k(hidden)


def _single(wf):
    return wf.get("frames") == "single"


def _default_out_cs(wf):
    out = wf["output"]
    return "raw" if out.get("raw") else out.get("colorspace", DEFAULT_COLORSPACE)


def _knob_value(knob):
    """A knob's value; text knobs as typed. Nuke's Eval string knobs run value() through TCL, so a prompt
    with [ ] or $ in it would arrive mangled ('[the wall]' -> 'the: Unknown command')."""
    if isinstance(knob, nuke.EvalString_Knob) and hasattr(knob, "getText"):   # String / Multiline knobs only
        return knob.getText()
    return knob.value()


def _make_knob(spec):
    name, label, kind = "wf_" + spec["name"], spec.get("label", spec["name"]), spec["type"]
    if kind == "divider":
        return nuke.Text_Knob("wf_" + spec["name"], label)
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


def version_up(group):
    """Next OUTPUT version (the source version is separate)."""
    group["cg_version"].setValue(int(group["cg_version"].value()) + 1)
    _update_label(group)
    _set_status(group, "output version up -> v{:03d}".format(int(group["cg_version"].value())))


def version_up_source(group):
    """Next SOURCE version: the input is rendered again at the next 'Render in ComfyUI'."""
    group["cg_src_version"].setValue(int(group["cg_src_version"].value()) + 1)
    _set_status(group, "source version up -> v{:03d} (rendered at the next Render in ComfyUI)".format(
        int(group["cg_src_version"].value())))


def _update_source_knobs(group):
    """Source version only matters when an input has to be rendered - grey it out when every connected
    input (plate and refs) is a plain Read."""
    needs_render = any(group.input(i) is not None and _direct_read(group, i) is None
                       for i in range(group.inputs()))
    for name in ("cg_src_version", "cg_src_version_up"):
        if name in group.knobs():
            group[name].setEnabled(needs_render)


def _update_label(group):
    """Label = the output's name and version as plain text, e.g. 'WallFix' / 'v003'. A plain Read with no
    Name shows the plate's file name - the same name the output gets."""
    if "cg_name" not in group.knobs():
        return
    name = _clean_name(_knob_value(group["cg_name"]))
    if not name:
        read = _direct_read(group)
        name = plate_name(_read_path(read)) if read is not None else ""
    group["label"].setValue("{}\nv{:03d}".format(name, int(group["cg_version"].value())).lstrip("\n"))


def _clean_name(text):
    """User Name -> safe for folders/files ('Wall Fix!' -> 'Wall_Fix')."""
    return re.sub(r"[^\w.-]+", "_", (text or "").strip()).strip("_")


def plate_name(path):
    """'.../SH0400_plate_v002.%04d.exr' -> 'SH0400_plate_v002' (movies keep their whole name)."""
    name = os.path.basename(path.replace("\\", "/"))
    name = re.sub(r"\.[^.]+$", "", name)
    return re.sub(r"[._-]?(%0?\d*d|#+|@+)$", "", name)


def _direct_read(group, index=0):
    """The connected Read when it can be sent as-is: file numbers = Nuke frame numbers."""
    up = group.input(index)
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


def _prepare(group, mode="render"):
    """mode 'render': the full job. mode 'enhance': only the prompt enhancer (nothing is written)."""
    wf = workflows()[group["cg_workflow"].value()]
    host = group["cg_host"].value().strip() or client.DEFAULT_HOST
    if group.fullName() in _jobs:
        raise _Abort("a render is already running - Cancel it first")
    if group.input(0) is None:
        raise _Abort("nothing connected to the input")
    if not client.is_up(host):
        raise _Abort("ComfyUI is not running at " + host)

    # frame range
    if _single(wf):
        first = last = int(group["cg_frame"].value())       # fixed - never follows the playhead
    elif group["cg_input_range"].value():
        first, last = int(group.input(0).firstFrame()), int(group.input(0).lastFrame())
    else:
        first, last = int(group["cg_first"].value()), int(group["cg_last"].value())
    count = last - first + 1
    if count < 1:
        raise _Abort("empty frame range {}-{}".format(first, last))
    max_frames = 0 if _single(wf) else _max_frames(group, wf)
    if max_frames and count > max_frames:
        raise _Abort("{} frames ({}-{}) - 'max frames' is {}. Raise it, split the shot or set a "
                     "shorter range.".format(count, first, last, max_frames))

    # naming: <Name>_<gizmo>; a plain Read with no Name uses the plate's file name
    gizmo_name = wf["suffix"]
    typed = _clean_name(_knob_value(group["cg_name"])) if "cg_name" in group.knobs() else ""
    read = _direct_read(group)
    if read is not None:
        src_path = _read_path(read)
        if not src_path:
            raise _Abort("the Read's path could not be made absolute - save the script first")
        raw = bool(read["raw"].value())
        nuke_cs = "" if raw else read["colorspace"].value()
        base = typed or plate_name(src_path)
    else:
        if not typed:
            raise _Abort("fill in 'Name' at the top - it is needed when the input is not a plain Read")
        src_path, nuke_cs, raw, base = None, None, False, typed
    stem = "{}_{}".format(base, gizmo_name)

    out_root = _output_root(group)
    if not out_root:
        raise _Abort("no output root: set the project directory, save the script, or type a path in "
                     "'output root'")
    version = int(group["cg_version"].value())
    out_dir = "{}/outputs/{}".format(out_root, stem)
    out_name = "{}_v{:03d}".format(stem, version)

    src_version = int(group["cg_src_version"].value()) if "cg_src_version" in group.knobs() else 1
    src_dir = "{}/sources/{}".format(out_root, stem)
    notes = []                                            # what happened to the inputs, for the status line
    if read is None:
        src_path = "{}/{}_Source_v{:03d}.%04d.exr".format(src_dir, stem, src_version)
        nuke_cs, rendered = _ensure_source(group, "SourceWrite", src_path, first, last)
        notes.append("source v{:03d} {}".format(src_version, rendered or "reused"))
        nuke.tprint("[ComfyGizmos] {}: {}".format(group.name(), notes[-1]))

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

    refs = _prepare_refs(group, wf, available, src_dir, stem, src_version, first, notes)

    # output colorspace
    out_cs = group["cg_out_cs"].value().strip() if "cg_out_cs" in group.knobs() else ""
    out_cs = out_cs or _default_out_cs(wf)
    out_raw = out_cs.lower() == "raw"
    if not out_raw:
        mapped = colorspace.to_comfy(out_cs, available)
        if not mapped:
            raise _Abort("output colorspace '{}' has no match in ComfyUI's OCIO config".format(out_cs))
        out_cs = mapped

    prompt = _fill_graph(wf, group, src_path, raw, comfy_cs, first, last, out_dir, out_name, out_cs, out_raw)
    _add_refs(prompt, wf, refs)

    enh = wf.get("enhancer")
    if mode == "enhance":
        del prompt[wf["write"]["node"]]                     # only the enhancer's output node is left to run
        for f in enh["force"]:
            prompt[f["node"]]["inputs"][f["input"]] = f["value"]
        prompt_id = client.submit(host, prompt)
        job = {"group": group.fullName(), "host": host, "prompt_id": prompt_id, "kind": "enhance",
               "enhancer_node": enh["output_node"], "started": time.time()}
        _jobs[group.fullName()] = job
        group["cg_prompt_id"].setValue(prompt_id)
        _set_status(group, "prompt enhancer queued")
        return job

    # output naming = whatever OCIO Write will produce (<stem>_<tag>.####.exr)
    first_file = client.write_first_path(host, prompt[wf["write"]["node"]]["inputs"])
    result = re.sub(r"\.(\d+)(\.[^./]+)$", lambda m: ".%0{}d{}".format(len(m.group(1)), m.group(2)),
                    first_file)
    existing = os.path.isfile(first_file)
    if existing and nuke.GUI and not nuke.ask(
            "The OUTPUT of this version already exists:\n{}\n\nOverwrite it?\n\n"
            "(No = nothing is sent. To keep it and render a new one, press 'Version up output' first.)".format(first_file)):
        raise _Abort("")

    prompt_id = client.submit(host, prompt)
    job = {"group": group.fullName(), "host": host, "prompt_id": prompt_id, "result": result,
           "first": first, "last": last, "colorspace": "raw" if out_raw else out_cs, "started": time.time(),
           "notes": "; ".join(notes)}
    if enh and "wf_" + enh["after"] in group.knobs() and group["wf_" + enh["after"]].value():
        job["enhancer_node"] = enh["output_node"]           # show what the enhancer made of the prompt
    _jobs[group.fullName()] = job
    group["cg_prompt_id"].setValue(prompt_id)
    what = "frame {}".format(first) if _single(wf) else "{} frames".format(count)
    _set_status(group, "queued - {}, {}{}".format(what, os.path.basename(result),
                                                  " - " + "; ".join(notes) if notes else ""))
    return job


def _prepare_refs(group, wf, available, src_dir, stem, src_version, frame, notes):
    """Reference inputs that are connected -> [(ref spec, path, raw, comfy colorspace, frame)]."""
    refs = []
    for i, ref in enumerate(wf.get("refs", []), 1):
        if group.input(i) is None:
            continue
        read = _direct_read(group, i)
        if read is not None:
            path = _read_path(read)
            if not path:
                raise _Abort("{}: the Read's path could not be made absolute".format(ref["name"]))
            raw = bool(read["raw"].value())
            nuke_cs = "" if raw else read["colorspace"].value()
            f = min(max(frame, int(read["first"].value())), int(read["last"].value()))   # hold its ends
        else:
            path = "{}/{}_Source_{}_v{:03d}.%04d.exr".format(src_dir, stem, ref["name"], src_version)
            nuke_cs, rendered = _ensure_source(group, "SourceWrite_" + ref["name"], path, frame, frame)
            notes.append("{} source v{:03d} {}".format(ref["name"], src_version, rendered or "reused"))
            nuke.tprint("[ComfyGizmos] {}: {}".format(group.name(), notes[-1]))
            raw, f = False, frame
        cs = ""
        if not raw:
            cs = colorspace.to_comfy(DEFAULT_COLORSPACE if colorspace.is_default(nuke_cs) else nuke_cs, available)
            if not cs:
                raise _Abort("{}: colorspace '{}' has no match in ComfyUI's OCIO config".format(ref["name"], nuke_cs))
        refs.append((ref, path, raw, cs, f))
    return refs


def _add_refs(prompt, wf, refs):
    """One OCIO Read per reference (a copy of the plate's), wired to the ref's targets."""
    base = prompt[wf["read"]["node"]]
    for ref, path, raw, cs, f in refs:
        nid = "cg_" + ref["name"]
        node = copy.deepcopy(base)
        node["inputs"].update({"source": path, "frame_mode": _frame_mode(path), "raw_data": raw,
                               "start_frame": f, "end_frame": f, "frame_shift": f})
        if not raw:
            node["inputs"]["input_colorspace"] = cs
        prompt[nid] = node
        for t in ref["targets"]:
            prompt[t["node"]]["inputs"][t["input"]] = [nid, 0]


def _frame_mode(path):
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext in MOVIE_EXT:
        return "video"
    return "sequence" if FRAME_TOKEN.search(os.path.basename(path)) else "single"


def _ensure_source(group, write_name, path, first, last):
    """Use the source if its frames are on disk; render only the frames that are missing.
    Returns (colorspace, '' if nothing was rendered else what was)."""
    write = group.node(write_name)
    try:
        write["colorspace"].setValue(DEFAULT_COLORSPACE)
    except Exception:
        pass                                         # not in this config - keep the Write's default
    cs = colorspace.unwrap_default(write["colorspace"].value())
    missing = [f for f in range(first, last + 1) if not os.path.isfile(path % f)]
    if not missing:
        return cs, ""
    _render_source(group, path, missing, write_name)
    if len(missing) == last - first + 1:
        return cs, "rendered"
    return cs, "rendered ({} missing frame(s))".format(len(missing))


def _max_frames(group, wf):
    if "cg_max_frames" in group.knobs():
        return int(group["cg_max_frames"].value())
    return int(wf.get("max_frames") or 0)


def _render_source(group, path, frames, write_name="SourceWrite"):
    """Render the given frames of an input to EXR (ACES2065-1 when the config has it)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write = group.node(write_name)
    write["file"].setValue(path)
    write["disable"].setValue(False)
    try:
        _set_status(group, "rendering source {}...".format(os.path.basename(path)))
        start = prev = frames[0]
        for f in frames[1:] + [None]:                # contiguous runs, so existing frames are left alone
            if f is not None and f == prev + 1:
                prev = f
                continue
            nuke.execute(write, start, prev, 1)
            start = prev = f
    finally:
        write["disable"].setValue(True)


def _fill_graph(wf, group, src_path, raw, comfy_cs, first, last, out_dir, stem, out_cs, out_raw):
    prompt = copy.deepcopy(_api_graph(wf))
    frame_mode = _frame_mode(src_path)

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
    if mfi and not _single(wf) and _max_frames(group, wf):
        prompt[mfi["node"]]["inputs"][mfi["input"]] = _max_frames(group, wf)

    for spec in wf.get("knobs", []):
        if spec["type"] == "divider":
            continue
        val = _knob_value(group["wf_" + spec["name"]])
        if spec["type"] == "int":
            val = int(val)
        elif spec["type"] == "float":
            val = float(val)
        elif spec["type"] == "bool":
            val = bool(val)
        for target in spec.get("targets") or [{"node": spec["node"], "input": spec["input"]}]:
            prompt[target["node"]]["inputs"][target["input"]] = val      # one knob may drive several nodes
    return prompt


def _poll(job, in_main=False):
    def ui(fn, *args):
        if in_main or not nuke.GUI:
            fn(*args)
        else:
            nuke.executeInMainThread(fn, args)

    missing_since = None
    try:
        while True:
            try:
                state, info = client.status(job["host"], job["prompt_id"])
            except client.ComfyError as e:
                state, info = "error", str(e)
            # Just after a job finishes it can be out of the queue but not yet in the history - only give up
            # on a 'missing' job when it stays missing.
            if state == "missing":
                missing_since = missing_since or time.time()
                if time.time() - missing_since < 10:
                    time.sleep(0.5)
                    continue
            else:
                missing_since = None
            elapsed = int(time.time() - job["started"])
            if state == "queued":
                ui(_status_by_name, job, "queued (position {}) {}s".format(info, elapsed))
            elif state == "running":
                ui(_status_by_name, job, "{} in ComfyUI... {}s".format(
                    "refining the prompt" if job.get("kind") == "enhance" else "rendering", elapsed))
            elif state == "done":
                ui(_finish, job, None, info)
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


def _finish(job, error, outputs=None):
    node = _node(job)
    if node is None:
        return
    if error:
        _set_status(node, "failed: " + error)
        nuke.tprint("[ComfyGizmos] {}: {}".format(node.name(), error))
        return
    text = _output_text(outputs, job.get("enhancer_node"))
    if text and "cg_refined" in node.knobs():
        node["cg_refined"].setValue(text)
    if job.get("kind") == "enhance":
        _set_status(node, "refined prompt ready in {}s - see 'refined prompt'".format(int(time.time() - job["started"]))
                    if text else "prompt enhancer finished but returned no text")
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
    _set_status(node, "done in {}s - {} - frames {}-{}{}".format(
        int(time.time() - job["started"]), os.path.basename(job["result"]), got[0], got[1],
        " - " + job["notes"] if job.get("notes") else ""))


def _output_text(outputs, node_id):
    """Text a PreviewAny-style output node showed, or ''."""
    if not outputs or not node_id:
        return ""
    texts = (outputs.get(node_id) or {}).get("text") or []
    return "\n".join(str(t) for t in texts).strip()


def enhance(group, wait=False):
    """Run only the prompt enhancer; its rewrite lands in 'refined prompt'."""
    try:
        job = _prepare(group, mode="enhance")
    except _Abort as e:
        if str(e):
            _set_status(group, "not sent: " + str(e))
            if nuke.GUI:
                nuke.message("{}:\n\n{}".format(group.name(), e))
        return None
    if wait:
        _poll(job, in_main=True)
        return job
    threading.Thread(target=_poll, args=(job,), daemon=True).start()
    return job


def set_frame(group):
    """'Set to current frame': the frame to edit = where the playhead is now."""
    group["cg_frame"].setValue(int(nuke.frame()))


def use_refined(group):
    """Refined prompt -> prompt, and stop refining (the rewrite is already in the prompt)."""
    text = _knob_value(group["cg_refined"]).strip() if "cg_refined" in group.knobs() else ""
    if not text:
        if nuke.GUI:
            nuke.message("{}: no refined prompt yet - press 'Run enhancer only' first.".format(group.name()))
        return
    group["wf_prompt"].setValue(text)
    wf = workflows()[group["cg_workflow"].value()]
    after = (wf.get("enhancer") or {}).get("after")
    if after and "wf_" + after in group.knobs():
        group["wf_" + after].setValue(False)
    _set_status(group, "refined prompt copied into 'prompt', refine prompt off")


def _apply_hold(group, read):
    """Single-frame gizmos: hold the frame (or black). Range gizmos keep the Read's own defaults."""
    if "cg_hold" not in group.knobs():
        return
    hold = group["cg_hold"].value()
    for knob in ("before", "after"):
        _set_enum(read, knob, "hold" if hold else "black")


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
    _apply_hold(group, res)
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
    """Bring in output: a Read of the rendered EXRs next to this node, then a Crop (to the plate's format)
    under it - for a matte a Shuffle (red -> alpha), for normals an Expression (0-1 -> -1..1). Returns the Read."""
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
        _apply_hold(group, read)
        read.setName("Read_" + re.sub(r"\W", "_", plate_name(path)))
        read.setXYpos(group.xpos() + 150, group.ypos())
        crop = nuke.nodes.Crop(inputs=[read])
        crop["box"].setValue([0, 0, read.width(), read.height()])
        crop.setXYpos(read.xpos(), read.ypos() + 120)
        last = crop
        if wf.get("output", {}).get("kind") == "matte":
            last = nuke.nodes.Shuffle2(inputs=[crop])
            last["mappings"].setValue([(0, "rgba.red", "rgba.red"), (0, "rgba.green", "rgba.green"),
                                       (0, "rgba.blue", "rgba.blue"), (0, "rgba.red", "rgba.alpha")])
            last["label"].setValue("red -> alpha")
            last.setXYpos(read.xpos(), crop.ypos() + 50)
        elif wf.get("output", {}).get("kind") == "normals":
            last = _decode_normals(crop)
            last.setXYpos(read.xpos(), crop.ypos() + 50)
        last.setSelected(True)
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
