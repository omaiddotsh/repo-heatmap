"""Size and complexity, measured from a file's content at HEAD.

lizard does the language-aware work: cyclomatic complexity and nesting depth
for more than twenty languages, with no build step. Comment density is a narrow
line-prefix heuristic, because lizard doesn't count comments.
"""

from __future__ import annotations

import contextlib
import io
import logging
import signal
import threading
import time
from collections.abc import Iterator
from types import FrameType

import lizard

from ..models import ComplexityMetrics

log = logging.getLogger(__name__)

# Recorded in the JSON report: complexity numbers can change between lizard releases.
LIZARD_VERSION: str = lizard.version

# Bigger files are counted but not measured. Source this large is almost
# always generated, and lizard, being pure Python, takes about a second per
# megabyte.
MAX_BYTES = 1_000_000

# A file lizard hasn't finished with in this long is scored as neutral. Real
# source takes well under a second, but some input (an unterminated comment,
# repeated) makes lizard's tokenizer quadratic: one crafted 1 MB file can
# take most of an hour.
MAX_SECONDS = 10.0

# '#' and '//' cover most scripting and C-family languages. Broader prefixes
# cost more than they gained: a bare '*' also matches markdown bullets and C
# pointer dereferences, and '--' matches C decrements.
_COMMENT_PREFIXES = ("#", "//")

# The "nd" extension makes lizard record max_nesting_depth per function.
_ANALYZER = lizard.FileAnalyzer(lizard.get_extensions(["nd"]))

_BINARY_SNIFF_BYTES = 8192


def looks_binary(content: bytes) -> bool:
    return b"\0" in content[:_BINARY_SNIFF_BYTES]


def count_lines(content: bytes) -> int:
    """Newlines, plus one for a last line with no trailing newline."""
    if not content:
        return 0
    return content.count(b"\n") + (0 if content.endswith(b"\n") else 1)


def _lizard_name(path: str) -> str:
    r"""
    The file name lizard is given. It picks the language with `.*\.(ext)$`,
    and `.*` stops at a newline, so a name containing one would find no reader
    (and lizard would fall back to a reader that sees no functions). Only the
    extension matters to it.
    """
    return path.rsplit("/", 1)[-1].replace("\n", "_").replace("\r", "_")


def reader_for(path: str) -> type | None:
    """The lizard reader for a path's language, or None if lizard has none."""
    return lizard.get_reader_for(_lizard_name(path))  # type: ignore[no-any-return]


def measure(path: str, content: bytes) -> ComplexityMetrics | None:
    """
    Complexity of one file, or None if it can't be measured.

    lizard will run its generic C-like reader over any file it's handed,
    inventing numbers for markdown, config, and binaries, so only files whose
    extension maps to a real lizard reader are measured. Its parsers can
    also crash or give up on unusual input, which a hostile repository can
    supply on purpose; such a file is scored as neutral rather than ending
    the scan, and lizard's own messages are kept off the terminal.
    """
    if reader_for(path) is None or looks_binary(content) or len(content) > MAX_BYTES:
        return None
    text = _decode(content)
    lizard_said = io.StringIO()
    try:
        with contextlib.redirect_stderr(lizard_said), _time_limit(MAX_SECONDS):
            info = _ANALYZER.analyze_source_code(_lizard_name(path), text)
    except _TimeUp:
        log.warning(
            "gave up measuring %s after %g seconds; its complexity scores as neutral",
            path,
            MAX_SECONDS,
        )
        return None
    except Exception as exc:
        log.warning("can't measure %s (%s); its complexity scores as neutral", path, exc)
        return None
    if lizard_said.getvalue():  # e.g. "[skip] fail to process": the result is partial
        log.warning("can't measure %s; its complexity scores as neutral", path)
        return None
    functions = info.function_list
    return ComplexityMetrics(
        cyclomatic_complexity=info.CCN,
        nesting_depth=max((f.max_nesting_depth for f in functions), default=0),
        function_count=len(functions),
        avg_function_length=sum(f.nloc for f in functions) / len(functions) if functions else 0.0,
        comment_density=comment_density(text),
    )


class _TimeUp(BaseException):
    """Raised into lizard when time is up. Not an Exception, so lizard can't catch it."""


@contextlib.contextmanager
def _time_limit(seconds: float) -> Iterator[None]:
    """
    Interrupt the block with _TimeUp after `seconds`, using SIGALRM.

    Signals only reach the main thread, so elsewhere (a caller running
    repo_heatmap.scan() in a worker thread) there is no limit, and none is set
    if the caller already has a timer running.

    The timer can run out just as the block finishes, so its signal may
    arrive after the block. The handler only raises while `armed`, which is
    cleared first, and a signal already on its way is waited for before the
    previous handler returns: under the default one it would end the process.
    _TimeUp can still be raised as the block exits, inside this context
    manager, so callers catch it around the whole `with`.
    """
    if (
        threading.current_thread() is not threading.main_thread()
        or signal.getitimer(signal.ITIMER_REAL)[0]
    ):
        yield
        return

    state = {"armed": True, "fired": False}

    def expire(signum: int, frame: FrameType | None) -> None:
        state["fired"] = True
        if state["armed"]:
            state["armed"] = False
            raise _TimeUp

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        try:
            state["armed"] = False
        finally:
            remaining, _ = signal.setitimer(signal.ITIMER_REAL, 0)
            # No time left means the timer ran out, or was about to: its
            # signal, if any, lands within a millisecond or so.
            give_up = time.monotonic() + 0.05
            while not remaining and not state["fired"] and time.monotonic() < give_up:
                time.sleep(0.001)
            signal.signal(signal.SIGALRM, previous)


def comment_density(text: str) -> float:
    """Share of lines that start with a single-line comment marker."""
    # Split on "\n" only (`_decode` has normalised CR and CRLF): str.splitlines
    # also breaks on form feeds and Unicode separators, which would make the
    # denominator disagree with the file's line count.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return 0.0
    return sum(1 for line in lines if line.lstrip().startswith(_COMMENT_PREFIXES)) / len(lines)


def _decode(content: bytes) -> str:
    """Read source the way lizard reads a file: UTF-8, BOM stripped, universal newlines."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("utf-8", "ignore")
    return text.replace("\r\n", "\n").replace("\r", "\n")
