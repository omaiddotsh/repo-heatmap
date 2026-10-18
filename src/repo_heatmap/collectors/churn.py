"""Churn: how much, how often, and how recently each file changed in the window."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from itertools import pairwise

from ..models import ChangeEvent, ChurnMetrics, parse_day


def collect(
    events: Iterable[ChangeEvent], locs: Mapping[str, int], *, today: date
) -> dict[str, ChurnMetrics]:
    """
    Churn metrics for every file with at least one event.

    `locs` is each file's current line count, used to size-normalise churn.
    """
    commits: defaultdict[str, set[str]] = defaultdict(set)
    days: defaultdict[str, set[str]] = defaultdict(set)
    lines_changed: Counter[str] = Counter()
    for ev in events:
        commits[ev.path].add(ev.sha)
        days[ev.path].add(ev.date)
        lines_changed[ev.path] += ev.additions + ev.deletions

    out: dict[str, ChurnMetrics] = {}
    for path, shas in commits.items():
        active_days = sorted(d for d in map(parse_day, days[path]) if d is not None)
        churn_absolute = lines_changed[path]
        out[path] = ChurnMetrics(
            churn_absolute=churn_absolute,
            churn_relative=churn_absolute / max(locs.get(path, 0), 1),
            commit_count=len(shas),
            churn_recency_days=max(0, (today - active_days[-1]).days) if active_days else None,
            churn_burstiness=burstiness(active_days),
        )
    return out


def burstiness(days: Sequence[date]) -> float:
    """
    Coefficient of variation of the gaps between distinct commit days.

    High means sporadic bursts of edits, low means steady maintenance. Needs
    at least three days (two gaps) to mean anything; 0.0 otherwise.
    """
    if len(days) < 3:
        return 0.0
    gaps = [(later - earlier).days for earlier, later in pairwise(days)]
    mean_gap = statistics.mean(gaps)
    return statistics.stdev(gaps) / mean_gap if mean_gap else 0.0
