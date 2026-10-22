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

TILE_DEFAULT = 0x3F5F8AFF   # the gizmos' blue
# Node colour by render state. Green and red also differ a lot in brightness (bright vs dark), so they stay
# apart for red-green colour blindness too; the state word on the label says the same thing.
STATE_TILE = {
    "rendering": 0xD2B43CFF, "queued": 0xD2B43CFF, "refining": 0xD2B43CFF,   # yellow
    "done": 0x52C468FF,                                                        # bright green
    "failed": 0x7A1F1FFF, "cancelled": 0x7A1F1FFF,                              # dark red
}
# Retired workflow id -> the one that replaced it (nodes in old scripts keep working).
WORKFLOW_ALIASES = {"ltx_alpha_crop": "ltx_alpha"}     # the crop test became LTX Alpha Gen (2026-10-22)
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
    for old, new in WORKFLOW_ALIASES.items():           # retired workflows: their nodes keep working
        if new in found and old not in found:
            found[old] = found[new]
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
        if wf_id != wf["id"]:                            # an alias of a retired workflow: no menu entry
            continue
        menu.addCommand(wf["label"], "comfygizmos.create({!r})".format(wf_id))


# --------------------------------------------------------------------------- building the node

def create(wf_id):
    wf = workflows()[wf_id]
    # Like any Nuke node: connect to the selected node and take its place in the pipe. createNode can't do it
    # here - the Group has no inputs yet when it is made - so remember the selection and wire up afterwards.
    upstream = _single_selected()
    up_name = upstream.fullName() if upstream is not None else None
    downstream = [(n, i) for n in (upstream.dependent(nuke.INPUTS, forceEvaluate=False) if upstream else [])
                  for i in range(n.inputs()) if n.input(i) is not None and n.input(i).fullName() == up_name]
    group = nuke.createNode("Group", inpanel=False)
    group.setName("Comfy_" + wf["suffix"])
    group["tile_color"].setValue(TILE_DEFAULT)
    _build_internals(group, wf)
    _add_knobs(group, wf)
    if upstream is not None:
        group.setInput(0, upstream)
        _place_below(group, upstream)
        for node, i in downstream:                     # what read from the selected node now reads from the gizmo
            src = node.input(i)
            if node.fullName() != group.fullName() and src is not None and src.fullName() == up_name:
                node.setInput(i, group)
    group["knobChanged"].setValue(
        "try:\n    import comfygizmos\n    comfygizmos.knob_changed(nuke.thisNode(), nuke.thisKnob())\n"
        "except ImportError:\n    pass\n")
    _fit_crop_to_plate(group)
    _update_source_knobs(group)
    _update_send_knobs(group)
    _update_label(group)
    group.showControlPanel()
    return group


def _place_below(node, upstream):
    """Put a new gizmo under the node it reads from, as Nuke places nodes made with Tab."""
    try:
        nuke.autoplace(node)                           # Nuke's own placement, now that the input is connected
        if node.ypos() > upstream.ypos():
            return
    except Exception:
        pass
    w = node.screenWidth() or 80
    node.setXYpos(int(upstream.xpos() + (upstream.screenWidth() - w) / 2),
                  int(upstream.ypos() + (upstream.screenHeight() or 18) + 30))


def _single_selected():
    """The one selected node to connect a new gizmo to, or None (nothing or several selected)."""
    try:
        sel = nuke.selectedNodes()
    except Exception:
        return None
    if len(sel) != 1 or sel[0].maxOutputs() == 0:     # e.g. a Viewer or a Write has no output to feed us
        return None
    return sel[0]


def knob_changed(group, knob):
    """Group callback: grey out the source version when a plain Read is connected; refresh the result view."""
    if knob.name() in ("inputChange", "showPanel", "cg_crop_on"):
        _restore_text_knobs(group)
        _crops_without_black_edge(group)
        _fit_crop_to_plate(group)
        _update_source_knobs(group)
        _update_label(group)
        _update_send_knobs(group)
    elif knob.name() in ("cg_name", "cg_version"):
        _update_label(group)
    elif knob.name() == "cg_crop_preview":
        _crop_handles(group)
    elif knob.name() == "cg_crop_box" and not _fitting_crop:
        group["cg_crop_auto"].setValue(False)          # moved by the user: stop following the plate
    elif knob.name() == "cg_send":
        _update_send_knobs(group)
    elif knob.name() in ("cg_hold", "cg_output") and group["cg_result"].value():
        load_result(group)


def _build_internals(group, wf):
    group.begin()
    try:
        inp = nuke.nodes.Input(name=wf.get("input_name", "Input"))
        inp["number"].setValue(0)
        if wf.get("crop"):                               # the source can be cropped before it is rendered
            src_crop = nuke.nodes.Crop(name="SourceCrop", inputs=[inp], disable=True)
            src_crop["reformat"].setValue(True)          # the cropped area becomes the whole (smaller) frame
            src_crop["crop"].setValue(False)             # no 'black outside': bbox = the box, no 1 px black edge
            _source_write("SourceWrite", src_crop)
        else:
            _source_write("SourceWrite", inp)
        for i, ref in enumerate(wf.get("refs", []), 1):          # extra arrows: reference images
            ref_in = nuke.nodes.Input(name=ref["name"])
            ref_in["number"].setValue(i)
            _source_write("SourceWrite_" + ref["name"], ref_in)
        res = nuke.nodes.Read(name="Result")
        _set_enum(res, "localizationPolicy", "off")   # fresh renders, often re-rendered: never a stale local copy
        if wf.get("crop"):                               # a crop-size result, put back into the full plate frame
            res = _uncrop_nodes(res, "Uncrop")

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
        elif wf["output"]["kind"] == "text":
            choices = [inp]                               # a text reply: the plate just passes through
        else:
            choices = [inp, res]
        switch = nuke.nodes.Switch(name="OutputSwitch")
        for i, node in enumerate(choices):
            switch.setInput(i, node)
        switch["which"].setValue(0)
        if wf.get("crop"):
            # 'reformat' on: show the plate cropped (live crop area) exactly as it would be sent
            preview = nuke.nodes.Crop(name="CropPreview", inputs=[inp])
            preview["reformat"].setValue(True)
            preview["crop"].setValue(False)              # same as SourceCrop: bbox exactly the crop area
            for i, ch in enumerate("xyrt"):
                preview["box"].setExpression("parent.cg_crop_box.{}".format(ch), i)
            show = nuke.nodes.Switch(name="CropPreviewSwitch", inputs=[switch, preview])
            show["which"].setExpression("parent.cg_crop_preview && parent.cg_crop_on")
            nuke.nodes.Output(inputs=[show])
        else:
            nuke.nodes.Output(inputs=[switch])
    finally:
        group.end()


