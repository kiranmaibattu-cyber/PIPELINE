# V18 scene embeddings

V18 uses the image tower from `google/siglip2-base-patch16-224`, pinned to
revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`. Its native shared
image/text space is 768-dimensional. Vectors are L2 normalized and compared
with cosine similarity.

Edge runs only the OpenVINO image tower. Management must use the text tower
from the exact same checkpoint and preprocessing family. Never compare these
vectors with face, body, another SigLIP revision, or an arbitrary projected
1024D space.

Export the edge artifact with:

```bash
python3 PIPELINE/scripts/export_siglip2_scene_openvino.py
```

The exporter downloads the pinned upstream checkpoint, extracts the image
tower, converts it to FP16 OpenVINO IR, verifies its output shape, and writes
checksums. The V18 Docker build copies the generated directory to
`/models/scene/openvino`; direct model mounts remain useful during development.

Enable per camera with `scene_embeddings`, a `scene_embedding` block that
matches the pinned model metadata, and a `scene_emission` block. With no
configured scene zone the complete frame is embedded; otherwise each configured
scene zone produces its own evidence and vector.

Scene embeddings retrieve visually relevant moments. They do not prove an
interaction such as touching. Those rules also require structured object
relationships and frame or clip evidence.
