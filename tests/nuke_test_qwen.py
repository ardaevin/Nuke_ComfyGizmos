"""Qwen Image Edit end-to-end in Nuke (real ComfyUI runs):

    Nuke14.1.exe --nukex -i -t tests/nuke_test_qwen.py <package_parent_dir> <project_dir> <plate.%04d.exr> <ref_plate.%04d.exr> f<frame> [workflow_id]

workflow_id: qwen_edit (default) or qwen_edit_turbo.

1. Run enhancer only -> refined prompt, nothing written
2. Use refined prompt -> copied into prompt, refine off
3. Render one frame with ref2 = a Read (sent as-is) and ref3 = a Grade (rendered first)
4. Output / hold / Bring in output, and the refs wired in the graph ComfyUI ran"""
import json
import os
import sys
import urllib.request

import nuke

pkg_parent, project_dir, plate, ref_plate, frame = sys.argv[1:6]
wf_id = sys.argv[6] if len(sys.argv) > 6 else "qwen_edit"
frame = int(frame.lstrip("f"))
sys.path.insert(0, pkg_parent)
import comfygizmos  # noqa: E402
from comfygizmos import colorspace  # noqa: E402

fails = []


def check(ok, what):
    print("{:4} {}".format("ok" if ok else "FAIL", what))
    if not ok:
        fails.append(what)


def history(group):
    pid = group["cg_prompt_id"].value()
    return json.load(urllib.request.urlopen("http://127.0.0.1:8188/history/" + pid))[pid]["prompt"][2]


root = nuke.root()
root["colorManagement"].setValue("OCIO")
root["OCIO_config"].setValue("aces_1.2")
root["first_frame"].setValue(frame - 4)
root["last_frame"].setValue(frame + 4)
root["project_directory"].setValue(project_dir)
nuke.frame(frame)

read = nuke.nodes.Read(file=plate, first=frame - 4, last=frame + 4, origfirst=frame - 4, origlast=frame + 4)
ref_read = nuke.nodes.Read(file=ref_plate, first=1001, last=1051, origfirst=1001, origlast=1051)
ref_grade = nuke.nodes.Grade(inputs=[ref_read], white=1.1)
for n in nuke.allNodes():
    n.setSelected(False)
read.setSelected(True)
wf = comfygizmos.workflows()[wf_id]
g = comfygizmos.create(wf_id)
g.setInput(0, read)

check([g.input(i) is not None for i in range(4)] == [True, False, False, False], "4 inputs, plate connected")
check(int(g["cg_frame"].value()) == frame and g["cg_hold"].value(), "frame = current frame at creation, hold on")
nuke.frame(frame + 3)
check(int(g["cg_frame"].value()) == frame, "frame stays put when the playhead moves")
comfygizmos.set_frame(g)
check(int(g["cg_frame"].value()) == frame + 3, "Set to current frame picks up the playhead")
g["cg_frame"].setValue(frame)
nuke.frame(frame + 2)                       # playhead elsewhere at render time - must still edit 'frame'
check("cg_input_range" not in g.knobs() and "cg_max_frames" not in g.knobs(), "no range / max frames knobs")
check(not g["wf_refine_prompt"].value(), "refine prompt off by default")

g["wf_prompt"].setValue("Make the plain background wall behind the man a bright blue.")
probe = "keep [the man] as is, $5 {x}"
g["wf_resolution"].setValue(1024)
if "wf_steps" in g.knobs():
    g["wf_steps"].setValue(12)

# 1. enhancer only
job = comfygizmos.enhance(g, wait=True)
print("ENHANCE STATUS:", g["cg_status"].value())
refined = g["cg_refined"].getText()
print("REFINED:", refined[:300].replace("\n", " "))
check(job is not None and len(refined) > 20, "enhancer only: refined prompt came back ({} chars)".format(len(refined)))
hp = history(g)
check(wf["write"]["node"] not in hp and hp["459:484"]["inputs"]["switch"] is True and hp["459:500"]["inputs"]["thinking"] is True,
      "enhancer only: no Write in the job, enhancer forced on")
check(not g["cg_result"].value(), "enhancer only: nothing rendered")

# 2. use refined
comfygizmos.use_refined(g)
check(g["wf_prompt"].getText().strip() == refined.strip() and not g["wf_refine_prompt"].value(),
      "Use refined prompt: copied, refine off")

