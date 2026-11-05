"""Blender's Python API exists only inside Blender, so pyright reads it as Any here.

The one module that imports it, ``vedit/assets/blender/motion_card.py``, is a
template Blender runs; its tests compile it without ``bpy``.
"""

from typing import Any

def __getattr__(name: str) -> Any: ...
