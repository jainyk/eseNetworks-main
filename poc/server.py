"""Local HTTP API for the Rancher Desktop cold-start experiment."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = ROOT / "poc" / "workload"
DATA_DIR = ROOT / "poc" / "data"
RESULTS_FILE = DATA_DIR / "benchmarks.json"
REGISTRY_NAME = "ese-coldstart-poc-registry"
IMAGE_REPOSITORY = "localhost:5000/ese-coldstart-poc/workload"
REGISTRY_IMAGE = "registry:2"
DEFAULT_PAYLOAD_MIB = int(os.environ.get("POC_PAYLOAD_MIB", "256"))
DEFAULT_READ_BYTES = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 1200
READY_TIMEOUT_SECONDS = 90
RUN_LOCK = threading.Lock()
RESULTS_LOCK = threading.Lock()


class PocError(RuntimeError):
    pass


def nerdctl_path() -> str:
    configured = os.environ.get("NERDCTL_BIN")
    if configured:
        return configured
    discovered = shutil.which("nerdctl")
    if discovered:
        return discovered
    if os.name == "nt":
        rancher_cli = Path(
            r"C:\Program Files\Rancher Desktop\resources\resources\win32\bin\nerdctl.exe"
        )
        if rancher_cli.is_file():
            return str(rancher_cli)
    raise PocError(
        "nerdctl was not found. Put Rancher Desktop's nerdctl on PATH or set NERDCTL_BIN."
    )


def command(args: list[str], *, timeout: int = COMMAND_TIMEOUT_SECONDS) -> str:
    exe = nerdctl_path()
    try:
        result = subprocess.run(
            [exe, *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PocError(f"nerdctl timed out after {timeout} seconds: {args[0]}") from exc
    except OSError as exc:
        raise PocError(f"Could not start nerdctl: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PocError(
            f"nerdctl {' '.join(args[:3])} failed (exit {result.returncode}): "
            f"{detail[-3500:]}"
        )
    return result.stdout.strip()


def ensure_runtime() -> None:
    info = command(["info"], timeout=30)
    if "Server:" not in info:
        raise PocError("nerdctl reached no containerd server. Start Rancher Desktop first.")


def ensure_registry() -> None:
    names = command(
        ["ps", "--all", "--filter", f"name={REGISTRY_NAME}", "--format", "{{.Names}}"],
        timeout=30,
    )
    if REGISTRY_NAME in names.splitlines():
        running = command(
            ["ps", "--filter", f"name={REGISTRY_NAME}", "--format", "{{.Names}}"],
            timeout=30,
        )
        if REGISTRY_NAME not in running.splitlines():
            command(["start", REGISTRY_NAME], timeout=60)
    else:
        command(
            [
                "run",
                "--detach",
                "--name",
                REGISTRY_NAME,
                "--publish",
                "5000:5000",
                REGISTRY_IMAGE,
            ],
            timeout=180,
        )
    _wait_registry()


def _wait_registry() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:5000/v2/", timeout=2):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    raise PocError(
        "The local registry did not become reachable at http://127.0.0.1:5000. "
        "Check whether port 5000 is already in use."
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_get(url: str, timeout: float = 10) -> tuple[int, bytes, float]:
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read()
        status = response.status
    return status, body, (time.perf_counter() - started) * 1000


def _wait_ready(port: int, started_at: float) -> float:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_error = "no response yet"
    while time.monotonic() < deadline:
        try:
            status, body, _ = _http_get(f"http://127.0.0.1:{port}/healthz", timeout=2)
            if status == 200 and body == b"ready\n":
                return (time.perf_counter() - started_at) * 1000
            last_error = f"unexpected readiness response ({status})"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise PocError(f"Container did not become ready within {READY_TIMEOUT_SECONDS}s: {last_error}")


def _read_image_size(image_ref: str) -> int | None:
    try:
        raw = command(["image", "inspect", image_ref], timeout=30)
        doc = json.loads(raw)
        value = doc[0].get("Size") if isinstance(doc, list) and doc else None
        return int(value) if value is not None else None
    except (PocError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _prepare_images(run_id: str, payload_mib: int) -> dict[str, Any]:
    source = f"ese-coldstart-poc-build:{run_id}"
    lazy_local = f"ese-coldstart-poc-build:{run_id}-estargz"
    baseline_ref = f"{IMAGE_REPOSITORY}:baseline-{run_id}"
    lazy_ref = f"{IMAGE_REPOSITORY}:lazy-{run_id}"
    seed = secrets.randbits(31)
    prep_started = time.perf_counter()
    try:
        command(
            [
                "build",
                "--build-arg",
                f"PAYLOAD_MIB={payload_mib}",
                "--build-arg",
                f"PAYLOAD_SEED={seed}",
                "--tag",
                source,
                str(WORKLOAD),
            ]
        )
        image_size = _read_image_size(source)
        command(["tag", source, baseline_ref])
        command(["push", "--insecure-registry", baseline_ref])
        command(["image", "convert", "--oci", "--estargz", source, lazy_local])
        command(["tag", lazy_local, lazy_ref])
        command(["push", "--insecure-registry", lazy_ref])
    except PocError:
        for ref in (source, lazy_local, baseline_ref, lazy_ref):
            try:
                command(["image", "rm", "--force", ref], timeout=60)
            except PocError:
                pass
        raise

    # Remove only this trial's local names so the runtime has to resolve each
    # mode through the registry. No shared cache or unrelated images are pruned.
    for ref in (source, lazy_local, baseline_ref, lazy_ref):
        try:
            command(["image", "rm", "--force", ref], timeout=60)
        except PocError:
            pass

    return {
        "baseline_ref": baseline_ref,
        "lazy_ref": lazy_ref,
        "payload_seed": seed,
        "payload_mib": payload_mib,
        "source_image_size_bytes": image_size,
        "preparation_ms": (time.perf_counter() - prep_started) * 1000,
    }


def _run_one(mode: str, image_ref: str, read_bytes: int, run_id: str) -> dict[str, Any]:
    snapshotter = "overlayfs" if mode == "baseline" else "stargz"
    port = _free_port()
    name = f"ese-coldstart-poc-{run_id}-{mode}"
    started = time.perf_counter()
    try:
        command(
            [
                "run",
                "--detach",
                "--name",
                name,
                "--pull=always",
                "--snapshotter",
                snapshotter,
                "--insecure-registry",
                "--publish",
                f"{port}:8080",
                image_ref,
            ],
            timeout=COMMAND_TIMEOUT_SECONDS,
        ).splitlines()[0]
        ready_ms = _wait_ready(port, started)
        first_status, first_body, first_read_ms = _http_get(
            f"http://127.0.0.1:{port}/read?bytes={read_bytes}", timeout=120
        )
        warm_status, warm_body, warm_read_ms = _http_get(
            f"http://127.0.0.1:{port}/read?bytes={read_bytes}", timeout=120
        )
        if first_status != 200 or warm_status != 200:
            raise PocError(f"{mode} workload returned HTTP {first_status}/{warm_status}")
        if len(first_body) != read_bytes or len(warm_body) != read_bytes:
            raise PocError("Workload returned fewer bytes than requested")
        if first_body != warm_body:
            raise PocError("Repeated payload reads returned different content")
        return {
            "mode": mode,
            "snapshotter": snapshotter,
            "image_ref": image_ref,
            "container_start_to_ready_ms": ready_ms,
            "first_read_ms": first_read_ms,
            "warm_read_ms": warm_read_ms,
            "payload_bytes_returned": len(first_body),
            "registry_bytes_transferred": None,
            "registry_bytes_note": "Not available from the current Rancher Desktop CLI/runtime telemetry.",
            "success": True,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }
    finally:
        try:
            command(["stop", "--time", "2", name], timeout=30)
        except PocError:
            pass
        try:
            command(["rm", "--force", name], timeout=30)
        except PocError:
            pass


def run_benchmark(modes: list[str], trials: int, payload_mib: int) -> dict[str, Any]:
    if not RUN_LOCK.acquire(blocking=False):
        raise PocError("A benchmark is already running. Wait for it to finish before starting another.")
    benchmark_id = uuid.uuid4().hex[:12]
    record: dict[str, Any] = {
        "id": benchmark_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime": "Rancher Desktop containerd via nerdctl",
        "trials_requested": trials,
        "payload_mib": payload_mib,
        "results": [],
        "status": "running",
    }
    try:
        ensure_runtime()
        ensure_registry()
        for trial in range(1, trials + 1):
            pair_id = f"{benchmark_id}-{trial}"
            prepared = _prepare_images(pair_id, payload_mib)
            for mode in modes:
                ref = prepared["baseline_ref"] if mode == "baseline" else prepared["lazy_ref"]
                try:
                    result = _run_one(mode, ref, DEFAULT_READ_BYTES, pair_id)
                except PocError as exc:
                    result = {
                        "mode": mode,
                        "image_ref": ref,
                        "success": False,
                        "error": str(exc),
                    }
                    if mode == "lazy" and "snapshotter" in str(exc).lower():
                        result["setup_hint"] = (
                            "Install and register Stargz Snapshotter in Rancher Desktop's "
                            "containerd, then restart Rancher Desktop."
                        )
                result.update({"trial": trial, **{k: v for k, v in prepared.items() if k.endswith("_ms") or k.endswith("_bytes")}})
                record["results"].append(result)
            # Remove just the image tags created for this pair. The registry and
            # all unrelated images are left alone.
            for ref in (prepared["baseline_ref"], prepared["lazy_ref"]):
                try:
                    command(["image", "rm", "--force", ref], timeout=60)
                except PocError:
                    pass
        record["summary"] = _summarize(record["results"], modes)
        record["status"] = "completed" if all(
            r.get("success") for r in record["results"]
        ) else "partial"
    except PocError as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
    finally:
        record["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save_record(record)
        RUN_LOCK.release()
    return record


def _summarize(results: list[dict[str, Any]], modes: list[str]) -> dict[str, Any]:
    metrics = ("container_start_to_ready_ms", "first_read_ms", "warm_read_ms")
    summary: dict[str, Any] = {}
    for mode in modes:
        successful = [r for r in results if r.get("mode") == mode and r.get("success")]
        mode_summary: dict[str, Any] = {"successful_trials": len(successful)}
        for metric in metrics:
            values = sorted(float(r[metric]) for r in successful if metric in r)
            if values:
                p95_index = max(
                    0, min(len(values) - 1, int(0.95 * len(values) + 0.999999) - 1)
                )
                mode_summary[metric] = {
                    "median": statistics.median(values),
                    "p95": values[p95_index],
                }
        summary[mode] = mode_summary
    return summary


def _save_record(record: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with RESULTS_LOCK:
        records = _load_records()
        records.insert(0, record)
        RESULTS_FILE.write_text(json.dumps(records[:100], indent=2), encoding="utf-8")


def _load_records() -> list[dict[str, Any]]:
    if not RESULTS_FILE.is_file():
        return []
    try:
        value = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


class Handler(BaseHTTPRequestHandler):
    server_version = "ColdStartPoC/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/healthz":
            try:
                ensure_runtime()
                self._json(200, {"ok": True, "runtime": "Rancher Desktop containerd"})
            except PocError as exc:
                self._json(503, {"ok": False, "error": str(exc)})
            return
        if self.path == "/benchmarks":
            self._json(200, _load_records())
            return
        if self.path.startswith("/benchmarks/"):
            wanted = self.path.rsplit("/", 1)[-1]
            record = next((r for r in _load_records() if r.get("id") == wanted), None)
            if record is None:
                self._json(404, {"error": "benchmark not found"})
            else:
                self._json(200, record)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/benchmarks":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 16_384:
                raise ValueError("request body must be between 1 and 16384 bytes")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            modes = body.get("modes", ["baseline", "lazy"])
            trials = int(body.get("trials", 1))
            payload_mib = int(body.get("payload_mib", DEFAULT_PAYLOAD_MIB))
            if not isinstance(modes, list) or not modes or any(m not in ("baseline", "lazy") for m in modes):
                raise ValueError("modes must be a non-empty array containing baseline and/or lazy")
            modes = list(dict.fromkeys(modes))
            if not 1 <= trials <= 10:
                raise ValueError("trials must be between 1 and 10")
            if not 16 <= payload_mib <= 1024:
                raise ValueError("payload_mib must be between 16 and 1024")
            result = run_benchmark(modes, trials, payload_mib)
            self._json(200 if result["status"] in ("completed", "partial") else 503, result)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except PocError as exc:
            self._json(409, {"error": str(exc)})

    def log_message(self, fmt: str, *args: object) -> None:
        print("api: " + fmt % args, flush=True)

    def _json(self, status: int, value: Any) -> None:
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    address = ("127.0.0.1", 8765)
    print(f"Cold-start PoC API listening on http://{address[0]}:{address[1]}", flush=True)
    ThreadingHTTPServer(address, Handler).serve_forever()
