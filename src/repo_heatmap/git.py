"""All git access: opening the repository, listing files, reading history and blame.

Everything is read from HEAD, never the work tree, so a dirty checkout doesn't
skew results and bare repositories work. Paths are arbitrary bytes to git, so
output is read NUL-delimited wherever git offers it and decoded with
os.fsdecode, which round-trips: one non-UTF-8 filename, or one containing
" => ", must not abort or mis-key a scan.
"""

from __future__ import annotations

import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path
from typing import IO, BinaryIO

from .errors import HeatmapError
from .models import ChangeEvent

log = logging.getLogger(__package__)

# Pinned so neither the user's nor the repository's config can change results
# or the output we parse:
# - core.quotePath would C-escape non-ASCII paths, log.showSignature would
#   splice gpg output into commit headers, and i18n.logOutputEncoding would
#   re-encode author names.
# - diff.renameLimit, diff.algorithm, diff.indentHeuristic, and
#   core.bigFileThreshold (above which git treats a file as binary) are
#   pinned to git's own defaults.
# - core.attributesFile and mailmap.file would apply the user's own
#   attributes and mailmap.
# - core.precomposeUnicode, on by default on macOS, would rewrite a
#   decomposed-Unicode path passed back to git, so blame couldn't find it.
# - blame.blankBoundary and blame.coloring would blank the commit id each
#   blame line starts with, or put color codes before it (even with
#   color.ui=never).
# - mailmap.blob reads .mailmap from HEAD. git runs inside the git directory
#   (see GitRepo.run), so the checkout's copy, possibly edited, is never seen.
# The repository's .gitattributes are ignored too; see GitRepo.__init__.
_CONFIG = (
    "-c", "core.quotePath=false",
    "-c", "log.showSignature=false",
    "-c", "color.ui=never",
    "-c", "i18n.logOutputEncoding=UTF-8",
    "-c", "diff.renameLimit=1000",
    "-c", "diff.algorithm=myers",
    "-c", "diff.indentHeuristic=true",
    "-c", "diff.relative=false",
    "-c", "core.bigFileThreshold=512m",
    "-c", f"core.attributesFile={os.devnull}",
    "-c", "core.precomposeUnicode=false",
    "-c", "blame.blankBoundary=false",
    "-c", "blame.coloring=none",
    "-c", "mailmap.file=",
    "-c", "mailmap.blob=HEAD:.mailmap",
)  # fmt: skip

# The empty tree in each object format git supports.
_EMPTY_TREES = {
    "sha1": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "sha256": "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",
}

# A directory that doesn't exist, for files git must not find. Run from the
# git directory, git older than 2.43 reads .gitattributes from the index; it
# treats a missing index as empty, and can't create one here either.
_NOWHERE = os.path.join(tempfile.gettempdir(), f"heatmap-{secrets.token_hex(16)}")

# Author names are cut to this many characters. git prints one per commit,
# and a hostile repository can give each of thousands of commits a
# megabyte-long name that compresses to almost nothing, so they're read as
# a stream and capped in bytes too (see GitRepo._authors_from). git's own
# truncation can't do it: it counts display columns, and zero-width
# characters take none.
AUTHOR_MAX_CHARS = 128
_AUTHOR_LINE_MAX_BYTES = 4096

# Authors come from _authors_from, so the history's commit headers carry
# only the commit id and the author date.
_COMMIT_FORMAT = "--pretty=format:@@%H|%ad|"

# A .mailmap bigger than this is ignored. git parses it again in every
# process, in time that can grow with the square of its length: a crafted
# 3 MB one costs seconds per process. Real ones are a few kilobytes.
MAILMAP_MAX_BYTES = 256 * 1024

# History is diffed by this many `git diff-tree` processes at once.
HISTORY_WORKERS = min(16, os.cpu_count() or 4)
# Commits per diff-tree process: small enough to balance the load (diff
# cost varies wildly between commits), large enough that process startup
# stays negligible.
_HISTORY_CHUNK = 500


class GitError(HeatmapError):
    """A git command failed. The message carries git's own explanation."""


