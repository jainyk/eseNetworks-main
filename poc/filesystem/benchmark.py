"""Build and benchmark the standalone Python FUSE filesystem demonstrator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import statistics
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONTEXT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "poc" / "data"
RESULTS_FILE = DATA_DIR / "filesystem-benchmarks.json"
REGISTRY_NAME = "ese-coldstart-poc-registry"
REGISTRY_IMAGE = "registry:2"
IMAGE_REPOSITORY = "localhost:5000/ese-coldstart-poc/filesystem"
COMMAND_TIMEOUT = 1200
READ_BYTES = 1024 * 1024


class BenchmarkError(RuntimeError):
    pass


def nerdctl_path() -> str:
    configured = os.environ.get("NERDCTL_BIN")
    if configured:
        return configured
    found = shutil.which("nerdctl")
    if found:
        return found
    if os.name == "nt":
        candidate = Path(
            r"C:\Program Files\Rancher Desktop\resources\resources\win32\nerdctl.exe"
        )
        if candidate.is_file():
            return str(candidate)
    raise BenchmarkError("nerdctl was not found; start Rancher Desktop or set NERDCTL_BIN")


def command(args: list[str], timeout: int = COMMAND_TIMEOUT, *, check: bool = True) -> str:
    try:
        result = subprocess.run(
            [nerdctl_path(), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError(f"nerdctl {args[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise BenchmarkError(f"could not start nerdctl: {exc}") from exc
    output = result.stdout.strip()
    if result.returncode and check:
        detail = (result.stderr or output).strip()
        raise BenchmarkError(
            f"nerdctl {' '.join(args[:3])} failed (exit {result.returncode}): {detail[-3500:]}"
        )
    return output


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def http_json(url: str, *, method: str = "GET", timeout: float = 3) -> dict[str, Any]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"HTTP request failed for {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"unexpected JSON response from {url}")
    return value


def wait_http(url: str, *, timeout: int, expected: bytes | None = None) -> tuple[bytes, float]:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    started = time.perf_counter()
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                body = response.read()
                if expected is None or body == expected:
                    return body, (time.perf_counter() - started) * 1000
                last_error = f"unexpected HTTP body: {body[:100]!r}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise BenchmarkError(f"timed out waiting for {url}: {last_error}")


def ensure_registry() -> None:
    all_names = command(
        ["ps", "--all", "--filter", f"name={REGISTRY_NAME}", "--format", "{{.Names}}"]
    ).splitlines()
    if REGISTRY_NAME in all_names:
        running = command(["ps", "--filter", f"name={REGISTRY_NAME}", "--format", "{{.Names}}"]).splitlines()
        if REGISTRY_NAME not in running:
            command(["start", REGISTRY_NAME], timeout=60)
    else:
        command(
            ["run", "--detach", "--name", REGISTRY_NAME, "--publish", "5000:5000", REGISTRY_IMAGE],
            timeout=180,
        )
    wait_http("http://127.0.0.1:5000/v2/", timeout=30)


def image_ip(container_name: str) -> str:
    raw = command(["inspect", container_name], timeout=30)
    try:
        data = json.loads(raw)
        container = data[0] if isinstance(data, list) else data
        networks = container["NetworkSettings"]["Networks"]
        for network in networks.values():
            address = network.get("IPAddress")
            if address:
                return str(address)
    except (KeyError, TypeError, json.JSONDecodeError, IndexError) as exc:
        raise BenchmarkError(f"could not determine IP address for {container_name}: {exc}") from exc
    raise BenchmarkError(f"container {container_name} has no network IP")


def image_names(run_id: str) -> dict[str, str]:
    return {
        target: f"{IMAGE_REPOSITORY}:{target}-{run_id}"
        for target in ("origin", "baseline", "fuse")
    }


def build_and_push(run_id: str, payload_mib: int, seed: int) -> tuple[dict[str, str], float]:
    names = image_names(run_id)
    started = time.perf_counter()
    try:
        for target, tag in names.items():
            command(
                [
                    "build",
                    "--file",
                    str(CONTEXT / "Dockerfile"),
                    "--target",
                    target,
                    "--build-arg",
                    f"PAYLOAD_MIB={payload_mib}",
                    "--build-arg",
                    f"PAYLOAD_SEED={seed}",
                    "--tag",
                    tag,
                    str(CONTEXT),
                ]
            )
            command(["push", "--insecure-registry", tag], timeout=COMMAND_TIMEOUT)
    except BenchmarkError:
        for tag in names.values():
            command(["image", "rm", "--force", tag], timeout=60, check=False)
        raise
    for tag in names.values():
        command(["image", "rm", "--force", tag], timeout=60, check=False)
    return names, (time.perf_counter() - started) * 1000


def start_origin(run_id: str, image: str) -> tuple[str, int, str]:
    name = f"ese-fuse-origin-{run_id}"
    host_port = free_port()
    try:
        command(
            [
                "run",
                "--detach",
                "--pull=always",
                "--name",
                name,
                "--publish",
                f"127.0.0.1:{host_port}:8081",
                "--network",
                "bridge",
                image,
            ],
            timeout=COMMAND_TIMEOUT,
        )
        wait_http(f"http://127.0.0.1:{host_port}/healthz", timeout=30)
        address = image_ip(name)
        return name, host_port, address
    except BenchmarkError:
        command(["stop", "--time", "2", name], timeout=30, check=False)
        command(["rm", "--force", name], timeout=30, check=False)
        raise


def stats_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    return {
        key: int(after.get(key, 0)) - int(before.get(key, 0))
        for key in ("range_requests", "bytes_sent")
    }


def run_client(
    mode: str,
    image: str,
    run_id: str,
    *,
    origin_ip: str,
    origin_port: int,
    chunk_size: int,
    cache_size: int,
) -> dict[str, Any]:
    name = f"ese-fuse-client-{run_id}-{mode}"
    port = free_port()
    before_start = time.perf_counter()
    args = [
        "run",
        "--detach",
        "--pull=always",
        "--name",
        name,
        "--network",
        "bridge",
        "--snapshotter",
        "overlayfs",
    ]
    if mode == "fuse":
        args += [
            "--device",
            "/dev/fuse:/dev/fuse:rwm",
            "--cap-add",
            "SYS_ADMIN",
            "--security-opt",
            "apparmor=unconfined",
            "--env",
            f"FUSE_SOURCE_URL=http://{origin_ip}:8081/payload.bin",
            "--env",
            f"FUSE_CHUNK_SIZE={chunk_size}",
            "--env",
            f"FUSE_CACHE_SIZE={cache_size}",
        ]
    args += ["--publish", f"127.0.0.1:{port}:8080", image]
    origin_url = f"http://127.0.0.1:{origin_port}"
    origin_before_start = http_json(f"{origin_url}/stats")
    try:
        command(args, timeout=COMMAND_TIMEOUT)
        wait_http(
            f"http://127.0.0.1:{port}/healthz", timeout=60, expected=b"ready\n"
        )
        ready_ms = (time.perf_counter() - before_start) * 1000
        base = f"http://127.0.0.1:{port}"
        after_ready = http_json(f"{origin_url}/stats")
        status, first_body, first_ms = http_read(base, READ_BYTES, 0)
        after_first = http_json(f"{origin_url}/stats")
        status_warm, warm_body, warm_ms = http_read(base, READ_BYTES, 0)
        after_warm = http_json(f"{origin_url}/stats")
        if status != 200 or status_warm != 200:
            raise BenchmarkError(f"{mode} read returned HTTP {status}/{status_warm}")
        if len(first_body) != READ_BYTES or first_body != warm_body:
            raise BenchmarkError(f"{mode} payload reads did not return matching 1 MiB bodies")
        offset = max(0, chunk_size - 123)
        offset_status, offset_body, _ = http_read(base, 512, offset)
        eof_status, eof_body, _ = http_read(base, 128, payload_size_from(origin_url) - 64)
        empty_status, empty_body, _ = http_read(base, 1, payload_size_from(origin_url))
        after_edge_reads = http_json(f"{origin_url}/stats")
        if offset_status != 200 or len(offset_body) != 512:
            raise BenchmarkError(f"{mode} unaligned offset read returned {offset_status}/{len(offset_body)} bytes")
        if eof_status != 200 or len(eof_body) != 64:
            raise BenchmarkError(f"{mode} EOF-adjacent read returned {eof_status}/{len(eof_body)} bytes")
        if empty_status != 200 or empty_body:
            raise BenchmarkError(f"{mode} read at exact EOF returned {empty_status}/{len(empty_body)} bytes")
        return {
            "mode": mode,
            "snapshotter": "overlayfs",
            "container_start_to_ready_ms": ready_ms,
            "first_read_ms": first_ms,
            "warm_read_ms": warm_ms,
            "payload_bytes_returned": len(first_body),
            "origin_before_ready": stats_delta(origin_before_start, after_ready),
            "origin_first_read": stats_delta(after_ready, after_first),
            "origin_warm_read": stats_delta(after_first, after_warm),
            "origin_offset_and_eof_reads": stats_delta(after_warm, after_edge_reads),
            "offset_read_sha256": hashlib.sha256(offset_body).hexdigest(),
            "eof_adjacent_bytes": len(eof_body),
            "exact_eof_bytes": len(empty_body),
            "success": True,
            "elapsed_ms": (time.perf_counter() - before_start) * 1000,
        }
    finally:
        command(["stop", "--time", "2", name], timeout=30, check=False)
        command(["rm", "--force", name], timeout=30, check=False)


def http_read(base: str, size: int, offset: int) -> tuple[int, bytes, float]:
    url = f"{base}/read?bytes={size}&offset={offset}"
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            body = response.read()
            return response.status, body, (time.perf_counter() - started) * 1000
    except (OSError, urllib.error.URLError) as exc:
        raise BenchmarkError(f"workload read failed: {exc}") from exc


def payload_size_from(origin_url: str) -> int:
    return int(http_json(f"{origin_url}/healthz").get("payload_bytes", 0))


def run_trial(
    run_id: str,
    trial: int,
    payload_mib: int,
    chunk_size: int,
    cache_size: int,
) -> dict[str, Any]:
    pair_id = f"{run_id}-{trial}"
    seed = secrets.randbits(31)
    images, preparation_ms = build_and_push(pair_id, payload_mib, seed)
    origin_name = ""
    try:
        origin_name, origin_port, origin_ip = start_origin(pair_id, images["origin"])
        origin_url = f"http://127.0.0.1:{origin_port}"
        origin_info = http_json(f"{origin_url}/healthz")
        if int(origin_info.get("payload_bytes", -1)) != payload_mib * 1024 * 1024:
            raise BenchmarkError("origin payload size does not match requested payload size")
        modes = [
            run_client(
                "baseline",
                images["baseline"],
                pair_id,
                origin_ip=origin_ip,
                origin_port=origin_port,
                chunk_size=chunk_size,
                cache_size=cache_size,
            ),
        ]
        http_json(f"{origin_url}/stats/reset", method="POST")
        modes.append(
            run_client(
                "fuse",
                images["fuse"],
                pair_id,
                origin_ip=origin_ip,
                origin_port=origin_port,
                chunk_size=chunk_size,
                cache_size=cache_size,
            )
        )
        if modes[0]["offset_read_sha256"] != modes[1]["offset_read_sha256"]:
            raise BenchmarkError("baseline and FUSE reads at a non-zero offset differ")
        for result in modes:
            result["trial"] = trial
            result["preparation_ms"] = preparation_ms
            result["source_payload_bytes"] = payload_mib * 1024 * 1024
            result["chunk_size_bytes"] = chunk_size
            result["fuse_cache_limit_bytes"] = cache_size
        return {
            "trial": trial,
            "payload_seed": seed,
            "images": images,
            "results": modes,
            "preparation_ms": preparation_ms,
        }
    finally:
        if origin_name:
            command(["stop", "--time", "2", origin_name], timeout=30, check=False)
            command(["rm", "--force", origin_name], timeout=30, check=False)
        for image in images.values():
            command(["image", "rm", "--force", image], timeout=60, check=False)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for mode in ("baseline", "fuse"):
        successful = [item for item in results if item.get("mode") == mode and item.get("success")]
        current: dict[str, Any] = {"successful_trials": len(successful)}
        for metric in ("container_start_to_ready_ms", "first_read_ms", "warm_read_ms"):
            values = [float(item[metric]) for item in successful if metric in item]
            if values:
                current[metric] = statistics.median(values)
        summary[mode] = current
    return summary


def save_record(record: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        records = json.loads(RESULTS_FILE.read_text(encoding="utf-8")) if RESULTS_FILE.exists() else []
        if not isinstance(records, list):
            records = []
    except (OSError, json.JSONDecodeError):
        records = []
    records.insert(0, record)
    RESULTS_FILE.write_text(json.dumps(records[:100], indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--payload-mib", type=int, default=256)
    parser.add_argument("--chunk-kib", type=int, default=1024)
    parser.add_argument("--cache-mib", type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.trials <= 5:
        parser.error("--trials must be between 1 and 5")
    if not 16 <= args.payload_mib <= 1024:
        parser.error("--payload-mib must be between 16 and 1024")
    if args.chunk_kib < 64:
        parser.error("--chunk-kib must be at least 64")
    if args.cache_mib < args.chunk_kib / 1024:
        parser.error("--cache-mib must hold at least one chunk")
    return args


def main() -> None:
    args = parse_args()
    chunk_size = args.chunk_kib * 1024
    cache_size = args.cache_mib * 1024 * 1024
    run_id = uuid.uuid4().hex[:12]
    record: dict[str, Any] = {
        "id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime": "Rancher Desktop containerd via nerdctl",
        "experiment": "Python FUSE range filesystem vs. overlayfs payload image",
        "trials_requested": args.trials,
        "payload_mib": args.payload_mib,
        "results": [],
        "status": "running",
        "note": "The range-origin container is started before client timing; its startup and image pull are excluded.",
    }
    try:
        info = command(["info"], timeout=30)
        if "Server:" not in info:
            raise BenchmarkError("nerdctl did not reach a containerd server")
        ensure_registry()
        for trial in range(1, args.trials + 1):
            trial_record = run_trial(run_id, trial, args.payload_mib, chunk_size, cache_size)
            record["results"].extend(trial_record["results"])
        record["summary"] = summarize(record["results"])
        record["status"] = "completed" if all(r["success"] for r in record["results"]) else "partial"
    except (BenchmarkError, OSError) as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
    finally:
        record["completed_at"] = datetime.now(timezone.utc).isoformat()
        save_record(record)
    print(json.dumps(record, indent=2))
    if record["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
