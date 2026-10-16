"""Records passed between pipeline stages.

Metric dataclasses are grouped by family, and their field order is the column
order in every report. `None` always means "no data", never zero: a README has
no cyclomatic complexity, and a file nobody has committed to has no owner.
Scoring treats the two differently (see scoring.py), so the distinction is
carried all the way through.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """Lines added and deleted in one file by one commit."""

    sha: str
    date: str  # author date as git printed it (YYYY-MM-DD); see parse_day
    author: str
    path: str
    additions: int
    deletions: int


def parse_day(value: str) -> date | None:
    """
    Parse a YYYY-MM-DD author date, or None if it is out of range.

    Commits with corrupt or skewed timestamps exist in the wild (a five-digit
    year, for one), and a single bad commit must not abort a scan.
    """
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ChurnMetrics:
    """How much a file changed inside the --since/--until window."""

    churn_absolute: int = 0
    churn_relative: float = 0.0
    commit_count: int = 0
    churn_recency_days: int | None = None
    churn_burstiness: float = 0.0


@dataclass(frozen=True, slots=True)
class ComplexityMetrics:
    """Structure of a source file at HEAD, as measured by lizard."""

    cyclomatic_complexity: int
    nesting_depth: int
    function_count: int
    avg_function_length: float
    comment_density: float


@dataclass(frozen=True, slots=True)
class ActivityMetrics:
    """Who has worked on a file, over its full history."""

    distinct_authors: int = 0
    ownership_concentration: float | None = None
    bus_factor_flag: bool = False
    days_since_any_commit: int | None = None


def _field_names(cls: type) -> tuple[str, ...]:
    return tuple(f.name for f in fields(cls))


# Raw metric names per family, in report column order. The keys double as
# the attribute names on FileMetrics and the family names in scoring.FAMILIES.
METRIC_GROUPS: dict[str, tuple[str, ...]] = {
    "churn": _field_names(ChurnMetrics),
    "complexity": _field_names(ComplexityMetrics),
    "activity": _field_names(ActivityMetrics),
}
METRIC_NAMES: tuple[str, ...] = (
    "loc",
    *(name for group in METRIC_GROUPS.values() for name in group),
)


@dataclass(frozen=True, slots=True)
class FileMetrics:
    """Every raw metric for one file."""

    path: str
    loc: int
    churn: ChurnMetrics
    complexity: ComplexityMetrics | None  # None: not source code lizard can parse
    activity: ActivityMetrics

    def values(self) -> dict[str, object]:
        """All raw metrics by name, in METRIC_NAMES order, with None where there is no data."""
        out: dict[str, object] = {"loc": self.loc}
        for family, names in METRIC_GROUPS.items():
            record = getattr(self, family)
            out.update((name, None if record is None else getattr(record, name)) for name in names)
        return out

    @classmethod
    def from_values(cls, path: str, values: Mapping[str, Any]) -> FileMetrics:
        """Rebuild from the output of `values()`, such as a row of a saved JSON report."""

        def pick(family: str) -> dict[str, Any]:
            return {name: values[name] for name in METRIC_GROUPS[family]}

        complexity = pick("complexity")
        return cls(
            path=path,
            loc=values["loc"],
            churn=ChurnMetrics(**pick("churn")),
            complexity=None
            if all(v is None for v in complexity.values())
            else ComplexityMetrics(**complexity),
            activity=ActivityMetrics(**pick("activity")),
        )
