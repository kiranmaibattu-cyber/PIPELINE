# QAFT INT8 candidate publication — 2026-11-05

This release preserves the tested Opt79-based QAFT image and all pending optimization source, model and validation work. It is a development candidate with known failures, not a production-qualified replacement. The V8 contract and production gates were unchanged during the QAFT tests. See [validation report](VALIDATION_REPORT.md).

## Image

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.05-v8-qaft-int8-candidate
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime@sha256:53105b8a0c0654b9b50be6c6b7380223ac4f65588076c0c9ea9e32d6ecc1bd28
```

The registry manifest digest differs from the local manifest because registry publication serializes the manifest. Its config digest is the unchanged tested image ID, `sha256:d035cb44df82a66cbcdaa29e3ca22177f7bfdbf080009d6a27bf6633e5eb2127`. Anonymous registry access and manifest/config identity were verified; see `registry-verification.json`.

The active primary detector is QAFT INT8, fixed batch 1, eight requests. GPU fixed-batch probes 1/2/3/4/8 were internally consistent; fully dynamic GPU execution and export-reference numerical fidelity failed. Do not enable dynamic batching or assume an INT8 speedup from these tests. The inherited UTC mapping defect, thermal peaks and memory growth also remain. The image was published without rebuilding it or changing its model precision, thresholds, policy or contract.

## Code and image provenance

- `image-source/` is the source extracted directly from `/opt/traffic-pilot` in the tested image. `image-source-manifest.json` records every non-cache file hash.
- `build-context/` is the actual QAFT model overlay recipe and its input files. Its base is the recorded local Opt79 image, not Opt80. This historical recipe depends on that base; it is not advertised as a standalone clean-room rebuild.
- `source-snapshot-differences.json` identifies files where the development tree differs from the immutable image. In particular, the separate Opt80 row-partition gait experiment remains saved in the development tree and evidence, but is absent from this image.
- The branch includes all accumulated runtime, schema, native helper, build, test and report changes since its last push. Canonical and embedded schema files are preserved together. A fresh general Dockerfile build must be qualified separately; it is not guaranteed to reproduce this existing image digest.

## Repositories and large assets

The same commit and release tag are published on branch `sentry_cv_multi` in the personal repositories `kiranmaibattu-cyber/sentry_cv` and `kiranmaibattu-cyber/PIPELINE`. Existing default branches are not replaced. No ApexFlo organization publication is part of this request.

Large assets are GitHub release downloads on both repositories to avoid Git's 100 MiB file limit and the previously exhausted LFS allowance. Older LFS pointers are preserved in Git history; the complete model archive provides the bytes independently of LFS billing:

- `sentry-v8-qaft-int8-models-2026.11.05.tar.gz`: complete repository models under `repository/models/`, plus the exact baked image models under `image/models/`. Duplicate content is stored as tar hard links. `models-manifest.json` records all 86 file paths, lengths and SHA-256 hashes.
- `sentry-v8-qaft-int8-validation-2026.11.05.tar.gz`: complete QAFT validation captures, previous optimization evidence and isolated validation archive. Failed and thermally excluded attempts are retained. Includes test recordings and representative JPEG/vector captures; these are execution evidence, not labelled accuracy proof.
- `SHA256SUMS`: checksums for the release archives.

The QAFT model files and small new model artifacts are also directly tracked in Git. Existing LFS model files and the large static SigLIP weights are all available in the model archive; an LFS quota failure is not a missing-model condition for this release.

Example download from a checkout (requires GitHub CLI and `sha256sum`):

```bash
gh release download intel-285h-2026.11.05-v8-qaft-int8-candidate \
  --repo kiranmaibattu-cyber/sentry_cv --dir /tmp/sentry-qaft-release
cd /tmp/sentry-qaft-release
sha256sum -c SHA256SUMS
```

To restore all repository model files into a checkout, extract `repository/models` from the model archive with one leading component removed. Use an empty destination or review overwrites first:

```bash
tar -xzf /tmp/sentry-qaft-release/sentry-v8-qaft-int8-models-2026.11.05.tar.gz \
  --strip-components=1 repository/models
```

The validation archive restores its original paths relative to the checkout. Small summary data, audits, model diagnostics, test log and resource plots are also in Git. The API tests stop at the compatible local delivery-agent package endpoint; real Management retrieval and database durability were outside the requested scope.
