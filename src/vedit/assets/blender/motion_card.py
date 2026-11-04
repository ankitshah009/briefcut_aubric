"""Briefcut's 3D motion card: one fixed Blender template that renders a validated spec.

Run headless, never with a model's code:

    blender -b --factory-startup --python motion_card.py -- spec.json out_dir

The spec is data written by ``vedit.blender_cards`` after ``vedit.edit_spec``
has bounded it. This script reads it with ``json.load`` and only ever assigns
its strings to text bodies. Nothing from the spec is evaluated, imported,
formatted into code or used as a path outside the ones Briefcut chose. Every
choice is looked up in the tables below, so an unknown value falls back to a
default instead of reaching Blender.

Output: ``out_dir/frame_0001.png`` onwards, RGBA with straight alpha, plus
``out_dir/card.json`` with what was drawn and how long it took.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

import bpy
from mathutils import Vector

STARTED = time.time()

# --- Closed tables. The spec picks rows by name; it never supplies values. ---

STYLES = {
    # extrude and bevel are shares of the text height.
    "brushed_metal": {"extrude": 0.22, "bevel": 0.04, "bevel_segments": 4, "layers": 1},
    "warm_wood": {"extrude": 0.26, "bevel": 0.03, "bevel_segments": 3, "layers": 1},
    "soft_clay": {"extrude": 0.28, "bevel": 0.075, "bevel_segments": 6, "layers": 1},
    "neon_glass": {"extrude": 0.14, "bevel": 0.025, "bevel_segments": 3, "layers": 1},
    "paper_cut": {"extrude": 0.035, "bevel": 0.0, "bevel_segments": 0, "layers": 3},
}
CAMERAS = ("dolly_in", "orbit", "rise", "push_tilt")
KINDS = ("intro_title", "end_card", "lower_third")
DEFAULT_COLORS = {"primary": "#D9DDE3", "accent": "#7FB2FF", "shadow": "#0B0E14"}
DISTANCE = 12.0  # camera to text, in metres
FOCAL = 50.0


def _hex(value, fallback):
    text = value if isinstance(value, str) else fallback
    if len(text) != 7 or text[0] != "#":
        text = fallback
    try:
        channels = [int(text[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    except ValueError:
        channels = [int(fallback[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    # sRGB to the scene's linear space.
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels) + (1.0,)


def _number(value, low, high, fallback):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return fallback
    return min(high, max(low, float(value)))


def _string(value, limit):
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch == "\n" or ord(ch) >= 32)[:limit]


def read_spec(path):
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise SystemExit("motion card spec must be a JSON object")
    colors = raw.get("colors") if isinstance(raw.get("colors"), dict) else {}
    lines = raw.get("lines") if isinstance(raw.get("lines"), list) else [raw.get("text")]
    lines = [_string(line, 40) for line in lines[:3] if _string(line, 40).strip()]
    fonts = raw.get("fonts") if isinstance(raw.get("fonts"), dict) else {}
    spec = {
        "kind": raw.get("kind") if raw.get("kind") in KINDS else "intro_title",
        "lines": lines or ["Briefcut"],
        "subtitle": _string(raw.get("subtitle"), 60).strip(),
        "style": raw.get("style") if raw.get("style") in STYLES else "brushed_metal",
        "camera": raw.get("camera") if raw.get("camera") in CAMERAS else "dolly_in",
        "duration": _number(raw.get("duration"), 0.5, 4.0, 3.0),
        "fps": _number(raw.get("fps"), 12, 60, 30),
        "width": int(_number(raw.get("width"), 64, 3840, 1080)) // 2 * 2,
        "height": int(_number(raw.get("height"), 64, 3840, 1920)) // 2 * 2,
        "samples": int(_number(raw.get("samples"), 1, 128, 32)),
        "colors": {key: _hex(colors.get(key), DEFAULT_COLORS[key]) for key in DEFAULT_COLORS},
        "fonts": {key: fonts.get(key) if isinstance(fonts.get(key), str) and os.path.isfile(fonts.get(key)) else None
                  for key in ("title", "subtitle")},
        "backdrop": raw.get("backdrop") if isinstance(raw.get("backdrop"), str) and os.path.isfile(raw.get("backdrop")) else None,
    }
    return spec


# --- Scene ---


def clear_scene():
    for collection in (bpy.data.objects, bpy.data.meshes, bpy.data.curves, bpy.data.lights, bpy.data.cameras, bpy.data.materials):
        for block in list(collection):
            collection.remove(block)


def setup_render(scene, spec, out_dir):
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = spec["width"]
    scene.render.resolution_y = spec["height"]
    scene.render.resolution_percentage = 100
    fps = spec["fps"]
    scene.render.fps = int(round(fps))
    scene.render.fps_base = scene.render.fps / fps
    scene.frame_start = 1
    scene.frame_end = max(2, int(round(spec["duration"] * fps)))
    scene.render.film_transparent = True
    scene.render.use_persistent_data = True
    settings = scene.render.image_settings
    settings.file_format = "PNG"
    settings.color_mode = "RGBA"
    settings.color_depth = "8"
    settings.compression = 15
    scene.render.filepath = os.path.join(out_dir, "frame_")
    eevee = scene.eevee
    eevee.taa_render_samples = spec["samples"]
    for name, value in (("use_raytracing", True), ("use_shadows", True), ("shadow_ray_count", 2), ("shadow_step_count", 8),
                        ("use_fast_gi", True), ("bokeh_max_size", 48.0)):
        if hasattr(eevee, name):
            try:
                setattr(eevee, name, value)
            except (TypeError, ValueError):
                pass
    # Standard keeps the palette's colours and a backdrop plate as they were chosen.
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
    except TypeError:
        pass


def world(scene, spec):
    """A dark studio sky with a bright horizon band, so metal and glass have something to reflect."""
    sky = bpy.data.worlds.new("studio")
    scene.world = sky
    if sky.node_tree is None:  # node trees are always on from Blender 5
        sky.use_nodes = True
    nodes, links = sky.node_tree.nodes, sky.node_tree.links
    nodes.clear()
    coords = nodes.new("ShaderNodeTexCoord")
    split = nodes.new("ShaderNodeSeparateXYZ")
    ramp = nodes.new("ShaderNodeValToRGB")
    background = nodes.new("ShaderNodeBackground")
    output = nodes.new("ShaderNodeOutputWorld")
    links.new(coords.outputs["Generated"], split.inputs[0])
    links.new(split.outputs["Z"], ramp.inputs["Fac"])
    stops = ramp.color_ramp.elements
    accent = spec["colors"]["accent"]
    stops[0].position, stops[0].color = 0.42, (0.004, 0.004, 0.006, 1)
    stops[1].position, stops[1].color = 1.0, (0.05, 0.05, 0.06, 1)
    band = stops.new(0.53)
    band.color = (0.9, 0.86, 0.8, 1)
    tint = stops.new(0.62)
    tint.color = (accent[0] * 0.25, accent[1] * 0.25, accent[2] * 0.25, 1)
    links.new(ramp.outputs["Color"], background.inputs["Color"])
    background.inputs["Strength"].default_value = 0.9
    links.new(background.outputs["Background"], output.inputs["Surface"])


# --- Materials ---


def _principled(name):
    material = bpy.data.materials.new(name)
    nodes = material.node_tree.nodes
    bsdf = next(node for node in nodes if node.type == "BSDF_PRINCIPLED")
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    return material, bsdf


def _set(bsdf, **values):
    names = {"base": "Base Color", "metallic": "Metallic", "roughness": "Roughness", "coat": "Coat Weight", "coat_roughness": "Coat Roughness",
             "subsurface": "Subsurface Weight", "sheen": "Sheen Weight", "transmission": "Transmission Weight", "ior": "IOR",
             "emission": "Emission Color", "emission_strength": "Emission Strength", "anisotropic": "Anisotropic", "specular": "Specular IOR Level"}
    for key, value in values.items():
        socket = bsdf.inputs.get(names[key])
        if socket is not None:
            socket.default_value = value


def _bump(material, bsdf, *, scale, strength, kind="noise", detail=6.0):
    nodes, links = material.node_tree.nodes, material.node_tree.links
    coords = nodes.new("ShaderNodeTexCoord")
    texture = nodes.new("ShaderNodeTexNoise" if kind == "noise" else "ShaderNodeTexWave")
    texture.inputs["Scale"].default_value = scale
    if "Detail" in texture.inputs:
        texture.inputs["Detail"].default_value = detail
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = strength
    links.new(coords.outputs["Object"], texture.inputs["Vector"])
    links.new(texture.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return texture


def material_for(style, colors, role):
    """The face and side materials for one style. ``role`` is title, subtitle or a paper layer index."""
    primary, accent, shadow = colors["primary"], colors["accent"], colors["shadow"]
    material, bsdf = _principled(f"{style}-{role}")
    nodes, links = material.node_tree.nodes, material.node_tree.links
    if style == "brushed_metal":
        if role != "title":
            _set(bsdf, base=accent, metallic=0.35, roughness=0.32, coat=0.3, coat_roughness=0.1)
            return material
        _set(bsdf, base=primary, metallic=1.0, roughness=0.24, anisotropic=0.6, coat=0.2, coat_roughness=0.08)
        coords = nodes.new("ShaderNodeTexCoord")
        mapping = nodes.new("ShaderNodeMapping")
        mapping.inputs["Scale"].default_value = (1.0, 60.0, 1.0)
        noise = nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 8.0
        ramp = nodes.new("ShaderNodeMapRange")
        ramp.inputs["To Min"].default_value = 0.16
        ramp.inputs["To Max"].default_value = 0.34
        links.new(coords.outputs["Object"], mapping.inputs["Vector"])
        links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
        links.new(noise.outputs["Fac"], ramp.inputs["Value"])
        links.new(ramp.outputs["Result"], bsdf.inputs["Roughness"])
    elif style == "warm_wood":
        # Long grain along the letters: noise-warped bands, two tones of the palette, a satin coat.
        coords = nodes.new("ShaderNodeTexCoord")
        mapping = nodes.new("ShaderNodeMapping")
        mapping.inputs["Scale"].default_value = (0.12, 1.0, 0.5)
        wave = nodes.new("ShaderNodeTexWave")
        wave.wave_type = "BANDS"
        wave.bands_direction = "Y"
        wave.inputs["Scale"].default_value = 4.2
        wave.inputs["Distortion"].default_value = 12.0
        wave.inputs["Detail"].default_value = 5.0
        wave.inputs["Detail Scale"].default_value = 1.2
        wave.inputs["Detail Roughness"].default_value = 0.6
        ramp = nodes.new("ShaderNodeValToRGB")
        dark = tuple(c * 0.55 for c in primary[:3]) + (1.0,)
        ramp.color_ramp.elements[0].color = dark
        ramp.color_ramp.elements[1].color = primary
        ramp.color_ramp.elements[0].position = 0.05
        ramp.color_ramp.elements[1].position = 0.62
        links.new(coords.outputs["Object"], mapping.inputs["Vector"])
        links.new(mapping.outputs["Vector"], wave.inputs["Vector"])
        links.new(wave.outputs["Fac"], ramp.inputs["Fac"])
        bump = nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 0.08
        links.new(wave.outputs["Fac"], bump.inputs["Height"])
        links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
        if role == "subtitle":
            _set(bsdf, base=accent, roughness=0.5, specular=0.4)
        else:
            links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
            _set(bsdf, roughness=0.38, coat=0.45, coat_roughness=0.14, specular=0.6)
    elif style == "soft_clay":
        _set(bsdf, base=primary if role == "title" else accent, roughness=0.62, subsurface=0.25, sheen=0.3, specular=0.35)
        _bump(material, bsdf, scale=45.0, strength=0.06)
    elif style == "neon_glass":
        if role == "title":
            smoke = tuple(c * 0.6 for c in shadow[:3]) + (1.0,)
            _set(bsdf, base=smoke, metallic=0.0, roughness=0.08, coat=1.0, coat_roughness=0.03, specular=0.8)
        else:
            _set(bsdf, base=accent, roughness=0.3, emission=accent, emission_strength=3.0)
    else:  # paper_cut: layer 0 is the face, deeper layers are the palette's stacked sheets
        layer = int(role) if str(role).isdigit() else 0
        palette = (primary, accent, shadow)
        _set(bsdf, base=palette[min(layer, 2)], roughness=0.92, sheen=0.4, specular=0.2)
        _bump(material, bsdf, scale=180.0, strength=0.035)
    return material


def shadow_material():
    material = bpy.data.materials.new("drop-shadow")
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "BLENDED"
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    black = nodes.new("ShaderNodeEmission")
    black.inputs["Color"].default_value = (0, 0, 0, 1)
    clear = nodes.new("ShaderNodeBsdfTransparent")
    mix = nodes.new("ShaderNodeMixShader")
    mix.inputs["Fac"].default_value = 0.5
    links.new(clear.outputs[0], mix.inputs[1])
    links.new(black.outputs[0], mix.inputs[2])
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(mix.outputs[0], output.inputs["Surface"])
    return material


def neon_edge(colors):
    material, bsdf = _principled("neon-edge")
    _set(bsdf, base=colors["primary"], roughness=0.25, emission=colors["primary"], emission_strength=3.2)
    return material


# --- Text ---


def text_object(name, body, font_path, *, size, extrude, bevel, segments, align):
    curve = bpy.data.curves.new(name, type="FONT")
    curve.body = body  # data, never code
    if font_path:
        try:
            curve.font = bpy.data.fonts.load(font_path, check_existing=True)
        except RuntimeError:
            pass
    curve.size = size
    curve.extrude = extrude
    curve.bevel_depth = bevel
    curve.bevel_resolution = segments
    curve.align_x = align
    curve.align_y = "CENTER"
    curve.space_line = 0.92
    curve.resolution_u = 6
    if hasattr(curve, "offset"):
        curve.offset = -bevel * 0.5
    obj = bpy.data.objects.new(name, curve)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def frame_size(spec, distance):
    """The visible width and height at ``distance`` for a 36 mm sensor fitted to the longer side."""
    aspect = spec["width"] / spec["height"]
    long_side = distance * 36.0 / FOCAL
    return (long_side, long_side / aspect) if aspect >= 1 else (long_side * aspect, long_side)


def dimensions(obj):
    bpy.context.view_layer.update()
    return obj.dimensions.copy()


def build_text(spec):
    """Title and subtitle, sized to the frame, centred on the origin. Returns (root, title objects, subtitle, layout)."""
    style = STYLES[spec["style"]]
    kind = spec["kind"]
    visible_w, visible_h = frame_size(spec, DISTANCE)
    portrait = spec["height"] > spec["width"]
    lower = kind == "lower_third"
    align = "LEFT" if lower else "CENTER"
    body = "\n".join(spec["lines"])
    title = text_object("title", body, spec["fonts"]["title"], size=1.0, extrude=0.0, bevel=0.0, segments=0, align=align)
    raw = dimensions(title)
    target_w = visible_w * (0.78 if portrait else 0.62) * (0.7 if lower else 1.0)
    target_h = visible_h * ((0.36 if len(spec["lines"]) > 2 else 0.30) if portrait else 0.36) * (0.34 if lower else 1.0)
    scale = min(target_w / max(raw.x, 1e-3), target_h / max(raw.y, 1e-3))
    height = scale * raw.y / len(spec["lines"])
    title.data.size = scale
    title.data.extrude = style["extrude"] * height
    title.data.bevel_depth = style["bevel"] * height
    title.data.bevel_resolution = style["bevel_segments"]
    if hasattr(title.data, "offset"):
        title.data.offset = -style["bevel"] * height * 0.5
    title.rotation_euler = (math.radians(90), 0, 0)  # stand the text up, facing -Y
    root = bpy.data.objects.new("card", None)
    bpy.context.scene.collection.objects.link(root)
    title.parent = root
    titles = [title]
    title.data.materials.append(material_for(spec["style"], spec["colors"], "title" if spec["style"] != "paper_cut" else "0"))
    if spec["style"] == "neon_glass":
        # Backlit channel letters: smoked glass in front of a slightly larger lit copy of each glyph.
        halo = text_object("halo", body, spec["fonts"]["title"], size=scale, extrude=0.0, bevel=0.0, segments=0, align=align)
        halo.data.offset = height * 0.05
        halo.rotation_euler = title.rotation_euler
        halo.location = (0.0, style["extrude"] * height + height * 0.08, 0.0)
        halo.data.materials.append(neon_edge(spec["colors"]))
        halo.parent = root
        titles.append(halo)
    if style["layers"] > 1:
        step = style["extrude"] * height * 2.6
        for layer in range(1, style["layers"]):
            sheet = text_object(f"layer{layer}", body, spec["fonts"]["title"], size=scale, extrude=style["extrude"] * height,
                                bevel=0.0, segments=0, align=align)
            sheet.rotation_euler = title.rotation_euler
            sheet.location = (height * 0.04 * layer, step * layer, -height * 0.04 * layer)
            sheet.data.materials.append(material_for("paper_cut", spec["colors"], str(layer)))
            sheet.parent = root
            titles.append(sheet)
    if not lower:
        # A soft shadow: a flat dark copy behind the words, scaled for perspective and blurred by depth of field.
        depth = height * 0.5
        shadow = text_object("shadow", body, spec["fonts"]["title"], size=scale * (DISTANCE + depth) / DISTANCE, extrude=0.0,
                             bevel=0.0, segments=0, align=align)
        shadow.rotation_euler = title.rotation_euler
        shadow.location = (height * 0.05, depth, -height * 0.09)
        shadow.data.materials.append(shadow_material())
        shadow.visible_shadow = False
        shadow.parent = root
        titles.append(shadow)
    title_dims = dimensions(title)
    subtitle = None
    width = title_dims.x
    gap = height * 0.34
    if spec["subtitle"]:
        sub_size = height * (0.42 if not lower else 0.5)
        subtitle = text_object("subtitle", spec["subtitle"], spec["fonts"]["subtitle"], size=sub_size, extrude=sub_size * 0.1,
                               bevel=sub_size * 0.015, segments=2, align=align)
        subtitle.data.space_character = 1.06
        sub_dims = dimensions(subtitle)
        limit = max(title_dims.x, visible_w * (0.4 if lower else 0.78)) * 0.98
        if sub_dims.x > limit:
            subtitle.data.size = sub_size * limit / sub_dims.x
            sub_dims = dimensions(subtitle)
        subtitle.rotation_euler = (math.radians(90), 0, 0)
        subtitle.parent = root
        subtitle.data.materials.append(material_for(spec["style"], spec["colors"], "subtitle" if spec["style"] != "paper_cut" else "1"))
        total = title_dims.y + gap + sub_dims.y
        title.location.z = total / 2 - title_dims.y / 2
        subtitle.location.z = -total / 2 + sub_dims.y / 2
        for sheet in titles[1:]:
            sheet.location.z += title.location.z
        width = max(width, sub_dims.x)
    if lower:
        # Bottom-left, inside the title-safe area.
        root.location.x = -visible_w / 2 + visible_w * 0.08
        block = title_dims.y + ((gap + dimensions(subtitle).y) if subtitle else 0.0)
        # Above where captions sit, so the two never collide.
        root.location.z = -visible_h / 2 + visible_h * (0.3 if portrait else 0.2) + block / 2
    return root, titles, subtitle, {"visible": (visible_w, visible_h), "line_height": height, "scale": scale, "width": width}


# --- Light, scrim, backdrop, camera ---


def area(name, location, energy, size, color, target):
    light = bpy.data.lights.new(name, type="AREA")
    light.energy = energy
    light.size = size
    light.color = color[:3]
    obj = bpy.data.objects.new(name, light)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    constraint = obj.constraints.new("TRACK_TO")
    constraint.target = target
    constraint.track_axis = "TRACK_NEGATIVE_Z"
    constraint.up_axis = "UP_Y"
    return obj


def lights(spec, target):
    accent = spec["colors"]["accent"]
    warm = (1.0, 0.92, 0.82, 1.0)
    scale = DISTANCE / 12.0
    area("key", (-5.5 * scale, -8.0 * scale, 5.5 * scale), 3200 * scale * scale, 7.0 * scale, warm, target)
    area("fill", (7.0 * scale, -6.0 * scale, -1.0 * scale), 500 * scale * scale, 9.0 * scale, (0.85, 0.9, 1.0, 1.0), target)
    area("rim_left", (-7.0 * scale, 6.0 * scale, 3.0 * scale), 3200 * scale * scale, 3.0 * scale, accent, target)
    area("rim_right", (7.5 * scale, 5.0 * scale, 2.0 * scale), 2600 * scale * scale, 3.0 * scale, warm, target)
    area("top", (0.0, 1.0 * scale, 9.0 * scale), 900 * scale * scale, 6.0 * scale, warm, target)


def _camera_plane(name, camera, spec, depth, material):
    """A plane parented to the camera that exactly fills the frame at ``depth``."""
    width, height = frame_size(spec, depth)
    mesh = bpy.data.meshes.new(name)
    w, h = width * 0.56, height * 0.56
    mesh.from_pydata([(-w, -h, 0), (w, -h, 0), (w, h, 0), (-w, h, 0)], [], [(0, 1, 2, 3)])
    mesh.uv_layers.new(name="UVMap")
    for loop, uv in zip(mesh.uv_layers[0].data, ((0, 0), (1, 0), (1, 1), (0, 1))):
        loop.uv = uv
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.parent = camera
    obj.location = (0, 0, -depth)
    mesh.materials.append(material)
    obj.visible_shadow = False
    return obj


def scrim_material(spec):
    """Darkens the footage behind the words so they read on any picture: black at a soft, bounded alpha."""
    material = bpy.data.materials.new("scrim")
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "BLENDED"
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    coords = nodes.new("ShaderNodeTexCoord")
    curve = nodes.new("ShaderNodeMapRange")
    curve.interpolation_type = "SMOOTHSTEP"
    if spec["kind"] == "lower_third":
        # Dark along the bottom edge, gone by 42% of the height.
        split = nodes.new("ShaderNodeSeparateXYZ")
        links.new(coords.outputs["UV"], split.inputs[0])
        links.new(split.outputs["Y"], curve.inputs["Value"])
        curve.inputs["From Min"].default_value = 0.42
        curve.inputs["From Max"].default_value = 0.02
        strength = 0.66
    else:
        # A soft ellipse in the frame's own shape.
        mapping = nodes.new("ShaderNodeMapping")
        mapping.inputs["Location"].default_value = (-1.0, -1.0, 0)
        mapping.inputs["Scale"].default_value = (2.0, 2.0, 1.0)
        links.new(coords.outputs["UV"], mapping.inputs["Vector"])
        length = nodes.new("ShaderNodeVectorMath")
        length.operation = "LENGTH"
        links.new(mapping.outputs["Vector"], length.inputs[0])
        links.new(length.outputs["Value"], curve.inputs["Value"])
        curve.inputs["From Min"].default_value = 1.0
        curve.inputs["From Max"].default_value = 0.15
        strength = 0.84 if spec["kind"] == "end_card" else 0.78
    amount = nodes.new("ShaderNodeMath")
    amount.operation = "MULTIPLY"
    amount.inputs[1].default_value = strength
    links.new(curve.outputs["Result"], amount.inputs[0])
    black = nodes.new("ShaderNodeEmission")
    black.inputs["Color"].default_value = (0, 0, 0, 1)
    clear = nodes.new("ShaderNodeBsdfTransparent")
    mix = nodes.new("ShaderNodeMixShader")
    links.new(amount.outputs[0], mix.inputs["Fac"])
    links.new(clear.outputs[0], mix.inputs[1])
    links.new(black.outputs[0], mix.inputs[2])
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(mix.outputs[0], output.inputs["Surface"])
    return material


def backdrop_material(path):
    """A still plate behind the card, opaque until the card hands over to the footage."""
    material = bpy.data.materials.new("backdrop")
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "BLENDED"
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    coords = nodes.new("ShaderNodeTexCoord")
    image = nodes.new("ShaderNodeTexImage")
    image.name = "plate"
    image.image = bpy.data.images.load(path, check_existing=True)
    image.extension = "EXTEND"
    links.new(coords.outputs["UV"], image.inputs["Vector"])
    shade = nodes.new("ShaderNodeEmission")
    shade.inputs["Strength"].default_value = 0.7
    links.new(image.outputs["Color"], shade.inputs["Color"])
    clear = nodes.new("ShaderNodeBsdfTransparent")
    fade = nodes.new("ShaderNodeValue")
    fade.name = "fade"
    fade.outputs[0].default_value = 1.0
    mix = nodes.new("ShaderNodeMixShader")
    links.new(fade.outputs[0], mix.inputs["Fac"])
    links.new(clear.outputs[0], mix.inputs[1])
    links.new(shade.outputs[0], mix.inputs[2])
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(mix.outputs[0], output.inputs["Surface"])
    return material


def _cover_uvs(obj, image_size, frame):
    """Crop the image's UVs so it covers the frame without stretching."""
    iw, ih = image_size
    fw, fh = frame
    if iw <= 0 or ih <= 0:
        return
    image_aspect, frame_aspect = iw / ih, fw / fh
    if image_aspect > frame_aspect:
        span = frame_aspect / image_aspect
        us, vs = (0.5 - span / 2, 0.5 + span / 2), (0.0, 1.0)
    else:
        span = image_aspect / frame_aspect
        us, vs = (0.0, 1.0), (0.5 - span / 2, 0.5 + span / 2)
    corners = ((us[0], vs[0]), (us[1], vs[0]), (us[1], vs[1]), (us[0], vs[1]))
    for loop, uv in zip(obj.data.uv_layers[0].data, corners):
        loop.uv = uv


