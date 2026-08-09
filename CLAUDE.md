# CLAUDE.md

Project context for Claude Code sessions. Read this before proposing changes.

## What this is

`qa_release_watch.py` — a release watcher for a Robot Framework-centred QA
automation stack. It polls PyPI RSS and GitHub Atom release feeds, remembers what
it already reported, and surfaces only what is new.

## Why it exists

Blog posts, comparison articles, and search results lag real package releases by
weeks, and are frequently re-dated older content. Release feeds do not lag. The
goal is same-day awareness of releases in the stack — if a package ships on a
Tuesday, it should be on the phone Wednesday morning.

This was validated on build day: the watcher surfaced axe-core 4.13.0 and
Robocop 8.6.0, both released within 72 hours, neither of which appeared in any
blog or search result consulted at the time.

## Hard constraints — do not violate without discussion

- **Python standard library only.** No pip, no venv, no requirements.txt. A
  dependency watcher that accumulates dependencies defeats itself, and this must
  run on a locked-down corporate laptop with no install rights.
- **Python 3.10+.** Uses `X | Y` union syntax in annotations.
- **Credentials from environment only.** `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` are never read from config files and never written to state.
  This repo may go public.
- **No silent failures.** One bad feed must not kill the run; errors are collected
  and reported at the end.

## Design decisions that look odd but are deliberate

- **Exit code 1 means "new releases found", not "error".** Codes are: 0 nothing
  new, 1 new releases, 2 a feed or push failed. This makes the script usable as a
  cron/CI signal. It also means any shell running under `set -e` or `bash -e`
  (including GitHub Actions `run:` steps) will abort on a normal successful run
  with new releases. Guard accordingly — see `.github/workflows/watch.yml`.
- **Telegram output is plain text, no `parse_mode`.** Telegram's Markdown parser
  rejects messages containing unescaped underscores and dashes, both of which are
  ubiquitous in package names and GitHub release URLs. Do not "improve" this by
  adding bold formatting.
- **State writes are atomic** (temp file then `replace`). An interrupted run must
  never leave a corrupt state file, because the failure mode is silently
  re-reporting two years of release history.
- **Feed order is not trusted.** Entries are sorted by publish date before
  slicing. PyPI and GitHub both emit newest-first today, but that is not a
  contract.
- **State is capped** at `per_feed * 3` identifiers per feed so it cannot grow
  without bound.

## Layout

```
qa_release_watch.py          single file, no package structure
README.md                    user-facing setup and usage
.github/workflows/watch.yml  scheduled run, commits digest back to repo
~/.qa_release_watch/state.json   default state location (not in repo)
```

## Watchlist

Feeds are grouped by tier (`core`, `web`, `desktop`, `api`, `a11y`, `tooling`)
purely so console output stays readable as the list grows. Defaults live in
`DEFAULT_FEEDS`; users override via `--write-config` then `--config`.

Feed URL patterns:
- PyPI: `https://pypi.org/rss/project/<package>/releases.xml`
- GitHub: `https://github.com/<owner>/<repo>/releases.atom`

## Testing notes

The pure functions are the testable surface and have no I/O:
`extract_version`, `is_prerelease`, `normalise_date`, `parse_feed`, `sort_releases`.
`fetch` and `push_telegram` are the only network calls; stub them.

There is no test suite yet. Adding one under `tests/` using stdlib `unittest` is
the next planned change — stdlib, to preserve the zero-dependency constraint.

## Not yet done

- [ ] `tests/` covering the pure functions
- [ ] `--json` output mode for downstream consumption
- [ ] git init and push to GitHub (the Actions workflow cannot run until then)
