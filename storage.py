from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PROFILE = {
    "level": "beginner",
    "topics": ["大模型", "提示词", "AI 应用"],
    "goal": "建立扎实的 AI 常识，并能把 AI 用到日常工作",
    "feedback": {"easy": 0, "ok": 0, "hard": 0},
    "question_count": 0,
}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._init()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    published TEXT NOT NULL DEFAULT '',
                    difficulty TEXT NOT NULL DEFAULT 'beginner',
                    topics TEXT NOT NULL DEFAULT '[]',
                    guide TEXT NOT NULL DEFAULT '',
                    recommended_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_key TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    recipient TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    article_id INTEGER,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (recipient, local_date, kind)
                );
                CREATE TABLE IF NOT EXISTS ai_usage (
                    local_date TEXT PRIMARY KEY,
                    requests INTEGER NOT NULL
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(articles)")}
            for column in ("content_status", "fetched_at", "doc_title", "doc_url"):
                if column not in columns:
                    db.execute(f"ALTER TABLE articles ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

    def get_json(self, key: str, default):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_json(self, key: str, value) -> None:
        raw = json.dumps(value, ensure_ascii=False)
        with self.connect() as db:
            db.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, raw),
            )

    def profile(self) -> dict:
        profile = self.get_json("profile", {})
        return {**DEFAULT_PROFILE, **profile, "feedback": {**DEFAULT_PROFILE["feedback"], **profile.get("feedback", {})}}

    def save_profile(self, profile: dict) -> None:
        self.set_json("profile", profile)

    def bind_recipient(self, open_id: str) -> bool:
        if not open_id:
            return False
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO kv(key,value) VALUES('recipient',?)", (json.dumps(open_id),))
            row = db.execute("SELECT value FROM kv WHERE key='recipient'").fetchone()
            return json.loads(row["value"]) == open_id

    def recipient(self) -> str:
        return str(self.get_json("recipient", ""))

    def claim_event(self, event_key: str) -> bool:
        if not event_key:
            return False
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO events(event_key,received_at) VALUES(?,?)",
                (event_key, datetime.now(timezone.utc).isoformat()),
            )
            return cursor.rowcount == 1

    def reserve_daily(self, recipient: str, local_date: str, article_id: int | None) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO deliveries(recipient,local_date,kind,article_id,status,updated_at) VALUES(?,?,'daily',?,?,?)",
                (recipient, local_date, article_id, "preparing" if article_id is None else "sending", datetime.now(timezone.utc).isoformat()),
            )
            return cursor.rowcount == 1

    def set_daily_article(self, recipient: str, local_date: str, article_id: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE deliveries SET article_id=?,updated_at=? WHERE recipient=? AND local_date=? AND kind='daily' AND status='preparing'",
                (article_id, datetime.now(timezone.utc).isoformat(), recipient, local_date),
            )

    def start_daily_send(self, recipient: str, local_date: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE deliveries SET status='sending',updated_at=? WHERE recipient=? AND local_date=? AND kind='daily' AND status='preparing' AND article_id IS NOT NULL",
                (datetime.now(timezone.utc).isoformat(), recipient, local_date),
            )
            return cursor.rowcount == 1

    def finish_daily(self, recipient: str, local_date: str, status: str) -> None:
        if status not in {"sent", "unknown"}:
            raise ValueError("invalid delivery status")
        with self.connect() as db:
            db.execute(
                "UPDATE deliveries SET status=?,updated_at=? WHERE recipient=? AND local_date=? AND kind='daily' AND status='sending'",
                (status, datetime.now(timezone.utc).isoformat(), recipient, local_date),
            )

    def daily_status(self, recipient: str, local_date: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT status FROM deliveries WHERE recipient=? AND local_date=? AND kind='daily'",
                (recipient, local_date),
            ).fetchone()
        return row["status"] if row else ""

    def reserve_ai_call(self, local_date: str, limit: int) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT requests FROM ai_usage WHERE local_date=?", (local_date,)).fetchone()
            if row and row["requests"] >= limit:
                return False
            db.execute(
                "INSERT INTO ai_usage(local_date,requests) VALUES(?,1) ON CONFLICT(local_date) DO UPDATE SET requests=requests+1",
                (local_date,),
            )
            return True

    def seen_urls(self, limit: int = 200) -> set[str]:
        with self.connect() as db:
            rows = db.execute("SELECT url FROM articles ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return {row["url"] for row in rows}

    def save_article(self, article: dict, guide: str, set_current: bool = True) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute(
                """INSERT INTO articles(url,title,source,summary,content,published,difficulty,topics,guide,recommended_at,
                     content_status,fetched_at,doc_title,doc_url)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET content=excluded.content, guide=excluded.guide,
                     difficulty=excluded.difficulty, topics=excluded.topics, recommended_at=excluded.recommended_at,
                     content_status=excluded.content_status, fetched_at=excluded.fetched_at,
                     doc_title=excluded.doc_title, doc_url=excluded.doc_url""",
                (article["url"], article["title"], article["source"], article.get("summary", ""),
                 article.get("content", ""), article.get("published", ""), article.get("difficulty", "beginner"),
                 json.dumps(article.get("topics", []), ensure_ascii=False), guide, now,
                 article.get("content_status", ""), article.get("fetched_at", ""),
                 article.get("doc_title", ""), article.get("doc_url", "")),
            )
            row = db.execute("SELECT id FROM articles WHERE url=?", (article["url"],)).fetchone()
            article_id = int(row["id"])
        if set_current:
            self.set_current_article(article_id)
        return article_id

    def set_current_article(self, article_id: int) -> None:
        self.set_json("current_article_id", article_id)

    def article(self, article_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        return dict(row) if row else None

    def current_article(self) -> dict | None:
        return self.article(self.get_json("current_article_id", 0))

    def log(self, kind: str, text: str, article_id: int | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO interactions(article_id,kind,text,created_at) VALUES(?,?,?,?)",
                (article_id, kind, text[:8000], datetime.now(timezone.utc).isoformat()),
            )