def _uncrop_nodes(src, prefix):
    """Reformat (to the plate size, no resize, anchored bottom-left, black outside) + Transform (to the crop's
    corner). Disabled = pass-through; set_uncrop() fills them in."""
    ref = nuke.nodes.Reformat(name=prefix + "_Reformat", inputs=[src], disable=True)
    ref["type"].setValue("to box")
    ref["box_fixed"].setValue(True)
    ref["resize"].setValue("none")
    ref["center"].setValue(False)
    ref["black_outside"].setValue(True)
    move = nuke.nodes.Transform(name=prefix + "_Move", inputs=[ref], disable=True)
    return move


def set_uncrop(ref, move, crop):
    """crop = 'x y r t W H' (Nuke pixels, y-up, of the plate) or '' for none."""
    vals = [float(v) for v in (crop or "").split()]
    on = len(vals) == 6
    ref["disable"].setValue(not on)
    move["disable"].setValue(not on)
    if on:
        x, y, _r, _t, w, h = vals
        ref["box_width"].setValue(int(w))
        ref["box_height"].setValue(int(h))
        move["translate"].setValue([x, y])


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
    name = nuke.String_Knob("cg_name", "Name", wf.get("default_name", ""))     # e.g. 'Chat' for a text gizmo
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
    if wf.get("crop"):
        c_on = nuke.Boolean_Knob("cg_crop_on", "crop", bool(wf["crop"].get("default", True)))
        c_on.setFlag(nuke.STARTLINE)
        c_on.setTooltip("Send only 'crop area' (more model resolution on the subject). The result is written at "
                        "crop size and put back into the full plate frame here. The area is saved with the "
                        "source version: after moving it, press 'Version up source'.")
        k(c_on)
        c_show = nuke.Boolean_Knob("cg_crop_preview", "reformat", False)
        c_show.clearFlag(nuke.STARTLINE)
        c_show.setTooltip("Show the plate cropped to 'crop area' and reformatted - exactly what is sent. "
                          "Untick to see the result again.")
        k(c_show)
        fmt = nuke.root().format()
        c_box = nuke.BBox_Knob("cg_crop_box", "crop area")
        c_box.setValue([0, 0, fmt.width(), fmt.height()])
        c_box.setTooltip("Starts as the whole plate and follows the plate's resolution until you change it. Drag "
                         "it in the viewer (panel open), in the plate's pixels.")
        k(c_box)
        c_auto = nuke.Boolean_Knob("cg_crop_auto", "cg_crop_auto", True)   # True until the user edits the box
        c_auto.setFlag(nuke.INVISIBLE)
        k(c_auto)
    if _has_send(wf):
        send = nuke.Enumeration_Knob("cg_send", "send", list(SEND_MODES))
        send.setFlag(nuke.STARTLINE)
        send.setTooltip("frame: one frame (as a PNG). frame range as video: the range as an H.264 MP4. "
                        "text only: just the prompt, nothing from the input.")
        k(send)
    if _single(wf) or _has_send(wf):
        fr = nuke.Int_Knob("cg_frame", "frame")
        fr.setFlag(nuke.STARTLINE)
        fr.setValue(int(nuke.frame()))
        fr.setTooltip("The frame to edit. Stays put when the playhead moves; change it here or with "
                      "'Set to current frame'.")
        k(fr)
        cur = nuke.PyScript_Knob("cg_frame_set", "Set to current frame", "comfygizmos.set_frame(nuke.thisNode())")
        cur.clearFlag(nuke.STARTLINE)
        k(cur)
        if not _is_text(wf):
            hold = nuke.Boolean_Knob("cg_hold", "hold on all frames", True)
            hold.clearFlag(nuke.STARTLINE)
            hold.setTooltip("Show the edited frame on every frame (off: only on its own frame, black elsewhere).")
            k(hold)
    if not _single(wf):
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
    _add_workflow_knobs(group, wf)

    k(nuke.Text_Knob("cg_div_out", "output"))
    out_root = nuke.File_Knob("cg_out_root", "output root")
    out_root.setTooltip("Follows the script: {} is the evaluated project directory (one folder up from the "
                        ".nk when it is empty). A plain path works too.".format(PROJECT_TOKEN))
    k(out_root)
    out_root.setValue(_default_output_root())
    if not _is_text(wf):
        out_cs = nuke.String_Knob("cg_out_cs", "output colorspace", _default_out_cs(wf))
        out_cs.setTooltip("Colorspace of the written EXRs and of the Read that loads them. 'raw' = untagged."
                          + ("" if wf["output"].get("convert", True) else
                             " This workflow's output is data (a matte): values are written untouched, only "
                             "labelled with this colorspace.")
                          + (" " + wf["output"]["tooltip"] if wf["output"].get("tooltip") else ""))
        k(out_cs)
    ver = nuke.Int_Knob("cg_version", "version")
    ver.setValue(1)
    ver.setTooltip(("Output version: outputs/<Name>_{0}/<Name>_{0}_v###. Separate from the source version."
                    if not _is_text(wf) else
                    "Version of the record kept in outputs/<Name>_{0}/: <Name>_{0}_v###.png / .mp4 (what was "
                    "sent) and .txt (the reply).").format(wf["suffix"]))
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
    if not _is_text(wf):
        k(nuke.PyScript_Knob("cg_reload", "Reload result", "comfygizmos.load_result(nuke.thisNode())"))
        k(nuke.PyScript_Knob("cg_bring_in", "Bring in output", "comfygizmos.read_output(nuke.thisNode())"))
    k(nuke.Text_Knob("cg_status", "status", "not rendered"))
    if _is_text(wf):
        k(nuke.Text_Knob("cg_div_reply", "reply"))
        k(nuke.Multiline_Eval_String_Knob("cg_reply", "reply", ""))
        cp = nuke.PyScript_Knob("cg_copy_reply", "Copy reply", "comfygizmos.copy_reply(nuke.thisNode())")
        cp.setFlag(nuke.STARTLINE)
        k(cp)
        down = nuke.PyScript_Knob("cg_send_reply", "Send to prompt below",
                                  "comfygizmos.send_reply_down(nuke.thisNode())")
        down.setTooltip("Write the reply into the 'prompt' of the ComfyGizmos connected below this node "
                        "(e.g. a Nano Banana gizmo fed by this one).")
        k(down)
    for name in ("cg_prompt_id", "cg_result", "cg_result_first", "cg_result_last", "cg_result_cs",
                 "cg_result_crop"):
        hidden = nuke.String_Knob(name, name)
        hidden.setFlag(nuke.INVISIBLE)
        k(hidden)


