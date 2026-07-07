#!/usr/bin/env python3
"""RawConvert GUI — local web interface for less-technical users.

Stdlib-only HTTP server bound to 127.0.0.1 that serves a single-page wizard
and drives rawconvert.py. Long conversions run as subprocesses using
--progress-json; reads reuse rawconvert's functions directly.

Safety: this server has NO endpoint that deletes files. Staged originals in
_rawconvert_trash/ are reviewed and emptied by a human, in Finder.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import rawconvert

BASE_DIR = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, "frozen", False))  # True inside the built .app
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR)) if FROZEN else BASE_DIR
INDEX_HTML = RESOURCE_DIR / "gui" / "index.html"
DEFAULT_PORT = 8765
TOKEN = secrets.token_hex(16)
EVENT_RING = 50  # most recent progress events kept for the UI feed

# The bundled .app ships its own exiftool (see packaging/APP_NOTES.md)
_bundled_exiftool = RESOURCE_DIR / "vendor" / "exiftool"
if FROZEN and _bundled_exiftool.exists():
    os.environ.setdefault("RAWCONVERT_EXIFTOOL", str(_bundled_exiftool))


def build_job_cmd(folder: str, options: dict) -> list:
    """CLI invocation for a conversion job (patched in tests).

    In the frozen .app there is no python interpreter or rawconvert.py on
    disk — the app re-executes itself with RAWCONVERT_RUN_CLI=1 (handled at
    the top of main()) so the same binary serves as the CLI subprocess.
    """
    if FROZEN:
        cmd = [sys.executable, "process", folder]
    else:
        cmd = [sys.executable, str(BASE_DIR / "rawconvert.py"), "process",
               folder]
    cmd += ["--to", options.get("format", "dng"), "--progress-json"]
    if options.get("quality"):
        cmd += ["--quality", str(int(options["quality"]))]
    if options.get("sample"):
        cmd += ["--sample", str(int(options["sample"]))]
    if options.get("output"):
        cmd += ["--output", str(options["output"])]
    if options.get("render"):
        cmd += ["--render"]
    return cmd


class JobManager:
    """Runs at most one conversion subprocess, mirroring its progress."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.state = {"running": False}

    def start(self, folder: str, options: dict) -> bool:
        with self.lock:
            if self.state.get("running"):
                return False
            self.state = {
                "running": True, "folder": folder,
                "format": options.get("format", "dng"),
                "step": "starting", "total": 0, "done": 0,
                "current": "", "eta_seconds": None,
                "events": [], "failures": [], "summary": None,
                "cancelled": False, "returncode": None,
            }
            env = dict(os.environ, RAWCONVERT_RUN_CLI="1") if FROZEN else None
            self.proc = subprocess.Popen(
                build_job_cmd(folder, options),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env=env)
        threading.Thread(target=self._reader, daemon=True).start()
        return True

    def _reader(self):
        proc = self.proc
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue  # human output from sub-steps; ignore
            self._apply(event)
        proc.wait()
        with self.lock:
            self.state["running"] = False
            self.state["returncode"] = proc.returncode

    def _apply(self, event: dict) -> None:
        with self.lock:
            kind = event.get("type")
            if kind == "step":
                self.state["step"] = event["name"]
            elif kind == "start":
                self.state["total"] = event["total"]
            elif kind == "progress":
                self.state["done"] = event["index"]
                self.state["current"] = event["source"]
                self.state["eta_seconds"] = event.get("eta_seconds")
                self.state["events"].append(event)
                del self.state["events"][:-EVENT_RING]
            elif kind == "failed":
                self.state["failures"].append(event)
            elif kind == "summary":
                self.state["summary"] = event
                self.state["step"] = "done"

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def cancel(self) -> bool:
        with self.lock:
            if not self.state.get("running") or self.proc is None:
                return False
            self.state["cancelled"] = True
            self.proc.terminate()
            return True


JOBS = JobManager()


def pick_folder() -> str:
    """Native macOS folder picker; returns POSIX path or '' on cancel."""
    rc, out, _ = rawconvert.run(
        ["osascript", "-e",
         'POSIX path of (choose folder with prompt '
         '"Choose the folder of RAW photos")'])
    return out.decode().strip() if rc == 0 else ""


def doctor_report() -> dict:
    return {
        "version": rawconvert.__version__,
        "sips": rawconvert.have_sips(),
        "exiftool": rawconvert.have_exiftool(),
        "exiftool_url": rawconvert.EXIFTOOL_URL,
        "dng_converter": rawconvert.dng_converter() is not None,
        "dng_converter_url": rawconvert.DNG_CONVERTER_URL,
    }


