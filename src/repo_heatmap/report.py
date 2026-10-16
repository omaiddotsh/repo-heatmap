"""Reports: CSV and JSON for machines, a Markdown summary and `explain` for people."""

from __future__ import annotations

import csv
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from . import __version__
from .collectors.complexity import LIZARD_VERSION
from .errors import HeatmapError
from .filters import ExcludeRules
from .git import COMMIT_ID_RE
from .models import METRIC_GROUPS, METRIC_NAMES, FileMetrics
from .pipeline import ScanOptions, ScanResult
from .scoring import FAMILIES, FileScore, normalize_weights, score

SCORE_COLUMNS = ("risk_score", *(f"{family}_score" for family in FAMILIES))
COLUMNS = ("path", *SCORE_COLUMNS, *METRIC_NAMES)

# A family score at or above this earns a mention in the summary's "Why" column.
HIGH_SCORE = 70.0
_FAMILY_LABELS = {
    "churn": "high churn",
    "complexity": "high complexity",
    "activity": "ownership risk",
}


def rows(result: ScanResult, *, rounded: bool = True) -> list[dict[str, object]]:
    """One flat record per file, in COLUMNS order, highest risk first."""
    fmt = _round if rounded else _keep
    out = []
    for s in result.files:
        row: dict[str, object] = {"path": s.path, "risk_score": fmt(s.risk_score)}
        row.update((f"{family}_score", fmt(v)) for family, v in s.families.items())
        row.update((name, fmt(v)) for name, v in s.metrics.values().items())
        out.append(row)
    return out


def write_csv(result: ScanResult, out_path: Path) -> None:
    """
    Write the report as CSV, rounded for reading.

    The scanned repository chooses its own file names, so they can't be
    trusted to be harmless. Paths are escaped as `printable` shows them, so
    `cat` can't send a control sequence to a terminal and a spreadsheet can't
    show an invisible or right-to-left character; bytes that aren't valid
    UTF-8 become \\xNN, keeping the file valid UTF-8. A path starting with =,
    +, -, or @ also gets a leading apostrophe, so a spreadsheet shows it as
    text instead of running it as a formula. The JSON report keeps paths
    exactly as git stores them.
    """
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows(result):  # None is written as an empty cell
            row["path"] = printable(str(row["path"]))
            writer.writerow({key: _spreadsheet_safe(value) for key, value in row.items()})


def write_json(result: ScanResult, out_path: Path) -> None:
    """
    Write the full-precision report, with everything `read_json` needs to
    rebuild the scan's exact scores later without touching git.

    The repository is recorded by its directory name, not its absolute path,
    so a shared report doesn't reveal where it was made (often under a home
    directory named after its user). json.dumps escapes every control and
    non-ASCII character, so the file is safe to print.
    """
    opts = result.options
    doc = {
        "generator": f"heatmap {__version__}",
        "lizard": LIZARD_VERSION,
        "repo": repo_name(result.root),
        "head": result.head,
        "scope": result.prefix,
        "since": opts.since,
        "until": opts.until,
        "exclude": list(opts.exclude),
        "include": list(opts.include),
        "blame": opts.blame,
        "weights": dict(opts.weights),
        "files": rows(result, rounded=False),
    }
    out_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def repo_name(root: Path) -> str:
    """The repository's directory name, as reports record it ("/" for the file system root)."""
    return root.name or str(root)


