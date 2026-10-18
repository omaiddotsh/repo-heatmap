"""repo_heatmap.git: opening repositories, listing files, parsing history and blame."""

from __future__ import annotations

import io
import os
import subprocess
from datetime import date
from pathlib import Path

import pytest

from repo_heatmap import git as git_module
from repo_heatmap.errors import HeatmapError
from repo_heatmap.git import GitError, GitRepo, git_executable, parse_blame, parse_numstat
from repo_heatmap.pipeline import ScanOptions, scan

Change = tuple[str, str, "str | tuple[str, str]"]


def numstat(*commits: tuple[str, str, str, list[Change]]) -> bytes:
    """
    Build `git log --numstat -z` output. Each change is (added, deleted, path),
    or (added, deleted, (old, new)) for a rename.
    """
    out = b""
    for sha, day, author, changes in commits:
        out += f"@@{sha}|{day}|{author}".encode()
        if changes:
            out += b"\n"
        for added, deleted, path in changes:
            out += f"{added}\t{deleted}\t".encode()
            if isinstance(path, tuple):
                out += b"\0" + path[0].encode() + b"\0" + path[1].encode() + b"\0"
            else:
                out += path.encode() + b"\0"
        out += b"\0"
    return out


# --- parse_numstat --------------------------------------------------------------


def test_parse_numstat_reads_each_commit_and_keeps_binary_as_zero_lines() -> None:
    raw = numstat(
        ("aaa", "2024-01-15", "alice", [("10", "2", "src/app.py"), ("-", "-", "logo.png")]),
        ("bbb", "2024-01-16", "bob", [("3", "1", "src/app.py")]),
    )
    events = [
        (e.sha, e.date, e.author, e.path, e.additions, e.deletions) for e in parse_numstat(raw)
    ]
    assert events == [
        ("aaa", "2024-01-15", "alice", "src/app.py", 10, 2),
        ("aaa", "2024-01-15", "alice", "logo.png", 0, 0),  # the commit still counts
        ("bbb", "2024-01-16", "bob", "src/app.py", 3, 1),
    ]


def test_parse_numstat_attributes_rename_to_destination() -> None:
    raw = numstat(("aaa", "2024-01-15", "alice", [("3", "1", ("old.py", "new.py"))]))
    assert [e.path for e in parse_numstat(raw)] == ["new.py"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (("1", "0", "a => b.py"), "a => b.py"),  # literal arrow is not a rename
        (("1", "0", ("a => b.py", "c.py")), "c.py"),  # rename whose source has an arrow
        (("1", "0", "src/été.py"), "src/été.py"),  # not C-quoted
        (("1", "0", 'quo"te.py'), 'quo"te.py'),
    ],
)
def test_parse_numstat_takes_paths_verbatim(change: Change, expected: str) -> None:
    raw = numstat(("aaa", "2024-01-15", "alice", [change]))
    assert [e.path for e in parse_numstat(raw)] == [expected]


def test_parse_numstat_author_may_contain_separator() -> None:
    raw = numstat(("aaa", "2024-01-15", "Ann | Ops", [("1", "0", "a.py")]))
    assert next(parse_numstat(raw)).author == "Ann | Ops"


def test_parse_numstat_commit_without_files_does_not_leak_into_the_next() -> None:
    raw = numstat(
        ("aaa", "2024-01-01", "merger", []),
        ("bbb", "2024-01-02", "bob", [("1", "0", "a.py")]),
    )
    assert [(e.sha, e.author) for e in parse_numstat(raw)] == [("bbb", "bob")]


def test_parse_numstat_prefix_matches_whole_directory_names() -> None:
    raw = numstat(
        (
            "aaa",
            "2024-01-01",
            "a",
            [("1", "0", "src/a.py"), ("1", "0", "srcx/b.py"), ("1", "0", "lib/c.py")],
        )
    )
    assert [e.path for e in parse_numstat(raw, prefix="src")] == ["src/a.py"]


