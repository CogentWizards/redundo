"""The contract every adapter source implements -- built-in or third-party.

`AdapterSource.detect()` never raises: "I don't recognize this" is a valid
answer for any source, expressed as `None`, not an exception. Detection
failure (nothing recognized it) is the registry's problem, not any one
source's. `AdapterSource.convert()` takes the full, unfiltered document
list every time -- each source is responsible for picking out whichever
subset (trace docs, log docs, both) it actually needs; see each source's
own `detect`/`convert` for which.

Most sources read OTLP, which `redundo adapt` loads for them. A source
whose agent never emits OTLP (Pi writes its own session JSONL instead)
overrides `load()` to read its native capture format into the same
list-of-dicts shape, and `detect()`/`convert()` then work on that list
exactly as they would on OTLP documents. `load()` returning `None` means
"this input isn't mine", the same contract as `detect()`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DetectionError(ValueError):
    pass


class InputLoadError(ValueError):
    """A source recognized an input as its own native format, but couldn't
    read it (a malformed line, an unreadable file). Never carries raw
    content in its message, only a path and position.
    """


@dataclass
class Detection:
    source: str
    reason: str


class AdapterSource(ABC):
    name: str

    @abstractmethod
    def detect(self, documents: list[dict[str, Any]]) -> Detection | None:
        """Return a Detection if this source recognizes the data, else None."""

    def load(self, path: Path) -> list[dict[str, Any]] | None:
        """Read `path` (a file or directory) as this source's own native
        capture format. Returns None when `path` isn't in that format,
        which is the default for every OTLP-based source. Raises
        InputLoadError when it is, but can't be read.
        """
        return None

    @abstractmethod
    def convert(self, documents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
        """documents -> (schema-conformant records, this source's own summary)."""
