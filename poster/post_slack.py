from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

API_URL = "https://slack.com/api/chat.postMessage"
KEYCHAIN_SERVICE = "tech-daily-slack-bot"
CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]{6,}$")
MAX_RETRY_AFTER_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 20


class TokenMissing(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"rate_limited (retry_after={retry_after})")
        self.retry_after = retry_after


class SlackError(Exception):
    pass


Sender = Callable[[dict], str]
Sleeper = Callable[[float], None]


@dataclass
class PostResult:
    header_ts: str | None = None
    posted_replies: int = 0
    failed_replies: list[int] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.header_ts is not None,
            "header_ts": self.header_ts,
            "posted_replies": self.posted_replies,
            "failed_replies": self.failed_replies,
            "error": self.error,
        }


def read_token(service: str = KEYCHAIN_SERVICE) -> str:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TokenMissing("security 명령을 찾을 수 없습니다") from exc
    token = out.stdout.strip()
    if out.returncode != 0 or not token:
        raise TokenMissing(f"키체인에 '{service}' 항목이 없습니다")
    return token


def load_messages(path: Path) -> tuple[str, list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    header = raw.get("header")
    replies = raw.get("replies")
    if not isinstance(header, str) or not header.strip():
        raise ValueError("header 는 비어 있지 않은 문자열이어야 합니다")
    if not isinstance(replies, list) or not all(isinstance(r, str) and r.strip() for r in replies):
        raise ValueError("replies 는 비어 있지 않은 문자열 목록이어야 합니다")
    return header, replies


def make_http_sender(token: str, url: str = API_URL) -> Sender:
    def send(payload: dict) -> str:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimited(int(exc.headers.get("Retry-After") or 1)) from None
            raise SlackError(f"HTTP {exc.code}") from None
        if not body.get("ok"):
            raise SlackError(str(body.get("error") or "unknown_error"))
        ts = body.get("ts")
        if not ts:
            raise SlackError("응답에 ts 가 없습니다")
        return str(ts)

    return send


def send_with_retry(send: Sender, payload: dict, sleep: Sleeper) -> str:
    try:
        return send(payload)
    except RateLimited as exc:
        sleep(min(max(exc.retry_after, 1), MAX_RETRY_AFTER_SECONDS))
    except (SlackError, OSError):
        pass
    return send(payload)


def post_report(
    channel: str,
    header: str,
    replies: list[str],
    send: Sender,
    sleep: Sleeper = time.sleep,
) -> PostResult:
    result = PostResult()
    base = {"channel": channel, "unfurl_links": False, "unfurl_media": False}
    try:
        result.header_ts = send_with_retry(send, {**base, "markdown_text": header}, sleep)
    except (RateLimited, SlackError, OSError) as exc:
        result.error = f"header: {type(exc).__name__}: {exc}"[:300]
        return result

    for i, text in enumerate(replies):
        payload = {**base, "markdown_text": text, "thread_ts": result.header_ts}
        try:
            send_with_retry(send, payload, sleep)
            result.posted_replies += 1
        except (RateLimited, SlackError, OSError):
            result.failed_replies.append(i)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tech Daily 리포트를 Slack 봇으로 게시")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--messages", type=Path, required=True)
    args = parser.parse_args(argv)

    def finish(result: PostResult) -> int:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.header_ts else 1

    if not CHANNEL_RE.match(args.channel):
        return finish(PostResult(error="channel ID 형식이 아닙니다"))
    try:
        header, replies = load_messages(args.messages)
    except (OSError, ValueError) as exc:
        return finish(PostResult(error=f"messages: {exc}"))
    try:
        token = read_token()
    except TokenMissing as exc:
        return finish(PostResult(error=f"token: {exc}"))

    return finish(post_report(args.channel, header, replies, make_http_sender(token)))


if __name__ == "__main__":
    sys.exit(main())
