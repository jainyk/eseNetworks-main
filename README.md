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


| Mode | Snapshotter | Start to ready | 
| --- | --- | ---: | 
| Baseline | `overlayfs` | 3,334 ms | 
| Lazy | `stargz` | 2,465 ms | 

Both modes succeeded. In this run, lazy mode reached readiness about 0.87 seconds sooner.

## API

- `GET /healthz`: backend and nerdctl availability.
- `POST /benchmarks`: start one or more baseline/lazy trials. JSON fields: `modes` (array containing `baseline` and/or `lazy`), `trials` (1-10), and optional `payload_mib` (16-1024).
- `GET /benchmarks`: list persisted benchmark records.
- `GET /benchmarks/{id}`: fetch one record.

Benchmark records are written to `poc/data/benchmarks.json`.

## Scope

This demonstrates eStargz lazy pulling and records local startup/read latency. 