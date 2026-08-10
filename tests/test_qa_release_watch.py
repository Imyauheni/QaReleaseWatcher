#!/usr/bin/env python3
"""
Test suite for qa_release_watch.

Standard library `unittest` only, to preserve the project's zero-dependency
constraint. No network access: feed parsing is exercised against fixtures, and
the two I/O functions (`fetch`, `push_telegram`) are stubbed.

Run:
    python3 -m unittest discover -s tests -v
    python3 tests/test_qa_release_watch.py
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class QuietMixin:
    """Suppress the script's console output so the test log shows only results."""

    def silence(self):
        for stream in ("stdout", "stderr"):
            patcher = mock.patch(f"sys.{stream}", new_callable=io.StringIO)
            patcher.start()
            self.addCleanup(patcher.stop)


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qa_release_watch as watcher  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — trimmed but structurally faithful to the real feeds
# ---------------------------------------------------------------------------

PYPI_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>robotframework releases</title>
    <item>
      <title>7.5b1</title>
      <link>https://pypi.org/project/robotframework/7.5b1/</link>
      <pubDate>Fri, 17 Jul 2026 08:00:00 GMT</pubDate>
    </item>
    <item>
      <title>7.4.2</title>
      <link>https://pypi.org/project/robotframework/7.4.2/</link>
      <pubDate>Tue, 03 Mar 2026 10:30:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

GITHUB_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Release notes from playwright</title>
  <entry>
    <id>tag:github.com,2008:Repository/1/v1.62.1</id>
    <title>v1.62.1</title>
    <link rel="alternate" type="text/html"
          href="https://github.com/microsoft/playwright/releases/tag/v1.62.1"/>
    <updated>2026-07-30T14:00:00Z</updated>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/1/v1.62.0</id>
    <title>v1.62.0</title>
    <link rel="alternate" type="text/html"
          href="https://github.com/microsoft/playwright/releases/tag/v1.62.0"/>
    <updated>2026-07-24T09:15:00Z</updated>
  </entry>
