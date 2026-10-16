# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [0.1.0] - 2026-10-15

First public release.

### Added

- `heatmap scan` ranks every first-party file in a git repository by a 0–100
  risk score built from churn, complexity, and developer activity. It writes
  `risk_report.csv`, `risk_report.json`, `summary.md`, and `hot_files.txt`:
  the highest-risk paths, one per line, highest first, for feeding to other
  tools.
- `heatmap explain` breaks a file's score down to each metric's percentile.
  It takes several files at once, and `--report` reuses a previous scan's
  JSON instead of rescanning. Any clone of the scanned repository can
  explain a report, as long as it contains the commit the report scanned.
- Options: `--since` / `--until` for the churn window, `--exclude` globs,
  `--include` globs, `--weights` for the family weights, `--no-blame` to skip
  `git blame`, and `--top` for how many files `summary.md` and
  `hot_files.txt` list: a count (`--top 30`) or a share of the files scored
  (`--top 10%`, the default `20%`, rounded up so a non-empty scan always
  lists at least one file).
- Built-in exclusions for tests, vendored and build output, documentation
  (`docs/` directories, prose files such as `*.md`, `*.rst`, and `*.txt`, and
  files such as `CHANGELOG`, `LICENSE.*`, and `COPYING.*`), project
  manifests, translation catalogs, lockfiles, generated and binary files,
  Terraform, and extensionless files. `--include` re-admits anything a
  built-in rule leaves out, and `explain` says so when a built-in rule is
  why a file has no score.
  - Manifests (`Cargo.toml`, `package.json`, `pyproject.toml`, `go.mod`,
    `requirements*.txt`, …) are left out because they change with every
    version bump and dependency update: on ripgrep, `Cargo.toml`,
    `README.md`, and `CHANGELOG.md` were the top three files, ahead of any
    code. Dependency review is a job for a dependency scanner, as with the
    lockfiles already excluded. CI workflows are still scored, since they
    can be attack surface.
- Scoping a scan to a subdirectory, and scanning bare repositories.
- Warnings, with exit status 0, when a scan scores nothing: a repository
  with no commits, or one whose every file an exclusion rule drops.
- A Python API in the `repo_heatmap` package (named for the distribution,
  so it can't clash with the unrelated `heatmap` project on PyPI):
  `repo_heatmap.scan()`, `ScanOptions`, `ScanResult`, and
  `repo_heatmap.report.read_json()`. `python -m repo_heatmap` runs the CLI.
- The JSON report records the heatmap and lizard versions that produced it.
- Python 3.11 to 3.15.

### Security

- Safe to point at a clone of an untrusted repository. Everything is read
  from `HEAD` with read-only git commands: nothing is checked out, no hooks
  or filters run, git never touches the network, and the repository is never
  written to.
- File names are escaped wherever they're shown, the terminal, the CSV, the
  summary, and `hot_files.txt`, so they can't inject terminal escape
  sequences or pose as another name with invisible or bidirectional
  characters. CSV paths that start with a formula character are prefixed so
  spreadsheets show them as text, and bytes that aren't valid UTF-8 are
  written as `\xNN`. The JSON report keeps exact paths in JSON escapes, so
  printing it is safe too.
- Reports name the scanned repository by its directory name, never its
  absolute path, so sharing one doesn't reveal the local user name or
  directory layout.
- Shallow and partial clones are refused with an explanation, rather than
  producing a silently wrong ranking or fetching data.
- A repository can't steer its own ranking: its `.gitattributes`,
  replacement objects, and grafts are ignored (so it can't hide a file's
  history or swap in a decoy), and settings in its config or yours that
  would change results are pinned.
- Hostile content is contained: files over 1 MB are counted but neither
  parsed nor blamed, identical files are measured once, and a file that
  crashes the complexity parser or keeps it busy for 10 seconds is scored
  as neutral. Author names and `.mailmap` files are size-capped.
- Dates git can't parse, which git would silently treat as "now", are
  rejected, as are a `--top` of 0 and a report whose commit id is malformed.

[0.1.0]: https://github.com/omaiddotsh/repo-heatmap/releases/tag/v0.1.0