def _add_workflow_knobs(group, wf):
    """The 'settings' part of the panel. One closed 'advanced' section holds, in this order: the enhancer
    buttons + refined prompt (if any), colorspace override, then the manifest's own 'advanced' knobs. With an
    enhancer it sits right under the 'refine prompt' checkbox; otherwise where the manifest puts 'advanced'
    (or at the end). Other manifest groups stay collapsible sections of their own."""
    k = group.addKnob
    specs = wf.get("knobs", [])
    enh = wf.get("enhancer")

    # split off the manifest's 'advanced' members (up to the next group, or the end)
    adv_members, main, in_adv = [], [], False
    for spec in specs:
        if spec["name"] == "advanced" and spec["type"] in ("group", "divider"):
            in_adv = True
            main.append({"name": "advanced", "type": "_advanced"})
            continue
        if in_adv and spec["type"] == "group":
            in_adv = False
        (adv_members if in_adv else main).append(spec)
    if enh:                                            # advanced goes right under the enhancer checkbox
        main = [m for m in main if m["type"] != "_advanced"]
        at = next((i + 1 for i, m in enumerate(main) if m["name"] == enh.get("after")), len(main))
        main.insert(at, {"name": "advanced", "type": "_advanced"})
    elif not any(m["type"] == "_advanced" for m in main):
        main.append({"name": "advanced", "type": "_advanced"})

    def add_advanced():
        k(nuke.Tab_Knob("cg_advanced", "advanced", nuke.TABBEGINCLOSEDGROUP))
        if enh:
            run = nuke.PyScript_Knob("cg_enhance", "Run enhancer only", "comfygizmos.enhance(nuke.thisNode())")
            run.setTooltip("Only the prompt enhancer runs (no image is made). Its rewrite lands in 'refined prompt'.")
            run.setFlag(nuke.STARTLINE)
            k(run)
            use = nuke.PyScript_Knob("cg_use_refined", "Use refined prompt", "comfygizmos.use_refined(nuke.thisNode())")
            use.setTooltip("Copy the refined prompt into 'prompt' and turn 'refine prompt' off.")
            k(use)
            k(nuke.Multiline_Eval_String_Knob("cg_refined", "refined prompt", ""))
        k(_colorspace_override_knob())
        for spec in adv_members:
            add_one(spec)
        k(nuke.Tab_Knob("cg_advanced_end", "", nuke.TABENDGROUP))

    def add_one(spec):
        k(_make_knob(_local_default(wf, spec)))
        if spec["name"] == "seed" and spec["type"] == "int":
            inc = nuke.Boolean_Knob("cg_seed_increment", "increment", True)
            inc.clearFlag(nuke.STARTLINE)
            inc.setTooltip("On: after each render is sent, the seed goes up by 1 (like ComfyUI's 'increment'), so "
                           "every render is a new attempt. The seed a render used is in its status line. Off: the "
                           "seed stays as typed, to reproduce a result.")
            k(inc)

    if specs:
        k(nuke.Text_Knob("cg_div_wf", "settings"))
    group_open = False
    for spec in main:
        if spec["type"] == "_advanced":
            if group_open:
                k(nuke.Tab_Knob("wf_group_end_adv", "", nuke.TABENDGROUP))
                group_open = False
            add_advanced()
        else:
            if spec["type"] == "group" and group_open:   # a new group closes the previous one
                k(nuke.Tab_Knob("wf_group_end_" + spec["name"], "", nuke.TABENDGROUP))
            group_open = group_open or spec["type"] == "group"
            add_one(spec)
        if wf.get("boxes") and spec["name"] == ("advanced" if enh else wf["boxes"].get("after")):
            _add_box_knobs(group, wf["boxes"])          # Flux: boxes follow the advanced section
    if group_open:
        k(nuke.Tab_Knob("wf_group_end", "", nuke.TABENDGROUP))


def _colorspace_override_knob():
    cs = nuke.String_Knob("cg_colorspace", "colorspace override")
    cs.setTooltip("Leave empty: worked out from the Read / the working space. Otherwise a ComfyUI-OCIO "
                  "colorspace name, e.g. 'ACES - ACEScg'.")
    return cs


def _single(wf):
    return wf.get("frames") == "single"


# 'send' knob (manifest "frames": "send"): label -> what goes to ComfyUI
SEND_MODES = {"frame": "frame", "frame range as video": "range", "text only": "none"}


