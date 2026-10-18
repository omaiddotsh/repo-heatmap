"""Command-line interface: `heatmap scan` and `heatmap explain`.

Progress and warnings go to stderr so stdout stays clean for piping.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import math
import os
import signal
import sys
import threading
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from types import FrameType

from . import __version__, report
from .errors import HeatmapError
from .filters import ExcludeRules
from .git import GitRepo
from .pipeline import ScanOptions, ScanResult, scan
from .scoring import FileScore, parse_weights

log = logging.getLogger(__package__)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(quiet=args.quiet)
    try:
        with _sigterm_as_exception():
            status: int = args.handler(args)
            sys.stdout.flush()  # here, so a closed pipe is caught below
        return status
    except HeatmapError as exc:
        _error(exc)
        return 1
    except KeyboardInterrupt:
        print("heatmap: interrupted", file=sys.stderr)
        return 130
    except _Terminated:
        print("heatmap: terminated", file=sys.stderr)
        return 143
    except BrokenPipeError:
        # stdout was closed early, as by `heatmap explain ... | head`. Point
        # it at devnull so the interpreter's final flush can't fail again.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141


class _Terminated(BaseException):
    """
    SIGTERM, raised like KeyboardInterrupt so cleanup (temporary files) runs.
    Further SIGTERMs are ignored until then, so they can't cut cleanup short.
    """


@contextmanager
def _sigterm_as_exception() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def terminate(signum: int, frame: FrameType | None) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise _Terminated

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _error(message: object) -> None:
    print(f"heatmap: error: {report.printable(str(message))}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heatmap",
        description="Rank the files in a git repository by security-review risk.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "repo",
        type=Path,
        metavar="REPO",
        help="a git repository, or a directory inside one to scope the scan to",
    )
    common.add_argument(
        "--since",
        metavar="DATE",
        help="count churn only from commits made on or after DATE (git syntax: '180 days ago')",
    )
    common.add_argument(
        "--until", metavar="DATE", help="count churn only from commits made on or before DATE"
    )
    common.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="also leave out paths matching GLOB (repeatable, or comma-separated)",
    )
    common.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="score paths matching GLOB even if a built-in rule leaves them out, such as "
        "'*.md' or 'tests/' (repeatable, or comma-separated); --exclude still applies",
    )
    common.add_argument(
        "--weights",
        metavar="SPEC",
        help="family weights as any ratio (default: churn=0.8,complexity=0.2,activity=0); "
        "families left out keep their default",
    )
    common.add_argument(
        "--no-blame",
        dest="blame",
        action="store_false",
        help="skip git blame, the slowest step; ownership_concentration is left empty",
    )
    common.add_argument("-q", "--quiet", action="store_true", help="print only warnings and errors")

    scan_cmd = commands.add_parser(
        "scan", parents=[common], help="score every file and write the reports"
    )
    scan_cmd.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("risk_report"),
        metavar="DIR",
        help="directory for the reports (default: ./risk_report)",
    )
    scan_cmd.add_argument(
        "--top",
        type=_top,
        default=Top(percent=20),
        metavar="N|P%",
        help="files in summary.md and hot_files.txt: a count, or a percentage of the files "
        "scored (default: 20%%)",
    )
    scan_cmd.set_defaults(handler=_cmd_scan)

    explain_cmd = commands.add_parser(
        "explain", parents=[common], help="show how files' scores were built"
    )
    explain_cmd.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="path relative to the repository root, to REPO if it's a subdirectory, or absolute",
    )
    explain_cmd.add_argument(
        "--report",
        type=Path,
        metavar="PATH",
        help="reuse a scan's risk_report.json (or the directory holding it) instead of "
        "rescanning; instant, and exactly the scan's numbers",
    )
    explain_cmd.set_defaults(handler=_cmd_explain)
    return parser


def _cmd_scan(args: argparse.Namespace) -> int:
    options = _scan_options(args)
    out_dir: Path = args.output
    created = not out_dir.exists()
    _ensure_dir(out_dir)  # fail before the scan if the reports could not be written
    try:
        result = scan(args.repo, options)
    except BaseException:
        if created:
            with contextlib.suppress(OSError):
                out_dir.rmdir()  # only if still empty: nothing of the user's is touched
        raise
    top: Top = args.top
    top_n = top.of(len(result.files))
    try:
        report.write_csv(result, out_dir / "risk_report.csv")
        report.write_json(result, out_dir / "risk_report.json")
        report.write_summary(result, out_dir / "summary.md", top_n=top_n, share=top.share)
        report.write_hot_files(result, out_dir / "hot_files.txt", top_n=top_n)
    except OSError as exc:
        raise HeatmapError(f"cannot write reports to {out_dir}: {exc}") from exc
    noun = "file" if len(result.files) == 1 else "files"
    log.info(
        "Scored %d %s. Wrote risk_report.csv, risk_report.json, summary.md, hot_files.txt to %s",
        len(result.files),
        noun,
        out_dir,
    )
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    if args.report:
        result, prefix = _load_report(args)
    else:
        # Percentiles are relative to every other file, so this is a full scan.
        result = scan(args.repo, _scan_options(args))
        prefix = result.prefix
    explanations, failed = [], False
    for given in args.files:
        try:
            explanations.append(report.explain(result, _find(result, given, prefix)))
        except HeatmapError as exc:
            _error(exc)
            failed = True
    if explanations:
        print("\n\n".join(explanations))
    return 1 if failed else 0


def _load_report(args: argparse.Namespace) -> tuple[ScanResult, str | None]:
    """
    Load a saved scan for `explain --report`, checking it belongs to REPO.

    Reports name the repository but don't record where it was, so the commit
    decides: a report belongs to REPO if the commit it scanned is in REPO's
    history. Any clone of the same repository qualifies, wherever it lives.
    """
    if args.since or args.until or args.exclude or args.include or not args.blame:
        raise HeatmapError(
            "--since, --until, --exclude, --include and --no-blame are fixed by the report; "
            "drop them, or rescan without --report"
        )
    path: Path = args.report / "risk_report.json" if args.report.is_dir() else args.report
    weights = parse_weights(args.weights) if args.weights else None
    result = report.read_json(path, weights=weights)
    repo = GitRepo.open(args.repo)
    if result.head is not None and not repo.has_commit(result.head):
        raise HeatmapError(
            f"{path} is a report on another repository ({result.root}): the commit it "
            f"scanned, {result.head[:12]}, is not in {repo.root}"
        )
    if repo.head != result.head:
        log.warning(
            "%s was made at commit %s, but HEAD is now %s; explaining the report as saved",
            path,
            (result.head or "none")[:12],
            (repo.head or "none")[:12],
        )
    return replace(result, root=repo.root), repo.prefix


def _find(result: ScanResult, given: str, prefix: str | None) -> FileScore:
    """
    Resolve a user-supplied path to a scored file, or explain why it has no score.
    A relative path may be relative to the root or to the `prefix` directory.
    """
    path = Path(given)
    absolute = path.is_absolute()
    if absolute:
        path = _relative_to(path, result.root, given)
    rel = path.as_posix().removeprefix("./")
    # An absolute path names exactly one file. A relative one is REPO-relative
    # first: with REPO=src, "utils.py" means src/utils.py even if the repository
    # root has a utils.py too, and "src/x.py" means src/src/x.py if that exists.
    candidates = [rel] if absolute or not prefix else [f"{prefix}/{rel}", rel]

    for candidate in candidates:
        if (found := result.find(candidate) or _respelled(result, candidate)) is not None:
            return found
    rules = ExcludeRules(tuple(result.options.exclude), tuple(result.options.include))
    for candidate in candidates:
        if reason := rules.reason(candidate):
            hint = "" if reason.startswith("matches --exclude") else " (--include re-admits it)"
            raise HeatmapError(f"{candidate} is not scored: {reason}{hint}")
    raise HeatmapError(f"{rel} is not a tracked file at HEAD in the scanned directory")


def _respelled(result: ScanResult, candidate: str) -> FileScore | None:
    """
    The scored file `candidate` names on a case-insensitive file system
    (macOS, Windows), where it may be typed in a different case, or Unicode
    normal form, than git spells it. The file system decides, so this never
    matches elsewhere.
    """
    key = _loose(candidate)
    matches = []
    for f in result.files:
        if _loose(f.path) == key:
            try:
                if os.path.samefile(result.root / f.path, result.root / candidate):
                    matches.append(f)
            except OSError:
                continue
    if len(matches) > 1:
        names = ", ".join(f.path for f in matches)
        raise HeatmapError(f"{candidate} could be any of: {names}. Give the exact name.")
    return matches[0] if matches else None


def _loose(path: str) -> str:
    return unicodedata.normalize("NFD", path).casefold()


def _relative_to(path: Path, root: Path, given: str) -> Path:
    """
    `path` relative to the repository root. The root is matched by identity,
    not spelling, so a path typed in a different case on a case-insensitive
    file system (macOS, Windows) still resolves.
    """
    try:
        path = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise HeatmapError(f"cannot read {given}: {exc}") from exc
    for parent in (path, *path.parents):
        try:
            if parent == root or os.path.samefile(parent, root):
                return path.relative_to(parent)
        except OSError:
            continue
    raise HeatmapError(f"{given} is outside the repository {root}")


def _scan_options(args: argparse.Namespace) -> ScanOptions:
    return ScanOptions(
        since=args.since,
        until=args.until,
        exclude=_globs(args.exclude),
        include=_globs(args.include),
        weights=parse_weights(args.weights),
        blame=args.blame,
    )


def _globs(given: list[str]) -> tuple[str, ...]:
    """Globs from repeated and comma-separated options."""
    return tuple(g.strip() for item in given for g in item.split(",") if g.strip())


def _ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HeatmapError(f"cannot create output directory {path}: {exc}") from exc


@dataclass(frozen=True)
class Top:
    """How many files `--top` keeps: a fixed count, or a share of the files scored."""

    count: int | None = None
    percent: float | None = None

    def of(self, total: int) -> int:
        if self.count is not None:
            return min(self.count, total)
        # Rounded up, so any share of a non-empty scan keeps at least one file.
        # Exact arithmetic: in binary floating point 250 * 64.4 / 100 is a hair
        # over 161, and ceil would keep one file too many.
        share = Fraction(repr(self.percent or 0))
        return min(total, math.ceil(total * share / 100))

    @property
    def share(self) -> str | None:
        """The percentage as summary.md states it, or None for a count."""
        if self.percent is None:
            return None
        return f"{self.percent:g}%"


def _top(text: str) -> Top:
    if text.endswith("%"):
        try:
            percent = float(text[:-1])
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"expected a percentage such as 20%, got {text!r}"
            ) from None
        if not 0 < percent <= 100:
            raise argparse.ArgumentTypeError(
                f"a percentage must be over 0 and at most 100, got {text}"
            )
        return Top(percent=percent)
    return Top(count=_positive_int(text))


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return value


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = report.printable(record.getMessage())
        if record.levelno >= logging.WARNING:
            return f"heatmap: {record.levelname.lower()}: {message}"
        return message


def _configure_logging(*, quiet: bool) -> None:
    # A fresh handler per call binds to the current sys.stderr, so repeated
    # in-process calls (tests) neither duplicate output nor write to a
    # stream that has since been swapped out.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter())
    log.handlers[:] = [handler]
    log.setLevel(logging.WARNING if quiet else logging.INFO)
    log.propagate = False
