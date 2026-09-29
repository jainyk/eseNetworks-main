"""Small HTTP workload whose large data file is not needed for readiness."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


PAYLOAD = Path(__file__).with_name("payload.bin")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        request = urlsplit(self.path)
        if request.path == "/healthz":
            self._send(200, b"ready\n", "text/plain")
            return
        if request.path == "/read":
            try:
                requested = int(parse_qs(request.query).get("bytes", ["1048576"])[0])
            except ValueError:
                self._send(400, b"bytes must be an integer\n", "text/plain")
                return
            if requested < 1 or requested > PAYLOAD.stat().st_size:
                self._send(400, b"bytes is outside the payload size\n", "text/plain")
                return
            with PAYLOAD.open("rb") as payload:
                content = payload.read(requested)
            self._send(200, content, "application/octet-stream")
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


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
