from .base import AdapterSource, Detection, DetectionError
from .detect import detect_source
from .registry import SourceRegistry, default_registry
from .sources import (
    ClaudeCodeSource,
    CoworkSource,
    OpenClawSource,
    OpenInferenceSource,
    convert_claude_code,
    convert_cowork,
    convert_openclaw,
    convert_openinference,
)

__all__ = [
    "AdapterSource",
    "ClaudeCodeSource",
    "CoworkSource",
    "Detection",
    "DetectionError",
    "OpenClawSource",
    "OpenInferenceSource",
    "SourceRegistry",
    "convert_claude_code",
    "convert_cowork",
    "convert_openclaw",
    "convert_openinference",
    "default_registry",
    "detect_source",
]
