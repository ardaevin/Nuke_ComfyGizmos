"""Any workflow end-to-end in Nuke (real ComfyUI render), direct Read:

    Nuke14.1.exe --nukex -i -t tests/nuke_test_workflow.py <package_parent_dir> <project_dir> <workflow_id> <plate> f<first> f<last>

Checks the output files and name, the node's Read (range, colorspace), that every
knob reached all its target nodes, and Bring in output (Read -> Crop [-> Shuffle for mattes])."""
import json
import os
import sys
import urllib.request

import nuke

pkg_parent, project_dir, wf_id, plate, first, last = sys.argv[1:7]
first, last = int(first.lstrip("f")), int(last.lstrip("f"))
sys.path.insert(0, pkg_parent)
import comfygizmos  # noqa: E402
from comfygizmos import colorspace  # noqa: E402

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

wf = comfygizmos.workflows()[wf_id]
read = nuke.nodes.Read(file=plate, first=first, last=last, origfirst=first, origlast=last)
for n in nuke.allNodes():
    n.setSelected(False)
read.setSelected(True)
g = comfygizmos.create(wf_id)
g.setInput(0, read)
g["cg_input_range"].setValue(True)

# non-default knob values, to see them arrive in ComfyUI
sent = {}
for spec in wf["knobs"]:
    k = g["wf_" + spec["name"]]
    if spec["type"] == "int":
        k.setValue(int(spec["default"]) + 7)
    elif spec["type"] == "float" and spec["name"] == "lora_strength":
        k.setValue(0.9)
    sent[spec["name"]] = k.value()

job = comfygizmos.render(g, wait=True)
print("STATUS:", g["cg_status"].value())
check(job is not None, "sent and finished")
result = g["cg_result"].value()
tag = colorspace.to_comfy(wf["output"]["colorspace"], ["ACES - ACES2065-1", "ACES - ACEScg"])
want_end = "_{}_v001_{}.%04d.exr".format(wf["suffix"], "aces2065" if "2065" in tag else "acescg")
check(result.endswith(want_end), "named ...{}: {}".format(want_end, os.path.basename(result)))
check(all(os.path.isfile(result % f) for f in range(first, last + 1)), "all {} frames on disk".format(last - first + 1))

res = g.node("Result")
check((int(res["first"].value()), int(res["last"].value())) == (first, last), "Read range {}-{}".format(
    res["first"].value(), res["last"].value()))
check(colorspace.unwrap_default(res["colorspace"].value()) == wf["output"]["colorspace"] and not res["raw"].value(),
      "Read colorspace " + res["colorspace"].value())
print("RESOLUTION: result {}x{}  plate {}x{}".format(res.width(), res.height(), read.width(), read.height()))

prompt = json.load(urllib.request.urlopen(
    "http://127.0.0.1:8188/history/" + g["cg_prompt_id"].value()))[g["cg_prompt_id"].value()]["prompt"][2]
for spec in wf["knobs"]:
    for t in spec.get("targets") or [{"node": spec["node"], "input": spec["input"]}]:
        got = prompt[t["node"]]["inputs"][t["input"]]
        check(abs(float(got) - float(sent[spec["name"]])) < 1e-6,
              "knob {} -> {}.{} = {}".format(spec["name"], t["node"], t["input"], got))
wr = prompt[wf["write"]["node"]]["inputs"]
print("WRITE:", wr["from_colorspace"], "->", wr["output_colorspace"], "raw", wr["raw_data"])

try:
    import OpenImageIO as oiio  # noqa: F401
except ImportError:
    pass
mid = (first + last) // 2
probe = nuke.nodes.Write(inputs=[g], file=os.path.join(project_dir, "probe.exr"), file_type="exr", raw=True,
                         channels="rgb")
nuke.execute(probe, mid, mid)
print("PROBE written for frame", mid)

br = comfygizmos.read_output(g)
chain = [br.Class()]
n = br
while n.dependent():
    n = n.dependent()[0]
    chain.append(n.Class())
want_chain = ["Read", "Crop", "Shuffle2"] if wf["output"]["kind"] == "matte" else ["Read", "Crop"]
check(chain == want_chain, "Bring in output chain: " + " -> ".join(chain))
print("FAILED: {}".format(fails) if fails else "PASS")