g["wf_negative_prompt"].setValue(probe)          # TCL-looking text must reach ComfyUI untouched
# 3. render with refs
g.setInput(1, ref_read)
g.setInput(2, ref_grade)
job = comfygizmos.render(g, wait=True)
print("RENDER STATUS:", g["cg_status"].value())
result = g["cg_result"].value()
want = "_{}_v001_aces2065.%04d.exr".format(wf["suffix"])
check(job is not None and result.endswith(want), "named ..." + want)
check(os.path.isfile(result % frame), "frame {} on disk".format(frame))
check(not os.path.isfile(result % (frame - 1)), "only that one frame written")
hp = history(g)
check(hp["459:474"]["inputs"].get("images.image_2") == ["cg_ref2", 0]
      and hp["459:474"]["inputs"].get("images.image_3") == ["cg_ref3", 0]
      and hp["459:485"]["inputs"].get("images.image1") == ["cg_ref2", 0]
      and "images.image_4" not in hp["459:474"]["inputs"], "ref2 + ref3 wired, ref4 left out")
check(hp["cg_ref2"]["inputs"]["source"] == ref_plate.replace("\\", "/") and hp["cg_ref2"]["inputs"]["start_frame"] == frame,
      "ref2 sent as-is: " + os.path.basename(hp["cg_ref2"]["inputs"]["source"]))
check("/sources/" in hp["cg_ref3"]["inputs"]["source"] and "_Source_ref3_v001." in hp["cg_ref3"]["inputs"]["source"] and os.path.isfile(hp["cg_ref3"]["inputs"]["source"] % frame),
      "ref3 rendered first: " + os.path.basename(hp["cg_ref3"]["inputs"]["source"]))
check(hp["459:474"]["inputs"]["negative_prompt"] == probe, "text with [ ] $ { } sent untouched")
check(hp["459:484"]["inputs"]["switch"] is False and hp["459:484"]["inputs"]["on_false"].strip() == refined.strip(),
      "render used the refined text with refine off")

ref3_file = hp["cg_ref3"]["inputs"]["source"] % frame
ref3_mtime = os.path.getmtime(ref3_file)
g["wf_seed"].setValue(int(g["wf_seed"].value()) + 1)
comfygizmos.version_up(g)
job = comfygizmos.render(g, wait=True)
print("SECOND RENDER STATUS:", g["cg_status"].value())
check(job is not None and "ref3 source v001 reused" in g["cg_status"].value() and os.path.getmtime(ref3_file) == ref3_mtime
      and "_v002_aces2065" in g["cg_result"].value(),
      "second render (new seed, output version up): output v002, ref3 source v001 reused")
result = g["cg_result"].value()

res = g.node("Result")
check((int(res["first"].value()), int(res["last"].value())) == (frame, frame), "Read range {0}-{0}".format(frame))
check(res["before"].value() == "hold" and res["after"].value() == "hold", "hold before/after")
check(colorspace.unwrap_default(res["colorspace"].value()) == "ACES - ACES2065-1", "Read ACES2065-1")
check((res.width(), res.height()) == (read.width(), read.height()), "plate size {}x{}".format(res.width(), res.height()))
g["cg_hold"].setValue(False)
comfygizmos.knob_changed(g, g["cg_hold"])
check(res["before"].value() == "black", "hold off -> black outside the frame")
g["cg_hold"].setValue(True)
comfygizmos.knob_changed(g, g["cg_hold"])

br = comfygizmos.read_output(g)
chain = [br.Class()] + [n.Class() for n in [br.dependent()[0]]]
check(chain == ["Read", "Crop"] and br["before"].value() == "hold", "Bring in output: Read (hold) -> Crop")

# contact sheet: plate | edit (display-referred)
plate_v = nuke.nodes.OCIOColorSpace(inputs=[read], in_colorspace="ACES - ACES2065-1", out_colorspace="Output - sRGB")
edit_v = nuke.nodes.OCIOColorSpace(inputs=[g], in_colorspace="ACES - ACES2065-1", out_colorspace="Output - sRGB")
cs = nuke.nodes.ContactSheet(inputs=[plate_v, edit_v], rows=1, columns=2, width=1600, height=800, gap=10)
print("FAILED: {}".format(fails) if fails else "PASS")
