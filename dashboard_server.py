"""Local server for dashboard.html: serves the repo AND runs the platform's commands from buttons.

    python3 dashboard_server.py            # http://127.0.0.1:8000/dashboard.html
    python3 dashboard_server.py --port 8080

Standard library only. Guardrails:
- Listens on 127.0.0.1 only, and rejects requests whose Host/Origin isn't this server
  (so another website can't make your browser trigger runs).
- Fixed command allowlist — the API never accepts a command, path or argument from the client.
- "apply" (writes issues/, compliance-issues/, state.json) needs {"confirm": "APPLY"} in the request.
- No --github from the UI: opening public GitHub issues stays a deliberate CLI step.
- One command at a time, 120 s timeout. Hidden paths (.git, .env, ...) are never served.
"""
import argparse
import json
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
ACTIONS = {
    "dry-run": {"argv": [PY, "run.py"], "writes": False,
                "label": "Dry run: grade, report, show planned write-back (writes out/run-report.md only)"},
    "apply":   {"argv": [PY, "run.py", "--apply"], "writes": True,
                "label": "Apply: update issues/, write compliance-issues/, record state.json"},
    "grade":   {"argv": [PY, "eval/grade.py"], "writes": False, "label": "Grade out/ against eval/gold.json"},
    "state":   {"argv": [PY, "state.py"], "writes": False, "label": "Show new / changed / removed files"},
}
RUN_LOCK = threading.Lock()
TIMEOUT_S = 120


class Handler(SimpleHTTPRequestHandler):
    server_version = "AlwaysOnDashboard/1.0"

    # --- request hygiene -------------------------------------------------------------
    def _allowed_hosts(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self):
        return self.headers.get("Host", "") in self._allowed_hosts()

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        return origin is None or origin.split("://", 1)[-1] in self._allowed_hosts()

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _hidden_path(self):
        parts = [p for p in self.path.split("?", 1)[0].split("/") if p]
        return any(p.startswith(".") for p in parts)

    # --- static files -----------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        if self.path.split("?", 1)[0] == "/api/actions":
            return self._json(200, {"actions": {k: {"label": v["label"], "writes": v["writes"]} for k, v in ACTIONS.items()},
                                    "busy": RUN_LOCK.locked()})
        if self._hidden_path():
            return self._json(404, {"error": "not found"})
        return super().do_GET()

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")   # always show the latest files
        super().end_headers()

    # --- actions ----------------------------------------------------------------------
    def do_POST(self):
        if not (self._host_ok() and self._origin_ok()) or self.headers.get("X-Dashboard") != "1":
            return self._json(403, {"error": "forbidden: requests must come from this dashboard"})
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/run/"):
            return self._json(404, {"error": "unknown endpoint"})
        name = path.rsplit("/", 1)[-1]
        action = ACTIONS.get(name)
        if not action:
            return self._json(404, {"error": f"unknown action {name!r}"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            body = {}
        if action["writes"] and body.get("confirm") != "APPLY":
            return self._json(400, {"error": "this action writes files — confirmation required"})
        if not RUN_LOCK.acquire(blocking=False):
            return self._json(409, {"error": "another action is still running"})
        try:
            started = time.time()
            proc = subprocess.run(action["argv"], cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT_S)
            out = proc.stdout + (("\n" + proc.stderr) if proc.stderr.strip() else "")
            self.log_message("action %s -> exit %s", name, proc.returncode)
            return self._json(200, {"action": name, "command": " ".join(Path(a).name if i == 0 else a for i, a in enumerate(action["argv"])),
                                    "exit_code": proc.returncode, "seconds": round(time.time() - started, 2), "output": out})
        except subprocess.TimeoutExpired:
            return self._json(504, {"error": f"timed out after {TIMEOUT_S}s"})
        finally:
            RUN_LOCK.release()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=str(ROOT)))
    print(f"Dashboard: http://127.0.0.1:{args.port}/dashboard.html   (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