def _has_send(wf):
    return wf.get("frames") == "send"


def _is_text(wf):
    return wf["output"]["kind"] == "text"


def _send_mode(group, wf):
    """'frame', 'range' or 'none' (text only) for this node right now."""
    if _has_send(wf):
        return SEND_MODES.get(group["cg_send"].value(), "frame")
    return "frame" if _single(wf) else "range"


def _update_send_knobs(group):
    """Show only the knobs the chosen 'send' mode uses."""
    if "cg_send" not in group.knobs():
        return
    mode = SEND_MODES.get(group["cg_send"].value(), "frame")
    for names, show in ((("cg_frame", "cg_frame_set"), mode == "frame"),
                        (("cg_input_range", "cg_first", "cg_last", "cg_max_frames"), mode == "range"),
                        (("cg_colorspace", "cg_src_version", "cg_src_version_up"), mode != "none")):
        for name in names:
            if name in group.knobs():
                group[name].setVisible(show)


def _default_out_cs(wf):
    out = wf["output"]
    return "raw" if out.get("raw") else out.get("colorspace", DEFAULT_COLORSPACE)


def _knob_value(knob):
    """A knob's value; text knobs as typed. Nuke's Eval string knobs run value() through TCL, so a prompt
    with [ ] or $ in it would arrive mangled ('[the wall]' -> 'the: Unknown command')."""
    if isinstance(knob, nuke.EvalString_Knob) and hasattr(knob, "getText"):   # String / Multiline knobs only
        return knob.getText()
    return knob.value()


def _local_default(wf, spec):
    """A knob default from ~/.nuke/comfygizmos_prefs.json ("knob_defaults": {workflow id: {knob: value}}) -
    for machine/account values that must not ship in the manifest (e.g. a GCP project)."""
    local = (prefs().get("knob_defaults") or {}).get(wf["id"], {})
    if spec.get("name") in local:
        spec = dict(spec, default=local[spec["name"]])
    return spec


def _make_knob(spec):
    name, label, kind = "wf_" + spec["name"], spec.get("label", spec["name"]), spec["type"]
    if kind == "divider":
        return nuke.Text_Knob("wf_" + spec["name"], label)
    if kind == "group":                              # collapsible (closed) section up to the next group / the end
        return nuke.Tab_Knob(name, label, nuke.TABBEGINCLOSEDGROUP)
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
    elif kind == "choice":                           # optional "labels": what the menu shows for each value
        labels = spec.get("labels") or spec["values"]
        knob = nuke.Enumeration_Knob(name, label, labels)
        knob.setValue(labels[spec["values"].index(spec.get("default", spec["values"][0]))])
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


_fitting_crop = False   # True while the crop area is set from the plate (not by the user)


NO_HANDLES = 0x8000     # DDImage Knob::NO_HANDLES - no viewer handles for this knob (not exposed as nuke.*)


def _crops_without_black_edge(group):
    """Older nodes: their internal crops had 'black outside' on (bbox 1 px bigger, a black edge in the source)."""
    for name in ("SourceCrop", "CropPreview"):
        node = group.node(name)
        if node is not None and node["crop"].value():
            node["crop"].setValue(False)


def _crop_handles(group):
    """With 'reformat' on the viewer shows the cropped image, which starts at the crop's corner - the crop area
    handles would be drawn in plate coordinates over it and look moved. Hide them then; the values don't change."""
    if "cg_crop_box" not in group.knobs():
        return
    if "cg_crop_preview" in group.knobs() and group["cg_crop_preview"].value():
        group["cg_crop_box"].setFlag(NO_HANDLES)
    else:
        group["cg_crop_box"].clearFlag(NO_HANDLES)


def _fit_crop_to_plate(group):
    """Crop area = the whole plate, until the user changes it."""
    global _fitting_crop
    if "cg_crop_auto" not in group.knobs() or not group["cg_crop_auto"].value() or group.input(0) is None:
        return
    fmt = group.input(0).format()
    _fitting_crop = True
    try:
        group["cg_crop_box"].setValue([fmt.x(), fmt.y(), fmt.r(), fmt.t()])
    finally:
        _fitting_crop = False


def _crop_on(group):
    return "cg_crop_on" in group.knobs() and bool(group["cg_crop_on"].value())


def _update_source_knobs(group):
    """Source version only matters when an input has to be rendered - grey it out when every connected
    input (plate and refs) is a plain Read."""
    needs_render = any(group.input(i) is not None and _direct_read(group, i) is None
                       for i in range(group.inputs())) or _crop_on(group)
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
    state = group["cg_state"].value() if "cg_state" in group.knobs() else ""
    label = "{}\nv{:03d}".format(name, int(group["cg_version"].value())).lstrip("\n")
    group["label"].setValue(label + ("\n" + state if state else ""))


