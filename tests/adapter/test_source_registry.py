import pytest
from helpers import span, traces_document

from redundo.adapter.base import AdapterSource, Detection, DetectionError
from redundo.adapter.registry import SourceRegistry


class _StubSource(AdapterSource):
    name = "stub"

    def __init__(self, hit=None):
        self._hit = hit

    def detect(self, documents):
        return self._hit

    def convert(self, documents):
        return [], None


def test_register_and_get():
    registry = SourceRegistry(discover=False)
    stub = _StubSource()
    registry.register(stub)
    assert registry.get("stub") is stub
    assert registry.names() == ["stub"]


def test_duplicate_name_raises():
    registry = SourceRegistry(discover=False)
    registry.register(_StubSource())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_StubSource())


def test_unknown_name_raises_with_registered_list():
    registry = SourceRegistry(discover=False)
    registry.register(_StubSource())
    with pytest.raises(ValueError, match=r"stub"):
        registry.get("nonexistent")


def test_detect_returns_first_registered_hit():
    registry = SourceRegistry(discover=False)
    registry.register(_StubSource(hit=None))
    hit = Detection("stub", "matched")
    stub2 = _StubSource(hit=hit)
    stub2.name = "stub2"
    registry.register(stub2)
    assert registry.detect([{}]) is hit


def test_detect_raises_when_nothing_recognizes_the_corpus():
    registry = SourceRegistry(discover=False)
    registry.register(_StubSource(hit=None))
    with pytest.raises(DetectionError, match="--source"):
        registry.detect([{"resourceSpans": []}])


# --- real entry-point discovery -------------------------------------------

def test_real_discovery_finds_the_built_in_sources():
    registry = SourceRegistry(discover=True)
    for name in ("claude-code", "cowork", "openclaw", "openinference"):
        assert name in registry.names()


def test_real_discovery_finds_the_installed_dummy_plugin():
    # tests/fixtures/dummy_redundo_plugin is installed editable as a dev
    # dependency specifically to prove entry-point discovery finds a
    # genuinely separate, independently-installed package -- not just
    # something importable from within this same package.
    registry = SourceRegistry(discover=True)
    assert "dummy" in registry.names()


def test_dummy_plugin_detects_and_converts_its_own_marker_span():
    registry = SourceRegistry(discover=True)
    doc = traces_document([span("s1", name="dummy.marker", start=0)])
    detection = registry.detect([doc])
    assert detection.source == "dummy"

    source = registry.get("dummy")
    records, summary = source.convert([doc])
    assert len(records) == 1
    assert records[0]["name"] == "dummy"
    assert "test fixture" in summary.notes()[0]
