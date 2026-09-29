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

Each trial builds one payload image and an eStargz-converted variant, publishes them to a local registry, then measures container start to `/healthz`, the first `/read`, and an immediate repeated `/read`. 


## Recorded benchmark result

One baseline/lazy pair was run on Rancher Desktop containerd v2.3.2 with a 256 MiB payload.git  


| Mode | Snapshotter | Start to ready | First 1 MiB read | Repeat 1 MiB read |
| --- | --- | ---: | ---: | ---: |
| Baseline | `overlayfs` | 3,334 ms | 20 ms | 23 ms |
| Lazy | `stargz` | 2,465 ms | 146 ms | 41 ms |

Both modes succeeded. In this run, lazy mode reached readiness about 0.87 seconds sooner, while its first read took longer, consistent with the on-demand fetch path. Registry transfer bytes were not measured (`null`), so this run cannot quantify image bytes saved. Build, conversion, and push preparation took about 54.0 seconds and are not included in the per-mode start-to-ready measurements. The complete record, benchmark ID `bdf3c6ffe809`, is saved in `poc/data/benchmarks.json`.

## Where the images and registry data are stored

This machine's Rancher Desktop containerd uses the WSL-side root `/var/lib/rancher/k3s/agent/containerd` (containerd state is `/run/k3s/containerd`). Image blobs and metadata are held in containerd's content store under that root; snapshot filesystem data is managed in its snapshotter-specific directories. These are Linux paths inside the Rancher Desktop WSL distribution, not ordinary Windows folders you should edit manually.

The benchmark removes its temporary/local baseline and lazy image tags after each trial. The only PoC image still listed in the local `nerdctl images` output after the run is `registry:2`. The benchmark's pushed workload images remain in the local registry's storage volume, whose exact WSL-side data path on this machine is:

```text
/var/lib/nerdctl/dbb19c5e/volumes/default/2233a49ff0a8f877309f975cf5f01cc6b23ce8ca23174ccce4bfd83f2e6c3fcd/_data
```

That directory was about 1.6 GiB when checked and contains registry data (including manifests and image layers), not a set of unpacked `Dockerfile` images. The containing Rancher data is backed by this Windows virtual disk:

```text
C:\Users\vjiit\AppData\Local\rancher-desktop\distro-data\ext4.vhdx
```

The VHDX is Rancher's virtual Linux disk, and its file size is not the same as the registry data size. Do not edit or delete the VHDX or containerd's internal files directly. Use `nerdctl` to inspect/manage images; to remove the PoC registry and its retained trial data, use the targeted command in the previous section.

## API

- `GET /healthz`: backend and nerdctl availability.
- `POST /benchmarks`: start one or more baseline/lazy trials. JSON fields: `modes` (array containing `baseline` and/or `lazy`), `trials` (1-10), and optional `payload_mib` (16-1024).
- `GET /benchmarks`: list persisted benchmark records.
- `GET /benchmarks/{id}`: fetch one record.

Benchmark records are written to `poc/data/benchmarks.json`.

## Scope

This demonstrates eStargz lazy pulling and records local startup/read latency. It does not implement Firecracker, CRIU, predictive scaling, multi-node orchestration, or production-grade cache isolation.