def _set_state(group, state):
    """One-word render state on the node's label: queued / rendering / done / failed / cancelled /
    refining / refined. It stays until the next render."""
    try:
        if "cg_state" not in group.knobs():
            knob = nuke.String_Knob("cg_state", "cg_state", "")
            knob.setFlag(nuke.INVISIBLE)
            group.addKnob(knob)
        if group["cg_state"].value() != state:
            group["cg_state"].setValue(state)
            _update_label(group)
            group["tile_color"].setValue(STATE_TILE.get(state, TILE_DEFAULT))
    except Exception:
        pass


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
    send = _send_mode(group, wf)                          # frame / range / none (text only)
    single = send != "range"
    if group.input(0) is None and send != "none":
        raise _Abort("nothing connected to the input")
    if not client.is_up(host):
        raise _Abort("ComfyUI is not running at " + host)

    # frame range
    if send == "none":
        first = last = int(nuke.frame())                  # nothing is read - only used in names / logs
    elif single:
        first = last = int(group["cg_frame"].value())       # fixed - never follows the playhead
    elif group["cg_input_range"].value():
        first, last = int(group.input(0).firstFrame()), int(group.input(0).lastFrame())
    else:
        first, last = int(group["cg_first"].value()), int(group["cg_last"].value())
    count = last - first + 1
    if count < 1:
        raise _Abort("empty frame range {}-{}".format(first, last))
    max_frames = 0 if single else _max_frames(group, wf)
    if max_frames and count > max_frames:
        raise _Abort("{} frames ({}-{}) - 'max frames' is {}. Raise it, split the shot or set a "
                     "shorter range.".format(count, first, last, max_frames))

    # naming: <Name>_<gizmo>; a plain Read with no Name uses the plate's file name
    gizmo_name = wf["suffix"]
    typed = _clean_name(_knob_value(group["cg_name"])) if "cg_name" in group.knobs() else ""
    read = _direct_read(group)
    crop_on = _crop_on(group)
    if read is not None and crop_on:                       # cropping: even a plain Read is rendered first
        base = typed or plate_name(_read_path(read))
        read, src_path, nuke_cs, raw = None, None, None, False
    elif read is not None:
        src_path = _read_path(read)
        if not src_path:
            raise _Abort("the Read's path could not be made absolute - save the script first")
        raw = bool(read["raw"].value())
        nuke_cs = "" if raw else read["colorspace"].value()
        base = typed or plate_name(src_path)
    elif not crop_on:
        if not typed and send != "none":
            raise _Abort("fill in 'Name' at the top - it is needed when the input is not a plain Read")
        src_path, nuke_cs, raw, base = None, None, False, typed or "Prompt"
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
    crop = ""
    if read is None and send != "none":
        src_path = "{}/{}_Source_v{:03d}.%04d.exr".format(src_dir, stem, src_version)
        if "SourceCrop" in [n.name() for n in group.nodes()]:
            crop = _prepare_source_crop(group, src_path, first, crop_on)
        nuke_cs, rendered = _ensure_source(group, "SourceWrite", src_path, first, last)
        if crop:
            _save_source_crop(src_path, crop)
        notes.append("source v{:03d} {}{}".format(src_version, rendered or "reused",
                                                  " (crop {})".format(_crop_text(crop)) if crop else ""))
        nuke.tprint("[ComfyGizmos] {}: {}".format(group.name(), notes[-1]))

    # colorspace -> ComfyUI name
    available = client.colorspaces(host) if send != "none" else []
    comfy_cs = group["cg_colorspace"].value().strip()
    if send != "none" and not raw and not comfy_cs:
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

    prompt = _fill_graph(wf, group, src_path, raw, comfy_cs, first, last, out_dir, out_name, out_cs, out_raw,
                         single)
    _add_refs(prompt, wf, refs)
    _apply_boxes(prompt, wf, group)

    if _is_text(wf):                                      # a text reply: no images come back
        _apply_media(prompt, wf, send, count)
        prompt_id = client.submit(host, prompt)
        _after_submit_seed(group, notes)
        job = {"group": group.fullName(), "host": host, "prompt_id": prompt_id, "kind": "text",
               "text_node": wf["output"]["text_node"], "reply_path": "{}/{}.txt".format(out_dir, out_name),
               "started": time.time(), "notes": "; ".join(notes)}
        _jobs[group.fullName()] = job
        group["cg_prompt_id"].setValue(prompt_id)
        what = {"none": "text only", "frame": "frame {}".format(first),
                "range": "frames {}-{} as video".format(first, last)}[send]
        _set_status(group, "queued - {}{}".format(what, " - " + "; ".join(notes) if notes else ""))
        _set_state(group, "queued")
        return job

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
        _set_state(group, "refining")
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
    _after_submit_seed(group, notes)
    job = {"group": group.fullName(), "host": host, "prompt_id": prompt_id, "result": result,
           "first": first, "last": last, "colorspace": "raw" if out_raw else out_cs, "started": time.time(),
           "notes": "; ".join(notes), "crop": crop}
    if enh and "wf_" + enh["after"] in group.knobs() and group["wf_" + enh["after"]].value():
        job["enhancer_node"] = enh["output_node"]           # show what the enhancer made of the prompt
    _jobs[group.fullName()] = job
    group["cg_prompt_id"].setValue(prompt_id)
    what = "frame {}".format(first) if single else "{} frames".format(count)
    _set_state(group, "queued")
    _set_status(group, "queued - {}, {}{}".format(what, os.path.basename(result),
                                                  " - " + "; ".join(notes) if notes else ""))
    return job


def _restore_text_knobs(group):
    """Nodes made while the tall text editors existed (2026-10-21): drop the editor knob and show the text knob
    again - the text itself was always kept in the real knob."""
    for knob in list(group.allKnobs()):
        name = knob.name()
        if name.endswith("_ui") and knob.Class() == "PyCustom_Knob" and name[:-3] in group.knobs():
            group.removeKnob(knob)
            group[name[:-3]].clearFlag(nuke.INVISIBLE)


def _set_text(node, knob_name, text):
    """Set a text knob."""
    node[knob_name].setValue(text)


def _add_box_knobs(group, spec):
    """Area boxes (Flux 3 layout): per box an on switch, a description and a rectangle you drag in the viewer.
    Box N is <obj_N> in the prompt."""
    k = group.addKnob
    k(nuke.Tab_Knob("cg_boxes_group", "boxes", nuke.TABBEGINCLOSEDGROUP))
    root = nuke.root().format()
    w, h = root.width(), root.height()
    # box 1 top-left, 2 top-right, 3 bottom-left, 4 bottom-right (Nuke is y-up); more boxes reuse the corners
    corners = [(0.05, 0.55, 0.45, 0.95), (0.55, 0.55, 0.95, 0.95), (0.05, 0.05, 0.45, 0.45), (0.55, 0.05, 0.95, 0.45)]
    for i in range(1, int(spec.get("count", 4)) + 1):
        on = nuke.Boolean_Knob("cg_box{}_on".format(i), "box {}".format(i), False)
        on.setFlag(nuke.STARTLINE)
        on.setTooltip("Use this box. It is <obj_{}> in the prompt.".format(i))
        k(on)
        desc = nuke.String_Knob("cg_box{}_desc".format(i), "", "")
        desc.clearFlag(nuke.STARTLINE)
        desc.setTooltip("What is in the box, e.g. 'dress' or 'left window'.")
        k(desc)
        box = nuke.BBox_Knob("cg_box{}".format(i), "area")
        cx, cy, cr, ct = corners[(i - 1) % len(corners)]
        box.setValue([w * cx, h * cy, w * cr, h * ct])
        box.setTooltip("Drag it in the viewer (panel open), in the plate's pixels.")
        k(box)
    k(nuke.Tab_Knob("cg_boxes_group_end", "", nuke.TABENDGROUP))


