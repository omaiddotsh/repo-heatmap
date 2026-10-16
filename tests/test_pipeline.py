"""repo_heatmap.pipeline: the blame index built in the background."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from repo_heatmap import pipeline
from repo_heatmap.git import GitRepo
from repo_heatmap.pipeline import ScanOptions, scan

OPTIONS = ScanOptions(today=date(2024, 6, 1))


@pytest.fixture
def history(repo):
    for i in range(6):
        repo.write(f"src/m{i}.py", "".join(f"v{j} = {j}\n" for j in range(i + 3)))
    repo.commit(author="Alice", day="2024-01-01")
    for i in range(0, 6, 2):
        repo.write(f"src/m{i}.py", "x = 1\n")
        repo.commit(author="Bob", day=f"2024-02-0{i + 1}")
    return repo


@pytest.fixture
def index_dirs(monkeypatch):
    """Record the temporary object directory of every blame index built."""
    dirs: list[Path] = []
    original = GitRepo._graph_env

    def spy(self, odb):
        dirs.append(Path(odb))
        return original(self, odb)

    monkeypatch.setattr(GitRepo, "_graph_env", spy)
    return dirs


def test_blame_index_does_not_change_results(history, monkeypatch, index_dirs) -> None:
    plain = scan(history.root, OPTIONS)
    assert index_dirs == []  # small scan: not worth an index
    monkeypatch.setattr(pipeline, "GRAPH_MIN_FILES", 0)
    indexed = scan(history.root, OPTIONS)
    assert len(index_dirs) == 1
    assert indexed.files == plain.files
    assert not index_dirs[0].exists()  # cleaned up


def test_blame_index_is_cleaned_up_when_a_scan_fails(history, monkeypatch, index_dirs) -> None:
    monkeypatch.setattr(pipeline, "GRAPH_MIN_FILES", 0)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "_measure_content", boom)
    with pytest.raises(RuntimeError, match="boom"):
        scan(history.root, OPTIONS)
    assert len(index_dirs) == 1
    assert not index_dirs[0].exists()


def test_no_blame_skips_the_index(history, monkeypatch, index_dirs) -> None:
    monkeypatch.setattr(pipeline, "GRAPH_MIN_FILES", 0)
    result = scan(history.root, ScanOptions(today=date(2024, 6, 1), blame=False))
    assert index_dirs == []
    assert all(f.metrics.activity.ownership_concentration is None for f in result.files)


def test_identical_files_are_measured_once(repo, monkeypatch) -> None:
    source = "def f(x):\n    if x:\n        return 1\n    return 2\n"
    for name in ("a.py", "b.py", "c.py", "d.js"):
        repo.write(name, source)
    repo.commit()
    calls = []
    real = pipeline.complexity.measure
    monkeypatch.setattr(pipeline.complexity, "measure", lambda p, c: calls.append(p) or real(p, c))
    result = scan(repo.root, OPTIONS)
    assert len(calls) == 2  # once for Python, once for JavaScript
    assert len({f.metrics.complexity for f in result.files if f.path.endswith(".py")}) == 1


def test_large_files_are_counted_but_not_measured(repo, monkeypatch) -> None:
    monkeypatch.setattr(pipeline.complexity, "MAX_BYTES", 30)
    repo.write("big.py", "x = 1\n" * 10)  # 60 bytes
    repo.write("small.py", "y = 2\n")
    repo.commit()
    result = scan(repo.root, OPTIONS)
    big, small = result.find("big.py"), result.find("small.py")
    assert big is not None and small is not None
    assert big.metrics.loc == 10 and big.metrics.complexity is None
    assert small.metrics.complexity is not None


def test_large_files_are_not_blamed(repo, monkeypatch) -> None:
    # Blame prints a file's text, so a huge one would be held in memory whole.
    monkeypatch.setattr(pipeline.complexity, "MAX_BYTES", 30)
    repo.write("big.py", "x = 1\n" * 10)  # 60 bytes
    repo.write("small.py", "y = 2\n")
    repo.commit()
    blamed = []
    real = GitRepo.blame_authors
    monkeypatch.setattr(
        GitRepo, "blame_authors", lambda self, p, n=None: blamed.append(p) or real(self, p, n)
    )
    scan(repo.root, OPTIONS)
    assert blamed == ["small.py"]


def test_globs_must_be_a_list_not_a_string() -> None:
    with pytest.raises(TypeError, match="exclude must be a list of globs"):
        ScanOptions(exclude="*.md")
    with pytest.raises(TypeError, match="include must be a list of globs"):
        ScanOptions(include="*.md")


def test_include_re_admits_files_in_a_library_scan(repo) -> None:
    repo.write("src/a.py")
    repo.write("README.md", "# a\n")
    repo.commit()
    assert [f.path for f in scan(repo.root, OPTIONS).files] == ["src/a.py"]
    included = scan(repo.root, ScanOptions(today=date(2024, 6, 1), include=("README.md",)))
    assert sorted(f.path for f in included.files) == ["README.md", "src/a.py"]
