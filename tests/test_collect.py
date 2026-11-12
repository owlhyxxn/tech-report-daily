import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collector.collect import (
    KST,
    Settings,
    Source,
    clean_url,
    is_recent,
    normalize_url,
    parse_feed,
    purge_old_files,
    record_id,
    run,
)

NOW = datetime(2026, 11, 13, 0, 0, 0, tzinfo=timezone.utc)


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link><description>{d}</description>"
        + (f"<pubDate>{p}</pubDate>" if p else "")
        + "</item>"
        for t, l, d, p in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'.encode()


ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>a</title>
  <entry><title>Atom &amp; Post</title><link href="https://Example.com/a/?utm_source=x"/>
    <published>2026-11-12T18:00:00+09:00</published><summary>&lt;p&gt;Hello&lt;/p&gt;</summary></entry>
</feed>"""

RECENT = "Thu, 12 Nov 2026 12:00:00 +0000"
OLD = "Mon, 09 Nov 2026 00:00:00 +0000"


class NormalizeTest(unittest.TestCase):
    def test_tracking_params_fragment_slash_case(self):
        self.assertEqual(
            normalize_url("HTTPS://News.Hada.io/topic/?id=1&utm_source=slack&utm_medium=bot#c"),
            "https://news.hada.io/topic?id=1",
        )

    def test_keeps_meaningful_query(self):
        self.assertEqual(normalize_url("https://x.com/p?id=7&gclid=z"), "https://x.com/p?id=7")

    def test_root_path_kept(self):
        self.assertEqual(normalize_url("https://x.com/"), "https://x.com/")

    def test_same_article_same_id(self):
        a = record_id(normalize_url("https://x.com/a/?utm_campaign=1"))
        b = record_id(normalize_url("https://x.com/a"))
        self.assertEqual(a, b)

    def test_scheme_and_www_unified(self):
        expected = normalize_url("https://example.com/post")
        for variant in ["http://example.com/post", "https://www.example.com/post", "http://WWW.Example.com/post/"]:
            self.assertEqual(normalize_url(variant), expected, variant)

    def test_medium_source_param_removed(self):
        a = normalize_url("https://medium.com/daangn/post-8dea?source=rss----4505f82a2dbd---4")
        b = normalize_url("https://medium.com/daangn/post-8dea")
        self.assertEqual(a, b)
        self.assertEqual(
            normalize_url("https://blog.medium.com/p?source=x"),
            normalize_url("https://blog.medium.com/p"),
        )

    def test_source_param_kept_outside_medium(self):
        self.assertEqual(normalize_url("https://x.com/p?source=a"), "https://x.com/p?source=a")
        self.assertNotEqual(
            normalize_url("https://notmedium.com/p?source=a"),
            normalize_url("https://notmedium.com/p"),
        )


class CleanUrlTest(unittest.TestCase):
    def test_keeps_www_scheme_and_slash(self):
        self.assertEqual(
            clean_url("http://www.Example.com/a/?utm_source=x&id=3#frag"),
            "http://www.example.com/a/?id=3",
        )

    def test_medium_source_removed_for_link(self):
        self.assertEqual(
            clean_url("https://medium.com/daangn/post?source=rss----1---4"),
            "https://medium.com/daangn/post",
        )


class ParseTest(unittest.TestCase):
    def test_rss(self):
        recs = parse_feed(rss([("Hi <b>there</b>", "https://x.com/1", "<p>a &amp; b</p>", RECENT)]), Source("S", "u"))
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(set(r), {"id", "source", "title", "url", "summary", "published_at"})
        self.assertEqual(r["title"], "Hi there")
        self.assertEqual(r["summary"], "a & b")
        self.assertEqual(r["published_at"], "2026-11-12T12:00:00Z")

    def test_atom_timezone_converted_to_utc(self):
        r = parse_feed(ATOM, Source("A", "u"))[0]
        self.assertEqual(r["published_at"], "2026-11-12T09:00:00Z")
        self.assertEqual(r["url"], "https://example.com/a/")
        self.assertEqual(r["id"], record_id("https://example.com/a"))
        self.assertEqual(r["summary"], "Hello")

    def test_entry_without_link_skipped(self):
        self.assertEqual(parse_feed(rss([("t", "", "d", RECENT)]), Source("S", "u")), [])

    def test_broken_xml_raises(self):
        with self.assertRaises(ValueError):
            parse_feed(b"<html>not a feed", Source("S", "u"))

    def test_summary_truncated(self):
        r = parse_feed(rss([("t", "https://x.com/1", "a" * 900, RECENT)]), Source("S", "u"))[0]
        self.assertEqual(len(r["summary"]), 500)


class RecentTest(unittest.TestCase):
    def test_age_filter(self):
        s = Settings()
        self.assertTrue(is_recent({"published_at": "2026-11-12T12:00:00Z"}, NOW, s))
        self.assertFalse(is_recent({"published_at": "2026-11-09T00:00:00Z"}, NOW, s))

    def test_undated(self):
        self.assertFalse(is_recent({"published_at": None}, NOW, Settings(keep_undated=False)))
        self.assertTrue(is_recent({"published_at": None}, NOW, Settings(keep_undated=True)))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self, name):
        p = self.data / name
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_collect_filter_and_file_by_kst_date(self):
        feeds = {"u1": rss([("new", "https://x.com/1", "d", RECENT), ("old", "https://x.com/2", "d", OLD)])}
        res = run(Settings(), [Source("S", "u1")], self.data, NOW, fetch=lambda u, t: feeds[u])
        self.assertEqual([r["title"] for r in res.new_records], ["new"])
        stored = self.lines("2026-11-13.jsonl")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["collected_at"], "2026-11-13T00:00:00Z")

    def test_dedupe_across_runs_and_sources(self):
        feeds = {
            "u1": rss([("a", "https://x.com/1?utm_source=a", "d", RECENT)]),
            "u2": rss([("a", "https://x.com/1/", "d", RECENT)]),
        }
        srcs = [Source("S1", "u1"), Source("S2", "u2")]
        first = run(Settings(), srcs, self.data, NOW, fetch=lambda u, t: feeds[u])
        second = run(Settings(), srcs, self.data, NOW + timedelta(hours=1), fetch=lambda u, t: feeds[u])
        self.assertEqual(len(first.new_records), 1)
        self.assertEqual(len(second.new_records), 0)
        self.assertEqual(len(self.lines("2026-11-13.jsonl")), 1)

    def test_dedupe_across_url_variants_between_runs(self):
        state = {"link": "http://www.x.com/1/"}
        fetch = lambda u, t: rss([("a", state["link"], "d", RECENT)])
        first = run(Settings(), [Source("S", "u")], self.data, NOW, fetch=fetch)
        state["link"] = "https://x.com/1?utm_medium=rss"
        second = run(Settings(), [Source("S", "u")], self.data, NOW + timedelta(hours=1), fetch=fetch)
        self.assertEqual(len(first.new_records), 1)
        self.assertEqual(first.new_records[0]["url"], "http://www.x.com/1/")
        self.assertEqual(len(second.new_records), 0)

    def test_dedupe_across_kst_day_boundary(self):
        feeds = {"u1": rss([("a", "https://x.com/1", "d", RECENT)])}
        before_midnight = datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(before_midnight.astimezone(KST).date().isoformat(), "2026-11-12")
        run(Settings(), [Source("S", "u1")], self.data, before_midnight, fetch=lambda u, t: feeds[u])
        self.assertEqual(len(self.lines("2026-11-12.jsonl")), 1)
        res = run(Settings(), [Source("S", "u1")], self.data, NOW, fetch=lambda u, t: feeds[u])
        self.assertEqual(len(res.new_records), 0)
        self.assertFalse((self.data / "2026-11-13.jsonl").exists())

    def test_one_feed_failure_does_not_stop_others(self):
        def fetch(url, timeout):
            if url == "bad":
                raise TimeoutError("timed out")
            if url == "broken":
                return b"<html>"
            return rss([("ok", "https://x.com/ok", "d", RECENT)])

        srcs = [Source("Bad", "bad"), Source("Broken", "broken"), Source("Good", "good")]
        res = run(Settings(), srcs, self.data, NOW, fetch=fetch)
        self.assertEqual([r["source"] for r in res.new_records], ["Good"])
        self.assertEqual(set(res.errors), {"Bad", "Broken"})
        status = json.loads((self.data / "status.json").read_text(encoding="utf-8"))
        self.assertIn("TimeoutError", status["Bad"]["last_error"])
        self.assertEqual(status["Good"]["last_new_count"], 1)
        self.assertNotIn("last_error", status["Good"])

    def test_error_cleared_after_success(self):
        state = {"fail": True}

        def fetch(url, timeout):
            if state["fail"]:
                raise OSError("down")
            return rss([])

        run(Settings(), [Source("S", "u")], self.data, NOW, fetch=fetch)
        state["fail"] = False
        run(Settings(), [Source("S", "u")], self.data, NOW + timedelta(hours=1), fetch=fetch)
        status = json.loads((self.data / "status.json").read_text(encoding="utf-8"))
        self.assertNotIn("last_error", status["S"])
        self.assertIn("last_error_at", status["S"])

    def test_removed_source_dropped_from_status(self):
        def fetch(url, timeout):
            if url == "gone":
                raise OSError("403")
            return rss([])

        run(Settings(), [Source("Gone", "gone"), Source("Kept", "kept")], self.data, NOW, fetch=fetch)
        run(Settings(), [Source("Kept", "kept")], self.data, NOW + timedelta(hours=1), fetch=fetch)
        status = json.loads((self.data / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(set(status), {"Kept"})

    def test_no_new_records_no_data_file(self):
        res = run(Settings(), [Source("S", "u")], self.data, NOW, fetch=lambda u, t: rss([]))
        self.assertEqual(res.new_records, [])
        self.assertFalse((self.data / "2026-11-13.jsonl").exists())


class PurgeTest(unittest.TestCase):
    def test_purge(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            for name in ["2026-08-14.jsonl", "2026-08-15.jsonl", "2026-11-13.jsonl", "status.json"]:
                (data / name).write_text("")
            today = datetime(2026, 11, 13).date()
            self.assertEqual(purge_old_files(data, today, 90), ["2026-08-14.jsonl"])
            self.assertTrue((data / "status.json").exists())


if __name__ == "__main__":
    unittest.main()