def _apply_boxes(prompt, wf, group):
    """The ticked boxes -> Create Bounding Boxes in plate pixels (y down, canvas a multiple of 16)."""
    spec = wf.get("boxes")
    if not spec:
        return
    plate = group.input(0)
    pw, ph = (plate.width(), plate.height()) if plate is not None else (nuke.root().width(), nuke.root().height())
    cw, ch = max(64, int(round(pw / 16.0)) * 16), max(64, int(round(ph / 16.0)) * 16)
    sx, sy = cw / float(pw), ch / float(ph)
    boxes = []
    for i in range(1, int(spec.get("count", 4)) + 1):
        if "cg_box{}_on".format(i) not in group.knobs() or not group["cg_box{}_on".format(i)].value():
            continue
        x, y, r, t = group["cg_box{}".format(i)].value()
        x, r = sorted((min(max(x, 0), pw), min(max(r, 0), pw)))
        y, t = sorted((min(max(y, 0), ph), min(max(t, 0), ph)))
        if r - x < 1 or t - y < 1:
            continue
        boxes.append({"x": int(round(x * sx)), "y": int(round((ph - t) * sy)),          # Nuke is y-up
                      "width": int(round((r - x) * sx)), "height": int(round((t - y) * sy)),
                      "metadata": {"type": "obj", "text": "", "palette": [],
                                   "desc": _knob_value(group["cg_box{}_desc".format(i)]).strip()}})
    node = prompt[spec["node"]]["inputs"]
    node.update({"width": cw, "height": ch, "editor_state": {"__value__": boxes},
                 "last_incoming": {"__value__": []}})


def _after_submit_seed(group, notes):
    """Once a render is in ComfyUI's queue: note the seed it used, and with 'increment' on, step the seed for
    the next render (ComfyUI's 'control after generate: increment'). Never for 'Run enhancer only'."""
    if "wf_seed" not in group.knobs():
        return
    seed = int(group["wf_seed"].value())
    notes.append("seed {}".format(seed))
    if "cg_seed_increment" in group.knobs() and group["cg_seed_increment"].value():
        group["wf_seed"].setValue(seed + 1)


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


def _crop_sidecar(src_path):
    """sources/.../X_Source_v001.%04d.exr -> sources/.../X_Source_v001.crop.json"""
    return re.sub(r"\.%0?\d*d\.[^./]+$", "", src_path) + ".crop.json"


def _save_source_crop(src_path, crop):
    with open(_crop_sidecar(src_path), "w", encoding="utf-8") as f:
        json.dump({"crop": crop}, f)


def _crop_text(crop):
    x, y, r, t, _w, _h = [int(float(v)) for v in crop.split()]
    return "{}x{} at {},{}".format(r - x, t - y, x, y)


def _prepare_source_crop(group, src_path, frame, crop_on):
    """Set the SourceCrop node for this source version; returns its crop 'x y r t W H' ('' = none).
    An existing source version keeps the area it was rendered with (its .crop.json), whatever the knob says."""
    node = group.node("SourceCrop")
    node["crop"].setValue(False)                          # bbox exactly the box (older nodes had black outside on)
    plate = group.input(0)
    pw, ph = (plate.width(), plate.height()) if plate is not None else (nuke.root().width(), nuke.root().height())
    crop = ""
    side = _crop_sidecar(src_path)
    if os.path.isfile(src_path % frame):                  # this source version exists: use what it was made with
        try:
            with open(side, encoding="utf-8") as f:
                crop = json.load(f).get("crop", "")
        except (OSError, ValueError):
            crop = ""                                      # rendered without a crop
    elif crop_on:
        x, y, r, t = group["cg_crop_box"].value()
        x, r = sorted((int(max(0, min(pw, x))), int(max(0, min(pw, r + 0.999)))))
        y, t = sorted((int(max(0, min(ph, y))), int(max(0, min(ph, t + 0.999)))))
        if r - x < 16 or t - y < 16:
            raise _Abort("the crop area is too small or outside the plate")
        crop = "{} {} {} {} {} {}".format(x, y, r, t, pw, ph)
    if crop:
        x, y, r, t, _w, _h = [float(v) for v in crop.split()]
        node["box"].setValue([x, y, r, t])
    node["disable"].setValue(not crop)
    return crop


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
        _set_state(group, "rendering")
        start = prev = frames[0]
        for f in frames[1:] + [None]:                # contiguous runs, so existing frames are left alone
            if f is not None and f == prev + 1:
                prev = f
                continue
            nuke.execute(write, start, prev, 1)
            start = prev = f
    finally:
        write["disable"].setValue(True)


