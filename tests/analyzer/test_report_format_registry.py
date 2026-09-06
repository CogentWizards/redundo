import pytest

from redundo.analyzer.metrics import compute_generic_coverage
from redundo.analyzer.analysis import AnalysisResult
from redundo.analyzer.report_formats import ReportFormatRegistry


def _stub_renderer(result: AnalysisResult, *, max_reasons: int = 20) -> str:
    return "stub output"


def test_register_and_get():
    registry = ReportFormatRegistry(discover=False)
    registry.register("stub", _stub_renderer)
    assert registry.get("stub") is _stub_renderer
    assert registry.names() == ["stub"]


def test_duplicate_name_raises():
    registry = ReportFormatRegistry(discover=False)
    registry.register("stub", _stub_renderer)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("stub", _stub_renderer)


def test_unknown_name_raises_with_registered_list():
    registry = ReportFormatRegistry(discover=False)
    registry.register("stub", _stub_renderer)
    with pytest.raises(ValueError, match=r"stub"):
        registry.get("nonexistent")


def test_real_discovery_finds_the_built_in_formats():
    registry = ReportFormatRegistry(discover=True)
    for name in ("text", "json", "html"):
        assert name in registry.names()


def test_registered_renderer_actually_renders():
    registry = ReportFormatRegistry(discover=True)
    render = registry.get("text")
    result = AnalysisResult(coverage=compute_generic_coverage([]))
    assert "Coverage" in render(result)
