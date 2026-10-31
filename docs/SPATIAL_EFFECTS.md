# 3D-style effects inside the video editor

Keep editing footage as usual. Add a spatial effect to a chosen part of the cut,
adjust its strength, and render it with the rest of the video. These effects use
perspective and animated framing to give flat footage a spatial appearance. They
do not infer scene depth, reveal unseen sides of a product, or create a 3D model.

| Effect | Good starting point | What moves |
| --- | --- | --- |
| Floating frame (`float`) | A product detail or a playful opening | The video frame floats over a blurred, darkened copy of the same footage, the usual vertical-short backdrop. |
| Perspective sway (`tilt`) | A short reveal or a detail worth holding | The frame changes perspective over the selected interval. |
| Push-in reveal (`push`) | An opening moment or a visual emphasis | The framing moves toward the footage, then returns smoothly. |

Use one deliberate accent before layering more. Speech, source cuts, and pacing
still do most of the storytelling. Start at moderate strength and preview the
finished render before sharing.

## Studio

1. Upload footage and choose the story, length, and output frame.
2. Choose a 3D-style effect, strength, start time, and duration.
3. Create the story and review the selected moments. Effect times refer to the
   assembled edit, so shortening or reordering moments changes what they affect.
4. Make the video, inspect the result, and download it with the edit record.

The effect runs after reframing and before captions. Captions remain fixed on
the output canvas. The original source preview does not show the effect; the
finished video is the rendered result. Choosing None leaves the edit without a
spatial effect. No Blender installation, cloud upload, or model API is required.
Changing only the effect updates the next render without asking Meta to plan
the story again. The previous verified delivery stays available during a new
attempt, with its version clearly labeled.

## YAML and Python

The compiler accepts the same operation used by Studio:

```yaml
name: floating-detail
sources:
  main: ./product.mp4
ops:
  - op: select_ranges
    ranges:
      - {start: 2, end: 10}
  - op: reframe
    aspect: "9:16"
  - op: spatial_effect
    preset: float
    intensity: 0.55
    start: 0
    duration: 3
  - op: export
    preset: shorts_9x16
```

```python
from vedit import Edit

edit = (Edit("product.mp4")
        .trim(start=2, end=10)
        .reframe("9:16")
        .spatial_effect("float", intensity=0.55, duration=3)
        .export("shorts_9x16"))
edit.plan("out/floating-detail")
# Inspect out/floating-detail/project.json and render.sh before execution.
```

Run `vedit execute out/floating-detail`, then read `report.json` and play the
output. The visual effect and the file's technical validity are separate checks.

Strength is between 0 and 1; zero applies no motion. Start time is nonnegative.
When duration is omitted it defaults to up to three seconds of the remaining
clip. An explicitly specified interval must fit inside the assembled edit.
Unknown presets, non-finite numbers, and invalid intervals are rejected before
rendering. The catalogue is available through `/api/capabilities`.

## Extending the workflow

A future subject pop-out would need a reliable foreground mask, background
handling, and visual checks for missing edges. Meta segmentation can provide a
mask, but the current tracking integration supplies reframing geometry; it does
not yet implement that compositing effect. True camera movement through a
reconstructed scene and arbitrary 3D asset editing are separate capabilities.
