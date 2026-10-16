# Methodology

heatmap orders the files in one repository by how likely they are to reward a
security review. It is a triage aid, not a vulnerability detector. Every
signal it uses is a *correlate* of vulnerable code from prior research, not a
cause, and every score is relative to the other files in the same scan.

## The three metric families

### Churn

Source: the per-file line counts of every commit reachable from `HEAD` (what
`git log --numstat` reports), limited to the `--since` / `--until` window if
one is given.

| Metric | Definition | Scored |
|---|---|---|
| `commit_count` | Distinct commits that changed the file | yes, weight 0.90 |
| `churn_relative` | `churn_absolute / max(loc, 1)`; size-normalised churn | yes, weight 0.05 |
| `churn_burstiness` | Coefficient of variation (stdev ÷ mean) of the gaps, in days, between the distinct days the file changed. 0 with fewer than three such days | yes, weight 0.05 |
| `churn_absolute` | Lines added plus lines deleted | reported only |
| `churn_recency_days` | Days from the last change to today | reported only |

High burstiness means sporadic bursts of edits, low means steady maintenance.

Details:

- Merge commits contribute nothing, because git shows no diff for them by
  default. Conflict-resolution edits are invisible.
- Rename detection is always on, whatever the user's `diff.renames` setting.
- A binary change still counts as a commit, with zero lines added or
  deleted, since git can't count lines in it. The repository's
  `.gitattributes` are ignored, so only real binary content counts as binary;
  marking a text file `binary` or `-diff` can't hide its line counts.
- A rename is attributed to its new path. A pure rename counts as a commit
  with zero lines changed; a rename with edits counts the edited lines.
  History from before the rename stays with the old path, which no longer
  exists at `HEAD`, so it is dropped.
- Recency and burstiness use author dates. The `--since` / `--until` window,
  like `git log`'s, uses committer dates (see [usage.md](usage.md#dates)). A
  commit whose author date is corrupt (five-digit years exist in real
  repositories) still counts toward `commit_count` and churn, but not toward
  recency or burstiness.

### Complexity

