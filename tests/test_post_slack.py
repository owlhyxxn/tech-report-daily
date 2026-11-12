import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from poster import post_slack
from poster.post_slack import (
    RateLimited,
    SlackError,
    TokenMissing,
    load_messages,
    main,
    post_report,
)

CHANNEL = "C0TEST1234"


class FakeSlack:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = []
        self.next_ts = 100

    def __call__(self, payload):
        self.calls.append(payload)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        self.next_ts += 1
        return f"{self.next_ts}.000"


class PostReportTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def post(self, slack, replies=("r1", "r2")):
        return post_report(CHANNEL, "head", list(replies), slack, sleep=self.sleeps.append)

    def test_header_then_replies_in_order_on_thread(self):
        slack = FakeSlack()
        res = self.post(slack, ["r1", "r2", "r3"])
        self.assertEqual([c["markdown_text"] for c in slack.calls], ["head", "r1", "r2", "r3"])
        self.assertNotIn("thread_ts", slack.calls[0])
        self.assertTrue(all(c["thread_ts"] == "101.000" for c in slack.calls[1:]))
        self.assertTrue(all(c["channel"] == CHANNEL for c in slack.calls))
        self.assertEqual(res.to_dict(), {
            "ok": True, "header_ts": "101.000", "posted_replies": 3, "failed_replies": [], "error": None,
        })

    def test_link_previews_disabled_on_every_message(self):
        slack = FakeSlack()
        self.post(slack, ["r1", "r2"])
        for c in slack.calls:
            self.assertIs(c["unfurl_links"], False)
            self.assertIs(c["unfurl_media"], False)

    def test_header_retried_once_then_succeeds(self):
        slack = FakeSlack([SlackError("internal_error")])
        res = self.post(slack)
        self.assertEqual(res.header_ts, "101.000")
        self.assertEqual([c["markdown_text"] for c in slack.calls], ["head", "head", "r1", "r2"])

    def test_header_fails_twice_stops_everything(self):
        slack = FakeSlack([SlackError("a"), SlackError("b")])
        res = self.post(slack)
        self.assertIsNone(res.header_ts)
        self.assertEqual(len(slack.calls), 2)
        self.assertIn("header", res.error)
        self.assertFalse(res.to_dict()["ok"])

    def test_reply_fails_twice_is_skipped_and_next_sent(self):
        slack = FakeSlack([None, OSError("net"), OSError("net")])
        res = self.post(slack, ["r1", "r2"])
        self.assertEqual([c["markdown_text"] for c in slack.calls], ["head", "r1", "r1", "r2"])
        self.assertEqual(res.failed_replies, [0])
        self.assertEqual(res.posted_replies, 1)

    def test_rate_limited_waits_retry_after(self):
        slack = FakeSlack([RateLimited(7)])
        res = self.post(slack, [])
        self.assertEqual(self.sleeps, [7])
        self.assertEqual(res.header_ts, "101.000")

    def test_rate_limit_wait_capped(self):
        slack = FakeSlack([RateLimited(3600)])
        self.post(slack, [])
        self.assertEqual(self.sleeps, [post_slack.MAX_RETRY_AFTER_SECONDS])

    def test_no_sleep_on_plain_error(self):
        slack = FakeSlack([SlackError("x")])
        self.post(slack, [])
        self.assertEqual(self.sleeps, [])


class LoadMessagesTest(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, f, ensure_ascii=False)
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return Path(f.name)

    def test_valid(self):
        self.assertEqual(load_messages(self.write({"header": "h", "replies": ["a", "b"]})), ("h", ["a", "b"]))

    def test_invalid(self):
        for data in [{"replies": []}, {"header": "", "replies": []}, {"header": "h"}, {"header": "h", "replies": ["a", ""]}]:
            with self.assertRaises(ValueError, msg=data):
                load_messages(self.write(data))


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.msgs = Path(tmp.name) / "m.json"
        self.msgs.write_text(json.dumps({"header": "h", "replies": ["a"]}), encoding="utf-8")

    def run_main(self, args):
        with mock.patch("builtins.print") as p:
            code = main(args)
        return code, json.loads(p.call_args[0][0])

    def test_token_missing_posts_nothing(self):
        with mock.patch.object(post_slack, "read_token", side_effect=TokenMissing("없음")), \
                mock.patch.object(post_slack, "make_http_sender") as maker:
            code, out = self.run_main(["--channel", CHANNEL, "--messages", str(self.msgs)])
        self.assertEqual(code, 1)
        self.assertIn("token", out["error"])
        maker.assert_not_called()

    def test_bad_channel_rejected_before_token(self):
        with mock.patch.object(post_slack, "read_token") as rt:
            code, out = self.run_main(["--channel", "tech-daily", "--messages", str(self.msgs)])
        self.assertEqual(code, 1)
        rt.assert_not_called()

    def test_success_exit_zero_and_token_not_printed(self):
        slack = FakeSlack()
        with mock.patch.object(post_slack, "read_token", return_value="xoxb-secret"), \
                mock.patch.object(post_slack, "make_http_sender", return_value=slack) as maker, \
                mock.patch("builtins.print") as p:
            code = main(["--channel", CHANNEL, "--messages", str(self.msgs)])
        maker.assert_called_once_with("xoxb-secret")
        self.assertEqual(code, 0)
        self.assertNotIn("xoxb-secret", p.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
