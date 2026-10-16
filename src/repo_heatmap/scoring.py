"""Turn raw metrics into percentile ranks, family scores, and one risk score.

The model is the FAMILIES table below. Each scored metric is percentile-ranked
across the repository (0-100), flipped where a *lower* value is the risky one,
and combined into its family score by a weighted mean. The family scores are
then combined by the user's family weights into `risk_score`.

Percentile rank, not z-score: churn and complexity are heavily right-skewed,
and a handful of hotspots would otherwise dominate everything else.

A metric with no data for a file (None) is left out of that metric's
percentile pool and scores a neutral 50. Folding it in as 0 would invent a
false extreme for the file and drag every real file's percentile around with
a mass of tied zeros.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .errors import HeatmapError
from .models import FileMetrics

NEUTRAL = 50.0


@dataclass(frozen=True)
class Component:
    """One scored metric within a family."""

    metric: str
    weight: float = 1.0
    invert: bool = False  # a lower raw value means more risk
    flag: bool = False  # boolean metric: 100 when true, 0 when false, not ranked


FAMILIES: dict[str, tuple[Component, ...]] = {
    # See docs/methodology.md, "Why these defaults". The 0.05s keep a foothold
    # for repositories where those metrics matter more.
    "churn": (
        Component("commit_count", 0.90),
        Component("churn_relative", 0.05),
        Component("churn_burstiness", 0.05),
    ),
    "complexity": (
        Component("cyclomatic_complexity"),
        Component("nesting_depth"),
        Component("comment_density", invert=True),
    ),
    # Both halves read as risk for opposite reasons: diffuse ownership means
    # nobody owns the file, a bus factor of one means only one person does.
    "activity": (
        Component("ownership_concentration", invert=True),
        Component("bus_factor_flag", flag=True),
    ),
}

# A starting point, not a universal answer: retune when you have known
# findings of your own. See docs/methodology.md, "Why these defaults".
DEFAULT_WEIGHTS: dict[str, float] = {"churn": 0.8, "complexity": 0.2, "activity": 0.0}


@dataclass(frozen=True, slots=True)
class FileScore:
    """A file's raw metrics alongside every score derived from them."""

    metrics: FileMetrics
    components: Mapping[str, float]  # scored metric -> 0-100, after inversion
    families: Mapping[str, float]  # family -> 0-100
    risk_score: float

    @property
    def path(self) -> str:
        return self.metrics.path


def score(files: Sequence[FileMetrics], weights: Mapping[str, float]) -> list[FileScore]:
    """Score every file, highest risk first. `weights` must already be normalised."""
    values = [f.values() for f in files]
    component_scores = {
        c.metric: _component_scores(c, [v[c.metric] for v in values])
        for components in FAMILIES.values()
        for c in components
    }
    results = []
    for i, metrics in enumerate(files):
        components = {name: scores[i] for name, scores in component_scores.items()}
        families = {
            family: _weighted_mean([(c.weight, components[c.metric]) for c in members])
            for family, members in FAMILIES.items()
        }
        risk = sum(weights[family] * families[family] for family in FAMILIES)
        results.append(FileScore(metrics, components, families, risk))
    results.sort(key=lambda s: (-s.risk_score, s.path))
    return results


def _component_scores(component: Component, values: Sequence[object]) -> list[float]:
    if component.flag:
        return [100.0 if v else 0.0 for v in values]
    known = [i for i, v in enumerate(values) if v is not None]
    ranks = percentile_rank([values[i] for i in known])  # type: ignore[misc]
    out = [NEUTRAL] * len(values)
    for i, rank in zip(known, ranks, strict=True):
        out[i] = 100.0 - rank if component.invert else rank
    return out


def _weighted_mean(pairs: Sequence[tuple[float, float]]) -> float:
    total = sum(w for w, _ in pairs)
    return sum(w * v for w, v in pairs) / total


def percentile_rank(values: Sequence[float]) -> list[float]:
    """Rank each value 0-100 among its peers; ties share their average rank."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [100.0]
    order = sorted(range(n), key=values.__getitem__)
    ranks = [0.0] * n
    start = 0
    while start < n:
        end = start
        while end + 1 < n and values[order[end + 1]] == values[order[start]]:
            end += 1
        for k in range(start, end + 1):
            ranks[order[k]] = 100.0 * (start + end) / 2 / (n - 1)
        start = end + 1
    return ranks


def parse_weights(spec: str | None) -> dict[str, float]:
    """
    Parse a --weights value such as 'churn=0.8,complexity=0.2,activity=0'.

    Families left out keep their default weight. The result is normalised,
    so any ratio works: 'churn=1,complexity=1,activity=1' is an even split.
    """
    weights = dict(DEFAULT_WEIGHTS)
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        key, _, value = (s.strip() for s in part.partition("="))
        if key not in FAMILIES:
            raise HeatmapError(f"unknown weight {key!r} (expected one of: {', '.join(FAMILIES)})")
        try:
            weights[key] = float(value)
        except ValueError:
            raise HeatmapError(f"weight for {key!r} must be a number, got {value!r}") from None
    return normalize_weights(weights)


def normalize_weights(weights: Mapping[str, float]) -> dict[str, float]:
    """Validate family weights and rescale them to sum to 1."""
    if unknown := set(weights) - set(FAMILIES):
        raise HeatmapError(f"unknown weight families: {', '.join(sorted(unknown))}")
    full = {family: float(weights.get(family, 0.0)) for family in FAMILIES}
    for family, w in full.items():
        if not w >= 0:  # also rejects NaN
            raise HeatmapError(f"weight for {family!r} must be >= 0, got {w}")
    total = sum(full.values())
    if not 0 < total < float("inf"):
        raise HeatmapError("at least one weight must be > 0, and all must be finite")
    return {family: w / total for family, w in full.items()}
