#!/usr/bin/env python3
"""
qa_release_watch.py — daily release watcher for a QA automation stack.

Polls PyPI RSS feeds and GitHub Atom release feeds, remembers what it has
already seen, and reports only what is new since the last run.

Standard library only. No pip install, no venv, no dependency drift.

Typical use:
    python qa_release_watch.py --init          # seed state, report nothing
    python qa_release_watch.py                 # daily run, prints new releases
    python qa_release_watch.py --markdown digest.md
    python qa_release_watch.py --telegram      # push via env vars

Exit codes:
    0  ran fine, nothing new
    1  ran fine, new releases found   (useful as a CI / cron signal)
    2  one or more feeds failed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"

USER_AGENT = f"qa-release-watch/{__version__} (+release monitoring; stdlib urllib)"
DEFAULT_TIMEOUT = 20
DEFAULT_RETRIES = 3

# XML namespaces we care about when parsing Atom.
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# Version-ish token in a release title, e.g. "7.5b1", "v1.62.0", "20.2.0".
VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)+(?:[a-z]+\d*)?)", re.IGNORECASE)
# PEP 440 / semver pre-release markers.
PRERELEASE_RE = re.compile(r"(?:\d)(?:a|b|rc|alpha|beta|dev|pre)\d*\b|-(?:alpha|beta|rc)", re.IGNORECASE)


# --------------------------------------------------------------------------
# Feed definitions
# --------------------------------------------------------------------------

def pypi_feed(package: str) -> str:
    return f"https://pypi.org/rss/project/{package}/releases.xml"


def github_feed(repo: str) -> str:
    return f"https://github.com/{repo}/releases.atom"


# The default watchlist: a Robot-Framework-centred QA automation stack.
# Edit feeds.json (written by --write-config) rather than editing this.
DEFAULT_FEEDS = [
    # Core runner
    {"name": "Robot Framework", "url": pypi_feed("robotframework"), "tier": "core"},
    # Web
    {"name": "Browser library (Playwright)", "url": pypi_feed("robotframework-browser"), "tier": "core"},
    {"name": "Browser batteries", "url": pypi_feed("robotframework-browser-batteries"), "tier": "core"},
    {"name": "SeleniumLibrary", "url": pypi_feed("robotframework-seleniumlibrary"), "tier": "web"},
    {"name": "Playwright (upstream)", "url": github_feed("microsoft/playwright"), "tier": "core"},
    # Desktop
    {"name": "FlaUI library", "url": pypi_feed("robotframework-flaui"), "tier": "desktop"},
    {"name": "pywinauto", "url": pypi_feed("pywinauto"), "tier": "desktop"},
    {"name": "Appium Python client", "url": pypi_feed("Appium-Python-Client"), "tier": "desktop"},
    # API / services
    {"name": "RequestsLibrary", "url": pypi_feed("robotframework-requests"), "tier": "api"},
    {"name": "pytest", "url": pypi_feed("pytest"), "tier": "api"},
    # Quality of the tests themselves
    {"name": "Robocop (lint/format)", "url": pypi_feed("robotframework-robocop"), "tier": "tooling"},
    {"name": "Pabot (parallel)", "url": pypi_feed("robotframework-pabot"), "tier": "tooling"},
    # Accessibility
    {"name": "axe-core", "url": github_feed("dequelabs/axe-core"), "tier": "a11y"},
]


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Release:
    feed: str
    tier: str
    title: str
    version: str
    url: str
    published: str  # ISO-8601 or "" if the feed omitted it
    prerelease: bool

    def uid(self) -> str:
        """Stable identity for dedupe. URL first; falls back to title."""
        return self.url or f"{self.feed}::{self.title}"


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch(url: str, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES) -> bytes:
    """GET a URL with retries and exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/rss+xml, */*"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"failed after {retries} attempts: {last_error}")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def normalise_date(raw: str) -> str:
    """Best-effort conversion of RSS/Atom dates to ISO-8601 (UTC)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    # Atom: 2026-08-01T12:00:00Z
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    # RSS 2.0: Sat, 01 Aug 2026 12:00:00 GMT
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return raw


def extract_version(title: str) -> str:
    match = VERSION_RE.search(title or "")
    return match.group(1) if match else ""


def is_prerelease(title: str, version: str) -> bool:
    return bool(PRERELEASE_RE.search(version) or PRERELEASE_RE.search(title or ""))


def parse_feed(name: str, tier: str, payload: bytes) -> list[Release]:
    """Parse either RSS 2.0 (PyPI) or Atom (GitHub) into Release records."""
    root = ET.fromstring(payload)
    releases: list[Release] = []

    # --- RSS 2.0 -----------------------------------------------------------
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = normalise_date(item.findtext("pubDate") or "")
        version = extract_version(title)
        releases.append(
            Release(
                feed=name,
                tier=tier,
                title=title,
                version=version,
                url=link,
                published=published,
                prerelease=is_prerelease(title, version),
            )
        )

    # --- Atom --------------------------------------------------------------
    for entry in root.findall("atom:entry", NS):
        title = (entry.findtext("atom:title", default="", namespaces=NS) or "").strip()
        link_el = entry.find("atom:link", NS)
        link = link_el.get("href", "").strip() if link_el is not None else ""
        published = normalise_date(
            entry.findtext("atom:updated", default="", namespaces=NS)
            or entry.findtext("atom:published", default="", namespaces=NS)
        )
        version = extract_version(title)
        releases.append(
            Release(
                feed=name,
                tier=tier,
                title=title,
                version=version,
                url=link,
                published=published,
                prerelease=is_prerelease(title, version),
            )
        )

    return releases


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if not path.exists():
        return {"seen": {}, "last_run": None}
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except (json.JSONDecodeError, OSError):
        # A corrupt state file should not break the run; start clean.
        return {"seen": {}, "last_run": None}
    state.setdefault("seen", {})
    state.setdefault("last_run", None)
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    temp.replace(path)  # atomic-ish: never leave a half-written state file


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

TIER_ORDER = ["core", "web", "desktop", "api", "a11y", "tooling"]


def sort_releases(releases: list[Release]) -> list[Release]:
    def key(release: Release):
        tier_rank = TIER_ORDER.index(release.tier) if release.tier in TIER_ORDER else len(TIER_ORDER)
        return (tier_rank, release.feed, release.published)

    return sorted(releases, key=key, reverse=False)


def render_console(releases: list[Release], errors: list[tuple[str, str]], use_colour: bool) -> str:
    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if use_colour else text

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [paint(f"QA stack releases — {stamp}", "1")]

    if not releases:
        lines.append("  nothing new")
    else:
        current_tier = None
        for release in sort_releases(releases):
            if release.tier != current_tier:
                current_tier = release.tier
                lines.append("")
                lines.append(paint(f"[{current_tier}]", "1;36"))
            tag = paint(" pre", "33") if release.prerelease else paint("    ", "0")
            date = release.published[:10] if release.published else "          "
            lines.append(f" {tag}  {date}  {paint(release.feed, '1;32')}  {release.version or release.title}")
            if release.url:
                lines.append(f"          {release.url}")

    if errors:
        lines.append("")
        lines.append(paint("feed errors:", "1;31"))
        for name, message in errors:
            lines.append(f"  {name}: {message}")

    return "\n".join(lines)


def render_markdown(releases: list[Release], errors: list[tuple[str, str]]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# QA stack releases — {stamp}", ""]

    if not releases:
        lines.append("_No new releases since the last run._")
    else:
        current_tier = None
        for release in sort_releases(releases):
            if release.tier != current_tier:
                current_tier = release.tier
                lines.append("")
                lines.append(f"## {current_tier}")
                lines.append("")
            flag = " `pre-release`" if release.prerelease else ""
            date = f" — {release.published[:10]}" if release.published else ""
            label = release.version or release.title
            link = f"[{label}]({release.url})" if release.url else label
            lines.append(f"- **{release.feed}** {link}{flag}{date}")

    if errors:
        lines.append("")
        lines.append("## Feed errors")
        lines.append("")
        for name, message in errors:
            lines.append(f"- `{name}`: {message}")

    lines.append("")
    return "\n".join(lines)


def render_telegram(releases: list[Release]) -> str:
    """Plain text on purpose: Telegram's Markdown parser chokes on the
    underscores and dashes that appear in package names and release URLs."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts = [f"QA stack releases — {stamp}", ""]
    for release in sort_releases(releases):
        flag = " (pre-release)" if release.prerelease else ""
        parts.append(f"• {release.feed} {release.version or release.title}{flag}")
        if release.url:
            parts.append(f"  {release.url}")
    return "\n".join(parts)


def _redact(text: str) -> str:
    """Strip the bot token from any string bound for a log.

    urllib embeds the full request URL in HTTPError.url, and the Telegram API
    puts the token *in the path*. GitHub Actions masks registered secrets, but
    cron and local runs have no such net, so redact at the source.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token and token in text:
        text = text.replace(token, "<redacted>")
    # Belt and braces: catch any bot<token>/ pattern regardless of env state.
    return re.sub(r"/bot[^/\s]+/", "/bot<redacted>/", text)


TELEGRAM_LIMIT = 4096
TELEGRAM_CHUNK = 2500  # conservative chunk size accounting for URL encoding and parameter overhead


def chunk_message(text: str, limit: int = TELEGRAM_CHUNK) -> list[str]:
    """Split a message into Telegram-sized pieces, breaking on line boundaries.

    Telegram rejects anything over 4096 characters with a 400. When the message
    is URL-encoded as a POST parameter (chat_id=...&text=...&...), the actual
    payload can grow significantly. We use a conservative chunk size that accounts
    for this overhead plus the part counter suffix.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for line in text.split("\n"):
        # A single line longer than the limit would loop forever; hard-split it.
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]

        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0

        current.append(line)
        size += len(line) + 1

    if current:
        chunks.append("\n".join(current))

    total = len(chunks)
    return [f"{c}\n\n({i} of {total})" for i, c in enumerate(chunks, 1)]


def push_telegram(message: str) -> None:
    """Send a message, splitting it if it exceeds Telegram's size limit."""
    parts = chunk_message(message)
    for index, part in enumerate(parts):
        _send_one(part)
        if index < len(parts) - 1:
            time.sleep(0.5)  # stay under Telegram's per-chat rate limit


def _send_one(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set")

    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}
    ).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Telegram puts the useful part in the body, not the status line.
        detail = ""
        try:
            detail = json.loads(exc.read()).get("description", "")
        except Exception:  # noqa: BLE001
            pass
        hint = ""
        if exc.code == 401:
            hint = " — check TELEGRAM_BOT_TOKEN"
        elif exc.code == 400 and "chat not found" in detail.lower():
            hint = " — check TELEGRAM_CHAT_ID, and send your bot a message first"
        elif exc.code == 403:
            hint = " — open the chat and press Start so the bot may message you"
        raise RuntimeError(_redact(f"Telegram API {exc.code}: {detail or exc.reason}{hint}")) from exc

    if not body.get("ok"):
        raise RuntimeError(_redact(f"Telegram rejected the message: {body.get('description', body)}"))


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_feeds(path: Path | None) -> list[dict]:
    if path is None:
        return DEFAULT_FEEDS
    with path.open(encoding="utf-8") as handle:
        feeds = json.load(handle)
    if not isinstance(feeds, list):
        raise ValueError("config must be a JSON list of {name, url, tier} objects")
    for feed in feeds:
        feed.setdefault("tier", "other")
        if "name" not in feed or "url" not in feed:
            raise ValueError(f"feed entry missing name or url: {feed}")
    return feeds


def write_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(DEFAULT_FEEDS, handle, indent=2)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def telegram_self_test() -> int:
    """Send a throwaway message so wiring can be verified on demand."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message = (
        "qa-release-watch test message\n"
        f"Sent {stamp}\n\n"
        "If you can read this, notifications are wired up correctly."
    )
    try:
        push_telegram(message)
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram test FAILED: {_redact(str(exc))}", file=sys.stderr)
        return 2
    print("Telegram test message sent — check your chat.")
    return 0


def run(args: argparse.Namespace) -> int:
    feeds = load_feeds(Path(args.config) if args.config else None)
    state_path = Path(args.state).expanduser()
    state = load_state(state_path)
    seen: dict = state["seen"]

    new_releases: list[Release] = []
    errors: list[tuple[str, str]] = []

    for feed in feeds:
        name, url, tier = feed["name"], feed["url"], feed.get("tier", "other")
        try:
            payload = fetch(url, timeout=args.timeout)
            releases = parse_feed(name, tier, payload)
        except Exception as exc:  # noqa: BLE001 — one bad feed must not kill the run
            errors.append((name, str(exc)))
            continue

        # Newest entries only; feeds are long and history is not interesting.
        # PyPI and GitHub both emit newest-first, but do not rely on it.
        releases.sort(key=lambda r: r.published or "", reverse=True)
        releases = releases[: args.per_feed]

        known = set(seen.get(name, []))
        fresh = [r for r in releases if r.uid() not in known]

        if args.stable_only:
            fresh = [r for r in fresh if not r.prerelease]

        if not args.init:
            new_releases.extend(fresh)

        # Remember everything we saw, capped so state does not grow forever.
        seen[name] = [r.uid() for r in releases][: args.per_feed * 3] or seen.get(name, [])

    state["last_run"] = datetime.now(timezone.utc).isoformat()

    if args.init:
        if not args.dry_run:
            save_state(state_path, state)
        print(f"Seeded state for {len(feeds) - len(errors)} feed(s) at {state_path}")
        if errors:
            for name, message in errors:
                print(f"  warning — {name}: {message}", file=sys.stderr)
        return 2 if errors else 0

    use_colour = sys.stdout.isatty() and not args.no_colour
    print(render_console(new_releases, errors, use_colour))

    if args.markdown:
        Path(args.markdown).write_text(render_markdown(new_releases, errors), encoding="utf-8")
        print(f"\nWrote {args.markdown}")

    # State is persisted only AFTER a successful notification. Saving first
    # would mark these releases as seen even when the push failed, and they
    # would never be reported again — silent data loss. Retrying a duplicate
    # is a far cheaper failure than never hearing about a release.
    notified = True
    if args.telegram and new_releases:
        try:
            push_telegram(render_telegram(new_releases))
            print("Pushed to Telegram")
        except Exception as exc:  # noqa: BLE001
            notified = False
            print(f"Telegram push failed: {_redact(str(exc))}", file=sys.stderr)
            print("State not saved — these releases will be retried next run.", file=sys.stderr)
            errors.append(("telegram", _redact(str(exc))))

    if not args.dry_run and notified:
        save_state(state_path, state)

    if errors:
        return 2
    return 1 if new_releases else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qa_release_watch",
        description="Watch PyPI and GitHub release feeds for a QA automation stack.",
    )
    parser.add_argument("--config", help="path to feeds.json (defaults to the built-in watchlist)")
    parser.add_argument("--write-config", metavar="PATH", help="write the built-in watchlist to PATH and exit")
    parser.add_argument(
        "--state",
        default="~/.qa_release_watch/state.json",
        help="where to keep the seen-releases state (default: %(default)s)",
    )
    parser.add_argument("--init", action="store_true", help="seed state without reporting anything")
    parser.add_argument("--markdown", metavar="PATH", help="also write a markdown digest to PATH")
    parser.add_argument("--telegram", action="store_true", help="push new releases via Telegram bot")
    parser.add_argument(
        "--telegram-test",
        action="store_true",
        help="send a test message to verify credentials, then exit",
    )
    parser.add_argument("--stable-only", action="store_true", help="ignore alpha/beta/rc releases")
    parser.add_argument("--per-feed", type=int, default=5, help="how many recent entries to inspect per feed")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="per-request timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="report but do not persist state")
    parser.add_argument("--no-colour", action="store_true", help="disable ANSI colour")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.write_config:
        write_config(Path(args.write_config))
        print(f"Wrote {args.write_config} — edit it and pass --config {args.write_config}")
        return 0
    if args.telegram_test:
        return telegram_self_test()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
