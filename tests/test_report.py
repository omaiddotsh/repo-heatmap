"""repo_heatmap.report: CSV, JSON, the Markdown summary, and explain."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from repo_heatmap import report
from repo_heatmap.errors import HeatmapError
from repo_heatmap.models import ActivityMetrics, ChurnMetrics, ComplexityMetrics, FileMetrics
from repo_heatmap.pipeline import ScanOptions, ScanResult
from repo_heatmap.scoring import normalize_weights, score


def result_for(*files: FileMetrics, weights: dict[str, float] | None = None) -> ScanResult:
    options = ScanOptions(weights=normalize_weights(weights or {"churn": 0.8, "complexity": 0.2}))
    return ScanResult(Path("/repo"), None, options, score(files, options.weights))


def file(path: str, commits: int, *, ccn: int | None = 5) -> FileMetrics:
    return FileMetrics(
        path=path,
        loc=100,
        churn=ChurnMetrics(churn_absolute=commits * 10, commit_count=commits, churn_recency_days=3),
        complexity=None if ccn is None else ComplexityMetrics(ccn, ccn // 10, 4, 12.5, 1 / 3),
        activity=ActivityMetrics(
            distinct_authors=1, ownership_concentration=100.0, bus_factor_flag=True
        ),
    )


@pytest.fixture
def result() -> ScanResult:
    return result_for(
        file("src/hot.py", 30, ccn=40), file("src/cold.py", 1), file("README.md", 2, ccn=None)
    )


def test_csv_has_every_column_in_order(result: ScanResult, tmp_path: Path) -> None:
    out = tmp_path / "r.csv"
    report.write_csv(result, out)
    with out.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert tuple(reader.fieldnames or ()) == report.COLUMNS
        rows = list(reader)
    assert [r["path"] for r in rows] == ["src/hot.py", "README.md", "src/cold.py"]
    readme = rows[1]
    assert readme["cyclomatic_complexity"] == ""  # no data, not zero
    assert readme["days_since_any_commit"] == ""
    assert rows[0]["comment_density"] == "0.3333"  # floats rounded to 4 places


def test_json_carries_run_metadata_and_nulls(result: ScanResult, tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    report.write_json(result, out)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["repo"] == "repo"  # the directory name, never the absolute path
    assert doc["scope"] is None
    assert doc["weights"] == {"churn": 0.8, "complexity": 0.2, "activity": 0.0}
    assert doc["generator"].startswith("heatmap ")
    assert [f["path"] for f in doc["files"]] == ["src/hot.py", "README.md", "src/cold.py"]
    assert doc["files"][1]["cyclomatic_complexity"] is None
    assert list(doc["files"][0]) == list(report.COLUMNS)
    assert doc["files"][0]["comment_density"] == 1 / 3  # full precision, unlike the CSV
    assert {"head", "exclude", "include", "blame"} <= set(doc)


def test_json_round_trip_reproduces_exact_scores(result: ScanResult, tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    report.write_json(result, out)
    loaded = report.read_json(str(out))  # a plain string path works too
    assert loaded.files == result.files
    assert loaded.options.weights == result.options.weights


def test_json_round_trip_can_reweight(result: ScanResult, tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    report.write_json(result, out)
    loaded = report.read_json(out, weights={"complexity": 1})
    assert [f.risk_score for f in loaded.files] == sorted(
        (f.families["complexity"] for f in result.files), reverse=True
    )


def test_read_json_rejects_other_files(tmp_path: Path) -> None:
    bad = tmp_path / "x.json"
    bad.write_text('{"files": []}', encoding="utf-8")
    with pytest.raises(HeatmapError, match="not a heatmap JSON report"):
        report.read_json(bad)
    with pytest.raises(HeatmapError, match="cannot read report"):
        report.read_json(tmp_path / "missing.json")


def test_summary_table(result: ScanResult, tmp_path: Path) -> None:
    out = tmp_path / "s.md"
    report.write_summary(result, out, top_n=2)
    text = out.read_text(encoding="utf-8")
    assert "- Files scored: 3" in text
    assert "## Top 2 files by risk score" in text
    assert "| 1 | `src/hot.py` |" in text
    assert "src/cold.py" not in text
    assert "high churn, high complexity" in text


def test_hot_files_list(result: ScanResult, tmp_path: Path) -> None:
    out = tmp_path / "hot.txt"
    report.write_hot_files(result, out, top_n=2)
    assert out.read_text(encoding="utf-8") == "src/hot.py\n" + result.files[1].path + "\n"
    report.write_hot_files(result, out, top_n=0)
    assert out.read_text(encoding="utf-8") == ""
    # A name that can't sit on one line is escaped, as the terminal shows it.
    report.write_hot_files(result_for(file("a\nb.py", 3)), out, top_n=1)
    assert out.read_text(encoding="utf-8") == "a\\x0ab.py\n"


def test_summary_heading_names_the_share(result: ScanResult, tmp_path: Path) -> None:
    out = tmp_path / "s.md"
    report.write_summary(result, out, top_n=1, share="20%")
    assert "## Top 1 files by risk score (the top 20% of 3)" in out.read_text(encoding="utf-8")


def test_summary_ignores_zero_weight_families_in_rationale(tmp_path: Path) -> None:
    # Every file is single-author, so activity scores high; with activity
    # weighted 0 it must not be offered as a reason.
    out = tmp_path / "s.md"
    report.write_summary(result_for(file("a.py", 1), file("b.py", 1)), out)
    assert "ownership risk" not in out.read_text(encoding="utf-8")


def test_summary_escapes_hostile_paths(tmp_path: Path) -> None:
    out = tmp_path / "s.md"
    report.write_summary(result_for(file("a|b`c\nd.py", 1)), out)
    row = next(
        line for line in out.read_text(encoding="utf-8").splitlines() if line.startswith("| 1 |")
    )
    assert "`a\\|b'c\\nd.py`" in row
    assert row.count(" | ") == 6  # the path didn't add a cell


def test_explain_breaks_down_every_metric(result: ScanResult) -> None:
    text = report.explain(result, result.files[0])
    assert text.splitlines()[0] == "src/hot.py"
    assert "rank 1 of 3" in text
    assert "commit_count" in text and "x 0.90" in text
    assert "churn_absolute" in text and "(reported, not scored)" in text
    assert "inverted: lower is riskier" in text


def test_explain_marks_missing_data_as_neutral(result: ScanResult) -> None:
    readme = result.find("README.md")
    assert readme is not None
    assert "no data: neutral" in report.explain(result, readme)


# --- hostile file names -------------------------------------------------------


@pytest.mark.parametrize("name", ["=SUM(1+1).py", "+cmd.py", "-x.py", "@x.py"])
def test_csv_neutralises_formula_paths(name: str, tmp_path: Path) -> None:
    result = result_for(file(name, 1))
    report.write_csv(result, tmp_path / "r.csv")
    report.write_json(result, tmp_path / "r.json")
    with (tmp_path / "r.csv").open(encoding="utf-8") as f:
        assert next(csv.DictReader(f))["path"] == "'" + name
    assert json.loads((tmp_path / "r.json").read_text())["files"][0]["path"] == name


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("plain/path.py", "plain/path.py"),
        ("esc\x1b[31mred.py", "esc\\x1b[31mred.py"),  # ANSI escape
        ("evil\u202egnp.py", "evil\\u202egnp.py"),  # right-to-left override
        ("zero\u200bwidth.py", "zero\\u200bwidth.py"),
        ("new\nline.py", "new\\x0aline.py"),
        ("café.py", "café.py"),  # ordinary non-ASCII is left alone
        ("dis\u034fguised.py", "dis\\u034fguised.py"),  # printable, yet invisible
        ("vs\ufe0f.py", "vs\\ufe0f.py"),  # variation selector
        ("bad\udcffbyte.py", "bad\\xffbyte.py"),  # os.fsdecode of an invalid byte
    ],
)
def test_printable_escapes_what_could_fool_a_reader(raw: str, shown: str) -> None:
    assert report.printable(raw) == shown


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("esc\x1b[31mred.py", "esc\\x1b[31mred.py"),
        ("new\nline.py", "new\\x0aline.py"),
        ("evil‮gnp.py", "evil\\u202egnp.py"),
        ("\tlead.py", "\\x09lead.py"),
    ],
)
def test_csv_and_json_carry_no_raw_control_characters(raw: str, shown: str, tmp_path: Path) -> None:
    result = result_for(file(raw, 1))
    report.write_csv(result, tmp_path / "r.csv")
    report.write_json(result, tmp_path / "r.json")
    for name in ("r.csv", "r.json"):  # the bytes `cat` would send to a terminal
        data = (tmp_path / name).read_bytes()
        assert not any(seq in data for seq in (b"\x1b", "‮".encode(), b"\t")), name
    assert len((tmp_path / "r.csv").read_bytes().splitlines()) == 2  # the path added no line
    with (tmp_path / "r.csv").open(encoding="utf-8") as f:
        assert next(csv.DictReader(f))["path"] == shown  # escaped as the terminal shows it
    assert report.read_json(tmp_path / "r.json").files[0].path == raw  # exact, for explain


def test_reports_do_not_reveal_where_the_repository_lives(tmp_path: Path) -> None:
    files = (file("src/a.py", 2), file("src/b.py", 1))
    options = ScanOptions(weights=normalize_weights({"churn": 1}))
    result = ScanResult(
        Path("/home/alice/work/acme"), "src", options, score(files, options.weights)
    )
    report.write_json(result, tmp_path / "r.json")
    report.write_summary(result, tmp_path / "s.md")
    doc = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert (doc["repo"], doc["scope"]) == ("acme", "src")
    summary = (tmp_path / "s.md").read_text(encoding="utf-8")
    assert "- Repository: `acme` (scoped to `src/`)" in summary
    for text in (summary, (tmp_path / "r.json").read_text(encoding="utf-8")):
        assert "alice" not in text and "/home" not in text


def test_read_json_accepts_reports_from_before_include_existed(
    result: ScanResult, tmp_path: Path
) -> None:
    out = tmp_path / "r.json"
    report.write_json(result, out)
    doc = json.loads(out.read_text())
    del doc["include"]
    out.write_text(json.dumps(doc))
    assert report.read_json(out).options.include == ()


def test_explain_and_summary_escape_hostile_paths(tmp_path: Path) -> None:
    result = result_for(file("esc\x1b[2Jclear.py", 1))
    assert report.explain(result, result.files[0]).splitlines()[0] == "esc\\x1b[2Jclear.py"
    report.write_summary(result, tmp_path / "s.md")
    assert "\x1b" not in (tmp_path / "s.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["files"][0].update(commit_count="many"),
        lambda doc: doc.update(exclude=["[z-a]"]),
        lambda doc: doc.update(weights={"churn": "x"}),
        lambda doc: doc["files"][0].update(loc=None, churn_absolute=[1]),
        lambda doc: doc.update(head=12345),
        lambda doc: doc.update(repo=None),
        lambda doc: doc.update(scope=["src"]),
        lambda doc: doc.update(exclude="*.md"),
        lambda doc: doc.update(include="*.md"),
        lambda doc: doc.update(include=["[z-a]"]),
        lambda doc: doc.update(blame="yes"),
        lambda doc: doc.update(head="--output=/tmp/x"),  # never reaches git as an option
        lambda doc: doc.update(head="abc123"),  # not a full commit id
    ],
)
def test_read_json_rejects_tampered_reports(result: ScanResult, tmp_path: Path, mutate) -> None:
    out = tmp_path / "r.json"
    report.write_json(result, out)
    doc = json.loads(out.read_text())
    mutate(doc)
    out.write_text(json.dumps(doc))
    with pytest.raises(HeatmapError):
        report.read_json(out)


def test_reports_handle_paths_that_are_not_utf8(tmp_path: Path) -> None:
    name = "bad\udcffname.py"  # os.fsdecode(b"bad\xffname.py")
    result = result_for(file(name, 1))
    report.write_csv(result, tmp_path / "r.csv")
    report.write_json(result, tmp_path / "r.json")
    report.write_summary(result, tmp_path / "s.md")
    assert "bad\\xffname.py" in (tmp_path / "r.csv").read_text(encoding="utf-8")
    assert "bad\\xffname.py" in (tmp_path / "s.md").read_text(encoding="utf-8")
    assert report.read_json(tmp_path / "r.json").files[0].path == name  # exact, for explain
