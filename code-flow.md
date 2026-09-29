# Code Flow and Benchmark Setup

This document describes the current local PoC implementation. It compares a regular OCI image mounted with containerd's `overlayfs` snapshotter against the same workload converted to eStargz and mounted with Stargz Snapshotter. It is a single-node experiment; it does not implement the larger system described in the original problem statement.

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
| Local Docker Registry (`registry:2`) | Holds the two trial image references at `localhost:5000`, so both benchmark modes use a registry pull path rather than simply starting from the build-local name. It remains running and retains pushed test manifests/layers after a run. |
| `nerdctl` | Containerd-compatible CLI used by the backend to build, tag, convert, push, run, and clean up the PoC's containers/images. |

The Stargz Snapshotter documentation describes it as a proxy plugin daemon that containerd contacts over a Unix socket, and eStargz as a gzip/tar-compatible layer format with metadata for fetching files/chunks independently. See the [Stargz Snapshotter overview](https://github.com/containerd/stargz-snapshotter/blob/main/docs/overview.md) and [eStargz format description](https://github.com/containerd/stargz-snapshotter/blob/main/docs/estargz.md). The project installation guide documents the FUSE requirement and containerd proxy-plugin registration ([installation guide](https://github.com/containerd/stargz-snapshotter/blob/main/docs/INSTALL.md)).

## Rancher setup

### Baseline: `overlayfs`

1. Rancher Desktop is running with **Container Engine = containerd**. The backend discovers Rancher's `nerdctl` executable on Windows or uses `NERDCTL_BIN` if set.
2. `nerdctl info` must reach Rancher's containerd. `overlayfs` is the normal baseline snapshotter; this mode does not require Stargz to start a container.
3. The API creates or starts a named local registry container (`ese-coldstart-poc-registry`) published on host port `5000`.
4. For each trial, the backend builds one ordinary image, tags and pushes its baseline reference, then starts that reference with `--snapshotter overlayfs`.

### Lazy mode: eStargz + Stargz Snapshotter

The `poc/rancher/stargz.start` file is a Rancher Desktop provisioning hook for the Rancher WSL distribution. It is installed on the Windows host at:

```text
%LOCALAPPDATA%\rancher-desktop\provisioning\stargz-snapshotter.start
```

When Rancher executes the hook as root during its startup sequence, it:

1. Installs the Stargz Snapshotter v0.18.2 binaries (`containerd-stargz-grpc` and `ctr-remote`) from the verified release archive. It can use an archive staged in the provisioning directory; otherwise it downloads the pinned release and checks its SHA256.
2. Creates the daemon configuration, state, and runtime directories under `/etc/containerd-stargz-grpc`, `/var/lib/containerd-stargz-grpc`, and `/run/containerd-stargz-grpc`.
3. Saves a one-time backup of Rancher's current `/etc/containerd/config.toml`.
4. Adds a `proxy_plugins.stargz` entry pointing to `/run/containerd-stargz-grpc/containerd-stargz-grpc.sock`, unless that entry is already present. It extends the managed config instead of replacing the whole file.
5. Installs and starts an OpenRC service for `containerd-stargz-grpc`, waits for the socket, and validates the containerd config.
6. Rancher starts/restarts containerd using that config. The `nerdctl info` output should list `stargz` alongside `overlayfs` under `Storage Driver`.

The PoC's conversion step uses `nerdctl image convert --oci --estargz` to make an eStargz variant of the built image. It tags and pushes that variant to the same local registry under a distinct `lazy-...` tag. To run it, the backend passes `--snapshotter stargz`. If the plugin is not installed, registered, and running, the lazy run fails while the overlayfs baseline can still run.

No Docker Desktop engine is used. FUSE support is provided by the Rancher Linux environment; the provisioning hook does not install a Windows FUSE driver. The relevant mount interface is inside Rancher's Linux VM/WSL, where Stargz Snapshotter operates.

## Detailed request and benchmark flow

### 1. Start the backend

Run `py -3 poc/server.py` from the repository root. `poc/server.py` starts Python's `ThreadingHTTPServer` bound to `127.0.0.1:8765`:

- `GET /healthz` checks that `nerdctl info` can reach containerd.
- `POST /benchmarks` validates JSON, then runs the benchmark synchronously in that request.
- `GET /benchmarks` lists saved records; `GET /benchmarks/{id}` returns one record.

Only one benchmark may run at once (`RUN_LOCK`). The API accepts modes `baseline` and/or `lazy`, 1–10 trials, and a payload size from 16 to 1024 MiB. Default payload size is 256 MiB.

### 2. Ensure the runtime and registry

Before each benchmark, `ensure_runtime()` calls `nerdctl info`. `ensure_registry()` checks for the named registry, starts it if stopped, or creates it from `registry:2` with host port `5000` published. The backend polls `http://127.0.0.1:5000/v2/` until it responds.

### 3. Build a paired image set for each trial

`_prepare_images()` creates a unique run/trial ID and random seed, then:

1. Runs `nerdctl build` on `poc/workload/Dockerfile`, passing `PAYLOAD_MIB` and `PAYLOAD_SEED`.
2. The Dockerfile copies in the small HTTP server and writes `/app/payload.bin` as deterministic pseudo-random bytes. The payload is a separate layer and marked read-only. Unique seeds vary the payload layer between trials and reduce accidental cache reuse.
3. Reads the source image size when image inspection supplies it.
4. Tags and pushes the source image as `localhost:5000/ese-coldstart-poc/workload:baseline-<run-id>`.
5. Converts the same source image to OCI eStargz using `nerdctl image convert --oci --estargz`, tags it `...:lazy-<run-id>`, and pushes it.
6. Removes the temporary build-local tags. The registry references remain and are used by the following run commands.

Both modes therefore use the same workload code and logical payload. The lazy variant is a different encoding of those image layers, not a different application.

### 4. Run the baseline and lazy variants

For each requested mode, `_run_one()`:

1. Picks a free host port and records a start timestamp.
2. Invokes `nerdctl run --detach --pull=always` with that mode's image reference, the mode's snapshotter, an HTTP port mapping, and a unique container name.
3. Polls `/healthz` until it gets `200 ready\n`; the elapsed time from before `nerdctl run` through readiness becomes `container_start_to_ready_ms`. This includes the CLI run/pull/create time plus application startup and readiness polling. It is not a pure Python process startup measurement.
4. Requests `/read?bytes=1048576`, measures the HTTP round trip, then repeats the same read immediately. It verifies both responses contain exactly 1 MiB and are byte-identical.
5. Records first-read and warm-read latency, returned bytes, image reference, snapshotter, and elapsed time.
6. In a `finally` block, stops and removes only that trial's named container.

In the overlayfs case, the first read normally reads from the already-present local filesystem layer. In the Stargz case, the server can become ready without touching the payload file; the first `/read` opens it, making the lazy filesystem path fetch whichever metadata/data chunks are required from the registry. The immediate repeat reads content that has been fetched/cached, though normal network, filesystem, and host cache effects remain.

### 5. Save results and clean trial tags

After both modes, `run_benchmark()` removes only the two local image tags for that trial. It does not prune the shared image store or remove unrelated containers. It also does not delete registry-side test content or stop the named registry. Results are inserted at the beginning of `poc/data/benchmarks.json` (up to 100 records) and the request receives the same JSON record.

## Reading the metrics and limits

- `container_start_to_ready_ms`: time from immediately before `nerdctl run` to a successful health response; includes image resolution/pull behavior, container setup, application startup, and probe interval effects.
- `first_read_ms`: HTTP time for the first 1 MiB file read. In lazy mode, this can include on-demand registry requests and FUSE/file access work.
- `warm_read_ms`: time for the next same-size read, after the first request has warmed relevant caches.
- `preparation_ms`: build, conversion, registry push, and preparation work, excluded from per-mode container start latency.
- `source_image_size_bytes`: reported size of the built source image if `nerdctl image inspect` provides it. It is not the bytes fetched during a mode's run.
- `registry_bytes_transferred`: currently `null`. The benchmark does not yet instrument registry range responses, so do not infer bandwidth or pull-byte savings from latency alone.

The benchmark currently runs baseline before lazy, uses one sample by default, and leaves registry/cache state warm between operations. For comparative conclusions, use multiple trials and alternate mode order, control cache conditions, and add registry/proxy telemetry for actual transferred bytes. A local PoC result is not a production performance guarantee.

## Scope and troubleshooting

- If `nerdctl info` fails, start Rancher Desktop and verify the containerd engine is active.
- If overlayfs succeeds but lazy reports that `stargz` is missing, inspect Rancher provisioning-hook installation and restart Rancher Desktop; confirm `nerdctl info` lists `stargz`.
- If the local registry cannot be reached on port 5000, check for another process using that port.
- If `/dev/fuse` is unavailable inside the Rancher Linux environment, Stargz's FUSE mount cannot operate; check the Rancher WSL runtime and Stargz daemon logs.
- When finished, the documented targeted cleanup is `nerdctl rm --force ese-coldstart-poc-registry`; this removes the PoC registry container and its retained test data, not unrelated containers.

This PoC does not implement Firecracker, CRIU, predictive scaling, multi-node orchestration, registry byte accounting, or production-grade cache isolation.