def trash_report(folder: Path) -> dict:
    trash = folder / rawconvert.TRASH_DIRNAME
    count = size = 0
    if trash.is_dir():
        for path in trash.rglob("*"):
            if path.is_file():
                count += 1
                size += path.stat().st_size
    return {"path": str(trash), "files": count, "bytes": size}


def run_compare(folder: Path) -> dict:
    """Compare formats on the first RAW in the folder; parse JSON events."""
    files = rawconvert.find_raw_files(folder)
    if not files:
        return {"error": "no RAW files found in this folder"}
    proc = subprocess.run(
        [sys.executable, str(BASE_DIR / "rawconvert.py"), "compare",
         str(files[0]), "--progress-json"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return {"source": files[0].name,
            "src_bytes": files[0].stat().st_size, "events": events}


class Handler(BaseHTTPRequestHandler):
    server_version = "RawConvertGUI"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("X-RawConvert-Token") == TOKEN

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def _query(self) -> dict:
        return dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(self.path).query))

    def _folder_param(self):
        folder = Path(self._query().get("folder", ""))
        return folder if folder.is_dir() else None

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        if route == "/":
            try:
                body = INDEX_HTML.read_bytes()
            except OSError:
                self._json({"error": "gui/index.html missing"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if route == "/api/ping":  # tokenless health check for the launcher
            self._json({"app": "rawconvert-gui"})
            return
        if not route.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        if not self._authorized():
            self._json({"error": "missing or bad token"}, 401)
            return
        if route == "/api/doctor":
            self._json(doctor_report())
        elif route == "/api/scan":
            folder = self._folder_param()
            if folder is None:
                self._json({"error": "not a folder"}, 400)
                return
            self._json(rawconvert.scan_stats(folder))
        elif route == "/api/job":
            self._json(JOBS.snapshot())
        elif route == "/api/trash":
            folder = self._folder_param()
            if folder is None:
                self._json({"error": "not a folder"}, 400)
                return
            self._json(trash_report(folder))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        if not self._authorized():
            self._json({"error": "missing or bad token"}, 401)
            return
        if route == "/api/pick-folder":
            path = pick_folder()
            self._json({"path": path, "cancelled": not path})
        elif route == "/api/compare":
            body = self._body()
            folder = Path(body.get("folder", ""))
            if not folder.is_dir():
                self._json({"error": "not a folder"}, 400)
                return
            self._json(run_compare(folder))
        elif route == "/api/job":
            body = self._body()
            folder = Path(body.get("folder", ""))
            if not folder.is_dir():
                self._json({"error": "not a folder"}, 400)
                return
            if JOBS.start(str(folder), body.get("options", {})):
                self._json({"started": True})
            else:
                self._json({"error": "a job is already running"}, 409)
        elif route == "/api/job/cancel":
            self._json({"cancelled": JOBS.cancel(),
                        "note": "Already-converted files are kept; running"
                                " Convert again resumes where it left off."})
        elif route == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown,
                             daemon=True).start()
        elif route == "/api/reveal":
            target = Path(self._body().get("path", ""))
            if not target.exists():
                self._json({"error": "path does not exist"}, 400)
                return
            args = ["open", "-R", str(target)] if target.is_file() \
                else ["open", str(target)]
            rawconvert.run(args)
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):  # no CORS: cross-origin preflights always fail
        self.send_response(405)
        self.end_headers()


def create_server(port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def already_running(port: int) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/ping" % port, timeout=1) as resp:
            return json.load(resp).get("app") == "rawconvert-gui"
    except Exception:
        return False


def main(argv=None) -> int:
    # Frozen .app re-invokes itself as the CLI for conversion jobs
    if os.environ.get("RAWCONVERT_RUN_CLI"):
        return rawconvert.main(sys.argv[1:] if argv is None else argv)
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    try:
        server = create_server(args.port)
    except OSError:
        if already_running(args.port):
            print("RawConvert is already running — use the browser tab"
                  " that's already open (or quit that Terminal window"
                  " and relaunch).")
            return 0
        raise
    url = "http://127.0.0.1:%d/?token=%s" % (server.server_address[1], TOKEN)
    print("RawConvert GUI running at %s" % url, flush=True)
    print("Keep this window open while you work. Press Ctrl+C to quit.",
          flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGoodbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
