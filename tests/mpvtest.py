"""Helpers for driving a real, headless mpv with the reload script loaded.

Nothing here mocks mpv: tests start `mpv --vo=null --ao=null`, talk to it over
its JSON IPC socket and read its log. Only the Python standard library, mpv and
ffmpeg are needed.

Environment:
  MPV     the mpv command, may contain arguments (default: "mpv"), e.g.
          MPV="./mpv.AppImage --appimage-extract-and-run"
  SCRIPT  path to the script under test (default: ../main.lua)
"""
import functools
import http.server
import json
import os
import re
import shlex
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(HERE, ".cache")
MPV = shlex.split(os.environ.get("MPV", "mpv"))
SCRIPT = os.environ.get("SCRIPT", os.path.join(ROOT, "main.lua"))

# The script name mpv derives from a directory script is the directory name,
# which is also the prefix of its script-opts. The README documents "reload".
SCRIPT_NAME = "reload"


def mpv_version():
    """(major, minor) of the mpv under test, e.g. (0, 37)."""
    out = subprocess.run(MPV + ["--version"], stdout=subprocess.PIPE,
                         universal_newlines=True).stdout
    m = re.search(r"mpv v?(\d+)\.(\d+)", out)
    if not m:
        raise RuntimeError("cannot parse mpv version from: %r" % out[:80])
    return int(m.group(1)), int(m.group(2))


