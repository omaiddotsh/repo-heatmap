"""Shared fixtures: throwaway git repositories with controlled authors and dates."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_git(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """
    Keep the developer's git config (commit signing, hooks, aliases) out of the
    tests, and stop git discovering a repository above the temp directory.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path_factory.getbasetemp()))
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """`main()` configures the package logger for the CLI; don't let that leak between tests."""
    logger = logging.getLogger("repo_heatmap")
    saved = (logger.handlers[:], logger.level, logger.propagate)
    yield
    logger.handlers[:], logger.level, logger.propagate = saved


class Repo:
    """A scratch git repository with helpers for building history."""

    def __init__(self, root: Path, object_format: str = "sha1") -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main", f"--object-format={object_format}")

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        return self.git_bytes(*args, env=env).decode("utf-8", "replace")

    def git_bytes(
        self, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None
    ) -> bytes:
        result = subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, env=env, input=stdin
        )
        return result.stdout

    def write(self, path: str, content: str | bytes = "x = 1\n") -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        return target

    def commit(
        self,
        message: str = "change",
        *,
        author: str = "Alice",
        day: str = "2024-01-01",
        stage: bool = True,
    ) -> str:
        """Stage everything (unless `stage` is False) and commit it as `author` on `day`."""
        stamp = f"{day}T12:00:00+0000"
        email = f"{author.lower().replace(' ', '.')}@example.com"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": author,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": author,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": stamp,
        }
        if stage:
            self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message, env=env)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[..., Repo]:
    return lambda name, object_format="sha1": Repo(tmp_path / name, object_format)
