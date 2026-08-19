# Saved Edge Image Builds

The original and 2026-08-19 image builds are stored separately. Do not use a
filename or tag from one row as an alias for the other build.

## Original builds

| Image | Docker image ID | Repository artifact |
|---|---|---|
| Surveillance | `7ef46a23f2e3` | `surveillance-edge-runtime.tar.part-aa` + `surveillance-edge-runtime.tar.part-ab` |
| Traffic | `282d62f4d4ca` | `traffic-edge-runtime.tar` |
| Edge agent | `c935c76e7bb2` | `pipeline-edge-agent.tar` |

These files retain their original `latest` tags and original contents.

## Build 2026-08-19

| Image | Docker tag | Docker image ID | Repository artifact |
|---|---|---|---|
| Surveillance | `surveillance-edge-runtime:intel-285h-20260819-c00b9a8865b7` | `c00b9a8865b7` | `surveillance-edge-runtime-intel-285h-20260819-c00b9a8865b7.tar.part-aa` + `.part-ab` |
| Traffic | `traffic-edge-runtime:intel-285h-20260819-3175ba8ca08b` | `3175ba8ca08b` | `traffic-edge-runtime-intel-285h-20260819-3175ba8ca08b.tar` |
| Edge agent | `pipeline-edge-agent:intel-285h-20260819-63f219c86d71` | `63f219c86d71` | `pipeline-edge-agent-intel-285h-20260819-63f219c86d71.tar` |

### SHA-256

```text
f546c056fe117ec98c38cd6ace5c46c183ab2a8a806e921422e8dc2bab29af5c  pipeline-edge-agent-intel-285h-20260819-63f219c86d71.tar
f2b1af9a2d2ef942426eff053d577598975969b5b1f16b3c4017d5cd6aa5a8b2  traffic-edge-runtime-intel-285h-20260819-3175ba8ca08b.tar
e2685697f1c0cdacba2d88fd043f0667e75025bce04afb9492dc9d2aa26d7fb9  surveillance-edge-runtime-intel-285h-20260819-c00b9a8865b7.tar.part-aa
0145493117baf4d629d2618399cf7bf93b07df0c1fb1a972f0596180eb0da0ce  surveillance-edge-runtime-intel-285h-20260819-c00b9a8865b7.tar.part-ab
```

## Loading the 2026-08-19 build

Reassemble surveillance first:

```bash
cat surveillance-edge-runtime-intel-285h-20260819-c00b9a8865b7.tar.part-aa \
    surveillance-edge-runtime-intel-285h-20260819-c00b9a8865b7.tar.part-ab \
    > surveillance-edge-runtime-intel-285h-20260819-c00b9a8865b7.tar
```

Load an archive with Docker or Podman:

```bash
docker load -i traffic-edge-runtime-intel-285h-20260819-3175ba8ca08b.tar
podman load -i pipeline-edge-agent-intel-285h-20260819-63f219c86d71.tar
```

## Model delivery build 2026-08-19

This is a separate edge-agent build. It adds management model-bundle download,
archive and per-file SHA-256 verification, immutable local caching, and exact
read-only model mounts. It does not replace or rename the earlier archives.

| Image | Docker tag | Docker image ID | Repository artifact |
|---|---|---|---|
| Edge agent | `pipeline-edge-agent:intel-285h-model-delivery-v1` | `c7bd2e208db8` | `pipeline-edge-agent-intel-285h-model-delivery-v1-c7bd2e208db8.tar` |

```text
19b1d9ba4d60a341736207dd9007c5e2ccfd48440fbd85417827adb7fdf025f0  pipeline-edge-agent-intel-285h-model-delivery-v1-c7bd2e208db8.tar
```

Load it without affecting the prior named build:

```bash
podman load -i pipeline-edge-agent-intel-285h-model-delivery-v1-c7bd2e208db8.tar
```
