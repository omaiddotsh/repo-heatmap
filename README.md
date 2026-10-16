# repo-heatmap

Find the files in a git repository that most deserve a security review.

`heatmap` ranks every first-party source file by a 0–100 risk score built from
its history and its code: how often it changes, how complex it is, and how its
ownership is spread. Run it before a review to decide where limited time goes
first.

```console
$ heatmap scan ~/src/click
Reading 44 files and the commit history
Running git blame on 41 files
Scored 44 files. Wrote risk_report.csv, risk_report.json, summary.md, hot_files.txt to risk_report
```

| Rank | File | Risk | Churn | Complexity | Activity | Why |
|---:|---|---:|---:|---:|---:|---|
| 1 | `src/click/core.py` | 92.5 | 98.3 | 69.4 | 46.5 | high churn |
| 2 | `src/click/types.py` | 89.4 | 94.8 | 67.8 | 48.8 | high churn |
| 3 | `src/click/termui.py` | 87.0 | 90.9 | 71.1 | 47.7 | high churn, high complexity |
| 4 | `.github/workflows/tests.yaml` | 82.5 | 90.6 | 50.0 | 17.4 | high churn |
| 5 | `src/click/_termui_impl.py` | 82.0 | 87.6 | 60.0 | 50.0 | high churn |

*The first five rows of `summary.md` for [pallets/click](https://github.com/pallets/click) at commit `2247b35`.*

- **Offline and read-only.** It works on a local clone, never touches the
  network, and never writes to the repository.
- **Built for untrusted code.** Nothing is checked out or run, the repository
  can't steer its own ranking, and hostile file names can't reach your
  terminal or spreadsheet unescaped. See
  [Untrusted repositories](docs/usage.md#untrusted-repositories).
- **Explainable.** `heatmap explain` shows how any score was built, metric by
  metric.
- **Reproducible.** The same commit, scanned with the same heatmap and lizard
  versions, gets the same scores on macOS or Linux, from a clean checkout, a
  dirty one, or a bare mirror.

## Install

Requires Python 3.11+ and git 2.39+, on macOS or Linux (Windows is untested).

```bash
pipx install git+https://github.com/omaiddotsh/repo-heatmap   # adds a `heatmap` command
```

Or, from a clone of this repository, `pipx install .` (or `pip install .`).
The Python package is `repo_heatmap`, and `python -m repo_heatmap` runs the
same command.

The repository you scan must be a full clone. Shallow and partial clones are
refused, because their history or contents are incomplete.

## Quick start

```bash
heatmap scan path/to/repo                    # writes ./risk_report/
heatmap scan path/to/repo/services/api       # score one subdirectory
heatmap scan path/to/repo --since "1 year ago" -o reports/
heatmap explain path/to/repo src/auth/session.py src/auth/token.py
heatmap explain path/to/repo src/auth/session.py --report risk_report  # instant
```

`scan` writes four reports:

| File | Contents |
|---|---|
| `risk_report.csv` | One row per file: scores, then every raw metric |
| `risk_report.json` | The same rows at full precision, plus the run's settings |
| `summary.md` | The top 20% of files with a one-line reason each |
| `hot_files.txt` | The same top 20% of paths, one per line, for other tools |

`--top` changes how many files the last two hold: a count (`--top 30`) or a
share of the files scored (`--top 10%`).

`explain` prints how a file's score was built: each metric's raw value and
the 0–100 score it earned. Use it when a ranking surprises you. With
`--report` it reads the scan you already ran instead of repeating it.

Progress goes to stderr and reports go to files, so stdout stays clean.

## How a file is scored

1. **Measure** each file. Churn comes from its commit history, complexity
   from [lizard](https://github.com/terryyin/lizard), and ownership from
   `git blame`.
2. **Rank** each measurement as a percentile (0–100) among the files in the
   scan.
3. **Combine** the percentiles into three family scores: churn, complexity,
   and developer activity.
4. **Weight** the families into `risk_score`. The defaults are churn 0.8,
   complexity 0.2, and activity 0; `--weights` changes them.

Activity is measured and reported but weighted zero by default, because the
ownership metrics didn't improve the ranking on the codebase the defaults
were tuned against ([why](docs/methodology.md#why-these-defaults)). `git
blame` still runs by default so the ownership column is filled in.
`--no-blame` skips it and leaves the ranking unchanged under the default
weights. On Django (35,000 commits, 1,150 files scored), a scan takes 5.6 s
with blame and 2.2 s without, on a 16-core Apple M3 Max.

The scores are relative. A file's score says where it sits in *this*
repository, not how risky it is in absolute terms. A high score means "look
here first", not "this contains a vulnerability", and a low score means
"unranked", not "safe". [docs/methodology.md](docs/methodology.md) defines
every metric, explains the defaults, and lists the limitations.

## What gets scored

Every regular file tracked at `HEAD`, except:

- test code (`tests/`, `__tests__/`, `fixtures/`, `test_*.py`, `*.spec.ts`, …)
- vendored and build output (`vendor/`, `node_modules/`, `dist/`, `build/`)
- documentation: `docs/` directories, prose files (`*.md`, `*.rst`, `*.txt`,
  …), and files such as `CHANGELOG`, `LICENSE.*`, and `COPYING.*`
- project manifests (`Cargo.toml`, `package.json`, `pyproject.toml`,
  `go.mod`, `requirements*.txt`, …), lockfiles, and translation catalogs
- minified files, source maps, images, and Terraform
- files with no extension, such as `Makefile` and `.gitignore`
- anything matching an `--exclude` glob you pass

`--include GLOB` scores files a built-in rule leaves out, such as
`--include '*.md'` or `--include tests/`. Symlinks and submodules are skipped.
CI configuration such as `.github/workflows/` is scored, since a workflow can
be attack surface, and it changes often (click's test workflow is fourth
above); add `--exclude .github/` if it crowds your results. The full rules
and the glob syntax are in [docs/usage.md](docs/usage.md#what-gets-scored).

## Documentation

- [docs/usage.md](docs/usage.md): commands, options, exclusions, output formats, and performance
- [docs/methodology.md](docs/methodology.md): the metrics, the scoring model, and its limits
- [docs/architecture.md](docs/architecture.md): code layout, the library API, extending, and testing
- [CHANGELOG.md](CHANGELOG.md): release notes

## Contributing

Bug reports and pull requests are welcome. Please add a test for any change
in behaviour, and run the checks before sending one:

```bash
pip install -e '.[dev]'   # editable, plus pytest, ruff, and mypy
pytest
ruff check . && ruff format --check .
mypy
```

## License

MIT. See [LICENSE](LICENSE).
