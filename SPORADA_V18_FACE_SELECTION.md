# V18 Face Sample Selection

V18 separates the usability floor from the preferred representative quality.

```text
face track
  -> reject samples below minimum_quality
  -> retain the best exact-frame candidate for selection_window_seconds
  -> emit immediately when preferred_quality is reached
  -> otherwise emit the best usable candidate when the window expires
```

Management controls the optional values in `config.emission`:

```json
{
  "minimum_quality": 0.20,
  "preferred_quality": 0.65,
  "selection_window_seconds": 1.5,
  "cooldown_seconds": 30,
  "material_change_threshold": 0.15
}
```

Old desired states remain valid. If the new fields are absent, the edge uses
`0.65` and `1.5` seconds. The selected crop still contains original-frame
context at twice the face extent, and the embedding still uses the aligned
112x112 model chip derived from that same source frame.

Live verification artifacts:

- `run/sporada-v18-face-delivery-1790141650/`
- `run/sporada-v18-face-delivery-1790141763/`

The latter verifies exact `sample_id` correlation between crop, embedding and
SSE after an outage and container restart.
