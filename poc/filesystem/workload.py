"""HTTP workload for either a local payload or the Python FUSE mount."""

from __future__ import annotations

import argparse
import errno
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MOUNTPOINT = Path(os.environ.get("FUSE_MOUNTPOINT", "/mnt/lazy"))


class WorkloadServer(ThreadingHTTPServer):
    daemon_threads = True


def make_handler(payload: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "PythonFuseWorkload/1.0"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            request = urlsplit(self.path)
            if request.path == "/healthz":
                if not payload.is_file():
                    self._send(503, b"payload mount unavailable\n", "text/plain")
                    return
                self._send(200, b"ready\n", "text/plain")
                return
            if request.path == "/read":
                try:
                    query = parse_qs(request.query)
                    size = int(query.get("bytes", ["1048576"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                except ValueError:
                    self._send(400, b"bytes and offset must be integers\n", "text/plain")
                    return
                if size < 1 or offset < 0:
                    self._send(400, b"bytes must be positive and offset non-negative\n", "text/plain")
                    return
                try:
                    with payload.open("rb") as stream:
                        stream.seek(offset)
                        body = stream.read(size)
                except OSError as exc:
                    self._send(502, f"payload read failed: {exc}\n".encode(), "text/plain")
                    return
                self._send(200, body, "application/octet-stream")
                return
            self._send(404, b"not found\n", "text/plain")

        def log_message(self, fmt: str, *args: object) -> None:
            print("workload: " + fmt % args, flush=True)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _stop_server(server: WorkloadServer, mountpoint: Path, *_: object) -> None:
    def shutdown() -> None:
        server.shutdown()

    threading.Thread(target=shutdown, daemon=True).start()
    if os.path.ismount(mountpoint):
        for command in ("fusermount", "fusermount3"):
            try:
                subprocess.run([command, "-u", str(mountpoint)], check=False, timeout=5)
                break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("baseline", "fuse"), required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.mode == "baseline":
        payload = Path("/app/payload.bin")
        if not payload.is_file():
            raise SystemExit(f"baseline payload does not exist: {payload}")
    else:
        from fusefs import mount_filesystem

        source_url = os.environ.get("FUSE_SOURCE_URL")
        if not source_url:
            raise SystemExit("FUSE_SOURCE_URL is required in fuse mode")
        MOUNTPOINT.mkdir(parents=True, exist_ok=True)
        mount_errors: list[BaseException] = []

        def run_mount() -> None:
            try:
                mount_filesystem(str(MOUNTPOINT), source_url)
            except BaseException as exc:
                mount_errors.append(exc)

        mount_thread = threading.Thread(target=run_mount, name="fuse-mount", daemon=True)
        mount_thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not os.path.ismount(MOUNTPOINT):
            if mount_errors:
                raise SystemExit(f"FUSE mount failed: {mount_errors[0]}")
            if not mount_thread.is_alive():
                raise SystemExit("FUSE mount exited before becoming available")
            time.sleep(0.1)
        if not os.path.ismount(MOUNTPOINT):
            raise SystemExit("FUSE mount did not become available within 30 seconds")
        payload = MOUNTPOINT / "payload.bin"
        print(f"FUSE mount ready at {MOUNTPOINT}", flush=True)

    server = WorkloadServer((args.host, args.port), make_handler(payload))
    signal.signal(signal.SIGTERM, lambda *_signal: _stop_server(server, MOUNTPOINT))
    signal.signal(signal.SIGINT, lambda *_signal: _stop_server(server, MOUNTPOINT))
    print(f"{args.mode} workload listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