def _fill_graph(wf, group, src_path, raw, comfy_cs, first, last, out_dir, stem, out_cs, out_raw, single):
    prompt = copy.deepcopy(_api_graph(wf))
    frame_mode = _frame_mode(src_path) if src_path else "single"

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
    if mfi and not single and _max_frames(group, wf):
        prompt[mfi["node"]]["inputs"][mfi["input"]] = _max_frames(group, wf)

    for spec in wf.get("knobs", []):
        if spec["type"] in ("divider", "group"):
            continue
        val = _knob_value(group["wf_" + spec["name"]])
        if not val and spec.get("env_fallback"):     # e.g. an empty GCP project -> GOOGLE_CLOUD_PROJECT
            val = os.environ.get(spec["env_fallback"], "")
        if spec["type"] == "int":
            val = int(val)
        elif spec["type"] == "float":
            val = float(val)
        elif spec["type"] == "bool":
            val = bool(val) != bool(spec.get("invert"))     # invert: the node's switch means the opposite
        elif spec["type"] == "choice" and spec.get("labels"):
            val = spec["values"][spec["labels"].index(val)]
        for target in spec.get("targets") or [{"node": spec["node"], "input": spec["input"]}]:
            prompt[target["node"]]["inputs"][target["input"]] = val      # one knob may drive several nodes
    return prompt


def _apply_media(prompt, wf, send, count):
    """Text gizmos: send the frame as a PNG, the range as an H.264 MP4 (both raw sRGB, shrunk by Plate
    Prep), or nothing at all (text only)."""
    media = wf["media"]
    model = prompt[media["node"]]["inputs"]
    for kind in ("frame", "range"):
        model[media[kind]["input"]] = ""
    if send == "none":
        for nid in (wf["read"]["node"], media["prep"], wf["write"]["node"]):
            prompt.pop(nid, None)
        return
    spec = media[send]
    wr = prompt[wf["write"]["node"]]["inputs"]
    wr.update(spec["write"])
    wr.update({"raw_data": True, "colorspace_in_name": False, "fps": float(nuke.root()["fps"].value())})
    prompt[media["prep"]]["inputs"].update({"max_width": spec["max_size"], "max_height": spec["max_size"],
                                           "max_frames": max(1, count)})
    model[spec["input"]] = [wf["write"]["node"], 0]      # OCIO Write's 'path' output -> the file Gemini reads
    model[spec["mime_input"]] = spec["mime"]


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
                if job.get("kind") != "enhance":
                    ui(_state_by_name, job, "rendering")
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


def _state_by_name(job, state):
    node = _node(job)
    if node is not None:
        _set_state(node, state)


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
        _set_state(node, "cancelled" if error == "cancelled" else "failed")
        nuke.tprint("[ComfyGizmos] {}: {}".format(node.name(), error))
        return
    if job.get("kind") == "text":
        reply = _output_text(outputs, job["text_node"])
        _set_text(node, "cg_reply", reply)
        if reply:
            try:                                          # keep what was answered next to what was sent
                os.makedirs(os.path.dirname(job["reply_path"]), exist_ok=True)
                with open(job["reply_path"], "w", encoding="utf-8") as f:
                    f.write(reply + "\n")
            except OSError as e:
                nuke.tprint("[ComfyGizmos] {}: reply not saved: {}".format(node.name(), e))
        _set_status(node, "reply ready in {}s{}".format(int(time.time() - job["started"]),
                                                        " - " + job["notes"] if job.get("notes") else "")
                    if reply else "finished, but Gemini returned no text (blocked or empty)")
        _set_state(node, "done" if reply else "failed")
        return
    text = _output_text(outputs, job.get("enhancer_node"))
    if text and "cg_refined" in node.knobs():
        _set_text(node, "cg_refined", text)
    if job.get("kind") == "enhance":
        _set_status(node, "refined prompt ready in {}s - see 'refined prompt'".format(int(time.time() - job["started"]))
                    if text else "prompt enhancer finished but returned no text")
        _set_state(node, "refined" if text else "failed")
        return
    missing = [f for f in range(job["first"], job["last"] + 1) if not os.path.isfile(job["result"] % f)]
    if missing:
        _set_status(node, "finished, but {} frame(s) are missing in {}".format(
            len(missing), os.path.dirname(job["result"])))
        _set_state(node, "failed")
        return
    node["cg_result"].setValue(job["result"])
    node["cg_result_first"].setValue(str(job["first"]))
    node["cg_result_last"].setValue(str(job["last"]))
    node["cg_result_cs"].setValue(job["colorspace"])
    if "cg_result_crop" in node.knobs():
        node["cg_result_crop"].setValue(job.get("crop", ""))
    got = load_result(node)
    _set_state(node, "done")
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


def copy_reply(group):
    """Text gizmos: the reply -> the clipboard."""
    text = _knob_value(group["cg_reply"]).strip()
    if not text:
        _set_status(group, "no reply yet")
        return
    try:
        from PySide6 import QtWidgets                     # Nuke 16+
    except ImportError:
        from PySide2 import QtWidgets
    QtWidgets.QApplication.clipboard().setText(text)
    _set_status(group, "reply copied to the clipboard")


def send_reply_down(group):
    """Text gizmos: the reply -> 'prompt' of every ComfyGizmo fed (directly or further down) by this node."""
    text = _knob_value(group["cg_reply"]).strip()
    if not text:
        _set_status(group, "no reply yet")
        return
    found, seen, todo = [], set(), list(group.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, forceEvaluate=False))
    while todo:
        n = todo.pop()
        if n.fullName() in seen:
            continue
        seen.add(n.fullName())
        if "cg_workflow" in n.knobs() and "wf_prompt" in n.knobs():
            _set_text(n, "wf_prompt", text)
            found.append(n.name())
        todo.extend(n.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, forceEvaluate=False))
    if found:
        _set_status(group, "reply sent to the prompt of " + ", ".join(sorted(found)))
    else:
        _set_status(group, "no ComfyGizmo with a prompt is connected below this node")
        if nuke.GUI:
            nuke.message("{}: connect a gizmo with a prompt (e.g. Nano Banana) below this node first.".format(
                group.name()))


