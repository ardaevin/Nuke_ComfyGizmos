"""Versioning (real ComfyUI renders):

    Nuke14.1.exe --nukex -i -t tests/nuke_test_version.py <package_parent_dir> <project_dir> <plate_v001> f<first> f<last>

- version from a directly connected Read (and from a v003 path), shown in the label
- a non-Read input gives v001
- typing a version turns 'from input' off
- Version up -> output in ..._v002_<suffix>/, and the rendered input is reused across it"""
import glob
import os
import sys
import time

import nuke

pkg_parent, project_dir, plate, first, last = sys.argv[1:6]
first, last = int(first.lstrip("f")), int(last.lstrip("f"))
sys.path.insert(0, pkg_parent)
import comfygizmos  # noqa: E402

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
fake = nuke.nodes.Read(file="X:/nowhere/SH0400_plate_v003.%04d.exr", first=first, last=last)
grade = nuke.nodes.Grade(inputs=[read], white=1.0)
for n in nuke.allNodes():
    n.setSelected(False)
read.setSelected(True)
g = comfygizmos.create("ltx_alpha")
g.setInput(0, read)
comfygizmos.knob_changed(g, g["inputChange"] if "inputChange" in g.knobs() else g["cg_version_auto"])

check(int(g["cg_version"].value()) == 1 and g["cg_version_auto"].value(), "v001 plate Read -> version 1, from input")
check(os.path.basename(g["label"].evaluate()) == "v001", "label shows " + g["label"].evaluate())

g.setInput(0, fake)
comfygizmos.sync_version(g)
check(int(g["cg_version"].value()) == 3, "v003 Read -> version 3")
check(os.path.basename(g["label"].evaluate()) == "v003", "label shows " + g["label"].evaluate())

g.setInput(0, grade)
comfygizmos.sync_version(g)
check(int(g["cg_version"].value()) == 1, "Grade (not a Read) -> version 1")

g["cg_version"].setValue(5)
comfygizmos.knob_changed(g, g["cg_version"])
check(not g["cg_version_auto"].value(), "typing a version turns 'from input' off")
g["cg_version_auto"].setValue(True)
comfygizmos.knob_changed(g, g["cg_version_auto"])
check(int(g["cg_version"].value()) == 1, "back to 'from input' -> 1")

# render v001 through the Grade (input gets rendered), then version up -> v002 reuses it
job = comfygizmos.render(g, wait=True)
r1 = g["cg_result"].value()
print("  v1:", g["cg_status"].value())
check(job is not None and "_Crop_v001_LTXAlpha/" in r1 and r1.endswith("_Crop_v001_LTXAlpha_acescg.%04d.exr"),
      "v001 output: " + os.path.basename(r1))
src = glob.glob(os.path.join(project_dir, "Comp", "Inputs", "ComfyUI", "_source", "*", "*.exr"))
stamps = {f: os.path.getmtime(f) for f in src}
check(len(src) == last - first + 1, "rendered input cached in _source/ ({} frames)".format(len(src)))

time.sleep(1.1)
comfygizmos.version_up(g)
check(int(g["cg_version"].value()) == 2 and not g["cg_version_auto"].value(), "Version up -> 2, from input off")
check(os.path.basename(g["label"].evaluate()) == "v002", "label shows " + g["label"].evaluate())
job = comfygizmos.render(g, wait=True)
r2 = g["cg_result"].value()
print("  v2:", g["cg_status"].value())
check(job is not None and r2.endswith("_Crop_v002_LTXAlpha_acescg.%04d.exr") and "_Crop_v002_LTXAlpha/" in r2,
      "v002 output: " + os.path.basename(r2))
check(all(os.path.isfile(r2 % f) for f in range(first, last + 1)), "v002 frames on disk")
check(all(os.path.isfile(r1 % f) for f in range(first, last + 1)), "v001 frames kept")
check(all(os.path.getmtime(f) == t for f, t in stamps.items()), "input reused across the version up")
check(int(g["cg_version"].value()) == 2, "render kept the version (from input off)")

print("FAILED: {}".format(fails) if fails else "PASS")
