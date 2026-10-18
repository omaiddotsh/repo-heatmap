"""repo_heatmap.cli: the scan and explain commands, end to end against real repositories."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from repo_heatmap import __version__
from repo_heatmap.cli import main

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def project(repo):
    repo.write("src/app.py", "def f(x):\n    if x:\n        return 1\n    return 2\n")
    repo.write("src/util.py", "def g():\n    return 1\n")
    repo.write("tests/test_app.py", "def test(): pass\n")
    repo.write("config/app.yaml", "debug: false\n")  # scored, though lizard can't parse it
    repo.write("README.md", "# demo\n")  # documentation: left out by default
    repo.commit(author="Alice", day="2024-01-01")
    repo.write("src/app.py", "def f(x):\n    if x:\n        return 3\n    return 2\n")
    repo.commit(author="Bob", day="2024-02-01")
    return repo


def test_scan_writes_all_three_reports(project, tmp_path, capsys) -> None:
    out = tmp_path / "out"
    assert main(["scan", str(project.root), "--output", str(out)]) == 0
    assert "Scored 3 files" in capsys.readouterr().err
    rows = json.loads((out / "risk_report.json").read_text())["files"]
    assert [r["path"] for r in rows] == ["src/app.py", "config/app.yaml", "src/util.py"]
    assert (out / "risk_report.csv").read_text().startswith("path,risk_score,")
    assert "`src/app.py`" in (out / "summary.md").read_text()


def test_scan_writes_the_hottest_files_list(project, tmp_path) -> None:
    out = tmp_path / "out"
    assert main(["scan", str(project.root), "-o", str(out), "-q"]) == 0
    # The default is the top 20%: one of three files, rounded up.
    assert (out / "hot_files.txt").read_text() == "src/app.py\n"
    assert "## Top 1 files by risk score (the top 20% of 3)" in (out / "summary.md").read_text()

    assert main(["scan", str(project.root), "-o", str(out), "-q", "--top", "2"]) == 0
    assert (out / "hot_files.txt").read_text() == "src/app.py\nconfig/app.yaml\n"
    assert "## Top 2 files by risk score\n" in (out / "summary.md").read_text()

    assert main(["scan", str(project.root), "-o", str(out), "-q", "--top", "100%"]) == 0
    assert (out / "hot_files.txt").read_text().splitlines() == [
        "src/app.py",
        "config/app.yaml",
        "src/util.py",
    ]


@pytest.mark.parametrize("bad", ["0", "0%", "101%", "x%", "-1", "many"])
def test_scan_refuses_a_bad_top(project, tmp_path, capsys, bad) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["scan", str(project.root), "-o", str(tmp_path / "o"), "--top", bad])
    assert exc.value.code == 2
    assert "--top" in capsys.readouterr().err


def test_scan_of_a_subdirectory_keeps_root_relative_paths(project, tmp_path) -> None:
    out = tmp_path / "out"
    assert main(["scan", str(project.root / "src"), "-o", str(out), "-q"]) == 0
    doc = json.loads((out / "risk_report.json").read_text())
    assert doc["scope"] == "src"
    assert sorted(r["path"] for r in doc["files"]) == ["src/app.py", "src/util.py"]


def test_scan_exclude_accepts_repeats_and_commas(project, tmp_path) -> None:
    out = tmp_path / "out"
    args = ["scan", str(project.root), "-o", str(out), "-q", "--exclude", "*.yaml,src/util.py"]
    assert main(args) == 0
    rows = json.loads((out / "risk_report.json").read_text())["files"]
    assert [r["path"] for r in rows] == ["src/app.py"]


def test_scan_refuses_a_date_git_cannot_parse(project, tmp_path, capsys) -> None:
    assert main(["scan", str(project.root), "-o", str(tmp_path / "o"), "--until", "180d"]) == 1
    assert "can't parse --until '180d'" in capsys.readouterr().err


def test_scan_warns_when_the_window_matches_nothing(project, tmp_path, capsys) -> None:
    assert (
        main(["scan", str(project.root), "-o", str(tmp_path / "o"), "--since", "2030-01-01"]) == 0
    )
    assert "heatmap: warning: no commit in the --since/--until window" in capsys.readouterr().err


def test_scan_of_an_empty_repository_succeeds_with_a_warning(repo, tmp_path, capsys) -> None:
    assert main(["scan", str(repo.root), "-o", str(tmp_path / "out")]) == 0
    err = capsys.readouterr().err
    assert "heatmap: warning: " in err and "has no commits yet" in err
    assert "Scored 0 files" in err


def test_scan_warns_when_every_file_is_excluded(repo, tmp_path, capsys) -> None:
    repo.write("README.md", "# notes\n")
    repo.write("tests/test_a.py", "def test(): pass\n")
    repo.commit()
    assert main(["scan", str(repo.root), "-o", str(tmp_path / "out")]) == 0
    err = capsys.readouterr().err
    assert "heatmap: warning: all 2 tracked files in the repository are left out" in err
    assert "--include" in err


def test_documentation_and_manifests_are_left_out_unless_included(project, tmp_path, capsys):
    project.write("Cargo.toml", '[package]\nname = "demo"\n')
    project.commit(day="2024-03-01")
    out = tmp_path / "out"
    assert main(["scan", str(project.root), "-o", str(out), "-q"]) == 0
    paths = {r["path"] for r in json.loads((out / "risk_report.json").read_text())["files"]}
    assert "README.md" not in paths and "Cargo.toml" not in paths

    args = ["scan", str(project.root), "-o", str(out), "-q", "--include", "*.md,Cargo.toml"]
    assert main(args) == 0
    doc = json.loads((out / "risk_report.json").read_text())
    assert {"README.md", "Cargo.toml"} <= {r["path"] for r in doc["files"]}
    assert doc["include"] == ["*.md", "Cargo.toml"]

    assert main(["explain", str(project.root), "README.md", "-q"]) == 1
    err = capsys.readouterr().err
    assert "README.md is not scored: documentation (.md file) (--include re-admits it)" in err
    assert main(["explain", str(project.root), "README.md", "-q", "--include", "*.md"]) == 0


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--weights", "nope=1"], "unknown weight 'nope'"),
        (["--weights", "churn=0,complexity=0,activity=0"], "at least one weight"),
    ],
)
def test_bad_weights_fail_cleanly(repo, tmp_path, capsys, args, message) -> None:
    assert main(["scan", str(repo.root), "-o", str(tmp_path / "o"), *args]) == 1
    err = capsys.readouterr().err
    assert message in err
    assert "Traceback" not in err


def test_not_a_repository_fails_cleanly(tmp_path, capsys) -> None:
    assert main(["scan", str(tmp_path), "-o", str(tmp_path / "out")]) == 1
    assert "heatmap: error: Not a git repository" in capsys.readouterr().err


def test_explain_prints_the_breakdown(project, capsys) -> None:
    assert main(["explain", str(project.root), "src/app.py", "-q"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("src/app.py\nrisk_score ")
    assert "commit_count" in out


@pytest.mark.parametrize("given", ["app.py", "./app.py", "src/app.py"])
def test_explain_accepts_paths_relative_to_a_scoped_directory(project, capsys, given) -> None:
    assert main(["explain", str(project.root / "src"), given, "-q"]) == 0
    assert capsys.readouterr().out.startswith("src/app.py\n")


def test_explain_prefers_the_scoped_directory(project, tmp_path, capsys) -> None:
    # REPO=src and FILE=util.py means src/util.py, with or without --report,
    # even though the root has a util.py too.
    project.write("util.py", "def root():\n    return 0\n")
    project.commit(day="2024-03-01")
    out = tmp_path / "out"
    main(["scan", str(project.root), "-o", str(out), "-q"])
    capsys.readouterr()
    for extra in ([], ["--report", str(out)]):
        assert main(["explain", str(project.root / "src"), "util.py", "-q", *extra]) == 0
        assert capsys.readouterr().out.startswith("src/util.py\n")


def test_bad_exclude_glob_fails_cleanly(project, tmp_path, capsys) -> None:
    assert main(["scan", str(project.root), "-o", str(tmp_path / "o"), "--exclude", "[z-a]"]) == 1
    err = capsys.readouterr().err
    assert "invalid --exclude pattern" in err and "Traceback" not in err


def test_explain_accepts_an_absolute_path(project, capsys) -> None:
    assert main(["explain", str(project.root), str(project.root / "src" / "app.py"), "-q"]) == 0
    assert capsys.readouterr().out.startswith("src/app.py\n")


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ("tests/test_app.py", "tests/test_app.py is not scored: test code"),
        ("src/missing.py", "src/missing.py is not a tracked file"),
    ],
)
def test_explain_says_why_a_file_has_no_score(project, capsys, given, message) -> None:
    assert main(["explain", str(project.root), given, "-q"]) == 1
    assert message in capsys.readouterr().err


def test_explain_several_files_reports_each(project, capsys) -> None:
    assert main(["explain", str(project.root), "src/app.py", "nope.py", "src/util.py", "-q"]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("src/app.py\n")
    assert "\n\nsrc/util.py\n" in captured.out
    assert "nope.py is not a tracked file" in captured.err


def test_explain_from_a_report_matches_a_rescan(project, tmp_path, capsys) -> None:
    out = tmp_path / "out"
    assert main(["scan", str(project.root), "-o", str(out), "-q", "--since", "2024-01-15"]) == 0
    capsys.readouterr()
    args = ["explain", str(project.root), "src/app.py", "config/app.yaml", "-q"]
    assert main([*args, "--since", "2024-01-15"]) == 0
    rescanned = capsys.readouterr().out
    assert main([*args, "--report", str(out)]) == 0  # a directory works too
    assert capsys.readouterr().out == rescanned


def test_explain_report_refuses_settings_it_cannot_honour(project, tmp_path, capsys) -> None:
    out = tmp_path / "out"
    main(["scan", str(project.root), "-o", str(out), "-q"])
    args = ["explain", str(project.root), "src/app.py", "--report", str(out)]
    for extra in (["--since", "2024-01-01"], ["--include", "*.md"]):
        assert main([*args, *extra]) == 1
        assert "fixed by the report" in capsys.readouterr().err


def test_explain_report_from_another_repository(project, make_repo, tmp_path, capsys) -> None:
    out = tmp_path / "out"
    main(["scan", str(project.root), "-o", str(out), "-q"])
    other = make_repo("other")
    other.write("a.py")
    other.commit()
    assert main(["explain", str(other.root), "a.py", "--report", str(out)]) == 1
    assert "is a report on another repository (repo)" in capsys.readouterr().err


def test_explain_report_works_from_another_clone(project, tmp_path, capsys) -> None:
    # Reports don't record where the repository lives, so a clone elsewhere,
    # at the same commit, can explain them.
    out = tmp_path / "out"
    main(["scan", str(project.root), "-o", str(out), "-q"])
    clone = tmp_path / "elsewhere" / "copy"
    project.git("clone", "-q", str(project.root), str(clone))
    capsys.readouterr()
    assert main(["explain", str(clone), "src/app.py", "--report", str(out), "-q"]) == 0
    assert capsys.readouterr().out.startswith("src/app.py\n")
    assert main(["explain", str(clone), str(clone / "src" / "util.py"), "--report", str(out)]) == 0


def test_reports_do_not_contain_the_absolute_repository_path(project, tmp_path) -> None:
    out = tmp_path / "out"
    assert main(["scan", str(project.root / "src"), "-o", str(out), "-q"]) == 0
    for name in ("risk_report.json", "risk_report.csv", "summary.md", "hot_files.txt"):
        assert str(tmp_path) not in (out / name).read_text(encoding="utf-8"), name
    assert "- Repository: `repo` (scoped to `src/`)" in (out / "summary.md").read_text()


def test_explain_report_warns_when_head_moved(project, tmp_path, capsys) -> None:
    out = tmp_path / "out"
    main(["scan", str(project.root), "-o", str(out), "-q"])
    project.write("src/util.py", "def g():\n    return 2\n")
    project.commit(day="2024-03-01")
    assert main(["explain", str(project.root), "src/app.py", "--report", str(out)]) == 0
    assert "but HEAD is now" in capsys.readouterr().err


def test_no_blame_leaves_ownership_empty(project, tmp_path) -> None:
    out = tmp_path / "out"
    assert main(["scan", str(project.root), "-o", str(out), "-q", "--no-blame"]) == 0
    doc = json.loads((out / "risk_report.json").read_text())
    assert doc["blame"] is False
    assert {r["ownership_concentration"] for r in doc["files"]} == {None}


def test_shallow_clone_fails_cleanly(project, tmp_path, capsys) -> None:
    shallow = tmp_path / "shallow"
    project.git("clone", "-q", "--depth", "1", f"file://{project.root}", str(shallow))
    assert main(["scan", str(shallow), "-o", str(tmp_path / "o")]) == 1
    assert "fetch --unshallow" in capsys.readouterr().err


def test_ctrl_c_exits_quietly(project, monkeypatch, capsys) -> None:
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("repo_heatmap.cli.scan", interrupted)
    assert main(["scan", str(project.root), "-o", str(project.root / "o")]) == 130
    assert capsys.readouterr().err == "heatmap: interrupted\n"


def test_sigterm_exits_cleanly(project, monkeypatch, capsys) -> None:
    cleaned_up = []

    def terminated(*args, **kwargs):
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)  # interrupted by the handler
            raise AssertionError("SIGTERM was not handled")
        finally:
            os.kill(os.getpid(), signal.SIGTERM)  # a second one, during cleanup
            time.sleep(0.05)
            cleaned_up.append(True)  # not reached if the second one interrupted

    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr("repo_heatmap.cli.scan", terminated)
    assert main(["scan", str(project.root), "-o", str(project.root / "o")]) == 143
    assert capsys.readouterr().err == "heatmap: terminated\n"
    assert cleaned_up == [True]
    assert signal.getsignal(signal.SIGTERM) == before


def test_closed_stdout_with_little_output_is_not_an_error(project) -> None:
    # `heatmap explain ... | true`: the reader is gone before anything is written.
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "repo_heatmap", "explain", str(project.root), "src/app.py", "-q"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.close()
    err = proc.stderr.read().decode()
    assert proc.wait() == 141
    assert "Traceback" not in err and "Exception ignored" not in err


def test_closed_stdout_is_not_an_error(project) -> None:
    # `heatmap explain ... | head -1`: enough output to fill the pipe, then
    # the reader goes away.
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "repo_heatmap",
            "explain",
            str(project.root),
            *["src/app.py"] * 2000,
            "-q",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.readline()
    proc.stdout.close()
    err = proc.stderr.read().decode()
    assert proc.wait() == 141
    assert "Traceback" not in err and "Exception ignored" not in err


def test_scan_of_an_empty_repository_with_a_window_succeeds(repo, tmp_path) -> None:
    assert main(["scan", str(repo.root), "-o", str(tmp_path / "o"), "--since", "1 year ago"]) == 0


def test_scan_writes_reports_for_names_that_are_not_utf8(repo, tmp_path) -> None:
    blob = repo.git_bytes("hash-object", "-w", "--stdin", stdin=b"x = 1\n").decode().strip()
    name = os.fsdecode(b"bad\xffname.py")
    repo.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    repo.commit(stage=False)
    out = tmp_path / "out"
    assert main(["scan", str(repo.root), "-o", str(out), "-q"]) == 0
    assert "bad\\xffname.py" in (out / "risk_report.csv").read_text(encoding="utf-8")
    assert (out / "summary.md").exists()


def test_explain_matches_case_the_way_the_file_system_does(project, capsys) -> None:
    if not (project.root / "SRC" / "APP.PY").exists():
        pytest.skip("case-sensitive file system")
    given = ["SRC/App.py", str(project.root / "Src" / "APP.py")]
    assert main(["explain", str(project.root), *given, "-q"]) == 0
    assert capsys.readouterr().out.count("src/app.py\n") == 2


def test_explain_refuses_a_spelling_that_could_mean_two_files(repo, capsys) -> None:
    blob = repo.git_bytes("hash-object", "-w", "--stdin", stdin=b"x = 1\n").decode().strip()
    for name in ("Auth.py", "auth.py"):  # only one can exist in the checkout
        repo.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    repo.commit(stage=False)
    repo.git("checkout", "-q", "--", "auth.py")
    if not (repo.root / "AUTH.PY").exists():
        pytest.skip("case-sensitive file system")
    assert main(["explain", str(repo.root), "AUTH.py", "-q"]) == 1
    assert "could be any of: " in capsys.readouterr().err


def test_error_messages_escape_hostile_input(project, capsys) -> None:
    assert main(["explain", str(project.root), "no\x1b[2Jpe.py", "-q"]) == 1
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert "no\\x1b[2Jpe.py" in err


def test_python_dash_m_entry_point(project, tmp_path) -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    version = subprocess.run(
        [sys.executable, "-m", "repo_heatmap", "--version"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert version.stdout.strip() == f"heatmap {__version__}"
    scan = subprocess.run(
        [sys.executable, "-m", "repo_heatmap", "scan", str(project.root), "-o", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert scan.returncode == 0, scan.stderr
    assert scan.stdout == ""  # progress goes to stderr


def test_explain_absolute_path_outside_the_scoped_directory_is_an_error(project, capsys) -> None:
    # An absolute path names one file; it must not be re-rooted under REPO's
    # directory and explain a different file with the same name.
    project.write("app.py", "def root():\n    return 0\n")
    project.commit(day="2024-03-02")
    assert main(["explain", str(project.root / "src"), str(project.root / "app.py"), "-q"]) == 1
    err = capsys.readouterr().err
    assert "app.py is not a tracked file at HEAD in the scanned directory" in err
    assert "src/app.py" not in err.replace("scanned", "")


def test_explain_prefers_the_scoped_directory_for_a_nested_same_name(project, capsys) -> None:
    # REPO=src and FILE=src/x.py means src/src/x.py when that exists.
    project.write("src/x.py", "def top():\n    return 1\n")
    project.write("src/src/x.py", "def nested():\n    return 2\n")
    project.commit(day="2024-03-03")
    assert main(["explain", str(project.root / "src"), "src/x.py", "-q"]) == 0
    assert capsys.readouterr().out.startswith("src/src/x.py\n")


def test_top_percentage_is_exact(project, tmp_path) -> None:
    from repo_heatmap.cli import Top

    # 64.4% of 250 is exactly 161; floating point says 161.00000000000003.
    assert Top(percent=64.4).of(250) == 161
    assert Top(percent=8.8).of(375) == 33
    assert Top(percent=20).of(44) == 9
    assert Top(percent=1).of(1) == 1
    assert Top(percent=100).of(7) == 7


def test_failed_scan_leaves_no_empty_output_directory(tmp_path, capsys) -> None:
    out = tmp_path / "fresh"
    assert main(["scan", str(tmp_path / "notrepo"), "-o", str(out), "-q"]) == 1
    assert not out.exists()
    # A directory the user already had is left alone.
    existing = tmp_path / "mine"
    existing.mkdir()
    assert main(["scan", str(tmp_path / "notrepo"), "-o", str(existing), "-q"]) == 1
    assert existing.is_dir()


def test_unborn_head_says_where_the_commits_are(project, tmp_path, capsys) -> None:
    # `git init --bare` then a push to another branch name leaves HEAD unborn.
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(bare)], check=True)
    project.git("push", "-q", str(bare), "HEAD:refs/heads/main")
    assert main(["scan", str(bare), "-o", str(tmp_path / "o"), "-q"]) == 0
    err = capsys.readouterr().err
    assert "unborn branch 'master'" in err
    assert "commits exist on main" in err


def test_scan_of_a_directory_with_no_tracked_files_warns(project, tmp_path, capsys) -> None:
    (project.root / "untracked_dir").mkdir()
    assert main(["scan", str(project.root / "untracked_dir"), "-o", str(tmp_path / "o")]) == 0
    assert "no tracked files at HEAD under untracked_dir/" in capsys.readouterr().err


def test_exclude_glob_with_a_bracket_class_is_quiet(project, tmp_path) -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a FutureWarning from `re` would raise here
        assert (
            main(["scan", str(project.root), "-o", str(tmp_path / "o"), "-q", "--exclude", "[[]"])
            == 0
        )
        assert (
            main(
                ["scan", str(project.root), "-o", str(tmp_path / "p"), "-q", "--exclude", "[&&|~-]"]
            )
            == 0
        )
