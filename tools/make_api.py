"""Turn a ComfyUI API-format export (app.graphToPrompt().output) into the graph a
ComfyGizmos workflow submits.

    python make_api.py <export.json> <out.api.json> <write_node_id> [drop_id ...]

- drops the given node ids (preview-only outputs, NukeLink's Path Builder, ...)
- replaces any link into the OCIO Write's output_folder / filename with ""
  (the gizmo fills both at submit time)
- fails if anything still links to a dropped node
"""
import json
import sys


def main(src, dst, write_id, drop):
    prompt = json.load(open(src, encoding="utf-8"))
    for nid in drop:
        prompt.pop(nid, None)

    write = prompt[write_id]
    assert write["class_type"] == "OCIOWrite", write["class_type"]
    for key in ("output_folder", "filename"):
        write["inputs"][key] = ""

    dangling = [(nid, key) for nid, node in prompt.items()
                for key, val in node["inputs"].items()
                if isinstance(val, list) and len(val) == 2 and str(val[0]) not in prompt]
    if dangling:
        raise SystemExit("links to dropped nodes: %s" % dangling)

    with open(dst, "w", encoding="utf-8") as f:
        json.dump(prompt, f, indent=2)
    print("%s: %d nodes" % (dst, len(prompt)))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:])
