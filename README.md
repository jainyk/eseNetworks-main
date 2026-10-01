# Container cold-start PoC

This PoC compares startup and first-request latency for the same Python service image using Rancher Desktop's containerd runtime:

- `baseline`: ordinary OCI image with containerd's `overlayfs` snapshotter.
- `lazy`: eStargz image with Stargz Snapshotter, fetched on demand.

The sample image contains a 256 MiB deterministic payload in a separate image layer. Its HTTP server becomes ready without opening the payload; `/read` reads a requested prefix so the first request exercises lazy file fetching.

## Prerequisites

- Rancher Desktop running with the **containerd** engine (the `nerdctl` CLI works).
- Python 3.10 or newer on Windows. The backend uses only the Python standard library.
- The Stargz Snapshotter plugin installed and registered with Rancher Desktop's containerd before selecting `lazy` mode. The repository includes `poc/rancher/stargz.start`, a Rancher Desktop Windows provisioning hook for installing Stargz Snapshotter v0.18.2 and registering it with containerd.

Keep the container engine set to containerd; Kubernetes can remain enabled or disabled. To install Stargz, copy `poc/rancher/stargz.start` to `%LOCALAPPDATA%\\rancher-desktop\\provisioning\\stargz-snapshotter.start`, then restart Rancher Desktop. This provisioning hook preserves and extends Rancher's generated containerd configuration. The installed runtime should report both `overlayfs` and `stargz` in `nerdctl info`.

## Components and what each one does

| Component | Role in this PoC |
| --- | --- |
| Windows Python backend (`poc/server.py`) | Exposes a small HTTP API, orchestrates `nerdctl`, times readiness and file-read requests, and saves JSON benchmark records. It uses only Python's standard library. |
| Workload (`poc/workload/server.py`) | A small HTTP server inside the test image. `/healthz` signals readiness. `/read?bytes=N` opens and reads the payload file only when requested. |
| Workload Dockerfile | Builds the Python service image and writes a deterministic, trial-specific large payload into its own image layer. |
| Rancher Desktop | Supplies the local Linux VM/WSL environment and containerd daemon. The PoC targets Rancher's **containerd** engine through its `nerdctl` CLI. |
| containerd | Pulls/resolves the image, asks a snapshotter to prepare the container root filesystem, and runs the container. |
| `overlayfs` snapshotter | The baseline snapshotter. It creates the container's writable filesystem view from locally available image layers using Linux OverlayFS. The normal image pull makes layer contents available before the workload can use them. |
| eStargz | An image-layer format compatible with OCI registries that adds a table of contents and independently addressable compressed regions/chunks. This lets a runtime fetch needed file data without downloading and unpacking the entire layer first. |
| Stargz Snapshotter (`containerd-stargz-grpc`) | A containerd proxy snapshotter. It serves the `stargz` snapshotter API over a Unix socket and presents eStargz layers as remote/lazy filesystem snapshots. It fetches metadata and file chunks from the registry as they are needed. |
| FUSE | Linux's Filesystem in Userspace interface. Stargz Snapshotter uses a FUSE mount to expose the remote image filesystem to the container. A workload's normal `open`/`read` is serviced by the mounted filesystem; missing file data can cause Stargz to fetch the required eStargz chunk(s), then make the data available to that read. FUSE is the kernel/userspace filesystem bridge; it is not the image format, registry, or snapshotter itself. |
| Local Docker Registry | Holds the two trial image references at `localhost:5000`, so both benchmark modes use a registry pull path rather than simply starting from the build-local name. It remains running and retains pushed test manifests/layers after a run. |
| `nerdctl` | Containerd-compatible CLI used by the backend to build, tag, convert, push, run, and clean up the PoC's containers/images. |

## Run

From the repository root in Command Prompt or PowerShell:

```powershell
py -3 poc\server.py
```

The API listens only on `127.0.0.1:8765`.

Run one baseline and one lazy trial:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/benchmarks `
  -ContentType 'application/json' `
  -Body '{"modes":["baseline","lazy"],"trials":1}'
```

Read saved benchmark results:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/benchmarks
```

