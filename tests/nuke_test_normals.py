"""DSINE Normals end-to-end in Nuke (real ComfyUI run):

    Nuke14.1.exe --nukex -i -t tests/nuke_test_normals.py <package_parent_dir> <project_dir> <image.jpg|png>

Makes a 3-frame ACES2065-1 EXR plate (1001-1003) from the image under <project_dir>/plate,
renders frame 1002 and checks: raw EXR at plate size, only that frame, the node's output
decoded to -1..1 (and the 'encoded 0..1' switch), Bring in output = Read -> Crop -> Expression."""
import os
import sys

import nuke

pkg_parent, project_dir, image = sys.argv[1:4]
sys.path.insert(0, pkg_parent)
import comfygizmos  # noqa: E402

fails = []


def check(ok, what):
    print("{:4} {}".format("ok" if ok else "FAIL", what))
    if not ok:
        fails.append(what)


def px(node, x, y, frame):
    return [node.sample(c, x, y, frame=frame) for c in ("rgba.red", "rgba.green", "rgba.blue")]


root = nuke.root()
root["colorManagement"].setValue("OCIO")
root["OCIO_config"].setValue("aces_1.2")
root["first_frame"].setValue(1001)
root["last_frame"].setValue(1003)
root["project_directory"].setValue(project_dir)

# plate: sRGB image -> ACES2065-1 EXR sequence
plate = "{}/plate/normals_test_plate.%04d.exr".format(project_dir)
os.makedirs(os.path.dirname(plate), exist_ok=True)
src = nuke.nodes.Read(file=image.replace("\\", "/"))
src["colorspace"].setValue("Utility - sRGB - Texture")
fr = nuke.nodes.FrameRange(inputs=[src], first_frame=1001, last_frame=1003)
pw = nuke.nodes.Write(inputs=[fr], file=plate, file_type="exr")
pw["colorspace"].setValue("ACES - ACES2065-1")
nuke.execute(pw, 1001, 1003, 1)

read = nuke.nodes.Read(file=plate, first=1001, last=1003, origfirst=1001, origlast=1003)
read["colorspace"].setValue("ACES - ACES2065-1")
nuke.frame(1002)
for n in nuke.allNodes():
    n.setSelected(False)
read.setSelected(True)
g = comfygizmos.create("dsine_normals")
g.setInput(0, read)

check(int(g["cg_frame"].value()) == 1002, "frame = current frame")
check(g["cg_out_cs"].value() == "raw", "output colorspace raw")
check([g["wf_" + k].value() for k in ("fov", "iterations", "resolution")] == [60.0, 5, 1024], "knob defaults")
g["wf_resolution"].setValue(512)

job = comfygizmos.render(g, wait=True)
print("STATUS:", g["cg_status"].value())
result = g["cg_result"].value()
print("RESULT:", result)
check(job is not None and "_DSINENormal" in result, "named ..._DSINENormal")
check(os.path.isfile(result % 1002) and not os.path.isfile(result % 1001), "only frame 1002 written")

res = g.node("Result")
check(res["raw"].value(), "Result Read is raw")
check((res.width(), res.height()) == (read.width(), read.height()), "plate size {}x{}".format(res.width(), res.height()))
x, y = read.width() // 2, read.height() // 2
enc = px(res, x, y, 1002)
dec = px(g, x, y, 1002)
print("encoded", enc, "decoded", dec)
check(all(0.0 <= v <= 1.0 for v in enc), "file is 0-1 encoded")
check(all(abs(d - (e * 2 - 1)) < 1e-4 for d, e in zip(dec, enc)), "node output = n*2-1")
length = sum(v * v for v in dec) ** 0.5
check(0.9 < length < 1.1, "decoded normal is unit length ({:.3f})".format(length))
g["cg_output"].setValue(1)
comfygizmos.knob_changed(g, g["cg_output"])
check(all(abs(a - b) < 1e-4 for a, b in zip(px(g, x, y, 1002), enc)), "'encoded 0..1' passes the file through")
g["cg_output"].setValue(0)
comfygizmos.knob_changed(g, g["cg_output"])

br = comfygizmos.read_output(g)
crop = br.dependent()[0]
expr = crop.dependent()[0]
check([br.Class(), crop.Class(), expr.Class()] == ["Read", "Crop", "Expression"], "Bring in output: Read -> Crop -> Expression")
check(br["raw"].value() and expr.isSelected(), "brought-in Read raw, Expression selected")
check(all(abs(a - b) < 1e-4 for a, b in zip(px(expr, x, y, 1002), dec)), "brought-in output matches the node")
print("FAILED: {}".format(fails) if fails else "PASS")