def media(name, seconds=60):
    """Generate (once) a small test video with ffmpeg and return its path."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        tmp = path + ".tmp.mkv"
        subprocess.check_call([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050",
            "-t", str(seconds), "-c:v", "mpeg4", "-q:v", "8",
            "-c:a", "aac", "-b:a", "24k", tmp])
        os.replace(tmp, path)
    return path


class MediaServer:
    """Local HTTP server for one media file, with the failure modes the script
    exists to survive.

    Python's stock http.server has no Range support, which makes mpv restart a
    reload from 0 and looks like a script bug, so Range is implemented here.

    stall_at / stalls_left: the next `stalls_left` connections that stream past
        byte `stall_at` stop sending (and stay open) until the server closes.
    gate: if set to a threading.Event, requests wait for it before answering,
        which lets a test hold a reload "in flight" without sleeping.
    waiting: number of requests currently held at the gate.
    texts: {"/list.m3u": "...{base}..."} served as text, {base} is replaced.
    """

    def __init__(self, path, rate=0.0):
        self.path = path
        self.size = os.path.getsize(path)
        self.rate = rate
        self.stall_at = None
        self.stalls_left = 0
        self.stalls_hit = 0
        self.gate = None
        self.waiting = 0
        self.texts = {}
        self.requests = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    server._serve(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self._srv = Srv(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def url(self, name="media.mkv"):
        return "%s/%s" % (self.base, name)

    def _serve(self, h):
        path = h.path.split("?")[0]
        if path in self.texts:
            body = self.texts[path].replace("{base}", self.base).encode()
            h.send_response(200)
            h.send_header("Content-Type", "text/plain")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
            return
        rng = h.headers.get("Range")
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        with self._lock:
            self.requests.append((path, start))
        gate = self.gate
        if gate is not None:
            with self._lock:
                self.waiting += 1
            try:
                gate.wait(30)
            finally:
                with self._lock:
                    self.waiting -= 1
        h.send_response(206 if rng else 200)
        h.send_header("Content-Type", "video/x-matroska")
        h.send_header("Accept-Ranges", "bytes")
        h.send_header("Content-Length", str(self.size - start))
        if rng:
            h.send_header("Content-Range",
                          "bytes %d-%d/%d" % (start, self.size - 1, self.size))
        h.end_headers()
        sent = 0
        with open(self.path, "rb") as f:
            f.seek(start)
            while not self._stop.is_set():
                if (self.stall_at is not None and self.stalls_left > 0
                        and start + sent >= self.stall_at):
                    with self._lock:
                        if self.stalls_left > 0:
                            self.stalls_left -= 1
                            self.stalls_hit += 1
                            stalled = True
                        else:
                            stalled = False
                    if stalled:
                        self._stop.wait(3600)
                        return
                chunk = f.read(4096)
                if not chunk:
                    break
                h.wfile.write(chunk)
                sent += len(chunk)
                if self.rate:
                    time.sleep(self.rate)

    def close(self):
        self._stop.set()
        if self.gate is not None:
            self.gate.set()
        self._srv.shutdown()
        self._srv.server_close()


class Mpv:
    """One headless mpv with the script under test, driven over IPC."""

    def __init__(self, script_opts=None, args=()):
        # Unix socket paths are limited to ~104 bytes; macOS $TMPDIR is long.
        base = tempfile.gettempdir()
        self.dir = tempfile.mkdtemp(prefix="mpvtest-", dir="/tmp" if len(base) > 40 else base)
        script_dir = os.path.join(self.dir, SCRIPT_NAME)
        os.makedirs(script_dir)
        shutil.copy(SCRIPT, os.path.join(script_dir, "main.lua"))
        self.sock_path = os.path.join(self.dir, "ipc.sock")
        self.log_path = os.path.join(self.dir, "mpv.log")
        opts = ",".join("%s-%s=%s" % (SCRIPT_NAME, k, v)
                        for k, v in (script_opts or {}).items())
        cmd = MPV + [
            "--no-config", "--load-scripts=no", "--ytdl=no",
            "--osc=no", "--load-stats-overlay=no", "--load-console=no",
            "--vo=null", "--ao=null", "--idle=yes", "--force-window=no",
            "--no-terminal", "--input-ipc-server=" + self.sock_path,
            "--log-file=" + self.log_path,
            "--msg-level=all=info,%s=debug" % SCRIPT_NAME,
            "--script=" + script_dir,
        ] + (["--script-opts=" + opts] if opts else []) + list(args)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self._events = []
        self._resp = {}
        self._rid = 0
        self._lock = threading.Lock()
        deadline = time.monotonic() + 10
        while not os.path.exists(self.sock_path):
            if time.monotonic() > deadline or self.proc.poll() is not None:
                raise RuntimeError("mpv did not create its IPC socket\n" + self.log())
            time.sleep(0.05)
        self._sock = socket.socket(socket.AF_UNIX)
        self._sock.connect(self.sock_path)
        self._reader_file = self._sock.makefile("r")
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            for line in self._reader_file:
                msg = json.loads(line)
                with self._lock:
                    if "event" in msg:
                        self._events.append(msg)
                    elif "request_id" in msg:
                        self._resp[msg["request_id"]] = msg
        except (ValueError, OSError):
            pass

    def _send(self, args):
        with self._lock:
            self._rid += 1
            rid = self._rid
        line = json.dumps({"command": list(args), "request_id": rid}) + "\n"
        self._sock.sendall(line.encode())
        return rid

    def cmd(self, *args):
        rid = self._send(args)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self._lock:
                if rid in self._resp:
                    return self._resp.pop(rid)
            time.sleep(0.01)
        raise AssertionError("no IPC reply to %r\n%s" % (args, self.log()))

    def get(self, prop):
        """Property value, or None while it is unavailable."""
        r = self.cmd("get_property", prop)
        return r.get("data") if r.get("error") == "success" else None

    def count(self, event):
        with self._lock:
            return sum(1 for e in self._events if e["event"] == event)

    def log(self):
        try:
            with open(self.log_path, errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def log_count(self, text):
        return self.log().count(text)

    def reload(self):
        r = self.cmd("script-binding", "%s/reload_resume" % SCRIPT_NAME)
        assert r.get("error") == "success", "reload binding failed: %r" % (r,)

    def seek(self, seconds):
        self.cmd("seek", str(seconds), "absolute+exact")

    def close(self):
        try:
            self._send(["quit"])  # mpv may exit before it replies
        except OSError:
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._sock.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def wait_until(cond, what, timeout=15, mpv=None):
    """Poll `cond` until truthy; a timeout is an AssertionError with the mpv log."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(0.05)
    tail = "\n".join(mpv.log().splitlines()[-25:]) if mpv else ""
    raise AssertionError("timed out after %ss waiting for %s\n%s" % (timeout, what, tail))


def reload_position(mpv):
    """Position (seconds) the script last said it would reload from, or None."""
    found = re.findall(r"reloading video from ([0-9.]+) second", mpv.log())
    return float(found[-1]) if found else None
