"""Read-only HTTP byte-range origin used by the standalone FUSE demo."""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")


class OriginState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.range_requests = 0
        self.bytes_sent = 0

    def record(self, byte_count: int) -> None:
        with self.lock:
            self.range_requests += 1
            self.bytes_sent += byte_count

    def reset(self) -> None:
        with self.lock:
            self.range_requests = 0
            self.bytes_sent = 0

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "range_requests": self.range_requests,
                "bytes_sent": self.bytes_sent,
            }


def make_handler(payload: Path, state: OriginState) -> type[BaseHTTPRequestHandler]:
    size = payload.stat().st_size

    class Handler(BaseHTTPRequestHandler):
        server_version = "FuseRangeOrigin/1.0"

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/payload.bin":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path == "/healthz":
                self._json(200, {"ok": True, "payload_bytes": size})
                return
            if self.path == "/stats":
                self._json(200, state.snapshot())
                return
            if self.path != "/payload.bin":
                self.send_error(404)
                return

            range_value = self.headers.get("Range")
            match = RANGE_RE.fullmatch(range_value or "")
            if not match:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            start = int(match.group(1))
            end = min(int(match.group(2)) if match.group(2) else size - 1, size - 1)
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            expected = end - start + 1
            with payload.open("rb") as stream:
                stream.seek(start)
                body = stream.read(expected)
            if len(body) != expected:
                self.send_error(500, "short read from origin payload")
                return

            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)
            state.record(len(body))

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/stats/reset":
                self.send_error(404)
                return
            state.reset()
            self._json(200, state.snapshot())

        def _json(self, status: int, value: dict[str, Any]) -> None:
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            print("origin: " + fmt % args, flush=True)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    if not args.file.is_file():
        parser.error(f"payload file does not exist: {args.file}")
    state = OriginState()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.file, state))
    print(f"range origin listening on {args.host}:{args.port}; size={args.file.stat().st_size}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
