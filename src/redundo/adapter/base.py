"""The contract every adapter source implements -- built-in or third-party.

`AdapterSource.detect()` never raises: "I don't recognize this" is a valid
answer for any source, expressed as `None`, not an exception. Detection
failure (nothing recognized it) is the registry's problem, not any one
source's. `AdapterSource.convert()` takes the full, unfiltered document
list every time -- each source is responsible for picking out whichever
subset (trace docs, log docs, both) it actually needs; see each source's
own `detect`/`convert` for which.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class DetectionError(ValueError):
    pass


@dataclass
class Detection:
    source: str
    reason: str


class AdapterSource(ABC):
    name: str

    @abstractmethod
    def detect(self, documents: list[dict[str, Any]]) -> Detection | None:
        """Return a Detection if this source recognizes the data, else None."""

    @abstractmethod
    def convert(self, documents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
        """documents -> (schema-conformant records, this source's own summary)."""