def use_refined(group):
    """Refined prompt -> prompt, and stop refining (the rewrite is already in the prompt)."""
    text = _knob_value(group["cg_refined"]).strip() if "cg_refined" in group.knobs() else ""
    if not text:
        if nuke.GUI:
            nuke.message("{}: no refined prompt yet - press 'Run enhancer only' first.".format(group.name()))
        return
    _set_text(group, "wf_prompt", text)
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
    if group.node("Uncrop_Move") is not None:            # crop-size result -> back into the plate frame
        set_uncrop(group.node("Uncrop_Reformat"), group.node("Uncrop_Move"), _knob_value(group["cg_result_crop"]))
    res["on_error"].setValue("black")
    # Read straight from disk: with localization on (e.g. an auto-localized network drive), a re-render of the same
    # version keeps showing the old local copy and 'reload' doesn't re-copy it. Also fixes nodes made earlier.
    _set_enum(res, "localizationPolicy", "off")
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


def _refresh_localization(read):
    """If this Read is localized, re-copy its files now - so a re-rendered version doesn't show the old copy."""
    try:
        if read["localizationPolicy"].value() == "off":
            return
        before = nuke.selectedNodes()
        for n in before:
            n.setSelected(False)
        read.setSelected(True)
        nuke.localization.forceUpdateSelectedNodes()
        read.setSelected(False)
        for n in before:
            n.setSelected(True)
    except Exception as e:                            # no localization in this Nuke / not available headless
        nuke.tprint("[ComfyGizmos] localization update skipped: {}".format(e))


def _node_size(node):
    """(width, height) in the node graph; a fresh node may not be drawn yet, so fall back to typical sizes."""
    w, h = node.screenWidth(), node.screenHeight()
    if w <= 0:
        w = 80
    if h <= 0:
        h = 78 if node.Class() == "Read" else 18       # a Read shows its thumbnail
    return w, h


def _stack_below(anchor, nodes, gap=30):
    """Place nodes straight under anchor, centred, one under another - no overlaps."""
    aw, ah = _node_size(anchor)
    y = anchor.ypos() + ah + gap
    for n in nodes:
        w, h = _node_size(n)
        n.setXYpos(int(anchor.xpos() + (aw - w) / 2), int(y))
        y += h + gap


def read_output(group):
    """Bring in output: a Read of the rendered EXRs next to this node. Mattes (LTX Alpha Gen) also get a Crop
    (to the plate's format) and a Shuffle (red -> alpha); normals an Expression (0-1 -> -1..1). Returns the Read."""
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
        if _single(wf) and first == last:
            # one frame: read the actual file as a still, so it shows on every frame (like a Read of that file)
            read["file"].fromUserText(path % first)
        else:
            _point_read(read, path, first, last)
            _apply_hold(group, read)
        _set_read_colorspace(read, _result_cs(group))
        read.setName("Read_" + re.sub(r"\W", "_", plate_name(path)))
        read.setXYpos(group.xpos() + 150, group.ypos())
        _refresh_localization(read)                  # keeps your localization policy, but never a stale copy
        last = read
        crop_info = _knob_value(group["cg_result_crop"]) if "cg_result_crop" in group.knobs() else ""
        if crop_info and wf.get("output", {}).get("kind") == "matte":
            # crop-size matte: Read -> Crop -> Undo crop (back to the plate, as undoCrop.py does) -> Shuffle
            x, y, _r, _t, pw, ph = [float(v) for v in crop_info.split()]
            crop = nuke.nodes.Crop(inputs=[read])
            crop["box"].setValue([0, 0, read.width(), read.height()])
            crop["crop"].setValue(True)                    # black outside (set explicitly: knob defaults may differ)
            crop.setXYpos(read.xpos(), read.ypos() + 120)
            undo = nuke.nodes.Crop(inputs=[crop])
            undo["reformat"].setValue(True)
            undo["crop"].setValue(False)
            undo["box"].setValue([-x, -y, -x + pw, -y + ph])
            undo["label"].setValue("Undo crop")
            undo.setXYpos(read.xpos(), crop.ypos() + 50)
            last = nuke.nodes.Shuffle2(inputs=[undo])
            last["mappings"].setValue([(0, "rgba.red", "rgba.red"), (0, "rgba.red", "rgba.green"),
                                       (0, "rgba.red", "rgba.blue"), (0, "rgba.red", "rgba.alpha")])
            last["label"].setValue("red -> rgba")
            last.setXYpos(read.xpos(), undo.ypos() + 50)
        elif wf.get("output", {}).get("kind") == "matte":   # LTX Alpha Gen: Read -> Crop -> Shuffle
            crop = nuke.nodes.Crop(inputs=[last])
            crop["crop"].setValue(True)                    # black outside (set explicitly: knob defaults may differ)
            if crop_info:
                _x, _y, _r, _t, pw, ph = [float(v) for v in crop_info.split()]
                crop["box"].setValue([0, 0, pw, ph])
            else:
                crop["box"].setValue([0, 0, read.width(), read.height()])
            crop.setXYpos(read.xpos(), last.ypos() + 120)
            last = nuke.nodes.Shuffle2(inputs=[crop])
            last["mappings"].setValue([(0, "rgba.red", "rgba.red"), (0, "rgba.green", "rgba.green"),
                                       (0, "rgba.blue", "rgba.blue"), (0, "rgba.red", "rgba.alpha")])
            last["label"].setValue("red -> alpha")
            last.setXYpos(read.xpos(), crop.ypos() + 50)
        elif wf.get("output", {}).get("kind") == "normals":  # DSINE: Read -> decode
            last = _decode_normals(read)
            last.setXYpos(read.xpos(), read.ypos() + 120)
        chain = [last]                                    # Read ... last, top to bottom
        while chain[0].fullName() != read.fullName() and chain[0].input(0) is not None:
            chain.insert(0, chain[0].input(0))
        _stack_below(group, chain)
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
        _set_state(group, "cancelled")
    except client.ComfyError as e:
        _set_status(group, "cancel failed: {}".format(e))


def _set_status(group, text):
    try:
        group["cg_status"].setValue(text)
    except Exception:
        pass