@cache
def git_executable() -> str:
    """
    The real git binary, resolved once per process.

    A scan starts thousands of git processes, so per-process overhead adds
    up. On macOS, /usr/bin/git is a shim that spends about 6 ms per call
    locating the developer tools before running git; `git --exec-path`
    names the directory that holds the real binary. An absolute path also
    lets subprocess use posix_spawn rather than forking this whole Python
    process, which is much cheaper once the process is large.
    """
    found = shutil.which("git")
    if found is None:
        raise HeatmapError("git executable not found on PATH")
    try:
        exec_path = subprocess.run(
            [found, "--exec-path"], capture_output=True, check=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return found
    for name in ("git", "git.exe"):
        candidate = os.path.join(exec_path, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return found


def _command(cwd: Path, *args: str, config: Sequence[str] = ()) -> list[str]:
    return [git_executable(), *_CONFIG, *config, "-C", str(cwd), *args]


def _environment(env: dict[str, str] | None) -> dict[str, str]:
    """
    The environment for a git process. git must never touch the network: a
    partial clone would otherwise download missing objects mid-scan (and
    write them into the repository), and anything needing credentials
    would hang on a prompt.
    """
    ignored = ("GIT_DIR", "GIT_WORK_TREE", "GIT_ATTR_SOURCE")
    base = {k: v for k, v in (env or os.environ).items() if k not in ignored}
    # GIT_LITERAL_PATHSPECS: a path argument is a path, never pathspec magic,
    # so a directory or file named ":(glob)*" means exactly that.
    # GIT_NO_REPLACE_OBJECTS and GIT_GRAFT_FILE: a mirror clone fetches
    # refs/replace/*, which could swap a file's content or history for a
    # decoy, and a copied .git directory can carry grafts that do the same.
    # GIT_ATTR_NOSYSTEM: the machine's attributes can't change results either.
    return {
        **base,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": os.path.join(_NOWHERE, "grafts"),
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_INDEX_FILE": os.path.join(_NOWHERE, "index"),
    }


def _git(
    cwd: Path,
    *args: str,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    config: Sequence[str] = (),
) -> bytes:
    try:
        # close_fds=False lets subprocess use posix_spawn. It's safe: Python
        # opens file descriptors non-inheritable, so the child gets none.
        result = subprocess.run(
            _command(cwd, *args, config=config),
            input=stdin,
            capture_output=True,
            check=True,
            env=_environment(env),
            close_fds=False,
        )
    except FileNotFoundError:
        raise HeatmapError("git executable not found on PATH") from None
    except OSError as exc:  # e.g. a hostile file name too long for an argument
        raise GitError(f"git {args[0]} could not start: {exc.strerror or exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise _failure(args[0], exc.returncode, exc.stderr) from exc
    return result.stdout


def _failure(command: str, status: int, stderr: bytes) -> GitError:
    """A GitError carrying the last lines of git's own explanation."""
    lines = [s for line in stderr.decode("utf-8", "replace").splitlines() if (s := line.strip())]
    detail = "; ".join(lines[-3:]) or f"exit status {status}"
    return GitError(f"git {command} failed: {detail}")


@dataclass(frozen=True, slots=True)
class LargeBlob:
    """A blob over `read_blobs`' size limit, measured while streaming rather than held."""

    size: int
    lines: int
    binary: bool


@dataclass(frozen=True, slots=True)
class TrackedFile:
    path: str  # relative to the repository root
    oid: str  # blob id at HEAD


class GitRepo:
    """A git repository, optionally narrowed to one subdirectory, read at HEAD."""

    def __init__(
        self,
        root: Path,
        prefix: str | None = None,
        git_dir: Path | None = None,
        object_format: str = "sha1",
    ) -> None:
        self.root = root  # the work tree's top level, or the repository itself if bare
        self.prefix = prefix  # repo-relative subdirectory, no trailing slash
        # Every command runs here rather than in the work tree, so nothing
        # in the checkout can affect a result.
        self.git_dir = git_dir or root
        self._env: dict[str, str] | None = None  # set while a commit-graph is attached
        # The repository's .gitattributes are ignored: attributes are read
        # from the empty tree (git 2.43+; older git sees no index, see
        # _NOWHERE). Otherwise one committed line marking a file `binary`
        # or `-diff` would hide its line counts, and a hostile repository
        # could use that to bury the files it least wants reviewed.
        try:
            self._config: tuple[str, ...] = ("-c", f"attr.tree={_EMPTY_TREES[object_format]}")
        except KeyError:
            raise HeatmapError(f"unsupported git object format: {object_format}") from None
        self._authors: dict[str, str] = {}  # commit id -> author, filled in from history

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> GitRepo:
        """
        Open the repository containing `path`.

        `git log` prints paths relative to the repository root no matter where
        it runs, so a path inside a work tree resolves to the root plus a
        subdirectory prefix that filters everything else.

        Shallow clones are refused: their history is cut off, so every file
        would look like it had a single commit, and the ranking would be
        meaningless without any visible error. Partial clones are refused
        too: their file contents live on a server, and heatmap never uses
        the network.
        """
        try:
            path = Path(path).resolve()
            path.stat()
        except (FileNotFoundError, NotADirectoryError):
            raise HeatmapError(f"repo path does not exist: {path}") from None
        except (OSError, RuntimeError) as exc:  # unreadable, or a symlink loop
            raise HeatmapError(f"cannot read {path}: {exc}") from exc
        try:
            # The git directory comes last and the work tree's paths are asked
            # for separately, since any of them may contain a newline.
            bare, shallow, object_format, git_dir = (
                _git(
                    path,
                    "rev-parse",
                    "--is-bare-repository",
                    "--is-shallow-repository",
                    "--show-object-format",
                    "--absolute-git-dir",
                )
                .removesuffix(b"\n")
                .split(b"\n", 3)
            )
            top = prefix = b""
            if bare != b"true":
                top = _git(path, "rev-parse", "--show-toplevel").removesuffix(b"\n")
                prefix = _git(path, "rev-parse", "--show-prefix").removesuffix(b"\n")
            partial = _is_partial_clone(path)
        except GitError as exc:
            if "dubious ownership" in str(exc):
                owned = re.search(r"repository at '([^']*)'", str(exc))
                where = owned.group(1) if owned else path
                raise HeatmapError(
                    f"git refuses to read {where} because another user owns it. If you "
                    f"trust it, allow it with: git config --global --add safe.directory {where}"
                ) from exc
            if "not a git repository" in str(exc):
                raise HeatmapError(f"Not a git repository: {path}") from exc
            raise HeatmapError(f"cannot open {path} as a git repository: {exc}") from exc
        if shallow == b"true":
            raise HeatmapError(
                f"{path} is a shallow clone, so its history is incomplete and churn and "
                f"authorship would be wrong. Fetch the full history first: "
                f"git -C {path} fetch --unshallow"
            )
        if partial:
            raise HeatmapError(
                f"{path} is a partial clone (made with --filter), so some file contents "
                f"exist only on the server, and heatmap works offline. Clone it again "
                f"without --filter."
            )
        repo = cls(
            Path(os.fsdecode(top)) if top else path,
            os.fsdecode(prefix).rstrip("/") or None,
            git_dir=Path(os.fsdecode(git_dir)),
            object_format=object_format.decode("ascii", "replace"),
        )
        repo._limit_mailmap()
        return repo

    def _limit_mailmap(self) -> None:
        """Ignore the committed .mailmap if it's too big to be real (see MAILMAP_MAX_BYTES)."""
        try:
            size = int(self.run("cat-file", "-s", "HEAD:.mailmap"))
        except (GitError, ValueError):
            return  # no commits yet, or no .mailmap
        if size > MAILMAP_MAX_BYTES:
            log.warning(
                ".mailmap is %s bytes, over the %s-byte limit, so it's ignored: "
                "authors are counted by the names they committed under",
                f"{size:,}",
                f"{MAILMAP_MAX_BYTES:,}",
            )
            self._config += ("-c", "mailmap.blob=")

    def run(self, *args: str, stdin: bytes | None = None) -> bytes:
        return _git(self.git_dir, *args, env=self._env, stdin=stdin, config=self._config)

    @cached_property
    def head(self) -> str | None:
        """The commit HEAD points at, or None for a repository with no commits yet."""
        try:
            return self.run("rev-parse", "--verify", "--quiet", "HEAD").decode("ascii").strip()
        except GitError:
            return None

    @property
    def has_commits(self) -> bool:
        return self.head is not None

    def has_commit(self, sha: str) -> bool:
        """True if `sha`, a full commit id, names a commit in this repository."""
        if not COMMIT_ID_RE.fullmatch(sha):  # never let it reach git as an option
            return False
        try:
            self.run("cat-file", "-e", f"{sha}^{{commit}}")
        except GitError:
            return False
        return True

    def tracked_files(self) -> list[TrackedFile]:
        """Regular files at HEAD under the prefix. Symlinks and submodules are skipped."""
        if not self.has_commits:
            return []
        args = ["ls-tree", "-r", "--full-tree", "-z", "HEAD"]
        if self.prefix:
            args += ["--", self.prefix]
        files = []
        for entry in self.run(*args).split(b"\0"):
            meta, tab, path = entry.partition(b"\t")
            parts = meta.split()
            # Mode 100644/100755 is a regular file; 120000 is a symlink and
            # 160000 a submodule, neither of which has source of its own.
            if tab and len(parts) == 3 and parts[1] == b"blob" and parts[0].startswith(b"100"):
                files.append(TrackedFile(os.fsdecode(path), parts[2].decode("ascii")))
        return files

    def change_events(self, *, workers: int = HISTORY_WORKERS) -> list[ChangeEvent]:
        """
        One event per (commit, file) for every commit reachable from HEAD,
        newest commit first.

        This is `git log --numstat` split across processes: `git rev-list`
        lists the commits, and `git diff-tree --stdin` diffs them in parallel
        chunks, which is several times faster on a multi-core machine and
        yields exactly the same events. Rename detection (-M) is pinned rather
        than left to the user's diff.renames setting.

        The prefix is applied while parsing, never as a pathspec. A pathspec
        turns on git's history simplification, which drops side-branch
        commits whose net effect vanished in a merge (a change and its
        revert, say), and it cuts rename detection at the boundary, so a
        file moved into the directory looks like it was written from scratch.
        Either way, a file's metrics would depend on the scan's scope.
        """
        if not self.has_commits:
            return []
        # Every commit, newest first, as `git rev-list HEAD` would list them.
        authors = self._authors_from("HEAD")
        self._authors.update(authors)
        commits = [oid.encode("ascii") for oid in authors]
        size = max(_HISTORY_CHUNK, math.ceil(len(commits) / (4 * workers)))
        chunks = [commits[i : i + size] for i in range(0, len(commits), size)]

        def diff(chunk: list[bytes]) -> list[ChangeEvent]:
            raw = self.run(
                "diff-tree", "--stdin", "-r", "-M", "--root", "--numstat", "-z",
                "--no-textconv", "--no-ext-diff",
                "--date=short", _COMMIT_FORMAT,
                stdin=b"\n".join(chunk) + b"\n",
            )  # fmt: skip
            return list(parse_numstat(raw, prefix=self.prefix, authors=authors))

        pool = ThreadPoolExecutor(max_workers=min(workers, len(chunks)))
        try:
            return [event for part in pool.map(diff, chunks) for event in part]
        finally:
            pool.shutdown(cancel_futures=True)  # on error or Ctrl-C, drop queued chunks

    def check_date(self, flag: str, text: str) -> None:
        """
        Refuse a date git can't parse.

        git never rejects a date: anything it can't make sense of ("180d", a
        typo) silently means "now", so a bad --since would match nothing and
        a bad --until everything. A date that resolves to this very moment
        without saying "now" or "today" is taken to be one of those.
        """
        if re.search(r"\b(?:now|today)\b", text, re.IGNORECASE):
            return
        before = time.time()
        resolved = int(self.run("rev-parse", f"--since={text}").decode().strip().split("=")[-1])
        if before - 2 <= resolved <= time.time() + 2:
            raise HeatmapError(
                f"git can't parse {flag} {text!r} (it would quietly mean 'now'). "
                f"Use a date such as 2024-01-01 or '180 days ago'."
            )

    def commits_between(self, *, since: str | None, until: str | None) -> set[str]:
        """
        Commits reachable from HEAD inside a --since/--until window.

        Filtering events by this set selects exactly what `git log --since
        --until` would show, using git's own date parsing and revision walk.
        """
        if not self.has_commits:
            return set()
        args = ["rev-list"]
        if since:
            args.append(f"--since={since}")
        if until:
            args.append(f"--until={until}")
        return set(self.run(*args, "HEAD").decode("ascii").split())

    @contextmanager
    def commit_graph(self) -> Iterator[bool]:
        """
        Speed up path-limited history walks, `git blame` above all, while the
        context is open. Yields whether the speed-up is active.

        Each blame otherwise diffs the trees of every commit in the history
        to find the few that touched its file. A commit-graph with
        changed-path Bloom filters answers "did this commit touch this path?"
        without the diff, making blame several times faster. Repositories
        rarely have one, so it is built for HEAD in a temporary object
        directory that layers over the real one through git's alternates
        mechanism. The repository itself is never written to.
        """
        with tempfile.TemporaryDirectory(prefix="heatmap-") as tmp:
            self._env = self._graph_env(tmp)
            try:
                yield self._env is not None
            finally:
                self._env = None

    def _graph_env(self, odb: str) -> dict[str, str] | None:
        if not self.has_commits:
            return None
        objects = Path(
            self.git_dir,
            os.fsdecode(self.run("rev-parse", "--git-path", "objects").removesuffix(b"\n")),
        )
        if os.pathsep in str(objects):
            return None  # can't be expressed in the alternates list
        alternates = os.pathsep.join(
            filter(None, [str(objects), os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")])
        )
        env = {
            **os.environ,
            "GIT_OBJECT_DIRECTORY": odb,
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": alternates,
        }
        try:
            _git(
                self.root, "commit-graph", "write", "--stdin-commits", "--changed-paths",
                env=env, stdin=f"{self.head}\n".encode(),
            )  # fmt: skip
        except GitError as exc:  # git older than 2.27, or an unusual object store
            log.debug("continuing without a commit-graph: %s", exc)
            return None
        return env

    def read_blobs(
        self, oids: Sequence[str], *, max_bytes: int | None = None
    ) -> Generator[bytes | LargeBlob | None, None, None]:
        """
        Yield each blob's content in order, or None for one git can't find.

        A blob larger than `max_bytes` is never held in memory: it streams
        past, and a LargeBlob with its line count stands in for it. That
        keeps memory flat however big a repository's files are, and a
        hostile repository can pack a huge, highly compressible file into a
        few kilobytes.

        Streams through a single `git cat-file --batch` rather than a process
        per file. Close the generator (or exhaust it) to release the process.
        """
        if not oids:
            return
        proc = subprocess.Popen(
            _command(self.git_dir, "cat-file", "--batch", config=self._config),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_environment(self._env),
            close_fds=False,
        )
        assert proc.stdin is not None and proc.stdout is not None
        # Feed requests from a thread: once git's stdout pipe fills it stops
        # reading stdin, so writing every request up front would deadlock.
        request = "".join(f"{oid}\n" for oid in oids).encode("ascii")
        feeder = threading.Thread(target=_feed, args=(proc.stdin, request), daemon=True)
        feeder.start()
        try:
            for _ in oids:
                header = proc.stdout.readline().split()
                if len(header) != 3:  # "<oid> missing", or EOF if git died
                    yield None
                    continue
                size = int(header[2])
                content: bytes | LargeBlob
                if max_bytes is not None and size > max_bytes:
                    content = _stream_past(proc.stdout, size)
                else:
                    content = proc.stdout.read(size)
                proc.stdout.read(1)  # newline after each blob
                yield content
        finally:
            # Stop git before joining the feeder, which may be blocked on a
            # full pipe if the caller stopped reading early.
            proc.stdout.close()
            if proc.poll() is None:
                proc.terminate()
            proc.wait()
            feeder.join()

    def blame_authors(self, path: str, lines: int | None = None) -> Counter[str] | None:
        """
        Lines at HEAD attributed to each author, or None if git can't blame
        the file. Pass the file's line count as `lines` to have the result
        checked against it.

        Each line of `git blame -s -l` output is a commit id, the old path if
        the line predates a rename, then the line's text. That path isn't
        quoted, so a hostile repository can put a newline and a commit id of
        its choosing in a name it renames from, and each blamed line then
        reads as two. Genuine output has exactly one line per line of the
        file, so a count that doesn't match `lines` is rejected.
        """
        try:
            # -s -l: each line comes back as its commit's full id and its
            # text, with no author or subject. Those come from the history
            # instead (authors_of), so a hostile repository's megabyte-long
            # names never pass through blame. --root: a root commit's lines
            # aren't marked as a boundary.
            # --no-textconv: blame would otherwise pipe file contents through any
            # textconv program the repository's .gitattributes asks for.
            # --no-ignore-revs-file: a blame.ignoreRevsFile setting (often set
            # globally) would make blame fail on every repository without
            # that file, or let the repository choose which commits to skip.
            raw = self.run(
                "blame", "-s", "-l", "--root", "--no-textconv", "--no-ignore-revs-file",
                "HEAD", "--", path,
            )  # fmt: skip
            by_commit = parse_blame(raw)
            if lines is not None and by_commit.total() != lines:
                log.debug("ignoring blame for %s: %d lines, not %d", path, by_commit.total(), lines)
                return None
            authors = self.authors_of(by_commit)
        except GitError:
            return None
        counts: Counter[str] = Counter()
        for oid, n in by_commit.items():
            counts[authors[oid]] += n
        return counts

    def authors_of(self, oids: Iterable[str]) -> dict[str, str]:
        """
        Each commit's author, as the history reports it: mailmap applied, cut
        to AUTHOR_MAX_CHARS. Commits change_events hasn't seen are looked up
        in one batch.
        """
        oids = list(oids)
        missing = [oid for oid in oids if oid not in self._authors]
        if missing:
            stdin = ("\n".join(missing) + "\n").encode("ascii")
            self._authors.update(self._authors_from("--no-walk=unsorted", "--stdin", stdin=stdin))
        return {oid: self._authors.get(oid, "") for oid in oids}

    def _authors_from(self, *args: str, stdin: bytes | None = None) -> dict[str, str]:
        """
        Commit id -> author for the commits `git log ARGS` lists, in its order.

        The output is read as it streams, keeping at most
        _AUTHOR_LINE_MAX_BYTES of each line, so however long a hostile
        repository makes its names, memory holds only the capped ones.
        """
        command = _command(self.git_dir, "log", "--format=%H%x00%aN", *args, config=self._config)
        with tempfile.TemporaryFile() as errors:
            try:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    env=_environment(self._env),
                    close_fds=False,
                )
            except FileNotFoundError:
                raise HeatmapError("git executable not found on PATH") from None
            except OSError as exc:
                raise GitError(f"git log could not start: {exc.strerror or exc}") from exc
            assert proc.stdout is not None
            if stdin is not None:
                assert proc.stdin is not None
                threading.Thread(target=_feed, args=(proc.stdin, stdin), daemon=True).start()
            authors: dict[str, str] = {}
            try:
                for line in _capped_lines(proc.stdout, _AUTHOR_LINE_MAX_BYTES):
                    oid, nul, name = line.partition(b"\0")
                    if nul:
                        authors[oid.decode("ascii")] = _author(name)
            finally:
                proc.stdout.close()
                if proc.poll() is None:
                    proc.terminate()  # only if reading stopped early, on an error
                status = proc.wait()
            if status:
                errors.seek(0)
                raise _failure("log", status, errors.read())
        return authors


def _is_partial_clone(path: Path) -> bool:
    """
    True for a clone made with --filter. Current git marks one by setting
    remote.<name>.promisor; older git set extensions.partialClone instead.
    """
    try:
        found = _git(
            path, "config", "--get-regexp", r"^(extensions\.partialclone|remote\..+\.promisor)$"
        )
    except GitError:
        return False  # exit status 1: no such keys
    for line in found.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition(" ")
        if key == "extensions.partialclone" or value.lower() in ("true", "yes", "on", "1"):
            return True
    return False


def _stream_past(stream: IO[bytes], size: int) -> LargeBlob:
    """Consume one blob of `size` bytes in chunks, counting lines as `count_lines` would."""
    lines, remaining, binary, last = 0, size, False, b""
    while remaining:
        chunk = stream.read(min(remaining, 1 << 20))
        if not chunk:
            break
        if remaining == size:
            binary = b"\0" in chunk[:8192]
        lines += chunk.count(b"\n")
        last = chunk[-1:]
        remaining -= len(chunk)
    if size and last != b"\n":
        lines += 1
    return LargeBlob(size, lines, binary)


def _feed(pipe: BinaryIO, data: bytes) -> None:
    try:
        pipe.write(data)
        pipe.close()
    except OSError:
        pass  # git exited early; the reader sees EOF


_HEADER_RE = re.compile(rb"@@([0-9a-f]+)\|([^|]*)\|([^\n]*)(?:\n(.*))?\Z", re.DOTALL)
_NUMSTAT_RE = re.compile(rb"(-|\d+)\t(-|\d+)\t(.*)\Z", re.DOTALL)


def parse_numstat(
    raw: bytes, *, prefix: str | None = None, authors: Mapping[str, str] | None = None
) -> Iterator[ChangeEvent]:
    """
    Parse `git log --numstat -z` (or `git diff-tree --stdin`) output into ChangeEvents.

    With -z every path is NUL-terminated and never quoted, and a rename is
    three fields, "added<TAB>deleted<TAB>", old path, new path, instead of the
    human "old => new" form, which is ambiguous when a filename itself
    contains " => ". A commit's header shares a field with its first numstat
    entry; a commit with no file changes (a merge) is a bare header.
    A binary change (counts of "-") is kept as a change of zero lines, so the
    commit still counts. Renames are attributed to the destination path.
    With `authors`, each commit's author comes from it rather than the header.
    """
    tokens = raw.split(b"\0")
    commit: tuple[str, str, str] | None = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        header = _HEADER_RE.match(token)
        if header:
            sha, day, author, token = header.groups()
            oid = sha.decode("ascii")
            name = _author(author) if authors is None else authors.get(oid, "")
            commit = (oid, day.decode("ascii", "replace"), name)
            if not token:
                continue
        stat = _NUMSTAT_RE.match(token)
        if stat is None or commit is None:
            continue
        added, deleted, path_bytes = stat.groups()
        if not path_bytes:
            if i + 1 >= len(tokens):
                break
            path_bytes = tokens[i + 1]
            i += 2
        if added == b"-" or deleted == b"-":
            added = deleted = b"0"  # binary: the commit counts, its lines can't be
        path = os.fsdecode(path_bytes)
        if prefix and not (path == prefix or path.startswith(prefix + "/")):
            continue
        yield ChangeEvent(commit[0], commit[1], commit[2], path, int(added), int(deleted))


def _author(name: bytes) -> str:
    """An author name as git printed it, cut to AUTHOR_MAX_CHARS characters."""
    text = name.decode("utf-8", "replace")
    return text if len(text) <= AUTHOR_MAX_CHARS else text[: AUTHOR_MAX_CHARS - 2] + ".."


def _capped_lines(stream: IO[bytes], limit: int) -> Iterator[bytes]:
    """Lines from `stream`, without their newline, each cut to `limit` bytes."""
    while line := stream.readline(limit + 1):
        if line.endswith(b"\n"):
            yield line[:-1][:limit]
            continue
        yield line[:limit]
        while (rest := stream.readline(1 << 16)) and not rest.endswith(b"\n"):
            pass  # the overlong rest of the line, discarded


_OID_RE = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?")
# A full SHA-1 or SHA-256 object id, as text.
COMMIT_ID_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def parse_blame(raw: bytes) -> Counter[str]:
    """
    Count lines per commit in `git blame -s -l` output.

    Each line of the file comes back as "<commit id> [<old path>] <n>) <text>",
    so the id is everything before the first space. Split on newlines only:
    a file's text can contain a bare carriage return.
    """
    counts: Counter[str] = Counter()
    for line in raw.split(b"\n"):
        oid = line.partition(b" ")[0].removeprefix(b"^")
        if _OID_RE.fullmatch(oid):
            counts[oid.decode("ascii")] += 1
    return counts
