"""Name / sources / outputs / separate source + output versions, end-to-end in Nuke (real ComfyUI renders):

    Nuke14.1.exe --nukex -i -t tests/nuke_test_naming.py <package_parent_dir> <project_dir> <plate.%04d.exr> f<first> f<last>

LTX Alpha Gen through a Grade (input rendered to sources/), then a plain Read (sent as it is)."""
import glob
import os
import sys

import nuke

pkg_parent, project_dir, plate, first, last = sys.argv[1:6]
first, last = int(first.lstrip("f")), int(last.lstrip("f"))
sys.path.insert(0, pkg_parent)
import comfygizmos  # noqa: E402
from comfygizmos import gizmo  # noqa: E402

fails = []
root_dir = os.path.join(project_dir, "Comp", "Inputs", "ComfyUI").replace("\\", "/")


def check(ok, what):
    print("{:4} {}".format("ok" if ok else "FAIL", what))
    if not ok:
        fails.append(what)


def frames(pattern):
    return [pattern % f for f in range(first, last + 1)]


def stamps(pattern):
    return {p: os.path.getmtime(p) for p in frames(pattern) if os.path.isfile(p)}


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
g = comfygizmos.create("ltx_alpha")
g.setInput(0, grade)
gizmo._update_source_knobs(g)

# ---- knobs
check("cg_source" not in g.knobs() and "cg_reuse_input" not in g.knobs() and "cg_suffix" not in g.knobs()
      and "cg_version_auto" not in g.knobs(), "old knobs gone (source dropdown, reuse, suffix, from input)")
check(g.knobs()["cg_name"] is not None and g["cg_version_up"].label() == "Version up output",
      "Name knob + 'Version up output'")
check(int(g["cg_src_version"].value()) == 1 and int(g["cg_version"].value()) == 1, "source v001, output v001")
check(g["cg_src_version"].enabled(), "source version enabled for a Grade input")

# ---- Name required when not a plain Read
check(comfygizmos.render(g, wait=True) is None and "Name" in g["cg_status"].value(),
      "empty Name refused: " + g["cg_status"].value())

# ---- first render
g["cg_name"].setValue("Wall Fix!")
comfygizmos.knob_changed(g, g["cg_name"])
check(g["label"].value() == "Wall_Fix\nv001", "label = Name / v001: " + repr(g["label"].value()))
job = comfygizmos.render(g, wait=True)
print("  1:", g["cg_status"].value())
src1 = root_dir + "/sources/Wall_Fix_LTXAlpha/Wall_Fix_LTXAlpha_Source_v001.%04d.exr"
out1 = root_dir + "/outputs/Wall_Fix_LTXAlpha/Wall_Fix_LTXAlpha_v001_acescg.%04d.exr"
check(job is not None and g["cg_result"].value() == out1, "output " + g["cg_result"].value().replace(root_dir, "..."))
check(len(stamps(src1)) == last - first + 1, "source rendered to sources/Wall_Fix_LTXAlpha/..._Source_v001")
check("source v001 rendered" in g["cg_status"].value(), "status: source v001 rendered")
s1 = stamps(src1)

# ---- output version up: same source, new output
comfygizmos.version_up(g)
job = comfygizmos.render(g, wait=True)
print("  2:", g["cg_status"].value())
check(g["cg_result"].value().endswith("Wall_Fix_LTXAlpha_v002_acescg.%04d.exr"), "output v002")
check(g["label"].value() == "Wall_Fix\nv002", "label follows Version up output: " + repr(g["label"].value()))
check(stamps(src1) == s1 and "source v001 reused" in g["cg_status"].value(), "source v001 reused, not rendered")
check(all(os.path.isfile(p) for p in frames(out1)), "output v001 kept")

# ---- source version up after an upstream change
grade["white"].setValue(1.2)
comfygizmos.version_up_source(g)
comfygizmos.version_up(g)
job = comfygizmos.render(g, wait=True)
print("  3:", g["cg_status"].value())
src2 = src1.replace("_Source_v001", "_Source_v002")
check(len(stamps(src2)) == last - first + 1 and "source v002 rendered" in g["cg_status"].value(),
      "Version up source -> _Source_v002 rendered")
check(stamps(src1) == s1, "_Source_v001 untouched")
check(g["cg_result"].value().endswith("_v003_acescg.%04d.exr"), "output v003")

# ---- a missing source frame: only that one is rendered
s2 = stamps(src2)
gone = src2 % (first + 2)
os.remove(gone)
comfygizmos.version_up(g)
job = comfygizmos.render(g, wait=True)
print("  4:", g["cg_status"].value())
after = stamps(src2)
check(os.path.isfile(gone) and "1 missing frame" in g["cg_status"].value(), "missing frame re-rendered")
check(all(after[p] == t for p, t in s2.items() if p != gone), "other source frames untouched")

# ---- back to source v001
g["cg_src_version"].setValue(1)
comfygizmos.version_up(g)
job = comfygizmos.render(g, wait=True)
print("  5:", g["cg_status"].value())
check("source v001 reused" in g["cg_status"].value() and stamps(src1) == s1, "typing source 1 reuses _Source_v001")

# ---- plain Read, no Name: plate name, nothing in sources
before_sources = set(glob.glob(root_dir + "/sources/*"))
p = comfygizmos.create("ltx_alpha")
p.setInput(0, read)
gizmo._update_source_knobs(p)
check(not p["cg_src_version"].enabled() and not p["cg_src_version_up"].enabled(), "source knobs greyed for a plain Read")
comfygizmos.knob_changed(p, p["cg_name"])
check(p["label"].value() == gizmo.plate_name(plate) + "\nv001", "plain Read label = plate name / v001")
job = comfygizmos.render(p, wait=True)
print("  6:", p["cg_status"].value())
pname = gizmo.plate_name(plate)
check(p["cg_result"].value() == "{0}/outputs/{1}_LTXAlpha/{1}_LTXAlpha_v001_acescg.%04d.exr".format(root_dir, pname),
      "plain Read: outputs/<plate>_LTXAlpha/<plate>_LTXAlpha_v001 ({})".format(pname))
check(set(glob.glob(root_dir + "/sources/*")) == before_sources, "plain Read: nothing written to sources/")

# ---- plain Read + Name: Name wins
p["cg_name"].setValue("Hero")
job = comfygizmos.render(p, wait=True)
check(p["cg_result"].value() == root_dir + "/outputs/Hero_LTXAlpha/Hero_LTXAlpha_v001_acescg.%04d.exr",
      "plain Read + Name: outputs/Hero_LTXAlpha/Hero_LTXAlpha_v001")
print("FAILED: {}".format(fails) if fails else "PASS")