def read_json(
    path: str | os.PathLike[str], *, weights: Mapping[str, float] | None = None
) -> ScanResult:
    """
    Rebuild a scan from a saved risk_report.json.

    Scores are recomputed from the saved raw metrics, so they match the
    original scan exactly, or re-weight it if `weights` is given. Reports
    record the repository by name only, so the result's `root` is that name,
    not a full path.
    """
    path = Path(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        _check_header(doc)
        files = [_file_from_row(row) for row in doc["files"]]
        options = ScanOptions(
            since=doc["since"],
            until=doc["until"],
            exclude=tuple(doc["exclude"]),
            include=tuple(doc.get("include", ())),
            blame=doc["blame"],
            weights=normalize_weights(weights if weights is not None else doc["weights"]),
        )
        root, scope, head = Path(doc["repo"]), doc["scope"], doc["head"]
        ExcludeRules(tuple(options.exclude), tuple(options.include))  # validates the saved globs
        scored = score(files, options.weights)
    except OSError as exc:
        raise HeatmapError(f"cannot read report {path}: {exc.strerror or exc}") from exc
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise HeatmapError(
            f"{path} is not a heatmap JSON report, or was written by an older version ({exc!r})"
        ) from exc
    return ScanResult(root, scope, options, scored, head=head)


def _check_header(doc: Mapping[str, object]) -> None:
    optional_text = (str, type(None))
    expected = {
        "repo": (str,),
        "head": optional_text,
        "scope": optional_text,
        "since": optional_text,
        "until": optional_text,
        "blame": (bool,),
    }
    for key, types in expected.items():
        if not isinstance(doc[key], types):
            raise TypeError(f"{key} has the wrong type: {doc[key]!r}")
    head = doc["head"]
    if isinstance(head, str) and not COMMIT_ID_RE.fullmatch(head):
        raise ValueError(f"head must be a full commit id, got {head!r}")
    for key in ("exclude", "include"):
        globs = doc.get(key, [])  # "include" is absent from reports made before it existed
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise TypeError(f"{key} must be a list of globs, got {globs!r}")


def _file_from_row(row: Mapping[str, object]) -> FileMetrics:
    if not isinstance(row["path"], str):
        raise TypeError(f"path must be a string, got {row['path']!r}")
    for name in METRIC_NAMES:
        value = row[name]
        if value is not None and not isinstance(value, bool | int | float):
            raise TypeError(f"{name} must be a number, got {value!r}")
    return FileMetrics.from_values(row["path"], row)


def write_summary(
    result: ScanResult, out_path: Path, *, top_n: int = 50, share: str | None = None
) -> None:
    opts = result.options
    scope = f" (scoped to `{_md(result.prefix)}/`)" if result.prefix else ""
    window = " ".join(
        part
        for part in (
            f"since {opts.since}" if opts.since else "",
            f"until {opts.until}" if opts.until else "",
        )
        if part
    )
    weights = ", ".join(f"{family} {w:.2f}" for family, w in opts.weights.items())
    shown = result.files[:top_n]

    lines = [
        "# heatmap summary",
        "",
        f"- Repository: `{_md(repo_name(result.root))}`{scope}",
        f"- Churn window: {window or 'full history'}",
        f"- Weights: {weights}",
        f"- Files scored: {len(result.files)}",
        "",
        f"## Top {len(shown)} files by risk score"
        + (f" (the top {share} of {len(result.files)})" if share else ""),
        "",
        "| Rank | File | Risk | " + " | ".join(f.title() for f in FAMILIES) + " | Why |",
        "|---:|---|---:|" + "---:|" * len(FAMILIES) + "---|",
    ]
    for rank, s in enumerate(shown, start=1):
        families = " | ".join(f"{s.families[f]:.1f}" for f in FAMILIES)
        lines.append(
            f"| {rank} | `{_md(s.path)}` | {s.risk_score:.1f} | {families} "
            f"| {_rationale(s, opts.weights)} |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_hot_files(result: ScanResult, out_path: Path, *, top_n: int) -> None:
    """
    The `top_n` highest-risk paths, one per line, highest first, for other
    tools to read. A path with a control character (a newline, say) is
    escaped as `printable` shows it, since it couldn't sit on one line.
    """
    lines = [printable(f.path) for f in result.files[:top_n]]
    out_path.write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8", errors="surrogateescape"
    )


def explain(result: ScanResult, file: FileScore) -> str:
    """A breakdown of how one file's score was built, down to each metric's percentile."""
    rank = result.files.index(file) + 1
    raw = file.metrics.values()
    lines = [
        printable(file.path),
        f"risk_score {file.risk_score:.1f}  (rank {rank} of {len(result.files)}, "
        f"{file.metrics.loc} lines)",
    ]
    for family, members in FAMILIES.items():
        weight = result.options.weights[family]
        lines += ["", f"{family}_score {file.families[family]:.1f}  x weight {weight:.2f}"]
        total = sum(c.weight for c in members)
        scored = {c.metric: c for c in members}
        for name in METRIC_GROUPS[family]:
            value = _fmt(raw[name])
            if (c := scored.get(name)) is None:
                lines.append(f"  {name:<24} {value:>10}   (reported, not scored)")
                continue
            note = ""
            if raw[name] is None:
                note = "  no data: neutral"
            elif c.invert:
                note = "  inverted: lower is riskier"
            lines.append(
                f"  {name:<24} {value:>10}   score {file.components[name]:5.1f}"
                f"  x {c.weight / total:.2f}{note}"
            )
    return "\n".join(lines)


def _rationale(s: FileScore, weights: Mapping[str, float]) -> str:
    """Name the families that push this file up, ignoring any weighted at zero."""
    reasons = [
        label
        for family, label in _FAMILY_LABELS.items()
        if weights[family] > 0 and s.families[family] >= HIGH_SCORE
    ]
    return ", ".join(reasons) or "moderate across the board"


def printable(text: str) -> str:
    """
    Escape control and invisible characters for display.

    File names come from the scanned repository, so they can't be trusted:
    one containing ANSI escape sequences could rewrite the terminal, and
    bidirectional overrides or invisible characters can make one name look
    like another. They're shown as \\xNN or \\uNNNN escapes instead, and a
    byte that isn't valid UTF-8 as \\xNN.
    """
    if text.isprintable() and not _INVISIBLE.search(text):
        return text
    return "".join(
        _escape(ch) if not ch.isprintable() or _INVISIBLE.match(ch) else ch for ch in text
    )


# Unicode's Default_Ignorable_Code_Point characters: rendered as nothing, yet
# some (U+034F, variation selectors, Hangul fillers) count as printable.
_INVISIBLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)


def _escape(ch: str) -> str:
    code = ord(ch)
    if 0xDC80 <= code <= 0xDCFF:  # a byte os.fsdecode couldn't decode
        return f"\\x{code - 0xDC00:02x}"
    if code <= 0xFF:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def _md(text: str) -> str:
    """
    Make text safe inside a `code span` in a Markdown table cell.

    Paths are read verbatim from git and may legally contain '|' (splits the
    row), newlines or tabs (end it), backticks (close the code span), and
    any other character `printable` guards against.
    """
    out = text.replace("|", "\\|").replace("`", "'")
    out = out.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return printable(out)


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _spreadsheet_safe(value: object) -> object:
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _round(value: object) -> object:
    return round(value, 4) if isinstance(value, float) else value


def _keep(value: object) -> object:
    return value


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)