Set `NERDCTL_BIN` if `nerdctl` is not on `PATH`. Set `POC_PAYLOAD_MIB` to change the generated image payload size (default `256`).

## What is measured

Each trial builds one payload image and an eStargz-converted variant, publishes them to a local registry, then measures container start to `/healthz`.

## Recorded benchmark result

One baseline/lazy pair was run on Rancher Desktop containerd v2.3.2 with a 256 MiB payload. 


| Mode | Snapshotter | Start to ready | 
| --- | --- |---------------:| 
| Baseline | `overlayfs` |       3,334 ms | 
| Lazy | `stargz` |       1,665 ms | 


## API

- `GET /healthz`: backend and nerdctl availability.
- `POST /benchmarks`: start one or more baseline/lazy trials. JSON fields: `modes` (array containing `baseline` and/or `lazy`).
- `GET /benchmarks`: list persisted benchmark records.
- `GET /benchmarks/{id}`: fetch one record.

## Scope

This demonstrates eStargz lazy pulling and records local startup/read latency. 

## Python FUSE demonstrator

A separate read-only Python FUSE experiment serves a virtual payload file through HTTP range requests and reports source bytes fetched. It compares a payload-in-image baseline with a small FUSE client image; the source container is started before client timing. 

## Approach

The demo makes a large payload available as a single read-only virtual file, `/mnt/lazy/payload.bin`:

1. The origin container serves a generated payload through HTTP `HEAD` and `GET` with a single byte `Range` request. It counts range requests and payload bytes returned.
2. The FUSE process asks the origin for the file length, so it can report file metadata without downloading the file contents.
3. When the application opens and reads the virtual file, the kernel sends FUSE read operations to the Python daemon. The daemon maps each offset to fixed-size chunks and fetches only the missing chunks from the origin.
4. The daemon keeps a bounded least-recently-used in-memory chunk cache. For this demonstration, direct I/O bypasses the kernel page cache so the repeat request exercises the Python cache and should need no additional origin bytes.
5. The application exposes `/healthz` only after the FUSE mount is usable, plus `/read?bytes=N&offset=O` to exercise normal file access.

FUSE is the Linux kernel/userspace bridge for filesystem operations; the Python daemon supplies the file metadata and data. The cache chunk size is 1 MiB and its limit is 16 MiB by default. Both can be changed at runtime by the benchmark CLI. The demo opens the FUSE file with direct I/O so the kernel page cache does not hide repeated reads from the Python LRU cache; this makes the PoC's cache behavior visible, but is a measurement choice rather than a production tuning recommendation.


## Modules

| File | Responsibility |
| --- | --- |
| `poc/filesystem/Dockerfile` | Defines three build targets from shared generated data: `origin` (large payload plus range server), `baseline` (workload with payload in the image), and `fuse` (workload plus Python FUSE code, without the payload). |
| `poc/filesystem/origin.py` | Serves `HEAD /payload.bin`, exact `GET /payload.bin` byte ranges, `/healthz`, `/stats`, and `POST /stats/reset`. Rejects invalid/out-of-bounds ranges with HTTP 416. |
| `poc/filesystem/fusefs.py` | Implements the read-only FUSE root and `/payload.bin` callbacks. Validates range status, `Content-Range`, and response size before caching bytes. |
| `poc/filesystem/workload.py` | In `baseline` mode serves `/app/payload.bin`; in `fuse` mode mounts the virtual file first, then serves from `/mnt/lazy/payload.bin`. Provides health and range-of-file reads. |
| `poc/filesystem/benchmark.py` | Builds and pushes trial images, starts the origin before timing, runs baseline and FUSE clients, verifies content/edge reads, records source range byte counts, and writes JSON results. |
| `poc/filesystem/requirements.txt` | Pins `fusepy` for repeatable FUSE client image builds. |
| `poc/data/filesystem-benchmarks.json` | Stores the standalone FUSE benchmark records (created on first run). |


### Verified Rancher run

One 256 MiB trial completed on Rancher Desktop containerd. Origin startup was excluded as described above.

| Client | Start to ready | 
| --- |---------------:| 
| Baseline |       1,764 ms | 
| Python FUSE |       1,038 ms | 

