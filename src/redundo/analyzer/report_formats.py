"""Discover report renderers -- built-in or third-party, through the
identical `redundo.analyzer.report_formats` entry-point group.

Deliberately a registry of plain callables
(`Callable[[AnalysisResult], str]`), not a class hierarchy like
`AdapterSource`/`Analysis`. `to_text`/`to_json`/`to_html` share no
behavior worth hoisting into a base class -- the whole contract is "takes
an AnalysisResult, returns a string" -- so a Strategy expressed as
functions is the honest fit here, not ceremony for its own sake.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Callable

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "redundo.analyzer.report_formats"

RendererFn = Callable[..., str]


class ReportFormatRegistry:
    def __init__(self, *, discover: bool = True) -> None:
        self._renderers: dict[str, RendererFn] = {}
        if discover:
            self.discover_entry_points()

    def register(self, name: str, renderer: RendererFn) -> None:
        if name in self._renderers:
            raise ValueError(f"report format {name!r} is already registered")
        self._renderers[name] = renderer

    def get(self, name: str) -> RendererFn:
        try:
            return self._renderers[name]
        except KeyError:
            raise ValueError(
                f"unknown report format {name!r} -- registered: {self.names()}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._renderers)

    def discover_entry_points(self) -> None:
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                self.register(ep.name, ep.load())
            except Exception:
                logger.exception(
                    "failed to load report format plugin %r (%s)", ep.name, ep.value
                )


default_registry = ReportFormatRegistry()
