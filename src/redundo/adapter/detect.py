"""Backward-compatible entry point: `detect_source()` used to hold all of
this project's per-source detection logic directly. That logic now lives
on each source's own `detect()` method (see `sources/*.py`), with the one
piece that genuinely can't be a single source's own responsibility --
Cowork's residual, logs-only fallback -- in `registry.py`'s
`SourceRegistry.detect()`. This module just re-exports `Detection`/
`DetectionError` from `base.py` and forwards to the default registry, so
existing callers (`from redundo.adapter.detect import detect_source`)
keep working unchanged.
"""

from __future__ import annotations

from typing import Any

from .base import Detection, DetectionError
from .registry import default_registry

__all__ = ["Detection", "DetectionError", "detect_source"]


def detect_source(documents: list[dict[str, Any]]) -> Detection:
    return default_registry.detect(documents)
