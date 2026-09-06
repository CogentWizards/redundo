"""Discover and instantiate analyses -- built-in or third-party, through
the identical `redundo.analyzer.analyses` entry-point group. Stores
*classes*, not instances (unlike the adapter side's `SourceRegistry`,
which stores instances) -- an analysis can take constructor arguments
(`WasteAnalysis(keep_reasons=...)`) that only make sense to supply per
run, once the CLI knows `--samples`. `AdapterSource` has no comparable
per-run argument, so that registry doesn't need this.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from .analysis import Analysis

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "redundo.analyzer.analyses"


class AnalysisRegistry:
    def __init__(self, *, discover: bool = True) -> None:
        self._factories: dict[str, type[Analysis]] = {}
        if discover:
            self.discover_entry_points()

    def register(self, analysis_cls: type[Analysis]) -> None:
        name = analysis_cls.name
        if name in self._factories:
            raise ValueError(f"analysis {name!r} is already registered")
        self._factories[name] = analysis_cls

    def get(self, name: str, **kwargs) -> Analysis:
        try:
            cls = self._factories[name]
        except KeyError:
            raise ValueError(f"unknown analysis {name!r} -- registered: {self.names()}") from None
        return cls(**kwargs)

    def names(self) -> list[str]:
        return sorted(self._factories)

    def discover_entry_points(self) -> None:
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                self.register(ep.load())
            except Exception:
                logger.exception("failed to load analysis plugin %r (%s)", ep.name, ep.value)


default_registry = AnalysisRegistry()