def key(target, path, frame, value, index=-1):
    if index < 0:
        setattr(target, path, value)
    else:
        getattr(target, path)[index] = value
    target.keyframe_insert(path, index=index, frame=frame)


def _fcurves(block):
    action = getattr(getattr(block, "animation_data", None), "action", None)
    if action is None:
        return []
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    curves = []  # layered actions (Blender 4.4 and later)
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                curves.extend(bag.fcurves)
    return curves


def ease_all(blocks):
    """Bezier keys with clamped automatic handles: every move eases in and settles."""
    for block in blocks:
        for curve in _fcurves(block):
            for point in curve.keyframe_points:
                point.interpolation = "BEZIER"
                point.handle_left_type = point.handle_right_type = "AUTO_CLAMPED"


CAMERA_PATHS = {
    # (start, end) as (x, y, z) in units of the camera distance, looking at the words.
    # Every end sits a little above and to one side, so the bevel and the depth read.
    "dolly_in": ((0.06, -1.30, 0.22), (0.12, -1.0, 0.15)),
    "orbit": ((-0.36, -0.93, 0.14), (0.16, -0.99, 0.08)),
    "rise": ((0.04, -1.05, -0.26), (0.06, -1.0, 0.12)),
    "push_tilt": ((0.20, -1.22, 0.30), (-0.06, -1.0, 0.04)),
}


