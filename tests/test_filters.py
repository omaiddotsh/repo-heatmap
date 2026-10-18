"""repo_heatmap.filters: built-in exclusions and --exclude glob semantics."""

from __future__ import annotations

import pytest

from repo_heatmap.errors import HeatmapError
from repo_heatmap.filters import ExcludeRules, glob_matches

RULES = ExcludeRules()


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("pkg/tests/unit/test_server.py", "test code"),
        ("pkg/__tests__/widget.test.ts", "test code"),
        ("src/foo_test.go", "test code"),
        ("src/foo.spec.ts", "test code"),
        ("src/Test_Foo.py", "test code"),
        ("app/Fixtures/data.py", "test code"),
        ("vendor/lib.js", "inside a vendor/ directory"),
        ("src/vendor/lib.js", "inside a vendor/ directory"),
        ("pkg/node_modules/x.js", "inside a node_modules/ directory"),
        ("a/b/dist/x.js", "inside a dist/ directory"),
        ("docs/ref/settings.txt", "documentation (inside a docs/ directory)"),
        ("pkg/docs/conf.py", "documentation (inside a docs/ directory)"),
        ("django/conf/locale/de/LC_MESSAGES/django.po", "translation catalog"),
        ("locale/de/LC_MESSAGES/django.MO", "translation catalog"),
        ("po/messages.pot", "translation catalog"),
        ("package-lock.json", "lockfile"),
        ("web/pnpm-lock.yaml", "lockfile"),
        ("go.sum", "lockfile"),
        ("Cargo.lock", ".lock file"),
        ("app.min.js", ".min.js file"),
        ("static/LOGO.PNG", ".png file"),
        ("infra/main.tf", ".tf file"),
        ("infra/vars.tfvars", ".tfvars file"),
        ("infra/x.tf.json", ".tf.json file"),
        ("Makefile", "no file extension"),
        ("tools/bin/run", "no file extension"),
        (".gitignore", "no file extension"),
        ("README.md", "documentation (.md file)"),
        ("CHANGES.rst", "documentation (.rst file)"),
        ("crates/core/README.MD", "documentation (.md file)"),
        ("site/guide.mdx", "documentation (.mdx file)"),
        ("NOTICE.txt", "documentation (.txt file)"),
        ("COPYING.LESSER", "documentation"),
        ("LICENSE.APACHE2", "documentation"),
        ("README.html", "documentation"),
        ("Cargo.toml", "project manifest"),
        ("crates/grep/Cargo.toml", "project manifest"),
        ("web/package.json", "project manifest"),
        ("pyproject.toml", "project manifest"),
        ("go.mod", "project manifest"),
        ("CMakeLists.txt", "project manifest"),
        ("requirements-dev.txt", "project manifest"),
    ],
)
def test_built_in_exclusions(path: str, reason: str) -> None:
    assert RULES.reason(path) == reason


@pytest.mark.parametrize(
    "path",
    [
        "services/api/server.py",
        "src/app.tsx",
        "src/latest.py",  # 'test' inside a word
        "src/contest/a.py",  # 'contest' is not a test directory
        "src/attest.py",
        "src/manifest.py",
        "a.tfrecord.py",
        "src/heatmap.py",
        "vendorize.py",
        "src/vendors/a.py",
        "lib/sourcemap.py",
        "app/dist_utils.py",
        "cmd/node_modules_helper.js",
        "src/Vendor/a.py",  # directory names are case-sensitive, like git
        "src/docs.py",  # a file named docs, not a docs/ directory
        "src/docstring/parse.py",
        "src/report.pot.py",
        "src/photo.py",
        "src/license.py",  # a documentation name, but code lizard can parse
        "src/notice.ts",
        "src/readme_parser.py",
        "src/changes.go",
        "build.rs",
        "setup.py",
        "contracts/Vault.sol",
        ".github/workflows/ci.yml",  # CI is attack surface, so it stays in
        "src/package_json.py",
        "config/app.toml",
    ],
)
def test_built_in_exclusions_do_not_over_match(path: str) -> None:
    assert RULES.reason(path) is None


def test_extra_globs_add_to_the_built_ins() -> None:
    rules = ExcludeRules(("generated/**",))
    assert rules.reason("generated/deep/gen.py") == "matches --exclude 'generated/**'"
    assert rules.excludes("tests/a.py")
    assert not rules.excludes("src/a.py")


def test_include_globs_re_admit_what_built_ins_drop() -> None:
    rules = ExcludeRules(include_globs=("*.md", "Cargo.toml"))
    assert rules.reason("README.md") is None
    assert rules.reason("crates/grep/Cargo.toml") is None
    assert rules.reason("tests/a.py") == "test code"  # other built-ins still apply
    assert rules.reason("docs/guide.md") is None  # an include beats the docs/ rule too


def test_exclude_still_applies_to_included_paths() -> None:
    rules = ExcludeRules(("CHANGELOG.md",), ("*.md",))
    assert rules.reason("README.md") is None
    assert rules.reason("CHANGELOG.md") == "matches --exclude 'CHANGELOG.md'"


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("docs/*", "docs/a.py", True),
        ("docs/*", "docs/deep/a.py", False),  # '*' never crosses '/'
        ("docs/**", "docs/deep/a.py", True),
        ("src/**/gen.py", "src/gen.py", True),  # '**/' is zero or more directories
        ("src/**/gen.py", "src/a/b/gen.py", True),
        ("*.pb.go", "api/v1/x.pb.go", True),  # patterns anchor at any depth
        ("vendor/**", "src/vendor/x.js", True),
        ("/vendor/**", "vendor/x.js", True),  # leading '/' is ignored
        ("generated", "pkg/generated/x.py", True),  # bare name: any segment
        ("generated", "pkg/generated_x.py", False),
        ("gen?.py", "gen1.py", True),
        ("gen?.py", "gen12.py", False),
        ("v[0-9].py", "v3.py", True),
        ("v[!0-9].py", "v3.py", False),
        ("weird[.py", "weird[.py", True),  # unclosed bracket is a literal
        ("a[!b]c", "a/c", False),  # a class never matches '/'
        ("generated/", "pkg/generated/x.py", True),  # trailing slash: a directory
        ("generated/", "pkg/generated", False),  # ...not a file of that name
        ("src/", "src/app.py", True),
        ("", "a.py", False),
    ],
)
def test_glob_semantics(pattern: str, path: str, matches: bool) -> None:
    assert glob_matches(path, pattern) is matches


@pytest.mark.parametrize("bad", ["[z-a].py", "a[]b", "[!]"])
def test_invalid_globs_are_rejected_up_front(bad: str) -> None:
    with pytest.raises(HeatmapError, match="invalid --exclude pattern"):
        ExcludeRules((bad,))
    with pytest.raises(HeatmapError, match="invalid --include pattern"):
        ExcludeRules(include_globs=(bad,))


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("[[]x.py", "[x.py", True),
        ("[[]x.py", "ax.py", False),
        ("a[&|~]b.py", "a&b.py", True),
        ("a[&|~]b.py", "a|b.py", True),
        ("a[&|~]b.py", "a~b.py", True),
        ("a[&|~]b.py", "a-b.py", False),
        ("a[--]b.py", "a-b.py", True),
    ],
)
def test_regex_set_syntax_inside_a_class_is_literal(pattern: str, path: str, matches: bool) -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert glob_matches(path, pattern) is matches
