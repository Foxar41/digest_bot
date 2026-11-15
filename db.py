"""Хранилище на sqlite3: пользователи, источники, уже отправленные новости."""
import os
import re
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "digest.db")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                send_time TEXT NOT NULL DEFAULT '09:00',
                interests TEXT NOT NULL DEFAULT '',
                last_sent_date TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS sources (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title   TEXT NOT NULL,
                rss_url TEXT NOT NULL,
                UNIQUE (user_id, rss_url)
            );
            CREATE TABLE IF NOT EXISTS topics (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                topic   TEXT NOT NULL COLLATE NOCASE,
                UNIQUE (user_id, topic)
            );
            CREATE TABLE IF NOT EXISTS sent_items (
                user_id INTEGER NOT NULL,
                link    TEXT NOT NULL,
                PRIMARY KEY (user_id, link)
            );
            """
        )
        # миграция для базы, созданной более ранней версией без этого поля
        cols = [r["name"] for r in c.execute("PRAGMA table_info(users)")]
        if "last_sent_date" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN last_sent_date TEXT NOT NULL DEFAULT ''")
        # перенос старых интересов (одной строкой) в отдельные темы
        for u in c.execute("SELECT user_id, interests FROM users WHERE interests != ''").fetchall():
            for t in re.split(r"[,;\n]+", u["interests"]):
                t = t.strip()[:50].lower()
                if t:
                    c.execute(
                        "INSERT OR IGNORE INTO topics (user_id, topic) VALUES (?, ?)",
                        (u["user_id"], t),
                    )
            c.execute("UPDATE users SET interests = '' WHERE user_id = ?", (u["user_id"],))


# ---------- users ----------
def ensure_user(user_id: int) -> None:
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


def get_user(user_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def all_users():
    with connect() as c:
        return c.execute("SELECT * FROM users").fetchall()


def update_user(user_id: int, **fields) -> None:
    allowed = {"send_time", "interests", "last_sent_date"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE users SET {sets} WHERE user_id = ?", (*fields.values(), user_id))


# ---------- sources ----------
def add_source(user_id: int, title: str, rss_url: str) -> bool:
    with connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO sources (user_id, title, rss_url) VALUES (?, ?, ?)",
            (user_id, title, rss_url),
        )
        return cur.rowcount > 0


def get_sources(user_id: int):
    with connect() as c:
        return c.execute(
            "SELECT * FROM sources WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()


def remove_source(user_id: int, source_id: int) -> bool:
    with connect() as c:
        cur = c.execute(
            "DELETE FROM sources WHERE user_id = ? AND id = ?", (user_id, source_id)
        )
        return cur.rowcount > 0


# ---------- topics (интересы) ----------
MAX_TOPICS = 20


def get_topics(user_id: int):
    with connect() as c:
        return c.execute(
            "SELECT * FROM topics WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()


def add_topics(user_id: int, topics: list[str]) -> int:
    """Добавляет темы (повторы игнорируются). Возвращает, сколько реально добавлено."""
    added = 0
    with connect() as c:
        count = c.execute(
            "SELECT COUNT(*) FROM topics WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        for t in topics:
            if count + added >= MAX_TOPICS:
                break
            cur = c.execute(
                "INSERT OR IGNORE INTO topics (user_id, topic) VALUES (?, ?)", (user_id, t.strip().lower())
            )
            added += cur.rowcount
    return added


def remove_topic(user_id: int, topic_id: int) -> bool:
    with connect() as c:
        cur = c.execute(
            "DELETE FROM topics WHERE user_id = ? AND id = ?", (user_id, topic_id)
        )
        return cur.rowcount > 0


def clear_topics(user_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM topics WHERE user_id = ?", (user_id,))


# ---------- sent items ----------
def is_sent(user_id: int, link: str) -> bool:
    with connect() as c:
        row = c.execute(
            "SELECT 1 FROM sent_items WHERE user_id = ? AND link = ?", (user_id, link)
        ).fetchone()
        return row is not None


def mark_sent(user_id: int, links: list[str]) -> None:
    with connect() as c:
        c.executemany(
            "INSERT OR IGNORE INTO sent_items (user_id, link) VALUES (?, ?)",
            [(user_id, link) for link in links],
        )