</feed>
"""

# Deliberately oldest-first, to prove the parser does not trust feed order.
REVERSED_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>1.0.0</title>
      <link>https://example.invalid/1.0.0</link>
      <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
    </item>
    <item>
      <title>2.0.0</title>
      <link>https://example.invalid/2.0.0</link>
      <pubDate>Wed, 01 Jan 2025 00:00:00 GMT</pubDate>
    </item>
    <item>
      <title>3.0.0</title>
      <link>https://example.invalid/3.0.0</link>
      <pubDate>Thu, 01 Jan 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def make_release(**overrides) -> watcher.Release:
    defaults = dict(
        feed="Robot Framework",
        tier="core",
        title="7.4.2",
        version="7.4.2",
        url="https://pypi.org/project/robotframework/7.4.2/",
        published="2026-03-03T10:30:00+00:00",
        prerelease=False,
    )
    defaults.update(overrides)
    return watcher.Release(**defaults)


# ---------------------------------------------------------------------------
# Version extraction
# ---------------------------------------------------------------------------

class TestExtractVersion(unittest.TestCase):
    def test_plain_semver(self):
        self.assertEqual(watcher.extract_version("20.2.0"), "20.2.0")

    def test_strips_leading_v(self):
        self.assertEqual(watcher.extract_version("v1.62.1"), "1.62.1")

    def test_version_embedded_in_title(self):
        self.assertEqual(watcher.extract_version("Browser library v20.2.0 released"), "20.2.0")

    def test_pep440_prerelease_suffix_retained(self):
        self.assertEqual(watcher.extract_version("robotframework 7.5b1"), "7.5b1")

    def test_two_component_version(self):
        self.assertEqual(watcher.extract_version("Release 8.6"), "8.6")

    def test_no_version_returns_empty(self):
        self.assertEqual(watcher.extract_version("Nightly build"), "")

    def test_empty_title_is_safe(self):
        self.assertEqual(watcher.extract_version(""), "")


# ---------------------------------------------------------------------------
# Pre-release detection
# ---------------------------------------------------------------------------

class TestIsPrerelease(unittest.TestCase):
    def test_pep440_beta(self):
        self.assertTrue(watcher.is_prerelease("robotframework 7.5b1", "7.5b1"))

    def test_pep440_alpha(self):
        self.assertTrue(watcher.is_prerelease("7.4a1", "7.4a1"))

    def test_pep440_rc(self):
        self.assertTrue(watcher.is_prerelease("7.4rc2", "7.4rc2"))

    def test_semver_dash_rc(self):
        self.assertTrue(watcher.is_prerelease("4.11.0-rc.1", "4.11.0"))

    def test_stable_release_is_not_prerelease(self):
        self.assertFalse(watcher.is_prerelease("v1.62.1", "1.62.1"))

    def test_version_containing_b_in_number_is_not_prerelease(self):
        """Guards against a naive 'contains b' check misreading ordinary digits."""
        self.assertFalse(watcher.is_prerelease("20.2.0", "20.2.0"))


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

class TestNormaliseDate(unittest.TestCase):
    def test_rss_pubdate_gmt(self):
        self.assertEqual(
            watcher.normalise_date("Sat, 01 Aug 2026 12:00:00 GMT"),
            "2026-08-01T12:00:00+00:00",
        )

    def test_atom_iso_with_z(self):
        self.assertEqual(
            watcher.normalise_date("2026-08-01T12:00:00Z"),
            "2026-08-01T12:00:00+00:00",
        )

    def test_offset_is_converted_to_utc(self):
        self.assertEqual(
            watcher.normalise_date("2026-08-01T14:00:00+02:00"),
            "2026-08-01T12:00:00+00:00",
        )

    def test_empty_string(self):
        self.assertEqual(watcher.normalise_date(""), "")

    def test_unparseable_date_is_passed_through_not_crashed(self):
        self.assertEqual(watcher.normalise_date("sometime last Tuesday"), "sometime last Tuesday")


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

class TestParseFeed(unittest.TestCase):
    def test_parses_rss(self):
        releases = watcher.parse_feed("Robot Framework", "core", PYPI_RSS)
        self.assertEqual(len(releases), 2)
        self.assertEqual(releases[0].version, "7.5b1")
        self.assertTrue(releases[0].prerelease)
        self.assertEqual(releases[1].version, "7.4.2")
        self.assertFalse(releases[1].prerelease)

    def test_parses_atom(self):
        releases = watcher.parse_feed("Playwright", "core", GITHUB_ATOM)
        self.assertEqual(len(releases), 2)
        self.assertEqual(releases[0].version, "1.62.1")
        self.assertEqual(
            releases[0].url,
            "https://github.com/microsoft/playwright/releases/tag/v1.62.1",
        )

    def test_feed_and_tier_are_propagated(self):
        releases = watcher.parse_feed("axe-core", "a11y", GITHUB_ATOM)
        self.assertTrue(all(r.feed == "axe-core" and r.tier == "a11y" for r in releases))

    def test_malformed_xml_raises(self):
        with self.assertRaises(Exception):
            watcher.parse_feed("broken", "core", b"<rss><channel><item>")

    def test_empty_feed_yields_no_releases(self):
        empty = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        self.assertEqual(watcher.parse_feed("empty", "core", empty), [])


# ---------------------------------------------------------------------------
# Identity and ordering
# ---------------------------------------------------------------------------

class TestReleaseIdentity(unittest.TestCase):
    def test_uid_prefers_url(self):
        release = make_release(url="https://example.invalid/x")
        self.assertEqual(release.uid(), "https://example.invalid/x")

    def test_uid_falls_back_to_feed_and_title_when_url_missing(self):
        release = make_release(url="", feed="F", title="T")
        self.assertEqual(release.uid(), "F::T")

    def test_uid_is_stable_across_equal_releases(self):
        self.assertEqual(make_release().uid(), make_release().uid())


class TestSortReleases(unittest.TestCase):
    def test_orders_by_tier_precedence(self):
        releases = [
            make_release(feed="Robocop", tier="tooling"),
            make_release(feed="Robot Framework", tier="core"),
            make_release(feed="pywinauto", tier="desktop"),
        ]
        ordered = [r.tier for r in watcher.sort_releases(releases)]
        self.assertEqual(ordered, ["core", "desktop", "tooling"])

    def test_unknown_tier_sorts_last(self):
        releases = [
            make_release(feed="Mystery", tier="not-a-real-tier"),
            make_release(feed="Robot Framework", tier="core"),
        ]
        self.assertEqual(watcher.sort_releases(releases)[0].tier, "core")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRendering(unittest.TestCase):
    def setUp(self):
        self.releases = [
            make_release(feed="Robot Framework", version="7.5b1", prerelease=True),
            make_release(feed="axe-core", tier="a11y", version="4.13.0"),
        ]

    def test_telegram_body_contains_no_markdown_markers(self):
        """parse_mode is deliberately unset; stray markers would render literally."""
        body = watcher.render_telegram(self.releases)
        self.assertNotIn("*", body)
        self.assertNotIn("_", body.replace("https://", "").split("\n")[0])

    def test_telegram_body_flags_prereleases(self):
        body = watcher.render_telegram(self.releases)
        self.assertIn("(pre-release)", body)

    def test_markdown_reports_empty_state(self):
        output = watcher.render_markdown([], [])
        self.assertIn("No new releases", output)

    def test_markdown_lists_feed_errors(self):
        output = watcher.render_markdown([], [("pytest", "timed out")])
        self.assertIn("Feed errors", output)
        self.assertIn("timed out", output)

    def test_console_renders_without_colour_codes_when_disabled(self):
        output = watcher.render_console(self.releases, [], use_colour=False)
        self.assertNotIn("\033[", output)

    def test_console_reports_nothing_new(self):
        self.assertIn("nothing new", watcher.render_console([], [], use_colour=False))


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

class TestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_state_file_returns_empty_scaffold(self):
        state = watcher.load_state(self.path)
        self.assertEqual(state["seen"], {})
        self.assertIsNone(state["last_run"])

    def test_roundtrip(self):
        watcher.save_state(self.path, {"seen": {"RF": ["a", "b"]}, "last_run": "2026-08-08T00:00:00+00:00"})
        self.assertEqual(watcher.load_state(self.path)["seen"]["RF"], ["a", "b"])

    def test_save_creates_parent_directories(self):
        watcher.save_state(self.path, {"seen": {}, "last_run": None})
        self.assertTrue(self.path.exists())

    def test_corrupt_state_degrades_to_empty_rather_than_crashing(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ not json at all", encoding="utf-8")
        state = watcher.load_state(self.path)
        self.assertEqual(state["seen"], {})

    def test_save_leaves_no_temp_file_behind(self):
        watcher.save_state(self.path, {"seen": {}, "last_run": None})
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "feeds.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_watchlist_used_when_no_config(self):
        self.assertEqual(watcher.load_feeds(None), watcher.DEFAULT_FEEDS)

    def test_every_default_feed_is_well_formed(self):
        for feed in watcher.DEFAULT_FEEDS:
            self.assertIn("name", feed)
            self.assertTrue(feed["url"].startswith("https://"))
            self.assertIn(feed["tier"], watcher.TIER_ORDER)

    def test_write_then_load_roundtrip(self):
        watcher.write_config(self.path)
        self.assertEqual(len(watcher.load_feeds(self.path)), len(watcher.DEFAULT_FEEDS))

    def test_missing_tier_defaults_to_other(self):
        self.path.write_text(json.dumps([{"name": "X", "url": "https://x.invalid/f.xml"}]), encoding="utf-8")
        self.assertEqual(watcher.load_feeds(self.path)[0]["tier"], "other")

    def test_entry_without_url_is_rejected(self):
        self.path.write_text(json.dumps([{"name": "X"}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            watcher.load_feeds(self.path)

    def test_non_list_config_is_rejected(self):
        self.path.write_text(json.dumps({"name": "X"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            watcher.load_feeds(self.path)


# ---------------------------------------------------------------------------
# End-to-end run, with the network stubbed
# ---------------------------------------------------------------------------

class TestRun(QuietMixin, unittest.TestCase):
    def setUp(self):
        self.silence()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.config = Path(self.tmp.name) / "feeds.json"
        self.config.write_text(
            json.dumps([{"name": "Robot Framework", "url": "https://pypi.invalid/f.xml", "tier": "core"}]),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **overrides):
        defaults = dict(
            config=str(self.config), state=str(self.state), init=False, markdown=None,
            telegram=False, telegram_test=False, stable_only=False, per_feed=5,
            timeout=5, dry_run=False, no_colour=True, write_config=None,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_init_seeds_state_and_reports_nothing(self):
        with mock.patch.object(watcher, "fetch", return_value=PYPI_RSS):
            code = watcher.run(self.args(init=True))
        self.assertEqual(code, 0)
        self.assertEqual(len(watcher.load_state(self.state)["seen"]["Robot Framework"]), 2)

    def test_first_real_run_reports_new_releases(self):
        with mock.patch.object(watcher, "fetch", return_value=PYPI_RSS):
            self.assertEqual(watcher.run(self.args()), 1)

    def test_second_run_is_idempotent(self):
        with mock.patch.object(watcher, "fetch", return_value=PYPI_RSS):
            watcher.run(self.args())
            self.assertEqual(watcher.run(self.args()), 0)

    def test_stable_only_filters_prereleases(self):
        with mock.patch.object(watcher, "fetch", return_value=PYPI_RSS):
            watcher.run(self.args(init=True))
        # Forget everything, then re-run filtering pre-releases out.
        watcher.save_state(self.state, {"seen": {}, "last_run": None})
        with mock.patch.object(watcher, "fetch", return_value=PYPI_RSS):
            with mock.patch.object(watcher, "render_console", side_effect=watcher.render_console) as spy:
                watcher.run(self.args(stable_only=True))
        reported = spy.call_args[0][0]
        self.assertEqual([r.version for r in reported], ["7.4.2"])

    def test_feed_failure_returns_error_code_not_crash(self):
        with mock.patch.object(watcher, "fetch", side_effect=RuntimeError("connection refused")):
            self.assertEqual(watcher.run(self.args()), 2)

    def test_one_bad_feed_does_not_suppress_a_good_one(self):
        self.config.write_text(
            json.dumps([
                {"name": "Broken", "url": "https://broken.invalid/f.xml", "tier": "core"},
                {"name": "Robot Framework", "url": "https://pypi.invalid/f.xml", "tier": "core"},
            ]),
            encoding="utf-8",
        )

        def selective(url, **kwargs):
            if "broken" in url:
                raise RuntimeError("connection refused")
            return PYPI_RSS

        with mock.patch.object(watcher, "fetch", side_effect=selective):
            with mock.patch.object(watcher, "render_console", side_effect=watcher.render_console) as spy:
                code = watcher.run(self.args())

        self.assertEqual(code, 2)  # error surfaced
        self.assertTrue(spy.call_args[0][0])  # good feed still reported

    def test_dry_run_does_not_persist_state(self):
        with mock.patch.object(watcher, "fetch", return_value=PYPI_RSS):
            watcher.run(self.args(dry_run=True))
        self.assertFalse(self.state.exists())

    def test_newest_entries_win_when_feed_is_oldest_first(self):
        with mock.patch.object(watcher, "fetch", return_value=REVERSED_RSS):
            with mock.patch.object(watcher, "render_console", side_effect=watcher.render_console) as spy:
                watcher.run(self.args(per_feed=1))
        self.assertEqual([r.version for r in spy.call_args[0][0]], ["3.0.0"])

    def test_markdown_digest_is_written(self):
        digest = Path(self.tmp.name) / "digest.md"
        with mock.patch.object(watcher, "fetch", return_value=PYPI_RSS):
            watcher.run(self.args(markdown=str(digest)))
        self.assertIn("Robot Framework", digest.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Telegram — credential handling only, never a live call
# ---------------------------------------------------------------------------

class TestTelegramCredentials(QuietMixin, unittest.TestCase):
    def setUp(self):
        self.silence()

    def test_missing_token_is_named_explicitly(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "1"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                watcher.push_telegram("hi")
        self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))

    def test_missing_chat_id_is_named_explicitly(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                watcher.push_telegram("hi")
        self.assertIn("TELEGRAM_CHAT_ID", str(ctx.exception))

    def test_self_test_returns_error_code_when_unconfigured(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(watcher.telegram_self_test(), 2)

    def test_credentials_never_appear_in_saved_state(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "state.json"
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "SECRET123", "TELEGRAM_CHAT_ID": "999"}):
            watcher.save_state(path, {"seen": {"RF": ["x"]}, "last_run": None})
        self.assertNotIn("SECRET123", path.read_text(encoding="utf-8"))
        tmp.cleanup()


class TestRedaction(unittest.TestCase):
    """The bot token sits in the Telegram URL path, so any leaked URL leaks
    the credential. These guard the redactor that stands between an error and
    a log file."""

    def test_token_removed_from_url(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "8123:AAHsecret"}):
            out = watcher._redact("https://api.telegram.org/bot8123:AAHsecret/sendMessage")
        self.assertNotIn("AAHsecret", out)

    def test_token_removed_from_plain_text(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "8123:AAHsecret"}):
            out = watcher._redact("failed with token 8123:AAHsecret")
        self.assertNotIn("AAHsecret", out)

    def test_url_pattern_redacted_even_when_env_unset(self):
        """Defence in depth: works even if the variable is gone by log time."""
        with mock.patch.dict("os.environ", {}, clear=True):
            out = watcher._redact("https://api.telegram.org/botLEAKED999/sendMessage")
        self.assertNotIn("LEAKED999", out)

    def test_ordinary_text_is_untouched(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(watcher._redact("connection refused"), "connection refused")

    def test_http_error_message_never_carries_the_token(self):
        import io as _io
        import urllib.error
        exc = urllib.error.HTTPError(
            "https://api.telegram.org/bot8123:AAHsecret/sendMessage",
            401, "Unauthorized", {}, _io.BytesIO(b"{}"),
        )
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "8123:AAHsecret", "TELEGRAM_CHAT_ID": "1"}):
            with mock.patch("urllib.request.urlopen", side_effect=exc):
                with self.assertRaises(RuntimeError) as ctx:
                    watcher.push_telegram("hi")
        self.assertNotIn("AAHsecret", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
