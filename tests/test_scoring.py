"""repo_heatmap.scoring: percentiles, the family model, and weights."""

from __future__ import annotations

import pytest

from repo_heatmap.errors import HeatmapError
from repo_heatmap.models import ActivityMetrics, ChurnMetrics, ComplexityMetrics, FileMetrics
from repo_heatmap.scoring import (
    DEFAULT_WEIGHTS,
    FAMILIES,
    NEUTRAL,
    normalize_weights,
    parse_weights,
    percentile_rank,
    score,
)

EVEN = {family: 1 / 3 for family in FAMILIES}


def make(
    path: str,
    *,
    commits: int = 0,
    relative: float = 0.0,
    burst: float = 0.0,
    ccn: int | None = None,
    nesting: int = 0,
    comments: float = 0.0,
    ownership: float | None = None,
    single_author: bool = False,
) -> FileMetrics:
    return FileMetrics(
        path=path,
        loc=10,
        churn=ChurnMetrics(commit_count=commits, churn_relative=relative, churn_burstiness=burst),
        complexity=None if ccn is None else ComplexityMetrics(ccn, nesting, 1, 1.0, comments),
        activity=ActivityMetrics(ownership_concentration=ownership, bus_factor_flag=single_author),
    )


def by_path(files: list[FileMetrics], weights=DEFAULT_WEIGHTS):
    return {s.path: s for s in score(files, weights)}


# --- percentile_rank --------------------------------------------------------------


def test_percentile_rank() -> None:
    assert percentile_rank([]) == []
    assert percentile_rank([5]) == [100.0]
    assert percentile_rank([3, 1, 2]) == [100.0, 0.0, 50.0]


def test_percentile_rank_ties_share_their_average() -> None:
    assert percentile_rank([1, 1, 2]) == [25.0, 25.0, 100.0]


# --- the model --------------------------------------------------------------------


def test_churn_is_dominated_by_commit_count() -> None:
    # A flat mean of the three churn metrics diluted commit_count 3:1; with
    # the other two tied, the gap between files is commit_count's weight.
    s = by_path(
        [make("busy.py", commits=100, relative=1.0), make("calm.py", commits=1, relative=1.0)]
    )
    gap = s["busy.py"].families["churn"] - s["calm.py"].families["churn"]
    assert gap == pytest.approx(100 * 0.90)


def test_missing_complexity_is_neutral_not_zero() -> None:
    # A README has no cyclomatic complexity. As a 0 it would rank *above*
    # real source once comment density is inverted.
    s = by_path([make("code.py", ccn=10, nesting=3, comments=0.1), make("notes.md")])
    assert s["notes.md"].families["complexity"] == NEUTRAL
    assert s["notes.md"].components["comment_density"] == NEUTRAL


def test_missing_ownership_is_neutral_not_worst() -> None:
    s = by_path([make("code.py", ownership=60.0), make("asset.bin")])
    assert s["asset.bin"].components["ownership_concentration"] == NEUTRAL
    assert s["asset.bin"].families["activity"] == 25.0  # mean(neutral 50, no bus factor 0)


def test_no_file_has_ownership_data() -> None:
    s = by_path([make("a.bin"), make("b.bin", single_author=True)])
    assert s["a.bin"].families["activity"] == 25.0
    assert s["b.bin"].families["activity"] == 75.0


def test_inverted_metrics_score_low_values_as_risky() -> None:
    s = by_path([make("terse.py", ccn=1, comments=0.0), make("chatty.py", ccn=1, comments=0.5)])
    assert s["terse.py"].components["comment_density"] == 100.0
    assert s["chatty.py"].components["comment_density"] == 0.0


def test_risk_is_weighted_sum_of_families() -> None:
    s = by_path([make("a.py", commits=5, ccn=3), make("b.py", commits=1, ccn=9)], EVEN)["a.py"]
    assert s.risk_score == pytest.approx(sum(s.families.values()) / 3)


def test_results_are_sorted_riskiest_first() -> None:
    files = [
        make("quiet.py", commits=1, ccn=1, comments=0.5, ownership=50.0),
        make("hot.py", commits=20, relative=2.0, burst=1.0, ccn=30, nesting=5, ownership=100.0),
    ]
    assert [s.path for s in score(files, DEFAULT_WEIGHTS)] == ["hot.py", "quiet.py"]


def test_ties_are_ordered_by_path() -> None:
    assert [s.path for s in score([make("b.py"), make("a.py")], DEFAULT_WEIGHTS)] == [
        "a.py",
        "b.py",
    ]


# --- weights ----------------------------------------------------------------------


def test_default_weights_are_normalised() -> None:
    assert parse_weights(None) == pytest.approx(DEFAULT_WEIGHTS)
    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)


def test_weights_are_any_ratio() -> None:
    assert parse_weights("churn=1,complexity=1,activity=1") == pytest.approx(EVEN)
    assert parse_weights(" churn = 3 , complexity = 1 ") == pytest.approx(
        {"churn": 3 / 4.0, "complexity": 1 / 4.0, "activity": 0.0}
    )


@pytest.mark.parametrize(
    "spec",
    [
        "churn=abc",
        "churn=-1",
        "nope=1",
        "churn=0,complexity=0,activity=0",
        "churn=nan",
        "churn=inf",
    ],
)
def test_bad_weights_are_rejected(spec: str) -> None:
    with pytest.raises(HeatmapError):
        parse_weights(spec)


def test_normalize_weights_fills_missing_families_with_zero() -> None:
    assert normalize_weights({"churn": 2}) == {"churn": 1.0, "complexity": 0.0, "activity": 0.0}
