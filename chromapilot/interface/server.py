"""server.py — the web server for the agent interface.

Standard-library only (http.server + Server-Sent-Events): the conda environment
that runs the real graph carries no Flask/FastAPI, and adding a web framework to
it would mean re-solving a 1000-package environment. SSE is enough — the traffic
is one-directional streaming plus a handful of small POSTs, including the
`/api/resume` route that lets a human answer the graph's `interrupt()` prompts.

Routes:
    GET  /                       -> the single-page UI
    GET  /static/<f>             -> css / js
    GET  /api/health             -> {ready, status}
    GET  /api/bootstrap          -> tool catalog, pipeline DAG, examples, model
    POST /api/run {request,...}  -> start a run, returns {run_id}
    GET  /api/stream?run_id=...  -> SSE stream of UI events for that run
                                    (replays history; honours Last-Event-ID /
                                    ?after=N so reconnects and reloads resume)
    GET  /api/run_status?run_id= -> {exists, finished, n_events, ...}
    POST /api/resume {run_id,text}-> answer an interrupt
    POST /api/stop   {run_id}    -> stop a run
    GET  /api/file?path=...      -> serve a produced figure/file (sandboxed);
                                    &raw=1 serves a PDF as-is (the agent's report)
                                    instead of rendering page 1 to PNG

Run:  python -m server   (from the interface/ directory, under the `agent` env)
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import agent_runtime
import knowledge
from run_session import RunSession

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
RUNS_ROOT = os.path.join(HERE, "runs")

PORT = int(os.environ.get("IFACE_PORT", "8800"))
HOST = os.environ.get("IFACE_HOST", "0.0.0.0")
MAX_BODY_BYTES = 1_000_000  # reject absurd POST bodies (prompts are small)

# /api/file only ever serves figures, so we restrict it to image/PDF types. This
# is the key guard that keeps the endpoint from being turned into an arbitrary
# file reader even if an output directory outside runs/ gets registered. PDFs
# (the agent's most common figure format) are rendered to PNG before serving.
SERVABLE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".pdf"}
_PNG_CACHE: dict = {}        # (realpath, mtime) -> png bytes, for rendered PDFs


def _pdf_first_page_png(path: str) -> bytes:
    """Render page 1 of a PDF to PNG bytes (cached by path+mtime)."""
    key = (path, os.path.getmtime(path))
    if key in _PNG_CACHE:
        return _PNG_CACHE[key]
    import fitz  # PyMuPDF, available in the `agent` env
    doc = fitz.open(path)
    pix = doc[0].get_pixmap(dpi=150)
    png = pix.tobytes("png")
    doc.close()
    _PNG_CACHE[key] = png
    return png

# ------------------------------------------------------------------ run state
# run_id -> {session, history: [event...], cond, finished, created}. Every event
# is kept (they are small) so a browser that reconnects — or reloads the page —
# can replay the run from any point; finished runs are kept until pruned.
RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()
MAX_FINISHED_RUNS = 20


def _prune_runs():
    with RUNS_LOCK:
        finished = sorted((e["created"], rid) for rid, e in RUNS.items() if e["finished"])
        for _, rid in finished[:-MAX_FINISHED_RUNS] if len(finished) > MAX_FINISHED_RUNS else []:
            RUNS.pop(rid, None)
ALLOWED_FILE_ROOTS: set[str] = {os.path.realpath(RUNS_ROOT)}


def register_file_root(path: str):
    """Allow figures under `path` to be served by /api/file. Called both for a
    UI-provided output dir and for one the planner extracts from the prompt."""
    with RUNS_LOCK:
        ALLOWED_FILE_ROOTS.add(os.path.realpath(path))

# ------------------------------------------------------------------ boot state
BOOT = {"ready": False, "status": "booting", "error": None, "log": []}
RUNTIME: agent_runtime.Runtime | None = None
_BOOTSTRAP_CACHE: dict | None = None


def _boot_log(msg):
    print(msg, flush=True)
    BOOT["log"].append(msg)


def _boot():
    global RUNTIME
    try:
        RUNTIME = agent_runtime.boot(log=_boot_log)
        BOOT["ready"] = True
        BOOT["status"] = "ready"
        _boot_log("[boot] interface ready.")
    except Exception as e:
        BOOT["status"] = "error"
        BOOT["error"] = str(e)
        _boot_log(f"[boot] FAILED: {e}")


def _bootstrap_payload() -> dict:
    """Catalog + DAG + available models for the UI. The static catalog/DAG is
    cached; the active provider is read live (it can change at run time)."""
    global _BOOTSTRAP_CACHE
    if _BOOTSTRAP_CACHE is None:
        g = knowledge.parse_graph(RUNTIME.graph_txt if RUNTIME else "")
        _BOOTSTRAP_CACHE = {
            "tools": knowledge.parse_nodes(RUNTIME.node_txt if RUNTIME else ""),
            "dag": {**g, "layout": knowledge.layered_layout(g)},
            "control_nodes": [
                {"name": n, "role": r, "label": l} for n, r, l in agent_runtime.CONTROL_NODES
            ],
            "sub_nodes": {
                parent: [{"name": n, "label": l} for n, l in subs]
                for parent, subs in agent_runtime.SUB_NODES.items()
            },
        }
    payload = dict(_BOOTSTRAP_CACHE)
    payload["report_enabled"] = RUNTIME.report_enabled if RUNTIME else False
    payload["providers"] = [
        {"id": p["id"], "label": p["label"], "model": p["model"]}
        for p in (RUNTIME.providers if RUNTIME else [])
    ]
    payload["active"] = RUNTIME.current_id if RUNTIME else None
    payload["model"] = RUNTIME.model_name if RUNTIME else None
    return payload


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep the console clean
        pass

    # -- helpers ------------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype=None):
        with open(path, "rb") as f:
            body = f.read()
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The UI files are tiny; never let a browser hold on to a stale copy.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            return {}

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        route = u.path
        if route == "/":
            return self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html")
        if route.startswith("/static/"):
            fn = os.path.basename(route)
            fp = os.path.join(STATIC_DIR, fn)
            if os.path.isfile(fp):
                return self._send_file(fp)
            return self._send_json({"error": "not found"}, 404)
        if route == "/api/health":
            return self._send_json({"ready": BOOT["ready"], "status": BOOT["status"],
                                    "error": BOOT["error"]})
        if route == "/api/bootstrap":
            if not BOOT["ready"]:
                return self._send_json({"status": BOOT["status"], "error": BOOT["error"],
                                        "log": BOOT["log"][-12:]}, 503)
            return self._send_json(_bootstrap_payload())
        if route == "/api/stream":
            qs = parse_qs(u.query)
            after = qs.get("after", [None])[0]
            if after is None:
                after = self.headers.get("Last-Event-ID")     # sent by EventSource on reconnect
            try:
                after = int(after) if after is not None and str(after).strip() != "" else -1
            except ValueError:
                after = -1
            return self._stream(qs.get("run_id", [None])[0], after)
        if route == "/api/run_status":
            return self._run_status(parse_qs(u.query).get("run_id", [None])[0])
        if route == "/api/file":
            fq = parse_qs(u.query)
            return self._serve_artifact(fq.get("path", [None])[0],
                                        raw=fq.get("raw", ["0"])[0] not in ("0", "", "false"))
        return self._send_json({"error": "not found"}, 404)

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        route = urlparse(self.path).path
        if int(self.headers.get("Content-Length", 0) or 0) > MAX_BODY_BYTES:
            return self._send_json({"error": "request too large"}, 413)
        body = self._read_body()
        if route == "/api/run":
            return self._start_run(body)
        if route == "/api/resume":
            return self._resume(body)
        if route == "/api/stop":
            return self._stop(body)
        return self._send_json({"error": "not found"}, 404)

    # -- run lifecycle ------------------------------------------------------
    def _start_run(self, body):
        if not BOOT["ready"]:
            return self._send_json({"error": f"agent not ready ({BOOT['status']})"}, 503)
        request_text = (body.get("request") or "").strip()
        if not request_text:
            return self._send_json({"error": "empty request"}, 400)

        # Switch the active LLM if the user picked a different provider.
        provider = (body.get("provider") or "").strip()
        if provider:
            try:
                RUNTIME.activate(provider, log=_boot_log)
            except Exception as e:
                return self._send_json({"error": f"could not select provider: {e}"}, 400)

        run_id = uuid.uuid4().hex[:12]
        # The output directory may come from the UI field OR be left to the
        # prompt. If the field is set we honour it (absolute paths only — a user
        # may legitimately target a cluster data dir); if it is empty we pass
        # output_dir=None and let the planner extract whatever the prompt says,
        # registering that directory for figure-serving once we learn it.
        output_dir = (body.get("output_dir") or "").strip() or None
        if output_dir:
            if not os.path.isabs(output_dir):
                return self._send_json({"error": "output_dir must be an absolute path"}, 400)
            os.makedirs(output_dir, exist_ok=True)
            register_file_root(output_dir)

        entry = {"session": None, "history": [], "cond": threading.Condition(),
                 "finished": False, "created": time.time(), "request": request_text}

        def emit(etype, **payload):
            ev = {"type": etype, **payload, "ts": time.time()}
            with entry["cond"]:
                ev["seq"] = len(entry["history"])
                entry["history"].append(ev)
                # `error` is always followed by a `done` (success=False), so the
                # run is only closed on `done` — the browser needs both events.
                if etype == "done":
                    entry["finished"] = True
                entry["cond"].notify_all()
            if etype == "done":
                _prune_runs()

        session = RunSession(RUNTIME, run_id, request_text, output_dir, emit,
                             register_root=register_file_root)
        entry["session"] = session
        with RUNS_LOCK:
            RUNS[run_id] = entry
        session.start()
        return self._send_json({"run_id": run_id,
                                "output_dir": output_dir or "(from your prompt)"})

    def _resume(self, body):
        run_id, text = body.get("run_id"), body.get("text", "")
        with RUNS_LOCK:
            entry = RUNS.get(run_id)
        if not entry:
            return self._send_json({"error": "unknown run"}, 404)
        if entry["finished"]:
            return self._send_json({"error": "run already finished"}, 409)
        entry["session"].resume(text)
        return self._send_json({"ok": True})

    def _stop(self, body):
        run_id = body.get("run_id")
        with RUNS_LOCK:
            entry = RUNS.get(run_id)
        if not entry:
            return self._send_json({"error": "unknown run"}, 404)
        if entry["finished"]:
            return self._send_json({"error": "run already finished"}, 409)
        entry["session"].stop()
        return self._send_json({"ok": True})

    def _run_status(self, run_id):
        with RUNS_LOCK:
            entry = RUNS.get(run_id)
        if not entry:
            return self._send_json({"exists": False}, 404)
        with entry["cond"]:
            n = len(entry["history"])
            finished = entry["finished"]
        return self._send_json({"exists": True, "run_id": run_id, "finished": finished,
                                "n_events": n, "request": entry["request"],
                                "output_dir": entry["session"].output_dir,
                                "created": entry["created"]})

    def _stream(self, run_id, after=-1):
        """Server-Sent-Events for one run. Replays every event with seq > after
        (the whole history by default), then follows live. Each event carries an
        `id:` so a reconnecting EventSource resumes exactly where it left off;
        several clients (tabs) can follow the same run independently."""
        with RUNS_LOCK:
            entry = RUNS.get(run_id)
        if not entry:
            return self._send_json({"error": "unknown run"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        cursor = max(after + 1, 0)
        try:
            while True:
                with entry["cond"]:
                    while cursor >= len(entry["history"]) and not entry["finished"]:
                        if not entry["cond"].wait(timeout=15):
                            break                                  # → keepalive
                    events = entry["history"][cursor:]
                    finished = entry["finished"]
                if not events:
                    if finished:
                        break
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for ev in events:
                    self.wfile.write((f"id: {ev['seq']}\ndata: " + json.dumps(ev) + "\n\n").encode())
                    cursor = ev["seq"] + 1
                self.wfile.flush()
                if finished and cursor >= len(entry["history"]):
                    break
        except (BrokenPipeError, ConnectionResetError):
            # Client (browser) went away. The run keeps going and its history is
            # kept, so the browser can reconnect (Last-Event-ID) or reload.
            pass
        # An SSE response has no Content-Length, so the only way to signal
        # "end of stream" on a keep-alive (HTTP/1.1) connection is to close it.
        self.close_connection = True

    def _serve_artifact(self, path, raw=False):
        if not path:
            return self._send_json({"error": "no path"}, 400)
        # Two independent guards: the file must be an image (figures only), and
        # it must live under a registered run output directory.
        if os.path.splitext(path)[1].lower() not in SERVABLE_EXTS:
            return self._send_json({"error": "forbidden"}, 403)
        real = os.path.realpath(path)
        with RUNS_LOCK:
            roots = list(ALLOWED_FILE_ROOTS)
        if not any(real == r or real.startswith(r + os.sep) for r in roots):
            return self._send_json({"error": "forbidden"}, 403)
        if not os.path.isfile(real):
            return self._send_json({"error": "not found"}, 404)
        if real.lower().endswith(".pdf"):
            # The agent's report.pdf is multi-page prose+figures, so it is served
            # as a real PDF for the browser's own viewer (?raw=1). Figures stay
            # first-page-PNG so they can be <img> thumbnails.
            if raw:
                return self._send_file(real, "application/pdf")
            try:
                png = _pdf_first_page_png(real)
            except Exception as e:
                return self._send_json({"error": f"pdf render failed: {e}"}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            return self.wfile.write(png)
        return self._send_file(real)


def startup_banner(node: str, port: int) -> str:
    """The instructions printed on start-up. The tunnel command depends on where
    your `ssh` actually lands you, so we print the three shapes rather than
    guessing: reaching this node from elsewhere, landing on this node itself, and
    jumping through a login node when direct node-to-node traffic is blocked."""
    line = "\u2500" * 74
    return "\n".join([
        line,
        f"  ChromaPilot interface \u2014 listening on {node}:{port}",
        "  The agent imports in the background (~1\u20132 min); the page shows progress.",
        "",
        f"  Check it here on this node:   curl http://localhost:{port}/api/health",
        "",
        "  TO OPEN IT IN YOUR BROWSER",
        "  In a NEW terminal on your laptop run ONE of the commands below, leave it",
        f"  open, then browse to  http://localhost:{port}",
        "",
        "  <ssh-host> is however you normally reach the cluster: 'you@login-node',",
        "  or an alias from your ~/.ssh/config.",
        "",
        "  1. Usual case \u2014 your ssh lands somewhere that can reach this node:",
        "",
        f"       ssh -t -L {port}:{node}:{port} <ssh-host>",
        "",
        f"  2. If that ssh already lands you ON {node}:",
        "",
        f"       ssh -t -L {port}:localhost:{port} <ssh-host>",
        "",
        "  3. If node-to-node traffic is blocked, jump through the login node:",
        "",
        f"       ssh -J <ssh-host> $USER@{node} -L {port}:localhost:{port}",
        "",
        "  Not sure which? Try 1. If the page does not load, run this where that",
        f"  ssh puts you:  curl -s -m 5 http://{node}:{port}/api/health",
        '  A reply of {"ready": ...} means 1 is right; no reply means use 3.',
        "",
        "  Keep THIS server and your node allocation running while you use it.",
        line,
    ])


def main():
    mimetypes.add_type("image/svg+xml", ".svg")
    os.makedirs(RUNS_ROOT, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)   # bind now (fails fast if port busy)
    print(startup_banner(os.uname().nodename, PORT), flush=True)
    threading.Thread(target=_boot, name="boot", daemon=True).start()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
