# Architecture

## Layout

```
src/repo_heatmap/
  __init__.py          public API: scan, ScanOptions, ScanResult, FileScore, HeatmapError
  __main__.py          python -m repo_heatmap
  cli.py               argument parsing, logging setup, the scan and explain commands
  pipeline.py          orchestration: list files, run the collectors, score
  git.py               every git subprocess, plus the numstat and blame parsers
  filters.py           built-in exclusions, --include and --exclude glob matching
  models.py            the records passed between stages
  errors.py            HeatmapError, for problems the user can fix
  collectors/
    churn.py           ChangeEvents  → ChurnMetrics
    complexity.py      blob content  → line count + ComplexityMetrics (via lizard)
    activity.py        ChangeEvents + git blame → ActivityMetrics
  scoring.py           percentile ranks, the FAMILIES model, weights
  report.py            CSV, JSON, Markdown summary, explain
tests/                 test_git, test_filters, test_collectors (all three), test_scoring,
                       test_report, test_pipeline, test_cli; fixtures in conftest.py
docs/                  usage, methodology, and this file
```

## Data flow

```
GitRepo.open(path)                         root, .git directory, optional prefix
  │                                        (refuses shallow and partial clones)
  ├─ tracked_files()  ─► ExcludeRules ─►  files to score (path, blob id)
  │                                            │
  │   ┌─ background: commit_graph() ─────────┐ │   temporary blame index
  │   │                                      │ │
  │   │  read_blobs() ◄──────────────────────┼─┘   one `git cat-file --batch`
  │   │      ├─► complexity.count_lines ─► loc
  │   │      └─► complexity.measure     ─► ComplexityMetrics | None
  │   │                                      │
  │   │  change_events()  rev-list + parallel diff-tree ─► full history
  │   │      ├─ commits_between(since, until) ─► window ─► churn.collect ─► ChurnMetrics
  │   │      └─ (full history) ─────────────► activity.collect ─► ActivityMetrics
  │   └──────────── waits for the index ────►   └─ git blame per file (thread pool)

FileMetrics (one per file) ─► scoring.score ─► [FileScore, highest risk first]
                                                   └─► report.*
```

Steps 1–5 run in `pipeline.collect_metrics`, and `pipeline.scan` does step 6:

1. **List.** `git ls-tree -r -z HEAD` gives every regular file with its blob
   id. `ExcludeRules` drops what shouldn't be ranked.
   For a scan of 200 files or more, a background thread starts building the
   blame index (`GitRepo.commit_graph`): a commit-graph with changed-path
   Bloom filters, written to a temporary object directory that layers over
   the real one through git's alternates mechanism. The repository is never
   written to.
2. **Read content once.** Every distinct blob streams through a single
   `git cat-file --batch` process. Its bytes are used for the line count and
   complexity of every file that has that content, then discarded. Blobs over
   1 MB are counted as they stream past and never held, so memory stays flat
   however large the files.
3. **Read history.** `git rev-list HEAD` lists the commits, and chunks of them
   are diffed by parallel `git diff-tree --stdin --numstat -z` processes. The
   result is exactly `git log --numstat`, several times faster. This runs on
   a background thread alongside step 2, as does the blame index. A
   `--since` / `--until` window is a second `git rev-list` that selects which
   of those commits churn counts. Activity always uses the full history.
4. **Collect.** Each collector reduces events to per-file metric records.
   `activity` waits for the blame index, then runs `git blame -s` per file on
   a thread pool. Blame reports only which commit wrote each line, and the
   count must match the file's line count; authors come from the history
   (`GitRepo.authors_of`). That's still most of the runtime, which
   `--no-blame` skips.
5. **Assemble.** Each file gets a `FileMetrics`. A file with no events gets
   default `ChurnMetrics()` / `ActivityMetrics()`; a file lizard can't parse
   gets `complexity=None`.
6. **Score.** `scoring.score` ranks every scored metric, builds family scores
   and `risk_score`, and sorts.

### Invariants worth keeping

- **`None` means no data, and zero means zero.** Metric fields are `None` when
  the measurement doesn't apply. Scoring keeps `None` out of the percentile
  pool and gives it a neutral 50, and the reports write it as an empty cell or
  `null`. Don't replace `None` with a sentinel such as `0` or `99999`.