# --- GitRepo.open -----------------------------------------------------------------


def test_open_maps_subdirectory_to_root_and_prefix(repo) -> None:
    repo.write("src/mod/a.py")
    repo.commit()
    opened = GitRepo.open(repo.root / "src" / "mod")
    assert opened.root == repo.root.resolve()
    assert opened.prefix == "src/mod"
    assert GitRepo.open(repo.root).prefix is None


def test_open_rejects_a_directory_outside_any_repository(tmp_path) -> None:
    with pytest.raises(HeatmapError, match="Not a git repository"):
        GitRepo.open(tmp_path)


def test_open_rejects_a_missing_path(tmp_path) -> None:
    with pytest.raises(HeatmapError, match="does not exist"):
        GitRepo.open(tmp_path / "nope")


def test_bare_repository_is_read_from_head(repo, tmp_path) -> None:
    repo.write("src/a.py", "a\nb\n")
    repo.commit()
    bare = tmp_path / "mirror.git"
    repo.git("clone", "-q", "--bare", str(repo.root), str(bare))

    opened = GitRepo.open(bare)
    assert opened.prefix is None
    assert [f.path for f in opened.tracked_files()] == ["src/a.py"]
    assert [e.path for e in opened.change_events()] == ["src/a.py"]
    assert opened.blame_authors("src/a.py") == {"Alice": 2}


# --- tracked files and history ------------------------------------------------------


def test_empty_repository_has_no_files_or_history(repo) -> None:
    opened = GitRepo.open(repo.root)
    assert not opened.has_commits
    assert opened.tracked_files() == []
    assert opened.change_events() == []


def test_has_commit_knows_this_repositorys_commits(repo, make_repo) -> None:
    repo.write("a.py")
    first = repo.commit()
    other = make_repo("other")
    other.write("b.py")
    foreign = other.commit(author="Bob")
    opened = GitRepo.open(repo.root)
    assert opened.has_commit(first)
    assert not opened.has_commit(foreign)
    tree = repo.git("rev-parse", "HEAD^{tree}").strip()
    assert not opened.has_commit(tree)  # an object, but not a commit
    for bad in ("HEAD", "--help", first[:12], first.upper()):  # only full ids reach git
        assert not opened.has_commit(bad)


def test_tracked_files_are_root_relative_when_scoped(repo) -> None:
    repo.write("src/a.py")
    repo.write("top.py")
    repo.commit()
    scoped = GitRepo.open(repo.root / "src")
    assert [f.path for f in scoped.tracked_files()] == ["src/a.py"]
    assert {e.path for e in scoped.change_events()} == {"src/a.py"}


def test_tracked_files_skip_symlinks(repo) -> None:
    repo.write("real.py")
    os.symlink("real.py", repo.root / "link.py")
    repo.commit()
    assert [f.path for f in GitRepo.open(repo.root).tracked_files()] == ["real.py"]


def test_tracked_files_keep_awkward_names(repo) -> None:
    names = ['quo"te.py', "back\\slash.py", "sp ace.py", "été.py"]
    for name in names:
        repo.write(name)
    repo.commit()
    assert sorted(f.path for f in GitRepo.open(repo.root).tracked_files()) == sorted(names)


def test_change_events_follow_a_rename(repo) -> None:
    repo.write("old.py", "".join(f"line {i}\n" for i in range(20)))
    repo.commit("add")
    repo.git("mv", "old.py", "new.py")
    repo.commit("rename")
    paths = [e.path for e in GitRepo.open(repo.root).change_events()]
    assert paths == ["new.py", "old.py"]  # newest first; the rename is a 0/0 change to new.py


def test_change_events_do_not_depend_on_scope(repo) -> None:
    # Moving a file into the scanned directory must not read as writing it
    # from scratch, which is what a pathspec-limited `git log` reports.
    repo.write("lib/a.py", "".join(f"line {i}\n" for i in range(50)))
    repo.commit("add")
    repo.git("mv", "lib", "src")
    repo.commit("move")
    whole = [e for e in GitRepo.open(repo.root).change_events() if e.path.startswith("src/")]
    scoped = GitRepo.open(repo.root / "src").change_events()
    assert scoped == whole
    assert sum(e.additions for e in scoped) == 0


