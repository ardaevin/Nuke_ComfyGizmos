// Path placeholders for OCIO Write, filled from the OCIO Read that feeds it.
//
// Type these into an OCIO Write's output_folder / filename:
//   {plate_dir}   folder of the upstream OCIO Read's source  (e.g. X:/shows/SH010/plates)
//   {plate_name}  source file name without frame number / extension  (e.g. SH010_plate_v001)
// e.g. output_folder = {plate_dir}/LTXAlpha   filename = {plate_name}_LTXAlpha
//
// Expanded only in the prompt that gets queued; the widgets keep the placeholders, so the
// next plate loaded into the Read gets its own folder and name. A relative Read source
// (a file in ComfyUI/input) has no absolute folder, so {plate_dir} becomes "" and the
// Write falls back to ComfyUI/output.
import { app } from "../../scripts/app.js";

const TOKEN = /\{plate_(dir|name)\}/;
const VIDEO_EXT = /\.(mov|mp4|m4v|mxf|avi|mkv|webm|mpg|mpeg)$/i;

function widget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

function upstreamRead(node, seen = new Set()) {
    if (!node || seen.has(node.id)) return null;
    seen.add(node.id);
    if (node.type === "OCIORead") return node;
    for (const input of node.inputs || []) {
        if (input.link == null) continue;
        const link = app.graph.links[input.link] ?? app.graph.links.get?.(input.link);
        const found = link && upstreamRead(app.graph.getNodeById(link.origin_id), seen);
        if (found) return found;
    }
    return null;
}

function plateParts(source) {
    const src = String(source || "").trim().replace(/\\/g, "/").replace(/\/+$/, "");
    const cut = src.lastIndexOf("/");
    const base = cut >= 0 ? src.slice(cut + 1) : src;
    const isAbs = /^([a-zA-Z]:\/|\/)/.test(src);
    if (!/\.[^./]+$/.test(base)) {
        // a folder of frames: the folder itself is the plate
        return { dir: isAbs ? src : "", name: base };
    }
    let name = base.replace(/\.[^.]+$/, "");
    if (!VIDEO_EXT.test(base)) {
        // name.####.exr / name.%04d.exr / name.1001.exr / name_1001.png -> name
        name = name.replace(/[._]?(#+|%0?\d*d|\d+)$/, "");
    }
    return { dir: isAbs && cut > 0 ? src.slice(0, cut) : "", name: name || base };
}

function expand(value, parts) {
    let out = String(value).replace(/\{plate_dir\}/g, parts.dir).replace(/\{plate_name\}/g, parts.name);
    if (!parts.dir) out = out.replace(/^[\\/]+/, ""); // empty dir -> relative to ComfyUI/output
    return out;
}

app.registerExtension({
    name: "local.ltxPlateTools.platePathTokens",
    setup() {
        const original = app.graphToPrompt;
        app.graphToPrompt = async function (...args) {
            const result = await original.apply(this, args);
            for (const node of app.graph?._nodes || []) {
                if (node.type !== "OCIOWrite") continue;
                const entry = result?.output?.[String(node.id)];
                if (!entry) continue;
                const keys = ["output_folder", "filename"].filter(
                    (k) => typeof entry.inputs[k] === "string" && TOKEN.test(entry.inputs[k])
                );
                if (!keys.length) continue;
                const read = upstreamRead(node);
                const source = read && widget(read, "source")?.value;
                if (!source) {
                    throw new Error(
                        `OCIO Write #${node.id} uses {plate_dir}/{plate_name} but no OCIO Read with a source feeds it.`
                    );
                }
                const parts = plateParts(source);
                for (const k of keys) entry.inputs[k] = expand(entry.inputs[k], parts);
                console.log(`[plate tokens] OCIO Write #${node.id} ->`, entry.inputs.output_folder, entry.inputs.filename);
            }
            return result;
        };
    },
});
