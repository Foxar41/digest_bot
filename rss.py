"""Работа с RSS: проверка ленты, автообнаружение на сайте, сбор свежих записей."""
import re
from urllib.parse import urljoin

import feedparser
import requests

import db

HEADERS = {"User-Agent": "Mozilla/5.0 (DigestBot coursework project)"}
LINK_TAG_RE = re.compile(
    r"<link[^>]+type=[\"']application/(?:rss|atom)\+xml[\"'][^>]*>", re.I
)
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)

MAX_PER_FEED = 10   # сколько последних записей брать из одной ленты
MAX_TOTAL = 40      # сколько записей максимум отдавать в LLM


def _parse(url: str):
    """Скачивает ленту с таймаутом и разбирает её. При ошибке возвращает None."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return feedparser.parse(r.content)
    except requests.RequestException:
        return None


def check_feed(url: str) -> str | None:
    """Возвращает название ленты, если URL — рабочий RSS/Atom, иначе None."""
    feed = _parse(url)
    if feed is None or not feed.entries:  # bozo-флаг не проверяем: рабочие ленты тоже его ставят
        return None
    return feed.feed.get("title") or url


def discover_feed(site_url: str) -> str | None:
    """Ищет <link type="application/rss+xml"> на главной странице сайта."""
    try:
        r = requests.get(site_url, headers=HEADERS, timeout=10)
        page = r.text
    except requests.RequestException:
        return None
    for tag in LINK_TAG_RE.findall(page):
        m = HREF_RE.search(tag)
        if m:
            candidate = urljoin(site_url, m.group(1))
            if check_feed(candidate):
                return candidate
    return None


def fetch_new_items(user_id: int, sources) -> list[dict]:
    """Собирает ещё не отправленные записи из всех источников пользователя."""
    items: list[dict] = []
    for src in sources:
        feed = _parse(src["rss_url"])
        if feed is None:
            continue
        for e in feed.entries[:MAX_PER_FEED]:
            link = e.get("link")
            if not link or db.is_sent(user_id, link):
                continue
            items.append(
                {
                    "source": src["title"],
                    "title": (e.get("title") or "").strip(),
                    "summary": re.sub(r"<[^>]+>", "", e.get("summary", ""))[:300].strip(),
                    "link": link,
                }
            )
    return items[:MAX_TOTAL]
