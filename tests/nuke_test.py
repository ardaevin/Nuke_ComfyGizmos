"""End-to-end test in Nuke (real ComfyUI renders):

    Nuke14.1.exe --nukex -i -t tests/nuke_test.py <package_parent_dir> <project_dir> <plate> f<first> f<last> [render]

Numbers are prefixed with 'f' because Nuke eats bare numbers as a frame range.
'render' = send through a Grade, so the input gets rendered to EXR first.

Checks: Read left on 'default' colorspace, output root following the project
directory, max frames (refused below the range, sent to ComfyUI), result range
loaded in full, ACEScg-labelled matte, Bring in output."""
import glob
import json
import os
import sys
import urllib.request

import nuke

pkg_parent, project_dir, plate, first, last = sys.argv[1:6]
first, last = int(first.lstrip("f")), int(last.lstrip("f"))
via_render = len(sys.argv) > 6 and sys.argv[6] == "render"
sys.path.insert(0, pkg_parent)
import comfygizmos  # noqa: E402
from comfygizmos import gizmo  # noqa: E402

fails = []


def check(ok, what):
    print("{:4} {}".format("ok" if ok else "FAIL", what))
    if not ok:
        fails.append(what)


root = nuke.root()
root["colorManagement"].setValue("OCIO")
root["OCIO_config"].setValue("aces_1.2")
root["first_frame"].setValue(first)
root["last_frame"].setValue(last)
root["project_directory"].setValue(project_dir)

read = nuke.nodes.Read(file=plate, first=first, last=last, origfirst=first, origlast=last)
print("Read colorspace left at:", read["colorspace"].value())
up = nuke.nodes.Grade(inputs=[read], white=1.0) if via_render else read

for n in nuke.allNodes():
    n.setSelected(False)
up.setSelected(True)
g = comfygizmos.create("ltx_alpha")
g.setInput(0, up)
if via_render:
    g["cg_source"].setValue("render input")

# output root: a reference to the project directory, resolved at render time
check(g["cg_out_root"].value() == "[value root.project_directory]/Comp/Inputs/ComfyUI",
      "output root shows the project reference: " + g["cg_out_root"].value())
check(g["cg_out_cs"].value() == "ACES - ACEScg", "output colorspace defaults to ACEScg")
check(int(g["cg_max_frames"].value()) == 145, "max frames defaults to 145")

# max frames below the range -> refused, nothing sent
count = last - first + 1
g["cg_max_frames"].setValue(count - 1)
check(comfygizmos.render(g, wait=True) is None and "max frames" in g["cg_status"].value(),
      "max frames {} refuses {} frames: {}".format(count - 1, count, g["cg_status"].value()))
g["cg_max_frames"].setValue(count + 7)

job = comfygizmos.render(g, wait=True)
print("STATUS:", g["cg_status"].value())
check(job is not None, "sent and finished")
result = g["cg_result"].value()
want_dir = "{}/Comp/Inputs/ComfyUI/".format(project_dir.rstrip("/"))
check(result.startswith(want_dir), "output under <project>/Comp/Inputs/ComfyUI: " + result)
check(result.endswith("_LTXAlpha_acescg.%04d.exr"), "named ..._LTXAlpha_acescg.%04d.exr")
files = [result % f for f in range(first, last + 1)]
check(all(os.path.isfile(f) for f in files), "all {} frames on disk".format(count))
check("frames {}-{}".format(first, last) in g["cg_status"].value(), "status reports the loaded range")

# what ComfyUI actually got
hist = json.load(urllib.request.urlopen("http://127.0.0.1:8188/history/" + g["cg_prompt_id"].value()))
prompt = hist[g["cg_prompt_id"].value()]["prompt"][2]
rd, wr, prep = prompt["9010"]["inputs"], prompt["9014"]["inputs"], prompt["9011"]["inputs"]
print("SENT read cs:", rd["input_colorspace"], "| write:", wr["from_colorspace"], "->",
      wr["output_colorspace"], "raw", wr["raw_data"], "| prep max_frames:", prep["max_frames"])
if not via_render:
    check(rd["input_colorspace"] == "ACES - ACES2065-1", "'default' Read sent as ACES2065-1")
check(wr["from_colorspace"] == wr["output_colorspace"] == "ACES - ACEScg" and not wr["raw_data"],
      "matte labelled ACEScg, no conversion")
check(prep["max_frames"] == count + 7, "max frames reaches the plate prep node")

# the gizmo's own Read
res = g.node("Result")
check((int(res["first"].value()), int(res["last"].value())) == (first, last),
      "internal Read range {}-{}".format(res["first"].value(), res["last"].value()))
check(not res["raw"].value() and gizmo.colorspace.unwrap_default(res["colorspace"].value()) == "ACES - ACEScg",
      "internal Read is ACEScg: " + res["colorspace"].value())
w, h = res.width(), res.height()
check((w, h) == (read.width(), read.height()), "plate resolution {}x{}".format(w, h))

for frame in (first, last):
    nuke.frame(frame)
    vals = [nuke.sample(g, "rgba.alpha", x * w / 8.0, y * h / 8.0) for x in range(1, 8) for y in range(1, 8)]
    print("ALPHA frame {}: min {:.3f} max {:.3f} mean {:.3f}".format(frame, min(vals), max(vals),
                                                                        sum(vals) / len(vals)))
    check(max(vals) > 0.5, "matte present on frame {}".format(frame))

# ACEScg label must not change the values: same seed/plate/range as a raw write would give 0..1 matte
nuke.frame(first)
check(0.0 <= min(vals) and max(vals) <= 1.001, "matte values stay 0-1 (no colour conversion)")

# Bring in output
br = comfygizmos.read_output(g)
check(br is not None and br.Class() == "Read", "Bring in output made a Read: " + (br.name() if br else "-"))
if br is not None:
    check(br["file"].value() == result, "  same file")
    check((int(br["first"].value()), int(br["last"].value())) == (first, last),
          "  range {}-{}".format(br["first"].value(), br["last"].value()))
    check(not br["raw"].value() and gizmo.colorspace.unwrap_default(br["colorspace"].value()) == "ACES - ACEScg",
          "  colorspace " + br["colorspace"].value())
    check(br.xpos() == g.xpos() + 150, "  placed next to the gizmo")

if via_render:
    src = glob.glob(os.path.join(project_dir, "Comp", "Inputs", "ComfyUI", "_source", "*", "*.exr"))
    check(len(src) == count, "rendered input: {} source frames".format(len(src)))

nuke.scriptSaveAs(os.path.join(project_dir, "comfygizmos_test.nk"), overwrite=1)
print("FAILED: {}".format(fails) if fails else "PASS")
