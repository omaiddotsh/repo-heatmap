"""Which tracked files get ranked.

The tool ranks first-party source for review, so tests, vendored and generated
code, documentation, project manifests, translation catalogs, lockfiles,
binary assets, infrastructure config, and extensionless files are left out by
default. They'd otherwise crowd out the code worth reading. None of them is
cleanly a single glob (test code is a directory-segment *or* basename-shape
question; "no extension" isn't a glob at all), so each rule is written out
directly. User --include globs re-admit files these rules drop, and user
--exclude globs are layered on top of both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .collectors.complexity import reader_for
from .errors import HeatmapError

# A directory with one of these names, anywhere in the path, holds tests.
TEST_DIRS = frozenset(
    {"test", "tests", "__tests__", "spec", "specs", "fixtures", "__mocks__", "testdata", "testing"}
)
# test_x.py, spec_x.rb, x_test.go, x.test.ts, x.spec.ts, x_spec.rb
TEST_FILE_RE = re.compile(r"^(?:test|spec)_.|[._](?:test|spec)\..", re.IGNORECASE)
# Third-party or build output, anywhere in the path. Case-sensitive, like git.
VENDOR_DIRS = frozenset({"vendor", "node_modules", "dist", "build"})
# Documentation trees, anywhere in the path. Case-sensitive, like git.
DOC_DIRS = frozenset({"docs"})
# gettext catalogs: source (.po), template (.pot), and compiled (.mo). Large,
# constantly churned by translators, and not code. Matched case-insensitively.
TRANSLATION_SUFFIXES = (".po", ".pot", ".mo")
# Generated, binary, or infrastructure files, matched case-insensitively.
EXCLUDED_SUFFIXES = (
    ".min.js", ".min.css", ".map", ".lock",
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".tf", ".tfvars", ".tf.json",
)  # fmt: skip
# Lockfiles whose names don't end in .lock.
LOCKFILE_NAMES = frozenset({"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "go.sum"})
# Prose, matched case-insensitively. Top-level READMEs and changelogs change
# with nearly every release, so left in they top the ranking of any project
# that keeps one, and nothing in them runs.
DOC_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc", ".asciidoc")
# Files that are prose whatever their extension (COPYING.LESSER,
# LICENSE.APACHE2, README.html), matched on the name before the first dot,
# case-insensitively. A file lizard can parse is code, so license.py stays in.
DOC_NAMES = frozenset(
    {
        "readme", "changelog", "changes", "license", "licence", "copying", "notice",
        "authors", "contributors", "copyright", "patents", "unlicense",
    }
)  # fmt: skip
# Project and dependency manifests, matched case-insensitively. They change
# with every version bump and dependency update, so they'd top the churn
# ranking without holding reviewable logic. Lockfiles are excluded for the
# same reason; dependency review is a job for a dependency scanner.
MANIFEST_NAMES = frozenset(
    {
        "cargo.toml", "package.json", "pyproject.toml", "setup.cfg", "manifest.in",
        "go.mod", "go.work", "composer.json", "pom.xml", "pubspec.yaml",
        "deno.json", "deno.jsonc", "cmakelists.txt",
    }
)  # fmt: skip
# requirements.txt, requirements-dev.txt, requirements.in, and so on.
REQUIREMENTS_RE = re.compile(r"^requirements(?:[-_.][\w.-]*)?\.(?:txt|in)$", re.IGNORECASE)


@dataclass(frozen=True)
class ExcludeRules:
    """
    The built-in exclusions, minus any user --include globs, plus any user
    --exclude globs.
    """

    extra_globs: tuple[str, ...] = ()
    include_globs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for flag, globs in (("--exclude", self.extra_globs), ("--include", self.include_globs)):
            for glob in globs:
                try:
                    _glob_regex(glob.lstrip("/"))
                except re.error as exc:
                    raise HeatmapError(f"invalid {flag} pattern {glob!r}: {exc}") from None

    def excludes(self, path: str) -> bool:
        return self.reason(path) is not None

    def reason(self, path: str) -> str | None:
        """Why `path` is left out of the ranking, or None if it isn't."""
        built_in = builtin_reason(path)
        if built_in and not any(glob_matches(path, g) for g in self.include_globs):
            return built_in
        if glob := next((g for g in self.extra_globs if glob_matches(path, g)), None):
            return f"matches --exclude {glob!r}"
        return None


def builtin_reason(path: str) -> str | None:
    """Which built-in rule leaves `path` out of the ranking, or None if none does."""
    *dirs, name = path.split("/")
    lower = name.lower()
    if any(d.lower() in TEST_DIRS for d in dirs) or TEST_FILE_RE.search(name):
        return "test code"
    if vendored := next((d for d in dirs if d in VENDOR_DIRS), None):
        return f"inside a {vendored}/ directory"
    if docs := next((d for d in dirs if d in DOC_DIRS), None):
        return f"documentation (inside a {docs}/ directory)"
    if lower.endswith(TRANSLATION_SUFFIXES):
        return "translation catalog"
    if lower in LOCKFILE_NAMES:
        return "lockfile"
    if suffix := next((s for s in EXCLUDED_SUFFIXES if lower.endswith(s)), None):
        return f"{suffix} file"
    if lower in MANIFEST_NAMES or REQUIREMENTS_RE.match(name):
        return "project manifest"
    if suffix := next((s for s in DOC_SUFFIXES if lower.endswith(s)), None):
        return f"documentation ({suffix} file)"
    if has_no_extension(name):
        return "no file extension"
    if lower.partition(".")[0] in DOC_NAMES and reader_for(name) is None:
        return "documentation"
    return None


def has_no_extension(name: str) -> bool:
    """
    True for `Makefile`, `LICENSE`, `run`, `.gitignore`. A leading dot is the
    whole name, not an extension, so dotfiles count as extensionless.
    """
    return name.rfind(".") <= 0


def glob_matches(path: str, pattern: str) -> bool:
    """
    True if a gitignore-style `pattern` matches `path` at any directory depth.

    Patterns anchor to any directory boundary, not just the repository root,
    so 'vendor/**' also catches 'src/vendor/x.js', which is what people mean
    in a monorepo. A leading '/' is ignored. A bare name with no wildcard or
    slash ('node_modules') matches that file or any directory of that name
    along the path.
    """
    pattern = pattern.lstrip("/")
    if pattern.endswith("/"):  # 'generated/' means a directory of that name
        pattern = pattern.rstrip("/") + "/**"
    if not pattern:
        return False
    parts = path.split("/")
    if "/" not in pattern and not any(ch in pattern for ch in "*?["):
        return pattern in parts
    rx = _glob_regex(pattern)
    return any(rx.match("/".join(parts[i:])) for i in range(len(parts)))


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """
    Translate a glob to a regex where '*' and '?' stop at '/' and only '**'
    crosses directories.

    fnmatch can't be used: its '*' crosses '/', so 'docs/*' would also match
    'docs/deep/code.py' and silently drop real source.
    """
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")  # zero or more directories
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and (end := pattern.find("]", i + 1)) != -1:
            body = pattern[i + 1 : end].replace("\\", "\\\\")
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append(f"(?!/)[{body}]")  # like '*' and '?', a class never matches '/'
            i = end + 1
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("(?s:" + "".join(out) + r")\Z")
