from __future__ import annotations

import argparse
import calendar
import gzip
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

KST = timezone(timedelta(hours=9))
USER_AGENT = "tech-daily-feed/1.0 (+https://github.com/features/actions)"
SUMMARY_MAX_CHARS = 500
TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAM_KEYS = {"fbclid", "gclid"}
DATA_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
GZIP_MAGIC = b"\x1f\x8b"

Fetcher = Callable[[str, int], bytes]


@dataclass
class Settings:
    max_age_hours: int = 48
    keep_undated: bool = False
    retention_days: int = 90
    dedupe_lookback_days: int = 7
    timeout_seconds: int = 20


@dataclass
class Source:
    name: str
    url: str


@dataclass
class RunResult:
    new_records: list[dict] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def load_config(path: Path) -> tuple[Settings, list[Source]]:
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    s = raw.get("settings") or {}
    settings = Settings(
        max_age_hours=int(s.get("max_age_hours", 48)),
        keep_undated=bool(s.get("keep_undated", False)),
        retention_days=int(s.get("retention_days", 90)),
        dedupe_lookback_days=int(s.get("dedupe_lookback_days", 7)),
        timeout_seconds=int(s.get("timeout_seconds", 20)),
    )
    sources = [Source(name=str(x["name"]), url=str(x["url"])) for x in raw.get("sources") or []]
    names = [x.name for x in sources]
    if len(names) != len(set(names)):
        raise ValueError("sources.yml 에 같은 name 이 두 번 이상 있습니다")
    return settings, sources


def _is_medium_host(host: str) -> bool:
    return host == "medium.com" or host.endswith(".medium.com")


def _is_tracking_param(key: str, host: str) -> bool:
    k = key.lower()
    if k.startswith(TRACKING_PARAM_PREFIXES) or k in TRACKING_PARAM_KEYS:
        return True
    return k == "source" and _is_medium_host(host)


def clean_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k, host)
    ]
    return urlunsplit((parts.scheme.lower(), host, parts.path, urlencode(query), ""))


def normalize_url(url: str) -> str:
    parts = urlsplit(clean_url(url))
    scheme = "https" if parts.scheme == "http" else parts.scheme
    host = parts.netloc
    if host.startswith("www."):
        host = host[len("www."):]
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, host, path, parts.query, ""))


def record_id(normalized_url: str) -> str:
    return hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()[:16]


def clean_summary(text: str | None) -> str:
    if not text:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    collapsed = re.sub(r"\s+", " ", html.unescape(no_tags)).strip()
    if len(collapsed) > SUMMARY_MAX_CHARS:
        collapsed = collapsed[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return collapsed


def to_iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_feed(content: bytes, source: Source) -> list[dict]:
    import feedparser

    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"피드 파싱 실패: {parsed.get('bozo_exception')!r}")

    records = []
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
        published = (
            datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc) if struct else None
        )
        records.append(
            {
                "id": record_id(normalize_url(link)),
                "source": source.name,
                "title": clean_summary(entry.get("title")) or "(제목 없음)",
                "url": clean_url(link),
                "summary": clean_summary(entry.get("summary") or entry.get("description")),
                "published_at": to_iso_utc(published) if published else None,
            }
        )
    return records


def is_recent(record: dict, now: datetime, settings: Settings) -> bool:
    if record["published_at"] is None:
        return settings.keep_undated
    published = datetime.strptime(record["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return now - published <= timedelta(hours=settings.max_age_hours)


def data_file_for(day: date, data_dir: Path) -> Path:
    return data_dir / f"{day.isoformat()}.jsonl"


def load_seen_ids(data_dir: Path, today_kst: date, lookback_days: int) -> set[str]:
    seen: set[str] = set()
    for offset in range(lookback_days + 1):
        path = data_file_for(today_kst - timedelta(days=offset), data_dir)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                seen.add(json.loads(line)["id"])
    return seen


def append_records(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_status(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_status(path: Path, status: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def purge_old_files(data_dir: Path, today_kst: date, retention_days: int) -> list[str]:
    cutoff = today_kst - timedelta(days=retention_days)
    deleted = []
    if not data_dir.exists():
        return deleted
    for path in sorted(data_dir.iterdir()):
        m = DATA_FILE_RE.match(path.name)
        if m and date.fromisoformat(m.group(1)) < cutoff:
            path.unlink()
            deleted.append(path.name)
    return deleted


def decompress_if_gzip(body: bytes) -> bytes:
    if body[:2] == GZIP_MAGIC:
        return gzip.decompress(body)
    return body


def http_fetch(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return decompress_if_gzip(resp.read())


def run(
    settings: Settings,
    sources: list[Source],
    data_dir: Path,
    now: datetime,
    fetch: Fetcher = http_fetch,
) -> RunResult:
    result = RunResult()
    today_kst = now.astimezone(KST).date()
    status_path = data_dir / "status.json"
    current_names = {s.name for s in sources}
    status = {k: v for k, v in load_status(status_path).items() if k in current_names}
    seen = load_seen_ids(data_dir, today_kst, settings.dedupe_lookback_days)
    now_iso = to_iso_utc(now)

    for source in sources:
        entry = status.setdefault(source.name, {})
        entry["url"] = source.url
        entry["last_attempt_at"] = now_iso
        try:
            records = parse_feed(fetch(source.url, settings.timeout_seconds), source)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"[:300]
            entry["last_error"] = msg
            entry["last_error_at"] = now_iso
            result.errors[source.name] = msg
            continue

        entry["last_success_at"] = now_iso
        entry.pop("last_error", None)
        new_count = 0
        for r in records:
            if r["id"] in seen or not is_recent(r, now, settings):
                continue
            seen.add(r["id"])
            r["collected_at"] = now_iso
            result.new_records.append(r)
            new_count += 1
        entry["last_new_count"] = new_count

    result.new_records.sort(key=lambda r: r["published_at"] or "")
    if result.new_records:
        append_records(data_file_for(today_kst, data_dir), result.new_records)
    save_status(status_path, status)
    result.deleted_files = purge_old_files(data_dir, today_kst, settings.retention_days)
    return result


def write_github_output(result: RunResult) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"new_count={len(result.new_records)}\n")
        f.write(f"deleted_count={len(result.deleted_files)}\n")
        f.write(f"error_count={len(result.errors)}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tech Daily RSS 수집기")
    parser.add_argument("--sources", type=Path, default=Path("sources.yml"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args(argv)

    settings, sources = load_config(args.sources)
    result = run(settings, sources, args.data, datetime.now(timezone.utc))
    write_github_output(result)

    print(
        json.dumps(
            {
                "new": len(result.new_records),
                "deleted_files": result.deleted_files,
                "errors": result.errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if sources and len(result.errors) == len(sources) else 0


if __name__ == "__main__":
    sys.exit(main())
