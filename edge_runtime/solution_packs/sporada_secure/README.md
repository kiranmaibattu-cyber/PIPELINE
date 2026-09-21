# Sporada Secure Runtime Sources

`runtime_v14/` is the source snapshot used by `docker/Dockerfile.traffic-v14`.
Keep release-specific changes in this namespace so rebuilding older Traffic
images does not silently pick up Sporada Secure changes.

Large, immutable model files remain in the repository's shared versioned model
library and are copied into the image by the Dockerfile.