- **Paths are repository-root-relative strings with `/` separators,** exactly
  as git prints them, and are used as dictionary keys throughout. Never
  normalise or re-encode them; git paths may contain backslashes, newlines,
  or bytes that aren't valid UTF-8. They're decoded with `os.fsdecode`
  (surrogate escapes), which round-trips: two names differing in one invalid
  byte stay distinct, and a path passed back to git names the same file.
  Escape them only when displaying them (`report.printable`).
- **All git access goes through `git.py`.** It pins the config that would
  otherwise change the output being parsed (`core.quotePath`,
  `log.showSignature`, `color.ui`) and reads NUL-delimited output wherever
  git offers it.
- **Results depend on the commit only.** git runs inside the `.git`
  directory (`GitRepo.git_dir`), never the checkout, with `mailmap.blob`
  pointing at `HEAD`. A scan of a dirty checkout, a clean one, and a bare
  mirror of the same commit give the same report.
- **History is never read with a pathspec.** The subdirectory prefix is
  applied while parsing. See [methodology.md](methodology.md#design-decisions).
- **Speed-ups must not change results.** The parallel history reader is tested
  against `git log` itself, and the blame index against plain blame. Any new
  optimisation needs the same kind of test.
- **Every git failure becomes a `GitError`** (a `HeatmapError`) carrying
  git's own message, so the CLI reports it without a traceback.
- **Treat the repository as hostile.** Only read-only git commands, never
  a checkout; `blame` and `diff-tree` run with `--no-textconv` (and
  `--no-ext-diff`). `_environment()` gives every git process
  `GIT_NO_LAZY_FETCH=1`, `GIT_TERMINAL_PROMPT=0`, `GIT_LITERAL_PATHSPECS=1`,
  `GIT_NO_REPLACE_OBJECTS=1`, `GIT_ATTR_NOSYSTEM=1`, and a graft file that
  doesn't exist. `_CONFIG` pins every setting that could change results.
  The repository's `.gitattributes` are ignored twice over: `attr.tree`
  points at the empty tree for the repository's object format (git 2.43+),
  and `GIT_INDEX_FILE` names an index that can't exist, which is where older
  git reads them from. Anything sized by the repository is bounded: author
  names, read as a stream and capped (`git.AUTHOR_MAX_CHARS`), the
  `.mailmap` (`MAILMAP_MAX_BYTES`), and the files lizard parses and blame
  reads (`complexity.MAX_BYTES`). Blame output is checked against the
  file's line count, since an unquoted old path in it could otherwise add
  lines. lizard runs under a crash handler and a time limit, so hostile
  input costs one file's complexity, not the scan. Anything derived from the repository (paths,
  author names, git's error messages) goes through `report.printable()`
  before reaching a terminal, and through `_spreadsheet_safe()` before
  reaching the CSV.
- **Spawn git through `_command()`.** It uses the real binary
  (`git_executable()`, which skips the macOS `/usr/bin/git` shim) by
  absolute path, which together with `close_fds=False` lets Python use
  `posix_spawn` instead of forking. That's several times cheaper per process,
  and a scan starts thousands of them.

## Library use

```python
from repo_heatmap import ScanOptions, scan

result = scan("path/to/repo", ScanOptions(since="1 year ago", exclude=("generated/**",)))
for f in result.files[:10]:
    print(f"{f.risk_score:5.1f}  {f.path}")

hot = result.find("src/auth/session.py")
if hot:
    print(hot.families, hot.components, hot.metrics.values())
```

- `ScanOptions.weights` takes any non-negative ratio. `scan` normalises it,
  and families you leave out get weight 0. This differs from `--weights`,
  where omitted families keep their defaults.
- `ScanOptions.today` pins the reference date for the "days since" metrics,
  which keeps results reproducible.
- `ScanOptions.blame=False` skips `git blame` (the CLI's `--no-blame`).
- `ScanOptions.exclude` and `ScanOptions.include` take a list or tuple of
  globs; a bare string is rejected rather than read as one glob per
  character. `include` re-admits files the built-in exclusions drop, as the
  CLI's `--include` does.
- lizard's 10-second limit per file uses `SIGALRM`, so it applies only when
  `scan` runs on the main thread and no other timer is running.
- User-fixable problems raise `HeatmapError`. Progress is logged to the
  `repo_heatmap` logger, which is silent unless you configure logging.
- The writers in `repo_heatmap.report` take a `ScanResult` if you want the
  same files the CLI produces. `report.read_json(path)` rebuilds the exact
  scores from a saved JSON report without touching git, and
  `read_json(path, weights=...)` re-weights it. Reports record the
  repository by directory name only, so a rebuilt result's `root` is that
  name, not a path.

## Adding or changing a metric

The scoring model is two tables in `scoring.py`: `FAMILIES`, below, and
`DEFAULT_WEIGHTS`, the family weights.

```python
FAMILIES = {
    "churn": (
        Component("commit_count", 0.90),
        Component("churn_relative", 0.05),
        Component("churn_burstiness", 0.05),
    ),
    ...
}
```

A `Component` names a raw metric and gives its weight within the family.
`invert=True` makes lower values riskier, and `flag=True` scores a boolean as
100 or 0 instead of ranking it.

**To score an existing reported metric,** such as `churn_absolute`, add a
`Component` for it. The reports, `explain`, and the tests' column checks pick
it up automatically.

**To add a new raw metric to an existing family:**

1. Add a field to the family's dataclass in `models.py`. Field order is report
   column order. Use `None` for "no data".
2. Compute it in that family's collector.
3. Optionally add a `Component` for it in `FAMILIES`.
4. Document it in `docs/methodology.md` and in the column table in
   `docs/usage.md`.

**To add a new family:**

1. Add a `*Metrics` dataclass to `models.py`, an entry in `METRIC_GROUPS`
   keyed by the family name, and a field of the same name on `FileMetrics`.
2. Write `collectors/<family>.py` and call it from
   `pipeline.collect_metrics`.
3. Add the family's components to `FAMILIES` and its default weight to
   `DEFAULT_WEIGHTS`. Add a label to `report._FAMILY_LABELS`.
4. Build the family's record in `FileMetrics.from_values` in `models.py`, so
   `explain --report` can rebuild it from a saved JSON report.

The family name must match in all five places (`METRIC_GROUPS`, the
`FileMetrics` attribute, `FAMILIES`, `DEFAULT_WEIGHTS`, and
`_FAMILY_LABELS`). `--weights`, the CSV and JSON columns, the summary table,
and `explain` derive from those tables, so they need no changes.

## Tunables

| Setting | Where | Default |
|---|---|---|
| Family weights | `scoring.DEFAULT_WEIGHTS` | churn 0.8, complexity 0.2, activity 0 |
| Component weights | `scoring.FAMILIES` | see [methodology.md](methodology.md#3-family-scores) |
| Built-in exclusions | `filters.py` constants | see [usage.md](usage.md#built-in-exclusions) |
| Blame size limit | `collectors.activity.BLAME_LOC_LIMIT` | 5,000 lines (and files over the complexity size limit) |
| Complexity size limit | `collectors.complexity.MAX_BYTES` | 1,000,000 bytes |
| Complexity time limit | `collectors.complexity.MAX_SECONDS` | 10 seconds per file |
| `.mailmap` size limit | `git.MAILMAP_MAX_BYTES` | 256 KB |
| Author name length | `git.AUTHOR_MAX_CHARS` | 128 characters |
| Blame threads | `collectors.activity.BLAME_WORKERS` | `min(32, 2 × CPU count)` |
| Smallest scan that builds a blame index | `pipeline.GRAPH_MIN_FILES` | 200 files |
| History diff processes | `git.HISTORY_WORKERS` | `min(16, CPU count)` |
| Commits per history chunk | `git._HISTORY_CHUNK` | at least 500 |
| "Why" threshold in the summary | `report.HIGH_SCORE` | 70 |

## Testing

```bash
pip install -e '.[dev]'
pytest                        # about 310 tests, about 20 seconds
ruff check . && ruff format --check .
mypy
```

Most tests build real throwaway repositories with the `repo` fixture from
`tests/conftest.py`, which commits with chosen authors and dates:

```python
def test_something(repo):
    repo.write("src/a.py", "x = 1\n")
    repo.commit(author="Alice", day="2024-01-01")
    opened = GitRepo.open(repo.root)
```

An autouse fixture points `GIT_CONFIG_GLOBAL` at `/dev/null` and sets
`GIT_CONFIG_NOSYSTEM` and `GIT_CEILING_DIRECTORIES`. The developer's git config (commit signing,
hooks, aliases) can't affect the tests, and a temp directory inside some
other repository can't be mistaken for one.

Parsers (`parse_numstat`, `parse_blame`), the glob matcher, and the scorer are
pure functions with unit tests for the hostile cases they exist to handle:
filenames containing ` => `, quotes, backslashes, and non-ASCII; merge
commits; corrupt dates; and missing data.
