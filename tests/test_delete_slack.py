import json
import unittest
from unittest import mock

from poster import delete_slack
from poster.delete_slack import delete_messages, main
from poster.post_slack import RateLimited, SlackError, TokenMissing

CHANNEL = "C0TEST1234"


class FakeSlack:
    def __init__(self, outcomes=None):
        self.outcomes = dict(outcomes or {})
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        queue = self.outcomes.get(payload["ts"])
        if queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        return payload["ts"]


class DeleteMessagesTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def delete(self, slack, ts_list):
        return delete_messages(CHANNEL, ts_list, slack, sleep=self.sleeps.append)

    def test_deletes_in_given_order(self):
        slack = FakeSlack()
        res = self.delete(slack, ["2.0", "1.0"])
        self.assertEqual(slack.calls, [{"channel": CHANNEL, "ts": "2.0"}, {"channel": CHANNEL, "ts": "1.0"}])
        self.assertEqual(res, {"ok": True, "deleted": ["2.0", "1.0"], "failed": {}})

    def test_already_deleted_counts_as_deleted(self):
        nf = SlackError("message_not_found")
        slack = FakeSlack({"1.0": [nf, SlackError("message_not_found")]})
        res = self.delete(slack, ["1.0"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["deleted"], ["1.0"])

    def test_retry_once_then_success(self):
        slack = FakeSlack({"1.0": [OSError("net")]})
        res = self.delete(slack, ["1.0"])
        self.assertTrue(res["ok"])
        self.assertEqual(len(slack.calls), 2)

    def test_failure_recorded_and_next_continues(self):
        slack = FakeSlack({"1.0": [SlackError("cant_delete_message"), SlackError("cant_delete_message")]})
        res = self.delete(slack, ["1.0", "2.0"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["deleted"], ["2.0"])
        self.assertIn("cant_delete_message", res["failed"]["1.0"])

    def test_rate_limited_waits(self):
        slack = FakeSlack({"1.0": [RateLimited(3)]})
        res = self.delete(slack, ["1.0"])
        self.assertTrue(res["ok"])
        self.assertEqual(self.sleeps, [3])


class MainTest(unittest.TestCase):
    def run_main(self, args):
        with mock.patch("builtins.print") as p:
            code = main(args)
        return code, json.loads(p.call_args[0][0])

    def test_bad_ts_rejected_before_token(self):
        with mock.patch.object(delete_slack, "read_token") as rt:
            code, out = self.run_main(["--channel", CHANNEL, "--ts", "1.0", "abc"])
        self.assertEqual(code, 1)
        self.assertIn("abc", out["error"])
        rt.assert_not_called()

    def test_bad_channel_rejected(self):
        with mock.patch.object(delete_slack, "read_token") as rt:
            code, _ = self.run_main(["--channel", "tech-daily", "--ts", "1.0"])
        self.assertEqual(code, 1)
        rt.assert_not_called()

    def test_token_missing(self):
        with mock.patch.object(delete_slack, "read_token", side_effect=TokenMissing("없음")), \
                mock.patch.object(delete_slack, "make_http_sender") as maker:
            code, out = self.run_main(["--channel", CHANNEL, "--ts", "1.0"])
        self.assertEqual(code, 1)
        maker.assert_not_called()

    def test_uses_delete_endpoint_and_hides_token(self):
        slack = FakeSlack()
        with mock.patch.object(delete_slack, "read_token", return_value="xoxb-secret"), \
                mock.patch.object(delete_slack, "make_http_sender", return_value=slack) as maker, \
                mock.patch("builtins.print") as p:
            code = main(["--channel", CHANNEL, "--ts", "1.0"])
        maker.assert_called_once_with("xoxb-secret", delete_slack.DELETE_URL)
        self.assertEqual(code, 0)
        self.assertNotIn("xoxb-secret", p.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
