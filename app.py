#!/usr/bin/env python3
"""Tidy: organize every file on your Mac with Claude.  Run:  python3 app.py"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import engine

UI = Path(__file__).with_name("ui") / "index.html"
TOKEN = secrets.token_urlsafe(24)  # stops other websites from calling this local server
KEYCHAIN_SERVICE = "Tidy Anthropic API Key"

job = engine.Job()
state = {"plan": None}


# ---------------------------------------------------------------- API key (macOS Keychain)

def load_key() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    if engine.IS_MAC:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    f = engine.DATA_DIR / "key"
    return f.read_text().strip() if f.exists() else ""


def save_key(key: str):
    if engine.IS_MAC:
        subprocess.run(["security", "add-generic-password", "-U", "-a", os.environ.get("USER", "tidy"),
                        "-s", KEYCHAIN_SERVICE, "-w", key], check=True, capture_output=True)
    else:
        engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
        f = engine.DATA_DIR / "key"
        f.write_text(key)
        f.chmod(0o600)


def make_client():
    import anthropic
    return anthropic.Anthropic(api_key=load_key())


# ---------------------------------------------------------------- background work

def run_in_background(phase: str, fn):
    def target():
        try:
            fn()
        except engine.Cancelled:
            job.set_quiet(phase="review" if state["plan"] else "idle", step="", detail="")
        except Exception as e:  # shown in the UI
            job.set_quiet(phase="error", error=friendly(e))
    job.cancel = False
    job.set(phase=phase, error="", progress=0.0, step="", detail="")
    threading.Thread(target=target, daemon=True).start()


def friendly(e: Exception) -> str:
    name = e.__class__.__name__
    if name == "AuthenticationError":
        return "Your API key wasn't accepted. Check it in Settings."
    if name == "PermissionDeniedError":
        return "This API key doesn't have access to the model."
    return str(e) or name


def do_scan(opts):
    def work():
        plan = engine.build_plan(job, make_client(), opts["roots"], opts.get("read_content", True),
                                 opts.get("rename_media", True))
        state["plan"] = plan
        job.set(phase="review", progress=1.0)
    run_in_background("working", work)


def do_apply(edits):
    def work():
        job.result = engine.apply(job, state["plan"], edits)
        state["plan"] = None
        job.set(phase="done", progress=1.0)
    run_in_background("applying", work)


def do_undo():
    def work():
        last = json.loads((engine.DATA_DIR / "last.json").read_text())
        job.result = {"undone": engine.undo(job, last["journal"])}
        job.set(phase="undone", progress=1.0)
    run_in_background("applying", work)


def _set_quiet(self, **kw):  # like set() but never raises Cancelled
    with self.lock:
        for k, v in kw.items():
            setattr(self, k, v)


engine.Job.set_quiet = _set_quiet


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code=200, body=None, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return self.headers.get("X-Tidy-Token") == TOKEN

    def do_GET(self):
        if self.path.split("?")[0] == "/":
            html = UI.read_text().replace("__TOKEN__", TOKEN)
            return self.send(body=html.encode(), ctype="text/html; charset=utf-8")
        if not self.authorized():
            return self.send(403, {"error": "forbidden"})
        if self.path == "/api/state":
            snap = job.snapshot()
            if snap["phase"] in ("done", "undone"):
                snap["result"] = job.result
            return self.send(body=snap)
        if self.path == "/api/setup":
            last = engine.DATA_DIR / "last.json"
            return self.send(body={"roots": engine.available_roots(), "has_key": bool(load_key()),
                                   "can_undo": last.exists(), "home": str(engine.HOME)})
        if self.path == "/api/plan":
            return self.send(body=engine.preview(state["plan"]) if state["plan"] else {})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.authorized():
            return self.send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        busy = job.phase in ("working", "applying")
        if self.path == "/api/key":
            save_key(body["key"].strip())
            return self.send(body={"ok": True})
        if self.path == "/api/cancel":
            job.cancel = True
            return self.send(body={"ok": True})
        if busy:
            return self.send(409, {"error": "busy"})
        if self.path == "/api/scan":
            do_scan(body)
        elif self.path == "/api/apply" and state["plan"]:
            do_apply(body.get("edits", {}))
        elif self.path == "/api/undo":
            do_undo()
        elif self.path == "/api/reset":
            state["plan"] = None
            job.set_quiet(phase="idle", error="")
        elif self.path == "/api/reveal":
            target = str(engine.HOME / body.get("path", ""))
            if engine.IS_MAC:
                subprocess.run(["open", "-R", target] if body.get("path") else ["open", target])
        else:
            return self.send(404, {"error": "not found"})
        self.send(body={"ok": True})


def main():
    port = int(os.environ.get("TIDY_PORT", "0"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Tidy is running at {url}", flush=True)
    if os.environ.get("TIDY_HEADLESS"):
        threading.Event().wait()
    try:
        import webview  # pip install pywebview  -> a real Mac window
        webview.create_window("Tidy", url, width=1040, height=700, min_size=(820, 560))
        webview.start()
    except ImportError:
        webbrowser.open(url)
        print("Press Ctrl+C to quit.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