Source: the file's content at `HEAD`, measured by
[lizard](https://github.com/terryyin/lizard). Only files whose extension
lizard maps to a real language reader are measured, which covers more than
twenty languages. Everything else (Markdown, YAML, JSON, binaries) has no
complexity data, so it gets a neutral score instead of numbers from lizard's
generic fallback reader. So does a file over 1 MB, which is almost always
generated and would take lizard seconds; a file lizard fails to parse; and a
file lizard hasn't finished after 10 seconds, which real source never needs
but crafted input can force.

| Metric | Definition | Scored |
|---|---|---|
| `cyclomatic_complexity` | Sum of McCabe complexity over the file's functions. Top-level code outside any function adds nothing | yes |
| `nesting_depth` | Deepest control-flow nesting inside any function | yes |
| `comment_density` | Lines that start with `#` or `//` (after indentation) ÷ all lines. Inverted: *less* commenting scores as riskier | yes |
| `function_count` | Functions lizard found | reported only |
| `avg_function_length` | Mean lines of code per function | reported only |

The three scored metrics count equally within the family.

### Developer activity

Source: commit authorship and `git blame` at `HEAD`, always over **full
history**, whatever `--since` says. A file one person wrote five years ago is
single-author even if the window can't see that commit.

| Metric | Definition | Scored |
|---|---|---|
| `ownership_concentration` | % of the file's current lines attributed by `git blame` to its top author. Inverted: *diffuse* ownership scores as riskier | yes |
| `bus_factor_flag` | Exactly one distinct author, ever. Scores 100 when true, 0 when false | yes |
| `distinct_authors` | Distinct authors who changed the file | reported only |
| `days_since_any_commit` | Days since any commit changed the file | reported only |

Author identities go through the `.mailmap` committed at `HEAD`, so one person
who committed under two names or emails counts once. (A `.mailmap` over
256 KB is ignored with a warning; real ones are a few kilobytes.)

Both scored halves read as risk, for opposite reasons. Diffuse ownership means
nobody really owns the file, and a bus factor of one means only one person
does.

`ownership_concentration` falls back to the top author's share of *commits*
when `git blame` isn't used for a file: when it has more than 5,000 lines or
1 MB, is empty or binary, or blame fails on it. It is no data (a neutral 50) when no
commit's diff touches the file, which happens when its only changes arrived
through merges. With `--no-blame` it is always no data, and
`activity_score` rests on `bus_factor_flag` alone. That changes no ranking
under the default weights, where activity counts for nothing.

## From metrics to a score

### 1. Percentile ranks

Every scored metric is converted to a percentile rank from 0 to 100 among the
files in the scan. The lowest value scores 0, the highest 100, and tied values
share the average of their ranks. A metric marked *inverted* is flipped
(`100 − rank`), so in every case a higher number means more risk.
`bus_factor_flag` is a yes/no fact, so it scores 100 or 0 directly instead
of being ranked.

Percentile ranks rather than z-scores, because churn and complexity are
heavily right-skewed. With z-scores, a handful of extreme hotspots would
compress every other file into a narrow band.

### 2. No data is not zero

A file can lack a metric entirely. A YAML file has no cyclomatic complexity, and
a file with no commits in the history has no owner. Such a file is left out
of that metric's percentile pool and scores a neutral **50** on it.

Treating "no data" as 0 would do damage twice over. It would hand the file a
false extreme (the *riskiest* possible score, once comment density is
inverted). It would also drag every real file's percentile around with a mass
of tied zeros. The reports keep the distinction visible: missing metrics are
empty or `null`, never `0`.

Churn metrics are the exception. A file with no commits in the window
genuinely has zero commits there, so it is ranked with a value of 0 rather
than left out. Tied values share their average rank, so when many files are
untouched in the window, their shared score sits well above 0.

### 3. Family scores

Each family score is the weighted mean of its components' scores:

```
churn_score      = 0.90·commit_count + 0.05·churn_relative + 0.05·churn_burstiness
complexity_score = (cyclomatic_complexity + nesting_depth + inverted comment_density) / 3
activity_score   = (inverted ownership_concentration + bus_factor_flag) / 2
```

### 4. Risk score

```
risk_score = w_churn·churn_score + w_complexity·complexity_score + w_activity·activity_score
```

The weights are normalised to sum to 1, so `risk_score` stays on the 0–100
scale. The defaults are `0.8 / 0.2 / 0.0`, and `--weights` changes them.

The whole model is two tables in `src/repo_heatmap/scoring.py`: `FAMILIES` (each
family's metrics and their weights) and `DEFAULT_WEIGHTS` (the family weights).
[architecture.md](architecture.md#adding-or-changing-a-metric) shows how to
change them.

## Why these defaults

The defaults come from tuning against one private codebase with known
security findings. That data can't be published, so read what follows as the
reasoning behind the defaults, not as evidence you can check:

- **Commit count tracked the findings most closely,** more closely than an
  even blend of the metrics. That is why `commit_count` carries 0.90 of the
  churn family, and churn 0.8 of the score.
- **Dividing churn by file size pushed large, heavily edited files down,**
  which hurt. `churn_relative` and `churn_burstiness` keep a token 0.05 each,
  for codebases where they matter more.
- **The ownership metrics didn't improve the ranking,** so the activity
  family is computed and reported but weighted zero.

Treat the defaults as a starting point, not a settled answer. They come from
one codebase, and known findings only cover code someone chose to review. If
you have known findings for your own code, fit the weights to them.

## Design decisions

- **Everything is read at `HEAD`.** File lists, contents, history, blame,
  and the `.mailmap` all come from the commit. git runs inside the
  repository's `.git` directory rather than the checkout, so edited or
  uncommitted files can't skew a result, and bare mirrors can be scanned.
- **The repository and the user's git config can't steer results.** The
  repository's `.gitattributes` are ignored, so a hostile repository can't
  hide its riskiest file by marking it `binary`, and so are replacement
  objects and grafts, which could swap in a decoy. Rename detection, the diff
  algorithm, what counts as binary, and blame's ignored-revisions list are
  pinned to git's defaults, and attributes and mailmap files from outside
  the commit are ignored. Two people scanning the same commit with the same
  heatmap and lizard versions get the same scores.
- **Shallow and partial clones are refused.** A shallow clone's history stops
  at the clone depth, so every file would show a single commit and the
  ranking would be noise. A partial clone's file contents are on a server,
  and heatmap never uses the network. An error is better than a confident
  wrong answer.
- **Speed-ups never change results.** History is diffed by parallel
  `git diff-tree` processes instead of one `git log`, blame runs against a
  temporary commit-graph index, and blame is asked only which commit wrote
  each line, with authors taken from the history already read. All three
  were checked to give byte-identical reports on three real repositories,
  and the test suite compares the parallel history against `git log`
  directly.
- **History is never read with a pathspec,** even when scanning a
  subdirectory. A pathspec turns on git's history simplification, which drops
  side-branch commits whose net effect vanished in a merge, such as a change
  and its revert. It also breaks rename detection at the directory boundary,
  so a file moved into the scanned directory would look newly written.
  Filtering paths after the fact keeps a file's raw metrics independent of
  the scan's scope.
- **History is read NUL-delimited (`-z`)** and paths are never C-quoted. That
  keeps renames unambiguous even for filenames containing ` => `, and non-ASCII
  paths intact.
- **The exclusion rules are opinionated on purpose.** Tests, vendored code,
  documentation, project manifests, translation catalogs, lockfiles, and
  generated files churn constantly. They would fill the top of every ranking
  without being where the reviewable logic lives, and they would also skew
  the percentiles of the code that is: scores compare each file against the
  rest of the scan. `--include` re-admits any of them.

## Deliberately not included

- **Static-analysis alert density** (SAST / SARIF) as a fourth family.
- **A neglect watchlist** of long-untouched single-author files. That answers
  a different question, on weaker evidence.
- **CI gating** (failing a build when a changed file is in the top N) and
  backtesting against historical CVEs.

## Limitations

- **Correlation, not causation.** A high score means "worth a closer look",
  not "contains a vulnerability".
- **Detection bias at the low end.** A file with little churn may look calm
  only because nobody has examined it. A low score means unranked, not cleared.
- **Comment density is a line-prefix heuristic,** not a per-language lexer.
  It misses block comments and Python docstrings, and it counts lines that
  merely start with `#`: C's `#include` and `#define`, Rust's `#[...]`
  attributes, and shebangs. Broader prefixes were tried and dropped: a bare
  `*` also matches Markdown bullets and C pointer dereferences, and `--`
  matches C decrements.
- **`git blame` is skipped above 5,000 lines or 1 MB;** those files use the
  commit-share fallback for ownership.
- **Renames restart history.** History isn't followed across renames per
  file, so churn and authorship from before a rename (commit counts,
  `distinct_authors`, `bus_factor_flag`, recency) don't carry over to the new
  path. `git blame` itself does follow renames, so `ownership_concentration`
  does.
- **Merge commits are invisible,** and binary changes count as commits
  without lines.
- **The research base is old.** The studies behind these metrics (Nagappan &
  Ball 2005; Shin et al. 2011) are more than 15 years old and studied Windows
  Server 2003, Firefox, and the Red Hat Enterprise Linux kernel. No strong
  recent replication confirms they hold for modern codebases and language
  ecosystems.
- **Nothing predicts vulnerabilities well.** Even LLM-based detectors are
  weak: on SecVulEval (2025), a benchmark of real-world C/C++
  vulnerabilities, the best model tested reached 23.8% F1 at locating the
  vulnerable statements with correct reasoning. Use this ranking to allocate
  limited review time, not to certify that anything it doesn't flag is safe.

## References

- N. Nagappan and T. Ball, "Use of Relative Code Churn Measures to Predict
  System Defect Density," *ICSE*, 2005.
- Y. Shin, A. Meneely, L. Williams, and J. A. Osborne, "Evaluating Complexity,
  Code Churn, and Developer Activity Metrics as Indicators of Software
  Vulnerabilities," *IEEE Transactions on Software Engineering*, 37(6), 2011.
- "SecVulEval: Benchmarking LLMs for Real-World C/C++ Vulnerability
  Detection," arXiv:2505.19828, 2025.
