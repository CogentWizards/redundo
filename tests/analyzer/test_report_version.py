"""_redundo_version() must show what's actually running, not stale
installed-package metadata. See report.py's own docstring on
_source_checkout_version for the real scenario this guards against:
`uv run --no-sync` after hand-editing pyproject.toml's version still
reported the old one via importlib.metadata alone.
"""

from pathlib import Path

from redundo.analyzer.report import _redundo_version, _source_checkout_version


def _write_pyproject(tmp_path: Path, *, name: str, version: str) -> Path:
    project_dir = tmp_path / "project"
    package_dir = project_dir / "src" / "redundo" / "analyzer"
    package_dir.mkdir(parents=True)
    (project_dir / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    return package_dir / "report.py"


def test_source_checkout_version_reads_the_real_repo_pyproject_toml():
    # No `start` override: this exercises the real default (this file's
    # own location), inside the actual redundo checkout the tests run
    # from -- confirms the lookup works end to end, not just against a
    # synthetic fixture.
    repo_pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    text = repo_pyproject.read_text()
    import re
    expected = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text).group(1)
    assert _source_checkout_version() == expected


def test_source_checkout_version_finds_a_synthetic_project(tmp_path):
    fake_report_py = _write_pyproject(tmp_path, name="redundo", version="9.9.9")
    assert _source_checkout_version(start=fake_report_py) == "9.9.9"


def test_source_checkout_version_ignores_an_unrelated_projects_pyproject_toml(tmp_path):
    # redundo installed as a dependency inside some other project's tree:
    # walking up from an installed copy could reach that other project's
    # own pyproject.toml. It must never be mistaken for redundo's own.
    fake_report_py = _write_pyproject(tmp_path, name="someone-elses-app", version="1.0.0")
    assert _source_checkout_version(start=fake_report_py) is None


def test_source_checkout_version_none_when_no_pyproject_toml_is_findable(tmp_path):
    isolated = tmp_path / "no-project-here" / "report.py"
    isolated.parent.mkdir(parents=True)
    assert _source_checkout_version(start=isolated) is None


def test_redundo_version_prefers_source_checkout_over_installed_metadata(monkeypatch):
    # The real bug this guards against: installed package metadata is
    # frozen at install/sync time, so it can lag behind a locally-edited
    # pyproject.toml. Force the two functions to disagree and confirm
    # _redundo_version() picks the source checkout, not installed
    # metadata -- a direct test of precedence, not just "they happen to
    # agree in this repo right now."
    monkeypatch.setattr("redundo.analyzer.report._source_checkout_version", lambda: "9.9.9")
    monkeypatch.setattr("redundo.analyzer.report._installed_version", lambda: "0.0.1")
    assert _redundo_version() == "9.9.9"


def test_redundo_version_falls_back_to_installed_metadata_with_no_source_checkout(monkeypatch):
    monkeypatch.setattr("redundo.analyzer.report._source_checkout_version", lambda: None)
    monkeypatch.setattr("redundo.analyzer.report._installed_version", lambda: "0.0.1")
    assert _redundo_version() == "0.0.1"