def test_commits_between_selects_the_window(repo) -> None:
    repo.write("a.py", "1\n")
    repo.commit(day="2023-01-01")
    repo.write("a.py", "2\n")
    recent = repo.commit(day="2024-06-01")
    opened = GitRepo.open(repo.root)
    assert opened.commits_between(since="2024-01-01", until=None) == {recent}
    assert opened.commits_between(since="2030-01-01", until=None) == set()


def _events(events) -> list[tuple]:
    return sorted((e.sha, e.date, e.author, e.path, e.additions, e.deletions) for e in events)


@pytest.fixture
def busy_history(repo):
    """Root commit, edits by two authors, a rename, a binary file, and a merge."""
    repo.write("src/app.py", "".join(f"line {i}\n" for i in range(30)))
    repo.write("logo.png", b"\x89PNG\x00\x01")
    repo.commit("root", author="Alice", day="2024-01-01")
    repo.git("checkout", "-q", "-b", "side")
    repo.write("src/side.py", "s = 1\n")
    repo.commit("side", author="Bob", day="2024-01-02")
    repo.git("checkout", "-q", "main")
    repo.write("src/app.py", "".join(f"line {i}\n" for i in range(31)))
    repo.commit("edit", author="Bob", day="2024-01-03")
    repo.git("-c", "user.name=Alice", "-c", "user.email=a@example.com",
             "merge", "-q", "--no-ff", "-m", "merge", "side")  # fmt: skip
    repo.git("mv", "src/app.py", "src/main.py")
    repo.write("logo.png", b"\x89PNG\x00\x02")
    repo.commit("rename", author="Alice", day="2024-01-04")
    return repo


@pytest.mark.parametrize("chunk", [1, 500])
def test_change_events_match_git_log(busy_history, monkeypatch, chunk) -> None:
    # The parallel diff-tree reader must produce exactly what `git log
    # --numstat` would, whether history is split into many chunks or one.
    monkeypatch.setattr(git_module, "_HISTORY_CHUNK", chunk)
    opened = GitRepo.open(busy_history.root)
    raw = opened.run("log", "--numstat", "-z", "--date=short", "--pretty=format:@@%H|%ad|%aN")
    events = opened.change_events(workers=3)
    assert _events(events) == _events(parse_numstat(raw))
    assert [e.date for e in events] == sorted((e.date for e in events), reverse=True)


def test_shallow_clone_is_refused(busy_history, tmp_path) -> None:
    shallow = tmp_path / "shallow"
    busy_history.git("clone", "-q", "--depth", "1", f"file://{busy_history.root}", str(shallow))
    with pytest.raises(HeatmapError, match="shallow clone"):
        GitRepo.open(shallow)


def test_partial_clone_is_refused(busy_history, tmp_path) -> None:
    busy_history.git("config", "uploadpack.allowFilter", "true")
    partial = tmp_path / "partial"
    busy_history.git(
        "clone", "-q", "--filter=blob:none", f"file://{busy_history.root}", str(partial)
    )
    with pytest.raises(HeatmapError, match="partial clone"):
        GitRepo.open(partial)


def test_repository_owned_by_someone_else_explains_itself(repo, monkeypatch) -> None:
    repo.write("a.py")
    repo.commit()
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")  # git's own test hook
    try:
        GitRepo.open(repo.root)
    except HeatmapError as exc:
        assert "another user owns it" in str(exc)
        assert "safe.directory" in str(exc)
    else:
        pytest.skip("this git build ignores GIT_TEST_ASSUME_DIFFERENT_OWNER")


