# Intel Images: 2026.09.10-v2

Both application images are published to GHCR. Their complete archives are
saved locally in this directory but excluded from Git; checksums remain tracked.
The August 24 rollback archives remain tracked. Unversioned, August 20, and August 21
archives have been retired from the branch but retained locally. Removing
tracked archives does not reclaim GitHub's historical LFS storage quota.
Older archive names are not aliases for these
builds. Each archive includes its runtime base layers and baked models; no
separate environment container is required.

| Pack | Image tag | Tested image ID |
|---|---|---|
| Traffic | `traffic-edge-runtime:intel-285h-2026.09.10-v2` | `a084e4a94b1422b5d90df51b49cca2c6b8390a839eec66deb2382e253b2aa6ca` |
| Surveillance | `surveillance-edge-runtime:intel-285h-2026.09.10-v2` | `b37fcdd7c9858e9926656a195e4a4dd03584364d89d16fd0e3da75bc3e52d710` |

## Changes

- Surveillance evidence uses the exact processed frame propagated with the
  observation, instead of fetching the latest camera frame asynchronously.
- Event delivery filters previous runtime sessions and omits missing snapshot
  references. Current-session reconnects can replay stable event IDs;
  consumers should deduplicate. Last-Event-ID resume is not implemented.
- Illegal parking requires plate detection and OCR without implicitly enabling
  standalone ANPR events. With both apps selected, shared vehicle references
  link their separate events.
- Parking alerts include vehicle and available plate crops. Retained earlier
  plate crops carry their source timestamp and frame ID. Unreadable or unseen
  plates do not suppress parking alerts.
- Existing persistent `/state` mounts, hardware placement, and container-level
  desired-state reload remain in place. Reload replaces the runtime child,
  not the container.

## Verification

75 unit tests passed. Traffic parking-only and combined parking/ANPR live tests
ran against `traffic1`. The combined 120-second run produced 25 parking events
and 7 ANPR events; all 96 advertised JPEG assets were accessible, with 4 shared
vehicle references across event types. This is not an OCR accuracy measurement.
See the root `TRAFFIC_PARKING_PLATE_EVIDENCE.md` and
`SURVEILLANCE_FRAME_EVIDENCE.md` for evidence semantics and test limitations.

## GHCR Delivery

The exact tested images are available by immutable registry digest:

```bash
docker pull ghcr.io/kiranmaibattu-cyber/traffic-edge-runtime@sha256:c9300da73406887a76304cfa4bdf12de8222b3afe9b6f48ac759023041bf17b4
docker pull ghcr.io/kiranmaibattu-cyber/surveillance-edge-runtime@sha256:d7368b140b05201f241b3db78876676302517168e3ad443f57105c1f83ac3739
```

Both also have the tag `intel-285h-2026.09.10-v2`. The previous versions of
these two GHCR application packages were deleted. Base and edge-agent packages
were not changed. At publication, both application packages were private;
the owner must set package visibility to Public for anonymous pulls.

## Local Offline Archives

Traffic is saved locally as `traffic/image-2026.09.10-v2.tar`. Surveillance has
a complete local tar and `surveillance/image-2026.09.10-v2.tar.part-*` files.
These new binaries are not downloaded by `git lfs pull`. Transfer the saved
archives separately for offline installation. Source, schemas, examples, tests,
and checksums are delivered through Git.

From the traffic directory:

```bash
sha256sum -c image-2026.09.10-v2.sha256
docker load -i image-2026.09.10-v2.tar
```

From the surveillance directory:

```bash
sha256sum -c image-2026.09.10-v2.parts.sha256
cat image-2026.09.10-v2.tar.part-* > image-2026.09.10-v2.tar
sha256sum -c image-2026.09.10-v2.sha256
docker load -i image-2026.09.10-v2.tar
```

Loaded repository tags may include the `localhost/` prefix from Podman export.
Verify the loaded image IDs against the table before deployment. Use each pack's
README and image contract for configuration, hardware, and persistent mounts.
