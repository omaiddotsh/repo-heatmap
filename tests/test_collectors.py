"""repo_heatmap.collectors: churn, complexity, and activity metrics."""

from __future__ import annotations

import signal
import time
from datetime import date

import pytest

from repo_heatmap.collectors import activity, churn, complexity
from repo_heatmap.git import GitRepo
from repo_heatmap.models import ChangeEvent

TODAY = date(2024, 1, 10)


def ev(
    sha: str, day: str, author: str = "alice", path: str = "f.py", added: int = 1, deleted: int = 0
) -> ChangeEvent:
    return ChangeEvent(sha, day, author, path, added, deleted)


# --- churn ------------------------------------------------------------------------


def test_churn_counts_commits_lines_and_recency() -> None:
    events = [ev("a", "2024-01-01", added=10, deleted=2), ev("b", "2024-01-02", added=3)]
    m = churn.collect(events, {"f.py": 10}, today=TODAY)["f.py"]
    assert m.commit_count == 2
    assert m.churn_absolute == 15
    assert m.churn_relative == 1.5
    assert m.churn_recency_days == 8
    assert m.churn_burstiness == 0.0  # needs three distinct days


def test_churn_relative_survives_an_empty_file() -> None:
    m = churn.collect([ev("a", "2024-01-01", added=4)], {"f.py": 0}, today=TODAY)["f.py"]
    assert m.churn_relative == 4.0


def test_burstiness_is_coefficient_of_variation_of_gaps() -> None:
    steady = [date(2024, 1, d) for d in (1, 3, 5, 7)]
    bursty = [date(2024, 1, d) for d in (1, 2, 3, 30)]
    assert churn.burstiness(steady) == 0.0
    assert churn.burstiness(bursty) > 1.0
    assert churn.burstiness(steady[:2]) == 0.0


def test_churn_keeps_commits_with_corrupt_dates() -> None:
    m = churn.collect([ev("a", "33658-09-27")], {"f.py": 10}, today=TODAY)["f.py"]
    assert m.commit_count == 1
    assert m.churn_recency_days is None


# --- complexity -------------------------------------------------------------------


def test_complexity_measures_source() -> None:
    source = (
        b"# helper\ndef f(x):\n    if x:\n        for i in x:\n            pass\n    return 1\n"
    )
    m = complexity.measure("app.py", source)
    assert m is not None
    assert m.function_count == 1
    assert m.cyclomatic_complexity == 3
    assert m.nesting_depth >= 1
    assert m.comment_density == pytest.approx(1 / 6)


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("notes.md", b"# heading\n\n* one\n* two\n"),
        ("config.yaml", b"key: value\n"),
        ("app.py", b"\x00\x01\x02binary"),
    ],
)
def test_complexity_skips_what_lizard_cannot_read(path: str, content: bytes) -> None:
    assert complexity.measure(path, content) is None


def test_complexity_handles_bom_and_crlf() -> None:
    plain = complexity.measure("a.py", b"def f():\n    return 1\n")
    awkward = complexity.measure("a.py", b"\xef\xbb\xbfdef f():\r\n    return 1\r\n")
    assert plain == awkward


@pytest.mark.parametrize(
    ("content", "lines"),
    [(b"", 0), (b"a", 1), (b"a\n", 1), (b"a\nb", 2), (b"a\nb\n", 2), (b"\n\n", 2)],
)
def test_count_lines(content: bytes, lines: int) -> None:
    assert complexity.count_lines(content) == lines


# --- activity ---------------------------------------------------------------------


def test_activity_single_author_sets_bus_factor(tmp_path) -> None:
    events = [ev("a", "2024-01-01"), ev("b", "2024-01-02")]
    m = activity.collect(events, GitRepo(tmp_path), {"f.py": 0}, today=TODAY)["f.py"]
    assert m.distinct_authors == 1
    assert m.bus_factor_flag is True
    assert m.days_since_any_commit == 8
    assert m.ownership_concentration == 100.0  # commit-share fallback


def test_activity_corrupt_date_still_counts_the_author(tmp_path) -> None:
    m = activity.collect([ev("a", "33658-09-27")], GitRepo(tmp_path), {}, today=TODAY)["f.py"]
    assert m.distinct_authors == 1
    assert m.days_since_any_commit is None


def test_activity_uses_blame_for_ownership(repo) -> None:
    repo.write("f.py", "a\nb\nc\nd\n")
    repo.commit(author="Alice")
    repo.write("f.py", "a\nB\nc\nd\n")
    repo.commit(author="Bob")
    opened = GitRepo.open(repo.root)
    m = activity.collect(opened.change_events(), opened, {"f.py": 4}, today=TODAY)["f.py"]
    assert m.distinct_authors == 2
    assert m.ownership_concentration == 75.0  # blame; commit share would say 50


def test_activity_falls_back_to_commit_share_above_blame_limit(repo) -> None:
    repo.write("f.py", "a\n")
    for i, author in enumerate(["Alice", "Alice", "Alice", "Bob"]):
        repo.write("f.py", f"{i}\n")
        repo.commit(author=author)
    opened = GitRepo.open(repo.root)
    m = activity.collect(
        opened.change_events(), opened, {"f.py": 1}, today=TODAY, blame_loc_limit=0
    )["f.py"]
    assert m.ownership_concentration == 75.0  # 3 of 4 commits, though Bob owns every line


