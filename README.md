# qa-release-watch

A dependency-free release watcher for a QA automation stack.

Polls PyPI RSS and GitHub Atom release feeds, remembers what it already reported,
and surfaces only what is new. Written against the Python standard library only —
no `pip install`, no virtualenv, no dependency drift in the thing whose job is to
tell you about dependency drift.

## Why

Blog posts and search results lag real releases by weeks. Release feeds do not.
This reads the artifacts directly, so a package published yesterday shows up today.

## Install

```bash
git clone <your-repo> qa-release-watch
cd qa-release-watch
python3 qa_release_watch.py --version
```

Python 3.10 or newer. That is the whole install.

## First run

Seed the state file so you are not flooded with the last two years of releases:

```bash
python3 qa_release_watch.py --init
```

Then run it normally:

```bash
python3 qa_release_watch.py
```

## Usage

| Command | Effect |
|---|---|
| `--init` | Seed state, report nothing |
| `--markdown digest.md` | Also write a markdown digest |
| `--telegram` | Push new releases to a Telegram chat |
| `--telegram-test` | Send a test message to verify credentials, then exit |
| `--stable-only` | Ignore alpha / beta / rc releases |
| `--per-feed N` | Inspect N recent entries per feed (default 5) |
| `--dry-run` | Report but do not persist state |
| `--config feeds.json` | Use a custom watchlist |
| `--write-config feeds.json` | Dump the built-in watchlist to edit |

Exit codes: `0` nothing new, `1` new releases found, `2` one or more feeds failed.
The `1` is deliberate — it makes the script usable as a cron or CI signal.

## Default watchlist

Grouped by tier so the output stays readable as the list grows:

- **core** — Robot Framework, Browser library, Browser batteries, Playwright upstream
- **web** — SeleniumLibrary
- **desktop** — FlaUI library, pywinauto, Appium Python client
- **api** — RequestsLibrary, pytest
- **a11y** — axe-core
- **tooling** — Robocop, Pabot

Customise it:

```bash
python3 qa_release_watch.py --write-config feeds.json
# edit feeds.json
python3 qa_release_watch.py --config feeds.json
```

Feed URL patterns, if you want to add your own:

- PyPI: `https://pypi.org/rss/project/<package>/releases.xml`
- GitHub: `https://github.com/<owner>/<repo>/releases.atom`

## Scheduling

**Linux / macOS** — daily at 08:00:

```
0 8 * * * /usr/bin/python3 /path/to/qa_release_watch.py --markdown /path/to/digest.md
```

**Windows** — Task Scheduler, daily trigger:

```
Program:   pythonw.exe
Arguments: C:\path\to\qa_release_watch.py --telegram
```

## Telegram push

1. In Telegram, message `@BotFather` → `/newbot` → pick a name and a username
   ending in `bot`. It replies with a token like `8123456789:AAH...`.
2. **Open your new bot and press Start.** Telegram forbids bots from messaging a
   user who has never messaged them — skipping this is the usual cause of a 403.
3. Get your chat id: message `@userinfobot`, which replies with your numeric id.
4. Set both values in the environment and verify:

```bash
export TELEGRAM_BOT_TOKEN="8123456789:AAH..."
export TELEGRAM_CHAT_ID="987654321"
python3 qa_release_watch.py --telegram-test
```

`--telegram-test` sends a throwaway message and exits, so you can confirm the
wiring without waiting for a real release. Once it lands, run for real:

```bash
python3 qa_release_watch.py --telegram
```

Nothing is sent when nothing is new — no daily "all quiet" noise.

Credentials are read from the environment only — never from the config file, and
never written to state. Keep them out of the repo.

### Windows

`set` only lasts for the current shell, and Task Scheduler starts a fresh one.
Either store them permanently:

```
setx TELEGRAM_BOT_TOKEN "8123456789:AAH..."
setx TELEGRAM_CHAT_ID "987654321"
```

...or wrap the call in a `.bat` file that sets them before invoking the script,
and point Task Scheduler at the `.bat`.

### GitHub Actions

Add both as repository secrets (Settings → Secrets and variables → Actions), then
add to the workflow step:

```yaml
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
```

...and append `--telegram` to the command.

## GitHub Actions

`.github/workflows/watch.yml` runs the watcher every weekday morning, commits the
digest back to the repo, and keeps state in the repo so runs stay stateful across
ephemeral runners. Nothing to install on the runner.

## Design notes

- **State is atomic.** Written to a temp file then renamed, so an interrupted run
  never leaves a corrupt state file that silently re-reports everything.
- **A bad feed does not kill the run.** Failures are collected and reported at the
  end; every other feed still reports.
- **Retries with backoff.** Three attempts, exponential, before a feed is called failed.
- **Feed order is not trusted.** Entries are sorted by publish date before slicing,
  so a feed that emits oldest-first still yields the newest releases.
- **State is capped.** Only the most recent identifiers per feed are retained, so
  the state file does not grow without bound.
