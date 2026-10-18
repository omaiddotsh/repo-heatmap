"""Developer activity: who has changed each file, and how concentrated its ownership is."""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from ..git import GitRepo
from ..models import ActivityMetrics, ChangeEvent, parse_day

# Blaming a file this large costs more than its ownership figure is worth;
# such files, and files over complexity.MAX_BYTES (`too_big`), fall back to
# the top author's share of commits.
BLAME_LOC_LIMIT = 5000

# Workers only wait on git subprocesses, and with a commit-graph each blame
# spends much of its time on I/O rather than CPU, so oversubscribing cores
# pays: 32 threads beat 16 by about 17% on a 16-core machine.
BLAME_WORKERS = min(32, (os.cpu_count() or 4) * 2)


def collect(
    events: Iterable[ChangeEvent],
    repo: GitRepo,
    locs: Mapping[str, int],
    *,
    today: date,
    workers: int = BLAME_WORKERS,
    blame_loc_limit: int = BLAME_LOC_LIMIT,
    blame: bool = True,
    too_big: Collection[str] = frozenset(),
) -> dict[str, ActivityMetrics]:
    """
    Activity metrics for every file with at least one event.

    Pass full-history events, not a --since window: a file that one person
    wrote years ago and nobody has touched since is single-author, and a
    windowed view would hide that.

    With `blame=False`, `git blame` is skipped and ownership_concentration
    is left as no data rather than swapped for a different measure.
    """
    commits_by_author: defaultdict[str, Counter[str]] = defaultdict(Counter)
    days: defaultdict[str, set[str]] = defaultdict(set)
    for ev in events:
        commits_by_author[ev.path][ev.author] += 1
        days[ev.path].add(ev.date)

    def top_blame_share(path: str) -> float | None:
        if path in too_big or not 0 < locs.get(path, 0) <= blame_loc_limit:
            return None
        lines = repo.blame_authors(path, locs[path])  # checked against the line count
        if not lines:
            return None
        return 100.0 * lines.most_common(1)[0][1] / lines.total()

    # One `git blame` per file dominates runtime on a large repository. The
    # calls are independent and spend their time inside git, so a thread
    # pool scales nearly linearly.
    paths = list(commits_by_author)
    if blame:
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            blame_shares = list(pool.map(top_blame_share, paths))
        finally:
            # On an error or Ctrl-C, drop the queued blames instead of running them all.
            pool.shutdown(cancel_futures=True)
    else:
        blame_shares = [None] * len(paths)

    out: dict[str, ActivityMetrics] = {}
    for path, share in zip(paths, blame_shares, strict=True):
        authors = commits_by_author[path]
        if share is None and blame:
            share = 100.0 * authors.most_common(1)[0][1] / authors.total()
        last = max((d for d in map(parse_day, days[path]) if d is not None), default=None)
        out[path] = ActivityMetrics(
            distinct_authors=len(authors),
            ownership_concentration=share,
            bus_factor_flag=len(authors) == 1,
            days_since_any_commit=max(0, (today - last).days) if last else None,
        )
    return out
