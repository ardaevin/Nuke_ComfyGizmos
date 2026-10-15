"""Reuse of an unchanged rendered input (real ComfyUI renders):

    Nuke14.1.exe --nukex -i -t tests/nuke_test_reuse.py <package_parent_dir> <project_dir> <plate> f<first> f<last>

Read -> Grade -> gizmo (input rendered to EXR). Then:
  change LoRA strength      -> input NOT re-rendered
  move the Grade            -> input NOT re-rendered
  change the Grade          -> input re-rendered
  untick 'reuse'            -> input re-rendered"""
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
grade = nuke.nodes.Grade(inputs=[read], white=1.0)
for n in nuke.allNodes():
    n.setSelected(False)
grade.setSelected(True)
g = comfygizmos.create("ltx_alpha")
g.setInput(0, grade)
g["cg_input_range"].setValue(True)


def source_stamps():
    files = sorted(glob.glob(os.path.join(project_dir, "Comp", "Inputs", "ComfyUI", "_source", "*", "*.exr")))
    return {f: os.path.getmtime(f) for f in files}


def run(label):
    time.sleep(1.1)                     # so a re-render shows up as a newer mtime
    before = source_stamps()
    job = comfygizmos.render(g, wait=True)
    after = source_stamps()
    rerendered = bool(before) and any(after.get(f) != t for f, t in before.items())
    print("  {:28} status: {}".format(label, g["cg_status"].value()))
    return job, before, after, rerendered


job, _, after, _ = run("first render")
check(job is not None and len(after) == last - first + 1, "first render made {} source frames".format(len(after)))

g["wf_lora_strength"].setValue(0.8)
job, _, _, rer = run("LoRA strength 0.8")
check(job is not None and not rer, "LoRA change: input reused")

grade.setXYpos(grade.xpos() + 300, grade.ypos() + 50)
grade.setSelected(True)
job, _, _, rer = run("Grade moved")
check(job is not None and not rer, "Grade moved: input reused")

grade["white"].setValue(1.2)
job, _, _, rer = run("Grade white 1.2")
check(job is not None and rer, "Grade changed: input re-rendered")

g["cg_reuse_input"].setValue(False)
job, _, _, rer = run("reuse off")
check(job is not None and rer, "reuse off: input re-rendered")

print("FAILED: {}".format(fails) if fails else "PASS")