def camera_rig(spec, target, focus, last):
    data = bpy.data.cameras.new("camera")
    data.lens = FOCAL
    data.sensor_fit = "AUTO"
    data.sensor_width = 36.0
    data.clip_start = 0.1
    data.clip_end = 200.0
    data.dof.use_dof = True
    data.dof.focus_object = focus
    data.dof.aperture_fstop = 1.8
    data.dof.aperture_blades = 7
    camera = bpy.data.objects.new("camera", data)
    bpy.context.scene.collection.objects.link(camera)
    bpy.context.scene.camera = camera
    track = camera.constraints.new("TRACK_TO")
    track.target = target
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    gentle = 0.4 if spec["kind"] == "lower_third" else 1.0
    start, end = CAMERA_PATHS[spec["camera"]]
    base = Vector(target.location)
    begin = Vector(end).lerp(Vector(start), gentle)
    key(camera, "location", 1, base + begin * DISTANCE)
    key(camera, "location", last, base + Vector(end) * DISTANCE)
    return camera


def animate_text(root, subtitle, spec, last, fps):
    """The title tips up into place and settles; the subtitle follows a beat later."""
    settle = max(3, int(round(min(1.2, spec["duration"] * 0.5) * fps)))
    tip = math.radians(-24 if spec["camera"] != "rise" else -10)
    key(root, "rotation_euler", 1, tip, index=0)
    key(root, "rotation_euler", settle, 0.0, index=0)
    key(root, "scale", 1, Vector((0.86, 0.86, 0.86)))
    key(root, "scale", settle, Vector((1.0, 1.0, 1.0)))
    key(root, "scale", last, Vector((1.03, 1.03, 1.03)))
    if subtitle is not None:
        lag = max(2, int(round(0.25 * fps)))
        rest = subtitle.location.z
        drop = subtitle.data.size * 1.6
        key(subtitle, "location", 1, rest - drop, index=2)
        key(subtitle, "location", 1 + lag, rest - drop, index=2)
        key(subtitle, "location", min(last, settle + lag), rest, index=2)
        key(subtitle, "scale", 1 + lag, Vector((0.001, 0.001, 0.001)))
        key(subtitle, "scale", min(last, settle + lag), Vector((1.0, 1.0, 1.0)))
        ease_all([subtitle])
    ease_all([root])
    return settle


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(argv) != 2:
        raise SystemExit("usage: blender -b --factory-startup --python motion_card.py -- spec.json out_dir")
    spec_path, out_dir = argv
    spec = read_spec(spec_path)
    os.makedirs(out_dir, exist_ok=True)
    clear_scene()
    scene = bpy.context.scene
    setup_render(scene, spec, out_dir)
    world(scene, spec)
    root, titles, subtitle, layout = build_text(spec)
    focus = bpy.data.objects.new("focus", None)
    scene.collection.objects.link(focus)
    focus.location = (root.location.x + (layout["width"] / 2 if spec["kind"] == "lower_third" else 0.0), 0.0, root.location.z)
    # A lower third keeps its place in the frame: the camera aims at the frame's centre, not at the words.
    aim = focus
    if spec["kind"] == "lower_third":
        aim = bpy.data.objects.new("aim", None)
        scene.collection.objects.link(aim)
    last = scene.frame_end
    fps = spec["fps"]
    camera = camera_rig(spec, aim, focus, last)
    lights(spec, focus)
    _camera_plane("scrim", camera, spec, DISTANCE * 2.2, scrim_material(spec))
    backdrop = None
    if spec["backdrop"] and spec["kind"] != "lower_third":
        material = backdrop_material(spec["backdrop"])
        backdrop = _camera_plane("backdrop", camera, spec, DISTANCE * 2.4, material)
        _cover_uvs(backdrop, tuple(material.node_tree.nodes["plate"].image.size), (spec["width"], spec["height"]))
        # Opaque, then it hands over to the footage while the words stay.
        socket = material.node_tree.nodes["fade"].outputs[0]
        hand_over = int(round(last * 0.5))
        for frame, alpha in ((1, 1.0), (hand_over, 1.0), (min(last - 2, hand_over + int(round(0.6 * fps))), 0.0)):
            socket.default_value = alpha
            socket.keyframe_insert("default_value", frame=frame)
        ease_all([material.node_tree])
    settle = animate_text(root, subtitle, spec, last, fps)
    ease_all([camera])
    built = time.time()
    bpy.ops.render.render(animation=True)
    finished = time.time()
    frames = sorted(name for name in os.listdir(out_dir) if name.startswith("frame_") and name.endswith(".png"))
    manifest = {
        "blender": bpy.app.version_string,
        "engine": scene.render.engine,
        "lines": spec["lines"],
        "subtitle": spec["subtitle"],
        "title_body": bpy.data.objects["title"].data.body,
        "style": spec["style"],
        "camera": spec["camera"],
        "kind": spec["kind"],
        "size": [spec["width"], spec["height"]],
        "fps": fps,
        "frames": len(frames),
        "samples": spec["samples"],
        "backdrop": backdrop is not None,
        "settle_frame": settle,
        "setup_seconds": round(built - STARTED, 3),
        "render_seconds": round(finished - built, 3),
    }
    with open(os.path.join(out_dir, "card.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    print("MOTION_CARD_DONE " + json.dumps({"frames": len(frames), "render_seconds": manifest["render_seconds"]}))


main()
