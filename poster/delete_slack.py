from __future__ import annotations

import argparse
import json
import re
import sys
import time

from poster.post_slack import (
    CHANNEL_RE,
    RateLimited,
    Sender,
    Sleeper,
    SlackError,
    TokenMissing,
    make_http_sender,
    read_token,
    send_with_retry,
)

DELETE_URL = "https://slack.com/api/chat.delete"
TS_RE = re.compile(r"^\d+\.\d+$")
ALREADY_GONE = "message_not_found"


def delete_messages(channel: str, ts_list: list[str], send: Sender, sleep: Sleeper = time.sleep) -> dict:
    deleted: list[str] = []
    failed: dict[str, str] = {}
    for ts in ts_list:
        try:
            send_with_retry(send, {"channel": channel, "ts": ts}, sleep)
            deleted.append(ts)
        except SlackError as exc:
            if str(exc) == ALREADY_GONE:
                deleted.append(ts)
            else:
                failed[ts] = f"SlackError: {exc}"[:300]
        except (RateLimited, OSError) as exc:
            failed[ts] = f"{type(exc).__name__}: {exc}"[:300]
    return {"ok": not failed, "deleted": deleted, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="봇이 올린 Slack 메시지 삭제")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--ts", nargs="+", required=True)
    args = parser.parse_args(argv)

    def finish(result: dict) -> int:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    if not CHANNEL_RE.match(args.channel):
        return finish({"ok": False, "error": "channel ID 형식이 아닙니다"})
    bad = [t for t in args.ts if not TS_RE.match(t)]
    if bad:
        return finish({"ok": False, "error": f"ts 형식이 아닙니다: {bad}"})
    try:
        token = read_token()
    except TokenMissing as exc:
        return finish({"ok": False, "error": f"token: {exc}"})

    return finish(delete_messages(args.channel, args.ts, make_http_sender(token, DELETE_URL)))


if __name__ == "__main__":
    sys.exit(main())
