"""The scan pipeline: list files, measure them, score them.

git ls-tree ──► filters ──► blobs at HEAD ──► LOC + complexity ──────┐
git rev-list + diff-tree (parallel) ──► history ─┬─► window ─► churn ─┼─► scoring
                         commit-graph + git blame ┴─► activity ───────┘
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from .collectors import activity, churn, complexity
from .filters import ExcludeRules
from .git import GitRepo, LargeBlob, TrackedFile
from .models import (
    ActivityMetrics,
    ChangeEvent,
    ChurnMetrics,
    ComplexityMetrics,
    FileMetrics,
)
from .scoring import DEFAULT_WEIGHTS, FileScore, normalize_weights, score

log = logging.getLogger(__package__)

# Building a commit-graph costs roughly what blaming a couple of hundred files
# without one costs, so it only pays off for scans at least that big. (On a
# 35,000-commit repository it took 2.5-3.3s to build and cut each blame from
# about 0.4s to 0.1s.)
GRAPH_MIN_FILES = 200


@dataclass(frozen=True)
class ScanOptions:
    """What to scan and how to weigh it."""

    since: str | None = None  # git date syntax, e.g. "180 days ago" or "2024-01-01"
    until: str | None = None
    exclude: Sequence[str] = ()  # extra globs on top of the built-in exclusions
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    today: date | None = None  # reference date for "days since"; defaults to today
    blame: bool = True  # False skips git blame; ownership_concentration is then no data
    include: Sequence[str] = ()  # globs re-admitting files the built-in exclusions drop

    def __post_init__(self) -> None:
        # A str is a Sequence[str] too, and would add one glob per character.
        for name in ("exclude", "include"):
            if isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a list of globs, not a single string")


@dataclass(frozen=True)
class ScanResult:
    root: Path  # repository root
    prefix: str | None  # subdirectory the scan was scoped to, if any
    options: ScanOptions  # as used, with weights normalised
    files: list[FileScore]  # highest risk first
    head: str | None = None  # commit scanned; None for a repository with no commits

    def find(self, path: str) -> FileScore | None:
        return next((f for f in self.files if f.path == path), None)


def scan(path: str | os.PathLike[str], options: ScanOptions | None = None) -> ScanResult:
    """
    Score every eligible file in the repository containing `path`.

    If `path` is a subdirectory, only files under it are scored, though
    reported paths stay relative to the repository root. Percentiles are
    relative to the files scored, so narrowing the scope changes them.
    """
    options = options or ScanOptions()
    # Normalising validates the weights, so bad input fails before any git work.
    options = replace(options, weights=normalize_weights(options.weights))
    repo = GitRepo.open(path)
    for flag, text in (("--since", options.since), ("--until", options.until)):
        if text:
            repo.check_date(flag, text)
    if repo.prefix:
        log.info("Scoping to %s/ in %s", repo.prefix, repo.root)
    metrics = collect_metrics(repo, options)
    return ScanResult(
        repo.root, repo.prefix, options, score(metrics, options.weights), head=repo.head
    )


def collect_metrics(repo: GitRepo, options: ScanOptions) -> list[FileMetrics]:
    rules = ExcludeRules(tuple(options.exclude), tuple(options.include))
    if not repo.has_commits:
        log.warning("%s has no commits yet, so there is nothing to score", repo.root)
        return []
    every = repo.tracked_files()
    tracked = [f for f in every if not rules.excludes(f.path)]
    if every and not tracked:
        log.warning(
            "all %d tracked files %s are left out by the exclusion rules (tests, "
            "documentation, manifests, ...); --include re-admits files a built-in rule drops",
            len(every),
            f"under {repo.prefix}/" if repo.prefix else "in the repository",
        )
    # History (git subprocesses) and the blame index (one git process) are
    # read in the background while this thread measures file content
    # (lizard, pure Python), so the three overlap. The executor is shut down,
    # waiting for both, before the stack removes the index, whichever way
    # the block exits.
    with ExitStack() as stack, ThreadPoolExecutor(max_workers=2) as background:
        index: Future[bool] | None = None
        if options.blame and len(tracked) >= GRAPH_MIN_FILES:
            log.info("Indexing history for git blame")
            index = background.submit(stack.enter_context, repo.commit_graph())
        log.info("Reading %d files and the commit history", len(tracked))
        events = background.submit(repo.change_events)
        return _collect(repo, options, tracked, events, index)


def _collect(
    repo: GitRepo,
    options: ScanOptions,
    tracked: Sequence[TrackedFile],
    events: Future[list[ChangeEvent]],
    index: Future[bool] | None,
) -> list[FileMetrics]:
    today = options.today or date.today()
    locs, measured, too_big = _measure_content(repo, tracked)

    history = [e for e in events.result() if e.path in locs]
    if options.since or options.until:
        in_window = repo.commits_between(since=options.since, until=options.until)
        window = [e for e in history if e.sha in in_window]
        if history and not window:
            log.warning("no commit in the --since/--until window touches the files being scored")
    else:
        window = history
    churn_by_path = churn.collect(window, locs, today=today)

    # Authorship always uses full history, whatever the churn window.
    if options.blame:
        if index is not None:
            index.result()
        to_blame = sum(
            1
            for p in {e.path for e in history}
            if 0 < locs[p] <= activity.BLAME_LOC_LIMIT and p not in too_big
        )
        log.info("Running git blame on %d files", to_blame)
        activity_by_path = activity.collect(history, repo, locs, today=today, too_big=too_big)
    else:
        activity_by_path = activity.collect(history, repo, locs, today=today, blame=False)

    return [
        FileMetrics(
            path=f.path,
            loc=locs[f.path],
            churn=churn_by_path.get(f.path, ChurnMetrics()),
            complexity=measured.get(f.path),
            activity=activity_by_path.get(f.path, ActivityMetrics()),
        )
        for f in tracked
    ]


def _measure_content(
    repo: GitRepo, tracked: Sequence[TrackedFile]
) -> tuple[dict[str, int], dict[str, ComplexityMetrics], set[str]]:
    """
    Read each distinct blob once and take everything that comes from content:
    line counts, complexity, and which files are over complexity.MAX_BYTES.

    Files with identical content share a blob, so each blob is read, counted,
    and measured once, however many paths it appears at (once per language,
    since lizard reads by file extension).
    """
    paths_by_oid: dict[str, list[str]] = {}
    for f in tracked:
        paths_by_oid.setdefault(f.oid, []).append(f.path)
    locs: dict[str, int] = {}
    measured: dict[str, ComplexityMetrics] = {}
    too_big: set[str] = set()
    blobs = repo.read_blobs(list(paths_by_oid), max_bytes=complexity.MAX_BYTES)
    with closing(blobs):
        for paths, blob in zip(paths_by_oid.values(), blobs, strict=True):
            if isinstance(blob, LargeBlob):
                for path in paths:
                    locs[path] = 0 if blob.binary else blob.lines
                too_big.update(paths)
                continue
            if blob is None or complexity.looks_binary(blob):
                for path in paths:
                    locs[path] = 0
                continue
            lines = complexity.count_lines(blob)
            by_reader: dict[type, ComplexityMetrics | None] = {}
            for path in paths:
                locs[path] = lines
                reader = complexity.reader_for(path)
                if reader is None:
                    continue
                if reader not in by_reader:
                    by_reader[reader] = complexity.measure(path, blob)
                if (m := by_reader[reader]) is not None:
                    measured[path] = m
    return locs, measured, too_big