def test_git_failure_reports_gits_own_message(repo) -> None:
    repo.write("a.py", "1\n")
    repo.commit()
    repo.write("a.py", "2\n")
    repo.commit()
    blob = repo.git("rev-parse", "HEAD~1:a.py").strip()
    loose = repo.root / ".git" / "objects" / blob[:2] / blob[2:]
    loose.chmod(0o644)
    loose.write_bytes(b"corrupt")
    with pytest.raises(GitError, match=r"git diff-tree failed: .+"):
        GitRepo.open(repo.root).change_events()


def _snapshot(git_dir: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(git_dir)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in git_dir.rglob("*")
        if p.is_file()
    }


def test_commit_graph_speeds_blame_without_touching_the_repo(busy_history) -> None:
    opened = GitRepo.open(busy_history.root)
    before = _snapshot(busy_history.root / ".git")
    plain = opened.blame_authors("src/main.py")
    with opened.commit_graph() as active:
        assert active
        assert opened.blame_authors("src/main.py") == plain
        assert opened.change_events() == GitRepo.open(busy_history.root).change_events()
    assert opened.run("rev-parse", "HEAD")  # back to plain git afterwards
    assert _snapshot(busy_history.root / ".git") == before


def test_commit_graph_is_a_no_op_without_commits(repo) -> None:
    with GitRepo.open(repo.root).commit_graph() as active:
        assert not active


def test_read_blobs_streams_in_order(repo) -> None:
    repo.write("a.py", "aaa\n")
    repo.write("b.py", b"\x00binary")
    repo.commit()
    opened = GitRepo.open(repo.root)
    oids = [f.oid for f in opened.tracked_files()]
    blobs = list(opened.read_blobs([oids[1], "0" * 40, oids[0]]))
    assert blobs == [b"\x00binary", None, b"aaa\n"]


def test_read_blobs_streams_large_blobs_without_holding_them(repo) -> None:
    from repo_heatmap.collectors.complexity import count_lines
    from repo_heatmap.git import LargeBlob

    contents = [b"a\nb\nc\nd\ne", b"x" * 50 + b"\n" * 3, b"\x00binary" * 10, b"small\n"]
    for i, c in enumerate(contents):
        repo.write(f"f{i}.py", c)
    repo.commit()
    opened = GitRepo.open(repo.root)
    oids = {f.path: f.oid for f in opened.tracked_files()}
    blobs = list(opened.read_blobs([oids[f"f{i}.py"] for i in range(4)], max_bytes=8))
    assert blobs[3] == b"small\n"  # under the limit: the bytes themselves
    for content, blob in zip(contents[:3], blobs[:3], strict=True):
        assert isinstance(blob, LargeBlob)
        assert (blob.size, blob.lines, blob.binary) == (
            len(content),
            count_lines(content),
            b"\x00" in content,
        )


def test_a_failure_to_start_git_is_a_git_error(repo, monkeypatch) -> None:
    # e.g. a hostile file name longer than the OS allows for one argument
    repo.write("a.py")
    repo.commit()
    opened = GitRepo.open(repo.root)

    def too_long(*args, **kwargs):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr("repo_heatmap.git.subprocess.run", too_long)
    with pytest.raises(GitError, match="could not start: Argument list too long"):
        opened.run("rev-parse", "HEAD")
    assert opened.blame_authors("a.py") is None