def test_activity_without_blame_leaves_ownership_empty(repo) -> None:
    repo.write("f.py", "a\nb\n")
    repo.commit(author="Alice")
    opened = GitRepo.open(repo.root)
    m = activity.collect(opened.change_events(), opened, {"f.py": 2}, today=TODAY, blame=False)
    assert m["f.py"].ownership_concentration is None  # no data, not a substitute measure
    assert m["f.py"].distinct_authors == 1


def test_activity_parallel_blame_keeps_results_aligned(repo) -> None:
    # Bob rewrites only the even files, so each path has a distinct, known
    # owner; a thread pool that mixed up results would be caught.
    for i in range(12):
        repo.write(f"f{i}.py", f"a{i}\nb{i}\nc{i}\nd{i}\n")
    repo.commit(author="Alice")
    for i in range(0, 12, 2):
        repo.write(f"f{i}.py", f"w{i}\nx{i}\ny{i}\nz{i}\n")
    repo.commit(author="Bob")
    opened = GitRepo.open(repo.root)
    locs = {f"f{i}.py": 4 for i in range(12)}
    metrics = activity.collect(opened.change_events(), opened, locs, today=TODAY, workers=4)
    for i in range(12):
        m = metrics[f"f{i}.py"]
        assert m.ownership_concentration == 100.0
        assert m.distinct_authors == (2 if i % 2 == 0 else 1)


def test_a_lizard_crash_scores_the_file_as_neutral(caplog) -> None:
    # This 21-byte Solidity file crashes lizard's parser outright.
    assert complexity.measure("token.sol", b"}function(function\ne<") is None
    assert "can't measure token.sol" in caplog.text


def test_lizard_giving_up_scores_the_file_as_neutral(capfd, caplog) -> None:
    # lizard catches its own RecursionError, prints "[skip]" to stderr, and
    # returns partial numbers. Neither may reach the user.
    deep = b"function f() " + b"{" * 3000 + b"}" * 3000
    assert complexity.measure("evil\x1b[2J.js", deep) is None
    assert capfd.readouterr().err == ""
    assert "can't measure" in caplog.text


def test_complexity_skips_files_over_the_size_limit() -> None:
    big = b"def f():\n    return 1\n" * (complexity.MAX_BYTES // 20)
    assert len(big) > complexity.MAX_BYTES
    assert complexity.measure("big.py", big) is None


def test_lizard_running_too_long_scores_the_file_as_neutral(monkeypatch, caplog) -> None:
    # Unterminated comment openers make lizard's tokenizer quadratic: this
    # takes several seconds unchecked.
    monkeypatch.setattr(complexity, "MAX_SECONDS", 0.05)
    before = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    assert complexity.measure("slow.c", b"/* " * 30_000) is None
    assert time.monotonic() - started < 2
    assert "gave up measuring slow.c" in caplog.text
    assert signal.getsignal(signal.SIGALRM) == before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_time_limit_expiring_as_lizard_finishes_is_harmless(monkeypatch) -> None:
    # Deadlines around how long this takes, so the timer often runs out just
    # as the work ends. A signal arriving after the block must neither end
    # the process (SIGALRM's default action) nor raise outside measure().
    source = b"int f(int x) { if (x) { return 1; } return 0; }\n" * 20
    before = signal.getsignal(signal.SIGALRM)
    for i in range(120):
        monkeypatch.setattr(complexity, "MAX_SECONDS", 0.0001 + (i % 30) * 0.0001)
        complexity.measure("a.c", source)  # metrics, or None if time ran out
    assert signal.getsignal(signal.SIGALRM) == before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_comment_density_counts_lines_like_loc() -> None:
    # A form feed is a line separator for str.splitlines but not for `loc`.
    src = "int x;\n// a comment\n\x0c\nint y;\nint z;\nint w;\nint v;\nint u;\n"
    assert complexity.count_lines(src.encode()) == 8
    assert complexity.comment_density(src) == 1 / 8
    assert complexity.comment_density("") == 0.0
    assert complexity.comment_density("# only\n") == 1.0
    assert complexity.comment_density("# no trailing newline") == 1.0


def test_recency_is_never_negative_for_future_dated_commits() -> None:
    today = date(2024, 6, 1)
    out = churn.collect(
        [ev("1", "2031-01-01", path="src/a.py"), ev("2", "2024-05-01", path="src/a.py")],
        {"src/a.py": 10},
        today=today,
    )
    assert out["src/a.py"].churn_recency_days == 0
    out2 = churn.collect([ev("1", "2024-05-01", path="src/a.py")], {"src/a.py": 10}, today=today)
    assert out2["src/a.py"].churn_recency_days == 31


def test_days_since_any_commit_is_never_negative(tmp_path) -> None:
    events = [ev("a", "2031-01-01"), ev("b", "2024-01-02")]
    m = activity.collect(events, GitRepo(tmp_path), {"f.py": 0}, today=TODAY)["f.py"]
    assert m.days_since_any_commit == 0


def test_a_file_name_with_a_newline_is_still_measured() -> None:
    src = b"def f(x):\n    if x:\n        return 1\n    return 0\n"
    assert complexity.reader_for("src/new\nline.py") is not None
    assert complexity.reader_for("new\rline.py") is not None
    assert complexity.reader_for("src/plain.unknownext") is None
    measured = complexity.measure("src/new\nline.py", src)
    assert measured is not None and measured.function_count == 1
