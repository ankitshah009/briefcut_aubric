"""Blender's math types exist only inside Blender, so pyright reads them as Any here."""

from typing import Any

def __getattr__(name: str) -> Any: ...
