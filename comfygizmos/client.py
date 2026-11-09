"""Minimal ComfyUI HTTP client - stdlib only, so it runs in any Nuke Python."""
import base64
import json
import os
import socket
import ssl
import struct
import threading
import uuid
import urllib.parse
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


def submit(host, prompt, client_id=None):
    """Queue an API-format graph. Returns the prompt id."""
    res = _request(host, "/prompt", {"prompt": prompt, "client_id": client_id or CLIENT_ID}, timeout=30)
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


class NodeWatcher(threading.Thread):
    """Which node of a job ComfyUI is executing right now (.current), from its websocket. Submit the job with this
    .client_id - ComfyUI tells only that client. Purely informative: if the socket fails, .current stays None."""

    def __init__(self, host):
        super(NodeWatcher, self).__init__(daemon=True)
        self.client_id = "comfygizmos-" + uuid.uuid4().hex[:12]
        self.current = None
        self._stopped = False
        self._buf = b""
        self._sock = None
        try:
            self._connect(host)
        except (OSError, ValueError):
            self.stop()
            return
        self.start()

    def _connect(self, host):
        url = urllib.parse.urlparse(host)
        secure = url.scheme == "https"
        sock = socket.create_connection((url.hostname, url.port or (443 if secure else 80)), timeout=2)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=url.hostname)
        self._sock = sock
        sock.sendall(("GET {}/ws?clientId={} HTTP/1.1\r\nHost: {}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Key: {}\r\nSec-WebSocket-Version: 13\r\n\r\n").format(
            url.path.rstrip("/"), self.client_id, url.netloc,
            base64.b64encode(os.urandom(16)).decode("ascii")).encode("ascii"))
        while b"\r\n\r\n" not in self._buf:
            data = sock.recv(4096)
            if not data:
                raise ValueError("closed during the handshake")
            self._buf += data
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        if b" 101" not in head.split(b"\r\n", 1)[0]:
            raise ValueError("no websocket upgrade")
        sock.settimeout(1)                                  # so stop() is noticed while waiting

    def _read(self, n):
        while len(self._buf) < n:
            if self._stopped:
                raise OSError("stopped")
            try:
                data = self._sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                raise OSError("closed")
            self._buf += data
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def run(self):
        try:
            while True:
                b0, b1 = struct.unpack("BB", self._read(2))
                size = b1 & 0x7F
                if size == 126:
                    size = struct.unpack(">H", self._read(2))[0]
                elif size == 127:
                    size = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if b1 & 0x80 else None
                payload = self._read(size)
                if mask:
                    payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
                opcode = b0 & 0x0F
                if opcode == 8:                             # close
                    break
                if opcode == 9 and size <= 125:             # ping -> masked pong
                    self._sock.sendall(bytes([0x8A, 0x80 | size, 0, 0, 0, 0]) + payload)
                elif opcode == 1:                           # text; binary frames are previews
                    msg = json.loads(payload.decode("utf-8"))
                    if msg.get("type") == "executing":
                        self.current = (msg.get("data") or {}).get("node")
        except (OSError, ValueError, struct.error):
            pass
        finally:
            self._close()

    def stop(self):
        self._stopped = True
        if not self.is_alive():
            self._close()

    def _close(self):
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass


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
