# Usage

```
heatmap scan    REPO [options] [-o DIR] [--top N|P%]
heatmap explain REPO FILE [FILE ...] [options] [--report PATH]
heatmap --version
```

## Choosing what to scan

`REPO` can be a repository root, a directory inside one, or a bare repository
such as a `git clone --mirror`. Everything is read from `HEAD`, including the
`.mailmap`, so uncommitted or edited files in the checkout have no effect. The
repository's `.gitattributes` are ignored entirely (see
[Untrusted repositories](#untrusted-repositories)).

Pointing `REPO` at a subdirectory scores only the files under it. Paths in the
reports stay relative to the repository root. Raw metrics for a file are the
same whatever the scope, but percentiles and scores are relative to the files
being scored, so they shift when the scope narrows.

The repository needs its full history. Shallow clones (`git clone --depth N`)
are refused with an error: their history stops at the clone depth, so every
file would look like it had one commit and the ranking would be meaningless.
`git fetch --unshallow` converts one into a full clone. Partial clones
(`git clone --filter=...`) are refused too, because some of their file
contents exist only on the server and heatmap never uses the network.

### Untrusted repositories

heatmap is meant to be pointed at code you haven't reviewed yet, so it treats
the repository as hostile:

- It reads only from git's object store, through `ls-tree`, `cat-file`,
  `rev-list`, `diff-tree`, `log`, and `blame`, run from inside the `.git`
  directory so the checkout is never read. Nothing is checked out, and no
  hooks, clean/smudge filters, external diff tools, or `textconv` programs
  run.
- git is never allowed to touch the network (`GIT_NO_LAZY_FETCH=1`,
  `GIT_TERMINAL_PROMPT=0`), and the repository is never written to. The
  temporary blame index (see [Performance](#performance)) goes to a
  temporary directory.
- The repository can't steer the ranking. Its `.gitattributes` are ignored,
  so marking a file `binary` or `-diff` can't hide its history. So are
  replacement objects (`refs/replace/`, which a mirror clone fetches) and
  grafts, which could swap a file's content or history for a decoy. Path
  arguments are always literal (`GIT_LITERAL_PATHSPECS=1`).
- Neither its config nor yours can change results. Rename detection, the
  diff algorithm, what counts as a binary file, and blame's ignored-revisions
  list are pinned to git's defaults, and attributes and mailmap files from
  outside the commit are ignored.
- Hostile content is contained. A file over 1 MB is counted but neither
  parsed nor blamed, and identical files are measured once. A file that the
  complexity parser crashes on, or hasn't finished after 10 seconds, scores
  as neutral with a warning. Author names are cut to 128 characters, and a
  `.mailmap` over 256 KB is ignored, also with a warning.
- File names are printed with control and invisible characters escaped
  (`\x1b`, `\u202e`, …), so a name can't send escape sequences to your
  terminal, or use invisible or bidirectional characters to pose as another.
  Bytes that aren't valid UTF-8 are shown as `\xNN`. The Markdown summary
  does the same.
- The CSV escapes paths the same way, so `cat risk_report.csv` is as safe
  as the terminal output, and a path that then starts with `=`, `+`, `-`, or
  `@` gets a leading `'`, so spreadsheets display it rather than evaluate it
  as a formula. The JSON report keeps paths exactly as git stores them, in
  JSON's own escapes (`\u001b`), so printing the file is safe too: an
  invalid byte becomes a lone surrogate escape such as `\udcff`, as Python's
  `os.fsdecode` produces.
- Reports name the repository by its directory name, never its absolute
  path, so sharing one doesn't reveal where it was made.

Two limits to know about:

- **Scan a clone.** A `.git` directory copied from elsewhere, such as one
  inside a downloaded archive, brings its own config, hooks, and other files
  that git reads from it (`info/attributes`, or a `.mailmap` placed there),
  and git trusts all of them. heatmap runs only read-only commands with
  external programs switched off, but git makes no promise that this is
  enough. Clone the copy first (`git clone path/to/copy fresh`) and scan the
  clone: cloning copies none of those files.
- **Time and memory aren't capped.** A repository built to waste them can
  still make a scan slow or memory-hungry, as it can git itself: for
  example, millions of paths or megabyte-long commit headers that compress
  to a few kilobytes. For code you don't trust at all, run heatmap under a
  time and memory limit, such as `timeout` or a container.

If git refuses a repository because another user owns it ("dubious
ownership"), heatmap says so and shows the `safe.directory` command to allow
it. Only do that if you trust the files' owner.

## `heatmap scan`

Scores every eligible file and writes `risk_report.csv`, `risk_report.json`,
`summary.md`, and `hot_files.txt`.

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--output DIR` | `./risk_report` | Directory for the reports, created if needed |
| `--top N` or `--top P%` | `20%` | Files in `summary.md` and `hot_files.txt`: a count of 1 or more, or a percentage of the files scored over 0, rounded up (the CSV and JSON always list every file) |
| `--since DATE` | none | Count churn only from commits made on or after `DATE` |
| `--until DATE` | none | Count churn only from commits made on or before `DATE` |
| `--exclude GLOB` | none | Also leave out paths matching `GLOB`; repeatable or comma-separated |
| `--include GLOB` | none | Score paths matching `GLOB` even if a [built-in rule](#built-in-exclusions) leaves them out; repeatable or comma-separated |
| `--weights SPEC` | `churn=0.8,complexity=0.2,activity=0` | Family weights; see below |
| `--no-blame` | off | Skip `git blame`, the slowest step; see [Performance](#performance) |
| `-q`, `--quiet` | off | Print only warnings and errors |

### Dates

`--since` and `--until` take git's date syntax: `2024-01-01`,
`"180 days ago"`, `"last month"`. Shorthand such as `180d` is not understood.
git never rejects a date: one it can't make sense of silently means "now",
which would make a bad `--since` match nothing and a bad `--until` match
everything. heatmap catches that and exits with an error instead. It also
warns when the window is valid but no commit in it touches a scored file.

As in `git log`, the window uses each commit's *committer* date: when it was
made, which differs from the author date after a rebase or cherry-pick.

The window applies only to the churn metrics. Authorship metrics always use
the full history, because a file that one person wrote years ago is still
single-author even if nobody has touched it since.

### Weights

`--weights` sets how much each family counts toward `risk_score`. Values are
any non-negative ratio and are rescaled to sum to 1. Families you leave out
keep their default weight.

```bash
--weights churn=1,complexity=1,activity=1   # even three-way split
--weights complexity=0                      # churn only (activity is already 0)
--weights churn=3,complexity=1              # 0.75 / 0.25 / 0
```

At least one weight must be above zero.

## `heatmap explain`

```bash
heatmap explain path/to/repo src/auth/session.py src/auth/token.py
heatmap explain path/to/repo src/auth/session.py --report risk_report
```

Prints each file's score broken down metric by metric. Every scored metric
shows its raw value, its 0–100 score, and its weight within the family. The
score is the metric's percentile rank, flipped for inverted metrics; 100 or
0 for `bus_factor_flag`; and a neutral 50 when there's no data. Metrics that
are only reported show just their value. Explanations for several files are
separated by a blank line.

Percentiles depend on every other file, so without `--report`, `explain`
runs a full scan first. Pass the same `--since`, `--until`, `--exclude`,
`--include`, `--weights`, and `--no-blame` you gave `scan` to explain the
same ranking.

With `--report PATH`, `explain` reads a previous scan's `risk_report.json`
(or the directory holding it) instead, which takes a fraction of a second
and reproduces that scan's numbers exactly. The report fixes the date window,
exclusions, and blame setting, so `--since`, `--until`, `--exclude`,
`--include`, and `--no-blame` are rejected alongside it. `--weights` is
allowed and re-weights the saved metrics. `REPO` must contain the commit the
report scanned: the repository it was made from, or any clone of it, wherever
it lives. If `HEAD` has moved since, you get a warning and the report is
explained as saved.

`FILE` can be relative to the repository root, relative to `REPO` when that is
a subdirectory, or absolute. On a case-insensitive file system (the default
on macOS and Windows), it can be typed in any case. If a file has no score,
`explain` says why, naming the exclusion rule that dropped it (and, for a
built-in rule, that `--include` re-admits it). It still explains the other
files, then exits 1.

```
src/click/core.py
risk_score 92.5  (rank 1 of 44, 4038 lines)

churn_score 98.3  x weight 0.80
  churn_absolute                 9226   (reported, not scored)
  churn_relative                2.285   score  72.1  x 0.05
  commit_count                    237   score 100.0  x 0.90
  churn_recency_days               34   (reported, not scored)
  churn_burstiness              1.719   score  93.0  x 0.05

complexity_score 69.4  x weight 0.20
  cyclomatic_complexity           648   score 100.0  x 0.33
  nesting_depth                    15   score  98.3  x 0.33
  function_count                  147   (reported, not scored)
  avg_function_length           11.83   (reported, not scored)
  comment_density             0.08668   score  10.0  x 0.33  inverted: lower is riskier

activity_score 46.5  x weight 0.00
  ...
```

## What gets scored

Every regular file tracked at `HEAD` (mode `100644` or `100755`), minus the
built-in exclusions, plus anything your `--include` globs re-admit, minus
your `--exclude` globs. Symlinks and submodules are never scored.

### Built-in exclusions

| Rule | Matches |
|---|---|
| Test code | A directory named `test`, `tests`, `__tests__`, `spec`, `specs`, `fixtures`, `__mocks__`, `testdata`, or `testing` anywhere in the path (any case); or a file named `test_*`, `spec_*`, `*_test.*`, `*.test.*`, `*_spec.*`, or `*.spec.*` |
| Vendored and build output | A directory named `vendor`, `node_modules`, `dist`, or `build` anywhere in the path (case-sensitive) |
| Documentation | A directory named `docs` anywhere in the path (case-sensitive); `*.md`, `*.markdown`, `*.mdx`, `*.rst`, `*.txt`, `*.adoc`, `*.asciidoc` (any case); and files named `README`, `CHANGELOG`, `CHANGES`, `LICENSE`, `LICENCE`, `COPYING`, `NOTICE`, `AUTHORS`, `CONTRIBUTORS`, `COPYRIGHT`, `PATENTS`, or `UNLICENSE` with any extension, such as `COPYING.LESSER` (any case), unless lizard can parse the extension (`license.py` is code) |
| Project manifests | `Cargo.toml`, `package.json`, `pyproject.toml`, `setup.cfg`, `MANIFEST.in`, `go.mod`, `go.work`, `composer.json`, `pom.xml`, `pubspec.yaml`, `deno.json`, `deno.jsonc`, `CMakeLists.txt`, and `requirements*.txt` / `requirements*.in` (any case) |
| Translation catalogs | `*.po`, `*.pot`, `*.mo` (any case) |
| Lockfiles | `*.lock`, `package-lock.json`, `npm-shrinkwrap.json`, `pnpm-lock.yaml`, `go.sum` |
| Generated and binary files | `*.min.js`, `*.min.css`, `*.map`, `*.svg`, `*.png`, `*.jpg`, `*.jpeg`, `*.gif`, `*.webp`, `*.ico` (any case) |
| Terraform | `*.tf`, `*.tfvars`, `*.tf.json` (any case) |
| No extension | `Makefile`, `LICENSE`, `bin/run`, and dotfiles with no other extension, such as `.gitignore` (but not `.pre-commit-config.yaml`) |

These exist because the tool ranks first-party source. Left in, they crowd the
top of the list with files nobody needs to review, and they slow the scan:
on Django, translation catalogs and `docs/` were more than 70% of the files
that would otherwise be scored, and leaving them out cut a scan from 11 to 6
seconds. READMEs, changelogs, and manifests change with nearly every
release: on ripgrep they took the top three places (`Cargo.toml`,
`README.md`, `CHANGELOG.md`), and crate manifests three of the next four,
until they were left out. Dependency manifests and lockfiles are a job for a
dependency scanner, not this ranking.

Any other file type that `git` tracks is scored, including CI configuration
such as `.github/workflows/*.yml`, which can be attack surface. Files lizard
can't parse, such as YAML, get a neutral complexity score instead of a
made-up one.

### Re-admitting files with `--include`

`--include GLOB` scores files that match it even if a built-in rule leaves
them out: `--include '*.md'` brings back Markdown, `--include tests/` brings
back test code, and `--include Cargo.toml` one kind of manifest. It takes the
same glob syntax as `--exclude` (below), and `--exclude` still applies on
top, so `--include '*.md' --exclude CHANGELOG.md` scores every Markdown file
but the changelog. `explain` names `--include` whenever a built-in rule is
why a file has no score.

### `--exclude` glob syntax

| Pattern | Matches |
|---|---|
| `*` | Anything except `/`: `gen/*` matches `gen/a.py`, not `gen/deep/a.py` |
| `**` | Anything, including `/`: `gen/**` matches both |
| `**/` | Zero or more directories: `src/**/gen.py` matches `src/gen.py` and `src/a/b/gen.py` |
| `?` | One character other than `/` |
| `[abc]`, `[a-z]`, `[!0-9]` | One character from a class (`!` negates it); never `/` |
| A trailing `/`, as in `generated/` | Everything inside any directory of that name |
| A bare name such as `generated` | That file, or any directory of that name, anywhere in the path |

Patterns match at any depth: `vendor/**` also matches `src/vendor/x.js`, and
`*.pb.go` matches `api/v1/x.pb.go`. A leading `/` is ignored. Matching is
case-sensitive.

## Output formats

All three reports list files from highest to lowest `risk_score`. Ties are
broken by path.

### `risk_report.csv` and `risk_report.json`

The JSON document also records the run's settings, which is what lets
`explain --report` rebuild the scan, and the lizard version, because
complexity numbers can change between lizard releases. `repo` is the
repository's directory name, not its path, and `scope` the subdirectory
scanned, relative to the repository root:

```json
{
  "generator": "heatmap 0.1.0",
  "lizard": "1.24.1",
  "repo": "acme",
  "head": "3f9c…",
  "scope": "services/api",
  "since": "1 year ago",
  "until": null,
  "exclude": ["generated/**"],
  "include": [],
  "blame": true,
  "weights": {"churn": 0.8, "complexity": 0.2, "activity": 0.0},
  "files": [ { "path": "...", "risk_score": 92.973..., ... } ]
}
```

Both formats have these columns, in this order. The CSV rounds floats to four
decimal places, and the JSON keeps full precision. In the CSV only, paths
are escaped as the terminal shows them, and one starting with a formula
character gets a leading `'` (see
[Untrusted repositories](#untrusted-repositories)). A metric with no data is
an empty cell in the CSV and `null` in the JSON. It is never zero, because
"no data" and "zero" are scored differently.

| Column | Range | Meaning |
|---|---|---|
| `path` | | Relative to the repository root |
| `risk_score` | 0–100 | Weighted sum of the three family scores |
| `churn_score`, `complexity_score`, `activity_score` | 0–100 | Family scores |
| `loc` | ≥ 0 | Lines at `HEAD`; 0 for binary files |
| `churn_absolute` | ≥ 0 | Lines added plus lines deleted, in the window; a binary change counts as 0 lines |
| `churn_relative` | ≥ 0 | `churn_absolute / max(loc, 1)` |
| `commit_count` | ≥ 0 | Commits that changed the file, in the window |
| `churn_recency_days` | ≥ 0 or empty | Days since the last change in the window; empty if none |
| `churn_burstiness` | ≥ 0 | Irregularity of the gaps between commit days |
| `cyclomatic_complexity` | ≥ 0 or empty | Total McCabe complexity; empty if lizard has no reader for the language, the file is over 1 MB, or lizard fails on it or takes over 10 seconds |
| `nesting_depth` | ≥ 0 or empty | Deepest control-flow nesting in any function |
| `function_count` | ≥ 0 or empty | Functions lizard found |
| `avg_function_length` | ≥ 0 or empty | Mean lines of code per function |
| `comment_density` | 0–1 or empty | Share of lines that start with `#` or `//` |
| `distinct_authors` | ≥ 0 | Distinct authors, over full history, after `.mailmap` |
| `ownership_concentration` | 0–100 or empty | % of current lines by the top author (by share of commits for files over 5,000 lines or 1 MB, empty, or binary); empty if only merges touched the file, or with `--no-blame` |
| `bus_factor_flag` | `True`/`False` in the CSV, `true`/`false` in the JSON | Exactly one author, ever |
| `days_since_any_commit` | ≥ 0 or empty | Days since any commit touched the file, over full history |

[methodology.md](methodology.md) defines each metric precisely and says which
ones feed the score.

### `hot_files.txt`

The top `--top` paths, one per line, highest risk first, ready for other
tools (`xargs`, a review checklist, `codegraph`). A path containing a control
character such as a newline can't sit on one line, so it's escaped the way
the terminal output shows it (`\x0a`).

### `summary.md`

A header with the repository (by directory name), scope, churn window,
weights, and file count, then a table of the top `--top` files (headed with
the share, such as "the top 20% of 1200", when `--top` is a percentage). The
*Why* column names each family that scored 70 or more, skipping families
weighted at zero, or says *moderate across the board* when none did.

## Exit codes and messages

| Code | When |
|---|---|
| `0` | Success, including a scan that scored zero files: a repository with no commits, or one whose every file an exclusion rule drops, each with a warning |
| `1` | Not a git repository, a shallow or partial clone, a repository owned by another user, a missing path, bad `--weights`, an invalid `--exclude` or `--include` glob, a date git can't parse, an unwritable output directory, a git command failing (git's own message is shown), an unreadable `--report` or one whose commit isn't in `REPO`, or `explain` on a file with no score |
| `2` | Invalid command-line syntax (from argparse) |
| `130` | Interrupted with Ctrl-C |
| `141` | stdout closed early, as by `heatmap explain ... \| head` |
| `143` | Terminated by SIGTERM, as by `timeout`; temporary files are still removed |

Errors print as `heatmap: error: ...` on stderr, without a traceback.

## Performance

Measured on a 16-core Apple M3 Max:

| Repository | Full scan | `--no-blame` |
|---|---|---|
| pytest (17,800 commits, 500 files scored) | 1.6 s | — |
| Django (35,000 commits, 1,150 files scored) | 5.6 s | 2.2 s |
| Django, `django/contrib/admin` only (113 files) | 3.2 s | — |

`explain --report` takes about 0.1 s on any of them.

Most of a scan is `git blame`, one per file with history, run across up to 32
threads. For scans of 200 files or more, heatmap first builds a temporary
index of which commits touched which paths (a git commit-graph with
changed-path Bloom filters). On Django it cuts a typical blame from about
0.4 s to 0.1 s. It is built while files and history are being read, and is
deleted afterwards. It lives
in a temporary directory, so the repository itself is never written to. The
history itself is read by parallel `git diff-tree` processes. Both run while
complexity is being measured. git is called directly rather than through a
wrapper (on macOS, `/usr/bin/git` is a shim that costs about 6 ms a call),
because a scan starts thousands of git processes.

Two ways to go faster:

- **`--no-blame`** skips `git blame`. Only `ownership_concentration` is lost,
  and it only feeds the activity family. With the default weights (activity
  0), rankings are identical.
- **`explain --report`** reuses a scan instead of repeating it.

Files over 5,000 lines or 1 MB skip `git blame` regardless (see
[methodology.md](methodology.md#limitations)).