def test_open_reports_unreadable_paths_cleanly(tmp_path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    with pytest.raises(HeatmapError, match="cannot read"):
        GitRepo.open(loop)


def test_read_blobs_can_be_abandoned_early(repo) -> None:
    for i in range(50):
        repo.write(f"f{i}.py", "x" * 100_000)
    repo.commit()
    opened = GitRepo.open(repo.root)
    blobs = opened.read_blobs([f.oid for f in opened.tracked_files()])
    assert len(next(blobs)) == 100_000
    blobs.close()  # must not hang on git's full stdout pipe


def test_blame_counts_lines_per_author(repo) -> None:
    repo.write("f.py", "a\nb\nc\nd\n")
    repo.commit(author="Alice")
    repo.write("f.py", "a\nB\nc\nd\n")  # Bob's line splits Alice's into two blame groups
    repo.commit(author="Bob")
    assert GitRepo.open(repo.root).blame_authors("f.py") == {"Alice": 3, "Bob": 1}


def test_parse_blame_counts_lines_per_commit() -> None:
    a, b = "a" * 40, "b" * 64  # SHA-1 and SHA-256 ids
    raw = (
        f"{a} 1) def f():\n"
        f"^{a} 2)     pass\n"  # a boundary mark is tolerated
        f"{b} old/name.py 3) x = 1\r  # a bare CR stays inside its line\n"
    ).encode()
    assert parse_blame(raw) == {a: 2, b: 1}


@pytest.mark.parametrize(
    "name",
    ["A" * 5000, "A" + "\u0301" * 5000, "B" + "\u200b" * 5000],  # zero-width: no columns
    ids=["ascii", "combining", "zero-width"],
)
def test_huge_author_names_are_cut_short(repo, name) -> None:
    # A megabyte-long name costs a hostile repository almost nothing to
    # store, but would be printed for every commit. History and blame cut it.
    repo.write("a.py", "x\n")
    repo.commit(author=name)
    opened = GitRepo.open(repo.root)
    (event,) = opened.change_events()
    assert len(event.author) == 128 and event.author.endswith("..")
    assert opened.blame_authors("a.py") == {event.author: 1}
    assert GitRepo.open(repo.root).blame_authors("a.py") == {event.author: 1}  # looked up


def test_blame_credits_lines_a_merge_wrote_to_its_author(repo) -> None:
    repo.write("a.py", "one\n")
    repo.commit(author="Alice")
    repo.git("checkout", "-q", "-b", "side")
    repo.write("b.py", "b\n")
    repo.commit(author="Bob")
    repo.git("checkout", "-q", "main")
    repo.write("c.py", "c\n")
    repo.commit(author="Alice")
    identity = ("-c", "user.name=Mallory", "-c", "user.email=mallory@example.com")
    repo.git(*identity, "merge", "-q", "--no-ff", "--no-commit", "side")
    repo.write("a.py", "one\nwritten in the merge\n")
    repo.commit(author="Mallory")  # concludes the merge
    opened = GitRepo.open(repo.root)
    assert {e.author for e in opened.change_events()} == {"Alice", "Bob"}  # merges are invisible
    assert opened.blame_authors("a.py") == {"Alice": 1, "Mallory": 1}


@pytest.mark.parametrize("fake", ["real commit", "missing object"])
def test_a_renamed_path_cannot_inject_blame_lines(repo, fake) -> None:
    # blame prints a pre-rename path unquoted, so a newline and a commit id
    # in that path would turn each blamed line into two.
    repo.write("decoy.py", "d\n")
    decoy = repo.commit(author="Decoy")
    oid = decoy if fake == "real commit" else "0" * 39 + "1"
    blob = repo.git_bytes("hash-object", "-w", "--stdin", stdin=b"line\n" * 10).decode().strip()
    old = f"x\n{oid} y.py"
    repo.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{old}")
    repo.commit(author="Mallory", stage=False)
    repo.git("update-index", "--force-remove", old)
    repo.git("update-index", "--add", "--cacheinfo", f"100644,{blob},z.py")
    repo.commit(author="Mallory", stage=False)
    opened = GitRepo.open(repo.root)
    assert opened.blame_authors("z.py", 10) is None  # caught by the line count
    unchecked = opened.blame_authors("z.py")
    assert unchecked is None or unchecked.total() > 10  # what the check exists for
    result = scan(repo.root, ScanOptions(today=date(2024, 6, 1)))
    z = result.find("z.py")
    assert z is not None and z.metrics.activity.ownership_concentration == 100.0  # commit share


def test_capped_lines_cuts_long_lines_and_drops_the_rest() -> None:
    stream = io.BytesIO(b"short\n" + b"x" * 100 + b"\nnext\nlast")
    assert list(git_module._capped_lines(stream, 10)) == [b"short", b"x" * 10, b"next", b"last"]


def test_replace_refs_cannot_disguise_content_or_history(repo) -> None:
    # A mirror clone fetches refs/replace/*, which can swap a file's content
    # for a decoy or cut its history short. heatmap reads the real objects.
    repo.write("evil.c", "int f(int x) { return x; }\n")
    repo.commit(author="Alice")
    repo.write("evil.c", "int f(int x) { if (x) return 1; return 0; }\n")
    repo.commit(author="Bob")
    real = repo.git("rev-parse", "HEAD:evil.c").strip()
    decoy = repo.git_bytes("hash-object", "-w", "--stdin", stdin=b"int f(void);\n")
    repo.git("replace", real, decoy.decode().strip())
    repo.git("replace", "--graft", "HEAD")  # HEAD now looks like the first commit
    opened = GitRepo.open(repo.root)
    (tracked,) = opened.tracked_files()
    assert next(opened.read_blobs([tracked.oid])).startswith(b"int f(int x) { if")
    assert len(opened.change_events()) == 2
    assert opened.blame_authors("evil.c") == {"Bob": 1}


def test_an_oversized_mailmap_is_ignored(repo, monkeypatch, caplog) -> None:
    monkeypatch.setattr(git_module, "MAILMAP_MAX_BYTES", 10)
    repo.write(".mailmap", "Alice <bob@example.com>\n")
    repo.write("a.py")
    repo.commit(author="Bob")
    opened = GitRepo.open(repo.root)
    assert ".mailmap is 24 bytes, over the 10-byte limit" in caplog.text
    assert {e.author for e in opened.change_events()} == {"Bob"}
    assert opened.blame_authors("a.py") == {"Bob": 1}


def test_decomposed_unicode_names_are_blamed(repo) -> None:
    # macOS sets core.precomposeUnicode, which would rewrite this name to
    # its composed form when it's passed back to git, so blame couldn't find it.
    name = "cafe\u0301.py"
    blob = repo.git_bytes("hash-object", "-w", "--stdin", stdin=b"x\n").decode().strip()
    repo.git("config", "core.precomposeUnicode", "false")  # store the name as given
    repo.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    repo.commit(stage=False)
    repo.git("config", "core.precomposeUnicode", "true")
    opened = GitRepo.open(repo.root)
    assert [f.path for f in opened.tracked_files()] == [name]
    assert opened.blame_authors(name) == {"Alice": 1}


def test_repository_and_scope_paths_may_contain_newlines(make_repo) -> None:
    repo = make_repo("odd\nrepo")
    repo.write("sub\ndir/a.py")
    repo.commit()
    opened = GitRepo.open(repo.root / "sub\ndir")
    assert opened.root == repo.root.resolve()
    assert opened.prefix == "sub\ndir"
    assert [f.path for f in opened.tracked_files()] == ["sub\ndir/a.py"]


def test_git_executable_is_the_same_git(repo) -> None:
    exe = git_executable()
    assert os.path.isabs(exe)
    same = subprocess.run([exe, "--version"], capture_output=True, text=True, check=True)
    shim = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
    assert same.stdout == shim.stdout


def test_hostile_attributes_cannot_run_textconv_programs(repo, tmp_path) -> None:
    # A repository's .gitattributes can route files to a textconv program from
    # git config. `git blame` runs those by default; heatmap must not.
    marker = tmp_path / "textconv-ran"
    repo.write(".gitattributes", "*.py diff=probe\n")
    repo.write("a.py", "a\nb\n")
    repo.commit()
    repo.write("a.py", "a\nc\n")
    repo.commit(author="Bob")
    repo.git("config", "diff.probe.textconv", f"sh -c 'touch {marker}; cat \"$0\"'")
    opened = GitRepo.open(repo.root)
    opened.change_events()
    assert opened.blame_authors("a.py") == {"Alice": 1, "Bob": 1}
    assert not marker.exists()


def test_uncommitted_mailmap_and_attributes_change_nothing(repo) -> None:
    # Results depend on the commit only: an edited, uncommitted .mailmap is
    # ignored and the committed one applies. .gitattributes are ignored
    # entirely, committed or not (see the next test).
    repo.write(".mailmap", "Alice <al@example.com> Al <al@example.com>\n")
    repo.write(".gitattributes", "*.dat binary\n")
    repo.write("a.py", "a\nb\n")
    repo.write("b.dat", "x\n")
    repo.commit(author="Al")
    repo.write("a.py", "a\nc\n")
    repo.write("b.dat", "y\n")
    repo.commit(author="Bob")
    opened = GitRepo.open(repo.root)
    before = (opened.change_events(), opened.blame_authors("a.py"))
    assert {e.author for e in before[0]} == {"Bob", "Alice"}  # committed mailmap applied

    repo.write(".mailmap", "Alice <al@example.com> Bob <bob@example.com>\n")
    repo.write(".gitattributes", "*.py binary\n")
    assert (opened.change_events(), opened.blame_authors("a.py")) == before


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("layout", ["clone", "mirror", "git-before-2.43"])
def test_attributes_cannot_hide_a_files_history(
    tmp_path, make_repo, monkeypatch, object_format, layout
) -> None:
    # One committed line marking a file binary or -diff must not erase its
    # line counts, and a file git really sees as binary still has commits.
    # That holds in either object format, in a bare mirror, with
    # GIT_ATTR_SOURCE in the environment, and on git that ignores attr.tree.
    repo = make_repo("repo", object_format)
    repo.write(".gitattributes", "auth.py -diff\nsecret.dat binary\n")
    repo.write("logo.bin", b"\x00\x01")
    for i in range(3):
        repo.write("auth.py", "".join(f"check_{j} = {i}\n" for j in range(5)))
        repo.write("secret.dat", f"key = {i}\n")
        repo.write("logo.bin", bytes([0, i + 2]))
        repo.commit(author=f"Dev{i}")
    path = repo.root
    if layout == "mirror":
        path = tmp_path / "mirror.git"
        repo.git("clone", "-q", "--mirror", str(repo.root), str(path))
    monkeypatch.setenv("GIT_ATTR_SOURCE", "HEAD")
    opened = GitRepo.open(path)
    if layout == "git-before-2.43":
        opened._config = ()  # only the missing index keeps the attributes out
    events = opened.change_events()
    by_path = {p: [e for e in events if e.path == p] for p in ("auth.py", "secret.dat", "logo.bin")}
    assert len(by_path["auth.py"]) == 3 and sum(e.additions for e in by_path["auth.py"]) == 15
    assert len(by_path["secret.dat"]) == 3 and by_path["secret.dat"][0].additions == 1
    assert len(by_path["logo.bin"]) == 3  # real binary: counted, with zero lines
    assert all(e.additions == e.deletions == 0 for e in by_path["logo.bin"])


def test_user_git_config_does_not_change_results(repo) -> None:
    # Settings people commonly have in their own config must not leak in.
    repo.write("a.py", "".join(f"line {i}\n" for i in range(20)))
    repo.commit(author="Alice")
    repo.git("mv", "a.py", "b.py")
    repo.write("b.py", "".join(f"line {i}\n" for i in range(21)))
    repo.commit(author="Bob")
    opened = GitRepo.open(repo.root)
    clean = (opened.change_events(), opened.blame_authors("b.py"))
    repo.git("config", "blame.ignoreRevsFile", ".git-blame-ignore-revs")  # file doesn't exist
    repo.git("config", "diff.renameLimit", "1")
    repo.git("config", "diff.algorithm", "histogram")
    repo.git("config", "i18n.logOutputEncoding", "ISO-8859-1")
    repo.git("config", "diff.indentHeuristic", "false")
    repo.git("config", "core.bigFileThreshold", "1")  # every file "binary"
    attributes = repo.root.parent / "attributes"
    attributes.write_text("*.py binary\n")
    repo.git("config", "core.attributesFile", str(attributes))
    mailmap = repo.root.parent / "mailmap"
    mailmap.write_text("Mallory <bob@example.com>\n")
    repo.git("config", "mailmap.file", str(mailmap))
    repo.git("config", "blame.blankBoundary", "true")
    repo.git("config", "blame.coloring", "repeatedLines")
    fresh = GitRepo.open(repo.root)
    assert (fresh.change_events(), fresh.blame_authors("b.py")) == clean


def test_paths_that_are_not_utf8_keep_their_identity(repo) -> None:
    # Two names differing only in an invalid byte must stay two files, and
    # each must be blamed under its real name.
    for raw, lines in ((b"src/a\xff.py", 1), (b"src/a\xfe.py", 10)):
        blob = repo.git_bytes("hash-object", "-w", "--stdin", stdin=b"x\n" * lines).strip()
        repo.git(
            "update-index", "--add", "--cacheinfo", f"100644,{blob.decode()},{os.fsdecode(raw)}"
        )
    repo.commit(stage=False)  # the names can't exist on every file system, only in git
    opened = GitRepo.open(repo.root)
    paths = [f.path for f in opened.tracked_files()]
    assert len(set(paths)) == 2
    assert {opened.blame_authors(p).total() for p in paths} == {1, 10}


def test_scoped_directory_names_are_not_pathspec_magic(repo) -> None:
    repo.write(":src/real.py")
    repo.write("src/decoy.py")
    repo.commit()
    scoped = GitRepo.open(repo.root / ":src")
    assert [f.path for f in scoped.tracked_files()] == [":src/real.py"]


def test_linked_worktree_is_read_at_its_own_head(repo, tmp_path) -> None:
    repo.write("a.py", "1\n")
    first = repo.commit()
    repo.write("b.py", "2\n")
    repo.commit()
    linked = tmp_path / "linked"
    repo.git("worktree", "add", "-q", "--detach", str(linked), first)
    opened = GitRepo.open(linked)
    assert opened.head == first
    assert [f.path for f in opened.tracked_files()] == ["a.py"]
    assert {e.sha for e in opened.change_events()} == {first}


@pytest.mark.parametrize("text", ["180d", "garbage", "asdf qwerty", "fivedays"])
def test_unparseable_dates_are_refused(repo, text) -> None:
    repo.write("a.py")
    repo.commit()
    with pytest.raises(HeatmapError, match="can't parse --since"):
        GitRepo.open(repo.root).check_date("--since", text)


@pytest.mark.parametrize("text", ["2024-01-01", "180 days ago", "last month", "now", "today"])
def test_real_dates_are_accepted(repo, text) -> None:
    repo.write("a.py")
    repo.commit()
    GitRepo.open(repo.root).check_date("--since", text)


def test_blame_of_a_missing_file_is_none(repo) -> None:
    repo.write("a.py")
    repo.commit()
    assert GitRepo.open(repo.root).blame_authors("missing.py") is None


def test_git_runs_in_the_c_locale(repo, monkeypatch) -> None:
    # git translates its messages; the error branches match the English text,
    # and LC_ALL beats LANGUAGE/LANG.
    env = git_module._environment(
        {"LANG": "de_DE.UTF-8", "LANGUAGE": "de", "LC_ALL": "de_DE.UTF-8"}
    )
    assert env["LC_ALL"] == "C"
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANGUAGE", "de")
    with pytest.raises(HeatmapError, match="Not a git repository"):
        GitRepo.open(repo.root.parent)


def test_head_branch_and_branches(repo) -> None:
    repo.write("a.py")
    repo.commit()
    g = GitRepo.open(repo.root)
    assert g.head_branch == "main"
    assert g.branches() == ["main"]
    repo.git("checkout", "-q", "--orphan", "fresh")
    g = GitRepo.open(repo.root)
    assert not g.has_commits
    assert g.head_branch == "fresh"
    assert g.branches() == ["main"]
