import pytest

from redundo.analyzer.analysis import Analysis, AnalysisResult
from redundo.analyzer.metrics import compute_generic_coverage
from redundo.analyzer.registry import AnalysisRegistry


class _StubAnalysis(Analysis):
    name = "stub"

    def run(self, events):
        return AnalysisResult(coverage=compute_generic_coverage(events))


class _StubAnalysisTwo(Analysis):
    name = "stub2"

    def run(self, events):
        return AnalysisResult(coverage=compute_generic_coverage(events))


def test_register_and_get():
    registry = AnalysisRegistry(discover=False)
    registry.register(_StubAnalysis)
    assert isinstance(registry.get("stub"), _StubAnalysis)
    assert registry.names() == ["stub"]


def test_duplicate_name_raises():
    registry = AnalysisRegistry(discover=False)
    registry.register(_StubAnalysis)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_StubAnalysis)


def test_unknown_name_raises_with_registered_list():
    registry = AnalysisRegistry(discover=False)
    registry.register(_StubAnalysis)
    with pytest.raises(ValueError, match=r"stub"):
        registry.get("nonexistent")


def test_get_passes_constructor_kwargs_through():
    registry = AnalysisRegistry(discover=False)
    registry.register(_StubAnalysisTwo)
    # Doesn't accept keep_reasons -- should raise TypeError, same as any
    # normal Python constructor call with an unexpected kwarg (the CLI
    # catches this specific error to fall back to no kwargs; see cli.py).
    with pytest.raises(TypeError):
        registry.get("stub2", keep_reasons=5)


def test_real_discovery_finds_the_built_in_waste_analysis():
    registry = AnalysisRegistry(discover=True)
    assert "waste" in registry.names()
