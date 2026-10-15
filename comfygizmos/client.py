"""Minimal ComfyUI HTTP client - stdlib only, so it runs in any Nuke Python."""
import json
import uuid
import urllib.error
import urllib.request

DEFAULT_HOST = "http://127.0.0.1:8188"
CLIENT_ID = "comfygizmos-" + uuid.uuid4().hex[:8]


class ComfyError(Exception):
    pass


def _request(host, path, data=None, timeout=10):
    url = host.rstrip("/") + path
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise ComfyError(_http_error_text(e.code, e.read().decode("utf-8", "replace")))
    except urllib.error.URLError as e:
        raise ComfyError("ComfyUI is not reachable at {} ({})".format(host, e.reason))
    except OSError as e:
        raise ComfyError("ComfyUI request failed: {}".format(e))
    return json.loads(text) if text.strip() else None


def _http_error_text(code, text):
    """ComfyUI's /prompt validation errors -> readable lines."""
    try:
        data = json.loads(text)
    except ValueError:
        return "HTTP {}: {}".format(code, text[:500])
    lines = []
    err = data.get("error") or {}
    if isinstance(err, dict) and err.get("message"):
        lines.append(err["message"] + (": " + err["details"] if err.get("details") else ""))
    for nid, ne in (data.get("node_errors") or {}).items():
        for e in ne.get("errors", []):
            lines.append("node {} ({}): {} {}".format(nid, ne.get("class_type", "?"),
                                                       e.get("message", ""), e.get("details", "")).strip())
    return "\n".join(lines) or "HTTP {}: {}".format(code, text[:500])


def is_up(host):
    try:
        _request(host, "/system_stats", timeout=3)
        return True
    except ComfyError:
        return False


def colorspaces(host):
    """Colorspace names ComfyUI-OCIO's OCIO Read accepts (its active OCIO config)."""
    info = _request(host, "/object_info/OCIORead")
    try:
        return list(info["OCIORead"]["input"]["required"]["input_colorspace"][0])
    except (KeyError, TypeError, IndexError):
        raise ComfyError("ComfyUI has no OCIO Read node - is ComfyUI-OCIO installed?")


def write_first_path(host, params):
    """The first file OCIO Write will create for these Write inputs (ComfyUI-OCIO's own naming)."""
    keys = ("output_folder", "filename", "container", "still_format", "video_codec", "output_colorspace",
            "raw_data", "colorspace_in_name", "start_number")
    body = {k: params[k] for k in keys if k in params}
    body["raw_data"] = 1 if body.get("raw_data") else 0
    body["colorspace_in_name"] = 1 if body.get("colorspace_in_name") else 0
    res = _request(host, "/ocio/write_paths", body)
    first = (res or {}).get("first")
    if not first:
        raise ComfyError("ComfyUI-OCIO could not work out the output file name: {}".format(res))
    return first.replace("\\", "/")


def submit(host, prompt):
    """Queue an API-format graph. Returns the prompt id."""
    res = _request(host, "/prompt", {"prompt": prompt, "client_id": CLIENT_ID}, timeout=30)
    if not res or "prompt_id" not in res:
        raise ComfyError("ComfyUI did not accept the job: {}".format(res))
    if res.get("node_errors"):
        raise ComfyError(_http_error_text(400, json.dumps(res)))
    return res["prompt_id"]


def status(host, prompt_id):
    """('queued', position) | ('running', None) | ('done', outputs) | ('error', message) | ('missing', None)"""
    hist = _request(host, "/history/" + prompt_id) or {}
    entry = hist.get(prompt_id)
    if entry:
        st = entry.get("status") or {}
        if st.get("status_str") == "error":
            return "error", _execution_error(st.get("messages") or [])
        if st.get("completed", True):
            return "done", entry.get("outputs") or {}
    queue = _request(host, "/queue") or {}
    if any(item[1] == prompt_id for item in queue.get("queue_running", [])):
        return "running", None
    pending = sorted(queue.get("queue_pending", []), key=lambda item: item[0])
    for pos, item in enumerate(pending):
        if item[1] == prompt_id:
            return "queued", pos + 1
    return ("running", None) if entry else ("missing", None)


def _execution_error(messages):
    for kind, data in messages:
        if kind == "execution_error":
            return "{} ({}): {}".format(data.get("node_type", "?"), data.get("node_id", "?"),
                                        (data.get("exception_message") or "").strip())
        if kind == "execution_interrupted":
            return "cancelled"
    return "failed (see the ComfyUI console)"


def cancel(host, prompt_id):
    """Drop it from the queue, or interrupt it if it is the one running."""
    try:
        _request(host, "/queue", {"delete": [prompt_id]})
    except ComfyError:
        pass
    queue = _request(host, "/queue") or {}
    if any(item[1] == prompt_id for item in queue.get("queue_running", [])):
        _request(host, "/interrupt", {"prompt_id": prompt_id})
