"""
Static blog pipeline for Bookmark Digest.

This module owns the deterministic parts of the blog product:
- fetch bounded bookmark context from Bird
- decide what is new based on saved state
- ask Codex to write a post from a bounded brief
- render Markdown posts to a static HTML site
- persist checkpoints after a successful post
"""

from __future__ import annotations

import html
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from rich.console import Console

from .emailer import DigestEmailer, EmailConfig
from .researcher import AIResearcher

console = Console()

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass
class MediaItem:
    """Media attached to a tweet or quoted tweet."""

    type: str
    url: str
    display_url: str = ""
    video_url: str = ""


@dataclass
class TweetContext:
    """Sanitized tweet context used for blog research."""

    id: str
    text: str
    author_username: str
    author_name: str
    created_at: str
    tweet_url: str
    urls: list[str] = field(default_factory=list)
    media: list[MediaItem] = field(default_factory=list)
    quote_tweet: Optional["TweetContext"] = None
    in_reply_to_id: str = ""


@dataclass
class BookmarkContext:
    """A bookmarked tweet plus bounded surrounding context."""

    bookmark: TweetContext
    author_thread: list[TweetContext] = field(default_factory=list)


@dataclass
class BlogPost:
    """Generated blog post metadata and content."""

    title: str
    slug: str
    summary: str
    markdown: str
    bookmark_ids: list[str]
    latest_bookmark_id: str
    cover_image: str = ""


RATING_LABELS = {
    -1: "Low priority",
    0: "Unset",
    1: "Useful",
    2: "Strong",
    3: "Top interest",
}


def _clean_text(value: object) -> str:
    text = html.unescape(str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "bookmark-notes"


def _strip_markdown_fence(markdown: str) -> str:
    text = markdown.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _extract_title(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "Bookmark Notes"


def _extract_summary(markdown: str) -> str:
    body = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        if stripped.startswith("!") or stripped.startswith("["):
            continue
        body.append(re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", stripped))
        if len(" ".join(body)) > 180:
            break
    summary = " ".join(body).strip()
    if len(summary) <= 260:
        return summary
    clipped = summary[:260].rsplit(" ", 1)[0].rstrip(".,;:")
    sentence_end = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
    if sentence_end >= 120:
        return clipped[: sentence_end + 1]
    return f"{clipped}..."


def _strip_leading_h1(markdown: str) -> str:
    lines = markdown.lstrip().splitlines()
    if not lines or not lines[0].startswith("# "):
        return markdown
    lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    return "\n".join(lines).strip()


class BlogState:
    """SQLite-backed state for checkpointing and post metadata."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init(self):
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    slug TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    latest_bookmark_id TEXT NOT NULL,
                    bookmark_count INTEGER NOT NULL,
                    markdown_path TEXT NOT NULL,
                    html_path TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_bookmarks (
                    bookmark_id TEXT PRIMARY KEY,
                    post_slug TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bookmark_contexts (
                    bookmark_id TEXT PRIMARY KEY,
                    post_slug TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    author_username TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    tweet_url TEXT NOT NULL,
                    text TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bookmark_feedback (
                    bookmark_id TEXT PRIMARY KEY,
                    rating INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS post_feedback (
                    post_slug TEXT PRIMARY KEY,
                    feedback TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get(self, key: str, default: str = "") -> str:
        with self._connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row else default

    def set(self, key: str, value: str):
        with self._connect() as db:
            db.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def processed_ids(self) -> set[str]:
        with self._connect() as db:
            rows = db.execute("SELECT bookmark_id FROM processed_bookmarks").fetchall()
        return {str(row[0]) for row in rows}

    def save_post(self, post: BlogPost, markdown_path: Path, html_path: Path):
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO posts(
                    slug, title, summary, created_at, latest_bookmark_id,
                    bookmark_count, markdown_path, html_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post.slug,
                    post.title,
                    post.summary,
                    now,
                    post.latest_bookmark_id,
                    len(post.bookmark_ids),
                    str(markdown_path),
                    str(html_path),
                ),
            )
            for bookmark_id in post.bookmark_ids:
                db.execute(
                    "INSERT OR REPLACE INTO processed_bookmarks(bookmark_id, post_slug, created_at) VALUES (?, ?, ?)",
                    (bookmark_id, post.slug, now),
                )
            db.execute(
                "INSERT INTO metadata(key, value) VALUES('last_seen_bookmark_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (post.latest_bookmark_id,),
            )

    def save_bookmark_contexts(self, post_slug: str, bookmarks: list[BookmarkContext]):
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            for position, bundle in enumerate(bookmarks, 1):
                tweet = bundle.bookmark
                db.execute(
                    """
                    INSERT INTO bookmark_contexts(
                        bookmark_id, post_slug, position, author_username,
                        author_name, created_at, tweet_url, text, payload_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bookmark_id) DO UPDATE SET
                        post_slug = excluded.post_slug,
                        position = excluded.position,
                        author_username = excluded.author_username,
                        author_name = excluded.author_name,
                        created_at = excluded.created_at,
                        tweet_url = excluded.tweet_url,
                        text = excluded.text,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        tweet.id,
                        post_slug,
                        position,
                        tweet.author_username,
                        tweet.author_name,
                        tweet.created_at,
                        tweet.tweet_url,
                        tweet.text,
                        json.dumps(tweet_to_dict(tweet)),
                        now,
                    ),
                )

    def bookmarks_for_post(self, post_slug: str) -> list[TweetContext]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT payload_json
                FROM bookmark_contexts
                WHERE post_slug = ?
                ORDER BY position ASC
                """,
                (post_slug,),
            ).fetchall()
        return [tweet_from_dict(json.loads(row[0])) for row in rows]

    def bookmark_feedback_for_post(self, post_slug: str) -> dict[str, dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT c.bookmark_id, COALESCE(f.rating, 0), COALESCE(f.note, '')
                FROM bookmark_contexts c
                LEFT JOIN bookmark_feedback f ON f.bookmark_id = c.bookmark_id
                WHERE c.post_slug = ?
                """,
                (post_slug,),
            ).fetchall()
        return {
            str(row[0]): {"rating": int(row[1]), "note": str(row[2])}
            for row in rows
        }

    def set_bookmark_feedback(self, bookmark_id: str, rating: int, note: str = ""):
        rating = max(-1, min(3, int(rating)))
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO bookmark_feedback(bookmark_id, rating, note, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(bookmark_id) DO UPDATE SET
                    rating = excluded.rating,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (bookmark_id, rating, _clean_text(note)[:1000], now),
            )

    def post_feedback(self, post_slug: str) -> str:
        with self._connect() as db:
            row = db.execute(
                "SELECT feedback FROM post_feedback WHERE post_slug = ?",
                (post_slug,),
            ).fetchone()
        return str(row[0]) if row else ""

    def set_post_feedback(self, post_slug: str, feedback: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO post_feedback(post_slug, feedback, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(post_slug) DO UPDATE SET
                    feedback = excluded.feedback,
                    updated_at = excluded.updated_at
                """,
                (post_slug, str(feedback).strip()[:5000], now),
            )

    def feedback_payload(self, post_slug: str) -> dict:
        return {
            "post_feedback": self.post_feedback(post_slug),
            "bookmarks": self.bookmark_feedback_for_post(post_slug),
        }

    def preference_brief(self, limit: int = 20) -> str:
        with self._connect() as db:
            bookmark_rows = db.execute(
                """
                SELECT c.author_username, c.text, c.tweet_url, f.rating, f.note, f.updated_at
                FROM bookmark_feedback f
                JOIN bookmark_contexts c ON c.bookmark_id = f.bookmark_id
                WHERE f.rating != 0 OR f.note != ''
                ORDER BY f.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            post_rows = db.execute(
                """
                SELECT p.title, pf.feedback, pf.updated_at
                FROM post_feedback pf
                LEFT JOIN posts p ON p.slug = pf.post_slug
                WHERE pf.feedback != ''
                ORDER BY pf.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        if not bookmark_rows and not post_rows:
            return ""

        lines = [
            "# Ruari Preference Signals",
            "",
            "Use these signals to tune ordering, depth, and emphasis. A lower rating means the item can be explained briefly or placed later. A higher rating means it should get more curiosity, context, and prominent placement when relevant.",
            "",
        ]
        if bookmark_rows:
            lines.append("## Bookmark ratings")
            for username, text, url, rating, note, _updated_at in bookmark_rows:
                label = RATING_LABELS.get(int(rating), str(rating))
                lines.append(f"- {label} for @{username}: {_clean_text(text)[:220]} ({url})")
                if note:
                    lines.append(f"  Note: {_clean_text(note)[:300]}")
            lines.append("")
        if post_rows:
            lines.append("## Post feedback")
            for title, feedback, _updated_at in post_rows:
                lines.append(f"- {title or 'Untitled post'}: {_clean_text(feedback)[:500]}")
            lines.append("")
        return "\n".join(lines)

    def list_posts(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT slug, title, summary, created_at, latest_bookmark_id, bookmark_count, markdown_path, html_path
                FROM posts
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [
            {
                "slug": row[0],
                "title": row[1],
                "summary": row[2],
                "created_at": row[3],
                "latest_bookmark_id": row[4],
                "bookmark_count": row[5],
                "markdown_path": row[6],
                "html_path": row[7],
            }
            for row in rows
        ]


class BirdContextFetcher:
    """Fetches bookmarks and bounded tweet context via Bird."""

    def __init__(
        self,
        cookies_path: Path,
        command: str = "npx --yes @steipete/bird",
    ):
        self.cookies_path = cookies_path
        self.command = shlex.split(command)

    def _load_cookie_env(self) -> dict:
        if not self.cookies_path.exists():
            raise FileNotFoundError(f"Cookies file not found: {self.cookies_path}")
        cookies = json.loads(self.cookies_path.read_text())
        auth_token = cookies.get("auth_token", "")
        ct0 = cookies.get("ct0", "")
        if not auth_token or not ct0:
            raise ValueError("config/cookies.json must contain auth_token and ct0")
        env = os.environ.copy()
        env["AUTH_TOKEN"] = auth_token
        env["CT0"] = ct0
        return env

    def _run(self, args: list[str]) -> object:
        result = subprocess.run(
            self.command + args,
            env=self._load_cookie_env(),
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads((result.stdout or "").strip() or "[]")

    @staticmethod
    def _items(payload: object) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("tweets", "data", "items", "thread", "replies"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            if payload.get("id") or payload.get("rest_id") or payload.get("_raw"):
                return [payload]
        return []

    def fetch_bookmarks(
        self,
        max_count: int,
        since_id: str = "",
        scan_limit: int = 50,
        include_author_chain: bool = True,
        stop_ids: Optional[set[str]] = None,
    ) -> list[BookmarkContext]:
        stop_ids = stop_ids or set()
        count = max_count if not since_id else max(scan_limit, max_count)
        seed_payload = self._run(["bookmarks", "-n", str(count), "--json-full", "--plain"])
        seed_items = self._items(seed_payload)

        seed_contexts = [parse_tweet_context(item) for item in seed_items]
        new_seeds: list[TweetContext] = []
        for context in seed_contexts:
            if since_id and context.id == since_id:
                break
            if since_id and context.id in stop_ids:
                break
            new_seeds.append(context)
            if not since_id and len(new_seeds) >= max_count:
                break

        if not new_seeds:
            return []

        chain_items = seed_items
        if include_author_chain:
            chain_payload = self._run([
                "bookmarks",
                "-n",
                str(count),
                "--json-full",
                "--plain",
                "--author-chain",
                "--thread-meta",
            ])
            chain_items = self._items(chain_payload)

        chain_contexts = [parse_tweet_context(item) for item in chain_items]
        seed_ids = {seed.id for seed in new_seeds}
        by_id = {context.id: context for context in chain_contexts}

        bundles = [BookmarkContext(bookmark=seed) for seed in new_seeds]
        by_seed_id = {bundle.bookmark.id: bundle for bundle in bundles}

        for context in chain_contexts:
            if context.id in seed_ids:
                continue
            root_id = self._find_seed_root(context, by_id, seed_ids)
            if root_id and root_id in by_seed_id:
                by_seed_id[root_id].author_thread.append(context)

        return bundles

    def read_tweet(self, tweet_id: str) -> Optional[TweetContext]:
        payload = self._run(["read", tweet_id, "--json-full", "--plain"])
        items = self._items(payload)
        return parse_tweet_context(items[0]) if items else None

    def thread(self, tweet_id: str, limit: int = 12) -> list[TweetContext]:
        payload = self._run(["thread", tweet_id, "--json-full", "--plain"])
        return [parse_tweet_context(item) for item in self._items(payload)[:limit]]

    def replies(self, tweet_id: str, limit: int = 12) -> list[TweetContext]:
        payload = self._run(["replies", tweet_id, "--json-full", "--plain"])
        return [parse_tweet_context(item) for item in self._items(payload)[:limit]]

    @staticmethod
    def _find_seed_root(context: TweetContext, by_id: dict[str, TweetContext], seed_ids: set[str]) -> str:
        seen = set()
        current = context
        while current and current.in_reply_to_id and current.in_reply_to_id not in seen:
            parent_id = current.in_reply_to_id
            if parent_id in seed_ids:
                return parent_id
            seen.add(parent_id)
            current = by_id.get(parent_id)
        return ""


def parse_tweet_context(tweet: dict) -> TweetContext:
    """Normalize Bird JSON-full tweet payload into a small safe structure."""
    raw = tweet.get("_raw", {}) if isinstance(tweet.get("_raw"), dict) else {}
    legacy = raw.get("legacy", {}) if isinstance(raw.get("legacy"), dict) else {}
    author = tweet.get("author", {}) if isinstance(tweet.get("author"), dict) else {}
    user_legacy = (
        raw.get("core", {})
        .get("user_results", {})
        .get("result", {})
        .get("legacy", {})
        if isinstance(raw.get("core"), dict)
        else {}
    )

    tweet_id = str(tweet.get("id") or tweet.get("rest_id") or raw.get("rest_id") or "")
    username = (
        author.get("username")
        or author.get("screen_name")
        or user_legacy.get("screen_name")
        or tweet.get("author_username")
        or ""
    )
    text = tweet.get("text") or tweet.get("full_text") or legacy.get("full_text") or ""

    context = TweetContext(
        id=tweet_id,
        text=_clean_text(text),
        author_username=username,
        author_name=author.get("name") or user_legacy.get("name") or tweet.get("author_name") or "",
        created_at=tweet.get("createdAt") or tweet.get("created_at") or legacy.get("created_at") or "",
        tweet_url=f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id else "",
        urls=_extract_urls(legacy),
        media=_extract_media(legacy),
        in_reply_to_id=legacy.get("in_reply_to_status_id_str") or tweet.get("reply_to_id") or "",
    )

    quoted = raw.get("quoted_status_result", {}).get("result") if isinstance(raw.get("quoted_status_result"), dict) else None
    if isinstance(quoted, dict):
        context.quote_tweet = parse_tweet_context({"_raw": quoted})
    return context


def media_to_dict(media: MediaItem) -> dict:
    return {
        "type": media.type,
        "url": media.url,
        "display_url": media.display_url,
        "video_url": media.video_url,
    }


def media_from_dict(data: dict) -> MediaItem:
    return MediaItem(
        type=str(data.get("type") or "media"),
        url=str(data.get("url") or ""),
        display_url=str(data.get("display_url") or ""),
        video_url=str(data.get("video_url") or ""),
    )


def tweet_to_dict(tweet: TweetContext) -> dict:
    data = {
        "id": tweet.id,
        "text": tweet.text,
        "author_username": tweet.author_username,
        "author_name": tweet.author_name,
        "created_at": tweet.created_at,
        "tweet_url": tweet.tweet_url,
        "urls": tweet.urls,
        "media": [media_to_dict(item) for item in tweet.media],
        "in_reply_to_id": tweet.in_reply_to_id,
    }
    if tweet.quote_tweet:
        data["quote_tweet"] = tweet_to_dict(tweet.quote_tweet)
    return data


def tweet_from_dict(data: dict) -> TweetContext:
    tweet = TweetContext(
        id=str(data.get("id") or ""),
        text=str(data.get("text") or ""),
        author_username=str(data.get("author_username") or ""),
        author_name=str(data.get("author_name") or ""),
        created_at=str(data.get("created_at") or ""),
        tweet_url=str(data.get("tweet_url") or ""),
        urls=[str(url) for url in data.get("urls", []) if str(url).strip()],
        media=[media_from_dict(item) for item in data.get("media", []) if isinstance(item, dict)],
        in_reply_to_id=str(data.get("in_reply_to_id") or ""),
    )
    quote = data.get("quote_tweet")
    if isinstance(quote, dict):
        tweet.quote_tweet = tweet_from_dict(quote)
    return tweet


def _extract_urls(legacy: dict) -> list[str]:
    urls = []
    for item in legacy.get("entities", {}).get("urls", []) or []:
        url = item.get("expanded_url") or item.get("url") or ""
        if not url:
            continue
        if url.startswith("https://twitter.com/") or url.startswith("https://x.com/"):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def _extract_media(legacy: dict) -> list[MediaItem]:
    media_items = []
    raw_media = (
        legacy.get("extended_entities", {}).get("media", [])
        or legacy.get("entities", {}).get("media", [])
        or []
    )
    for media in raw_media:
        if not isinstance(media, dict):
            continue
        video_url = ""
        variants = media.get("video_info", {}).get("variants", []) or []
        mp4_variants = [v for v in variants if v.get("content_type") == "video/mp4"]
        if mp4_variants:
            best = max(mp4_variants, key=lambda v: v.get("bitrate", 0))
            video_url = best.get("url", "")
        media_items.append(
            MediaItem(
                type=media.get("type") or "media",
                url=media.get("media_url_https") or media.get("media_url") or media.get("url") or "",
                display_url=media.get("display_url", ""),
                video_url=video_url,
            )
        )
    return [item for item in media_items if item.url or item.video_url]


def context_to_brief(bookmarks: list[BookmarkContext], preference_brief: str = "") -> str:
    """Build a bounded Markdown research brief for Codex."""
    lines = [
        "# Bookmark Research Brief",
        "",
        "These are the new X bookmarks selected by the deterministic controller.",
        "Use this as the primary evidence. You may investigate further when useful.",
        "",
        "Local helper commands available if you need more X context:",
        "- `python -m src.blog_tools read <tweet_id>`",
        "- `python -m src.blog_tools thread <tweet_id> --limit 12`",
        "- `python -m src.blog_tools replies <tweet_id> --limit 12`",
        "",
        "Do not fetch huge public reply sets unless there is a clear reason.",
        "",
    ]
    if preference_brief:
        lines.extend([preference_brief.strip(), ""])

    for index, bundle in enumerate(bookmarks, 1):
        tweet = bundle.bookmark
        lines.extend([
            f"## Bookmark {index}: @{tweet.author_username}",
            f"- Tweet ID: {tweet.id}",
            f"- Tweet URL: {tweet.tweet_url}",
            f"- Created: {tweet.created_at}",
            f"- Text: {tweet.text}",
        ])
        if tweet.urls:
            lines.append(f"- URLs: {', '.join(tweet.urls)}")
        if tweet.media:
            lines.append("- Media:")
            for media in tweet.media:
                value = media.video_url or media.url
                lines.append(f"  - {media.type}: {value}")
        if tweet.quote_tweet:
            quote = tweet.quote_tweet
            lines.extend([
                "- Quoted tweet:",
                f"  - ID: {quote.id}",
                f"  - URL: {quote.tweet_url}",
                f"  - Author: @{quote.author_username}",
                f"  - Text: {quote.text}",
            ])
            if quote.media:
                lines.append("  - Media:")
                for media in quote.media:
                    value = media.video_url or media.url
                    lines.append(f"    - {media.type}: {value}")
        if bundle.author_thread:
            lines.append("- Connected author replies:")
            for reply in bundle.author_thread[:8]:
                lines.append(f"  - @{reply.author_username} ({reply.id}): {reply.text}")
                if reply.urls:
                    lines.append(f"    URLs: {', '.join(reply.urls)}")
                if reply.media:
                    media_values = [item.video_url or item.url for item in reply.media]
                    lines.append(f"    Media: {', '.join(media_values)}")
        lines.append("")
    return "\n".join(lines)


class CodexBlogWriter:
    """Uses Codex CLI as an agentic writer for a weekly blog post."""

    def __init__(
        self,
        model: str = "gpt-5.5",
        reasoning_effort: str = "low",
        search: bool = True,
        sandbox: str = "read-only",
        ignore_user_config: bool = True,
        automatic_cover_image: bool = False,
    ):
        self.automatic_cover_image = automatic_cover_image
        self.researcher = AIResearcher(
            provider="codex_cli",
            model=model,
            codex_cli_search=search,
            codex_cli_sandbox=sandbox,
            codex_cli_ignore_user_config=ignore_user_config,
            codex_cli_reasoning_effort=reasoning_effort,
        )

    def write_post(self, brief: str, bookmarks: list[BookmarkContext]) -> BlogPost:
        latest_id = bookmarks[0].bookmark.id
        bookmark_ids = [bundle.bookmark.id for bundle in bookmarks]
        cover_image = first_cover_image(bookmarks) if self.automatic_cover_image else ""
        prompt = build_blog_prompt(brief)
        markdown = _strip_markdown_fence(self.researcher._call_ai(prompt))
        title = _extract_title(markdown)
        summary = _extract_summary(markdown)
        date_prefix = datetime.now().strftime("%Y-%m-%d")
        slug = f"{date_prefix}-{_slugify(title)}"
        return BlogPost(
            title=title,
            slug=slug,
            summary=summary,
            markdown=markdown,
            bookmark_ids=bookmark_ids,
            latest_bookmark_id=latest_id,
            cover_image=cover_image,
        )


def build_blog_prompt(brief: str) -> str:
    return f"""You are writing a public blog post for bookmark.ruari.dev.

Goal:
- Write one coherent weekly blog post about the themes in my X bookmarks.
- The audience is technical readers who may care about AI tools, developer workflows, open source, infrastructure, and product thinking.
- Be curious and contextual. Explain why the bookmarks matter, not just what they say.

Research behavior:
- Use the bookmark brief as primary evidence.
- You may use web search and local command tools when useful.
- If a tweet references a YouTube video, a screenshot, a project, a quoted tweet, or a repo, investigate enough to understand the context.
- You may use helper commands like `python -m src.blog_tools read <tweet_id>` if the brief is missing tweet context.
- You may use tools such as `yt-dlp` or `ffmpeg` only if they are available and clearly useful.
- Do not fetch all public replies by default. Public replies are often noisy. Prefer author self-replies, quoted tweets, primary links, and source projects.
- Use Ruari preference signals when present. Put high-interest items earlier and give them more context. Put low-interest items later or cover them briefly.
- Do not choose a single bookmarked tweet image as the post hero unless it represents the whole post. Media is usually better near the section or source item it belongs to.

Writing constraints:
- Return Markdown only.
- Start with a single H1 title.
- Do not include YAML front matter.
- Use 3-6 sections.
- Include source links inline where relevant.
- Include a final `## Source Trail` section with the most important original links and tweet URLs.
- If a bookmark is mainly a demo video, image, YouTube video, or repo, make the original media/project easy to find in prose instead of burying it in the final source list.
- Do not invent claims. If context is uncertain, say so plainly.
- Do not mention that you are an AI or that this was generated from a prompt.

{brief}
"""


def first_cover_image(bookmarks: list[BookmarkContext]) -> str:
    for bundle in bookmarks:
        for media in bundle.bookmark.media:
            if media.type == "photo" and media.url:
                return media.url
        if bundle.bookmark.quote_tweet:
            for media in bundle.bookmark.quote_tweet.media:
                if media.type == "photo" and media.url:
                    return media.url
    return ""


class StaticSiteBuilder:
    """Renders generated Markdown posts to a static blog."""

    def __init__(
        self,
        content_dir: Path,
        site_dir: Path,
        site_title: str,
        site_description: str,
        base_url: str,
        state: Optional[BlogState] = None,
        cache_remote_media: bool = True,
        max_cached_media_bytes: int = 50 * 1024 * 1024,
    ):
        self.content_dir = content_dir
        self.site_dir = site_dir
        self.site_title = site_title
        self.site_description = site_description
        self.base_url = base_url.rstrip("/")
        self.state = state
        self.cache_remote_media = cache_remote_media
        self.max_cached_media_bytes = max_cached_media_bytes

    def write_post(self, post: BlogPost) -> tuple[Path, Path]:
        self.content_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = self.content_dir / f"{post.slug}.md"
        markdown_path.write_text(self._with_front_matter(post))
        self.render_all()
        html_path = self.site_dir / "posts" / post.slug / "index.html"
        return markdown_path, html_path

    def render_all(self) -> Path:
        self.site_dir.mkdir(parents=True, exist_ok=True)
        posts = [self._load_post(path) for path in sorted(self.content_dir.glob("*.md"), reverse=True)]
        self._write_assets()
        for post in posts:
            post_dir = self.site_dir / "posts" / post["slug"]
            post_dir.mkdir(parents=True, exist_ok=True)
            (post_dir / "index.html").write_text(self._post_page(post))
        (self.site_dir / "index.html").write_text(self._index_page(posts))
        (self.site_dir / "feed.xml").write_text(self._feed(posts))
        return self.site_dir / "index.html"

    def _with_front_matter(self, post: BlogPost) -> str:
        date = datetime.now(timezone.utc).isoformat()
        front_matter = {
            "title": post.title,
            "slug": post.slug,
            "date": date,
            "summary": post.summary,
            "cover_image": post.cover_image,
            "latest_bookmark_id": post.latest_bookmark_id,
            "bookmark_count": str(len(post.bookmark_ids)),
            "bookmark_ids": ",".join(post.bookmark_ids),
        }
        lines = ["---"]
        lines.extend(f"{key}: {value}" for key, value in front_matter.items())
        lines.extend(["---", "", post.markdown.strip(), ""])
        return "\n".join(lines)

    def _load_post(self, path: Path) -> dict:
        text = path.read_text()
        metadata, body = parse_front_matter(text)
        slug = metadata.get("slug") or path.stem
        body_without_title = _strip_leading_h1(body)
        bookmarks = self.state.bookmarks_for_post(slug) if self.state else []
        feedback = self.state.feedback_payload(slug) if self.state else {"post_feedback": "", "bookmarks": {}}
        return {
            "slug": slug,
            "title": metadata.get("title") or _extract_title(body),
            "summary": metadata.get("summary") or _extract_summary(body_without_title),
            "date": metadata.get("date") or "",
            "cover_image": metadata.get("cover_image") or "",
            "body": body_without_title,
            "html": markdown_to_html(body_without_title),
            "url": f"{self.base_url}/posts/{slug}/" if self.base_url else f"/posts/{slug}/",
            "bookmarks": bookmarks,
            "feedback": feedback,
        }

    def _write_assets(self):
        (self.site_dir / "styles.css").write_text(STYLES)

    def _layout(self, title: str, body: str) -> str:
        script = FEEDBACK_SCRIPT if 'class="private-feedback"' in body or 'data-bookmark-id=' in body else ""
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <meta name="description" content="{html.escape(self.site_description)}">
  <link rel="stylesheet" href="/styles.css">
</head>
<body>
  <header class="site-header">
    <a class="brand" href="/">
      <span class="brand-mark">BD</span>
      <span>{html.escape(self.site_title)}</span>
    </a>
    <nav>
      <a href="/posts/">Archive</a>
      <a href="/feed.xml">RSS</a>
    </nav>
  </header>
  {body}
  {script}
</body>
</html>
"""

    def _index_page(self, posts: list[dict]) -> str:
        latest = posts[0] if posts else None
        post_list = "\n".join(self._post_card(post) for post in posts)
        hero_image = ""
        if latest and latest.get("cover_image"):
            hero_image = f'<img src="{html.escape(latest["cover_image"])}" alt="" class="hero-image">'
        body = f"""
<main>
  <section class="hero">
    <div class="hero-copy">
      <p class="eyebrow">Weekly bookmark research</p>
      <h1>{html.escape(self.site_title)}</h1>
      <p>{html.escape(self.site_description)}</p>
    </div>
    {hero_image}
  </section>
  <section class="archive" id="posts">
    <div class="section-heading">
      <p class="eyebrow">Archive</p>
      <h2>Recent notes</h2>
    </div>
    <div class="post-grid">
      {post_list or '<p>No posts yet.</p>'}
    </div>
  </section>
</main>
"""
        posts_dir = self.site_dir / "posts"
        posts_dir.mkdir(parents=True, exist_ok=True)
        (posts_dir / "index.html").write_text(self._layout(f"Archive - {self.site_title}", body))
        return self._layout(self.site_title, body)

    def _post_card(self, post: dict) -> str:
        image = ""
        if post.get("cover_image"):
            image = f'<img src="{html.escape(post["cover_image"])}" alt="" class="card-image">'
        return f"""
<article class="post-card">
  {image}
  <div class="post-card-body">
    <p class="date">{html.escape(format_date(post.get("date", "")))}</p>
    <h3><a href="/posts/{html.escape(post["slug"])}/">{html.escape(post["title"])}</a></h3>
    <p>{html.escape(post["summary"])}</p>
  </div>
</article>
"""

    def _post_page(self, post: dict) -> str:
        cover = ""
        if post.get("cover_image"):
            cover = f'<img src="{html.escape(post["cover_image"])}" alt="" class="post-cover">'
        source_section = self._source_section(post)
        feedback_panel = self._feedback_panel(post)
        body = f"""
<main class="post-shell">
  <article class="post">
    <p class="date">{html.escape(format_date(post.get("date", "")))}</p>
    <h1>{html.escape(post["title"])}</h1>
    <p class="summary">{html.escape(post["summary"])}</p>
    {cover}
    <div class="post-body">
      {post["html"]}
    </div>
    {source_section}
    {feedback_panel}
  </article>
</main>
"""
        return self._layout(f"{post['title']} - {self.site_title}", body)

    def _source_section(self, post: dict) -> str:
        bookmarks = post.get("bookmarks") or []
        if not bookmarks:
            return ""
        cards = "\n".join(self._bookmark_card(tweet, post.get("feedback", {}).get("bookmarks", {})) for tweet in bookmarks)
        return f"""
<section class="source-bookmarks">
  <div class="section-heading compact">
    <p class="eyebrow">Bookmarked sources</p>
    <h2>Original context</h2>
  </div>
  <div class="source-list">
    {cards}
  </div>
</section>
"""

    def _bookmark_card(self, tweet: TweetContext, feedback: dict[str, dict]) -> str:
        current = feedback.get(tweet.id, {})
        rating = int(current.get("rating", 0))
        note = str(current.get("note", ""))
        links = "".join(f'<a href="{html.escape(url)}">{html.escape(compact_url(url))}</a>' for url in tweet.urls)
        media_html = self._tweet_media_html(tweet)
        quote_html = ""
        if tweet.quote_tweet:
            quote = tweet.quote_tweet
            quote_html = f"""
      <div class="quote-card">
        <p class="eyebrow">Quoted tweet</p>
        <p><a href="{html.escape(quote.tweet_url)}">@{html.escape(quote.author_username)}</a>: {html.escape(quote.text)}</p>
        {self._tweet_media_html(quote)}
      </div>
"""
        rating_buttons = "\n".join(
            f'<button type="button" data-rating="{value}" class="{html.escape("active" if rating == value else "")}">{html.escape(label)}</button>'
            for value, label in [(-1, "Low"), (1, "Useful"), (2, "Strong"), (3, "Top")]
        )
        return f"""
    <article class="source-card" data-bookmark-id="{html.escape(tweet.id)}">
      <div class="source-copy">
        <p class="date">@{html.escape(tweet.author_username)} · {html.escape(format_date(tweet.created_at))}</p>
        <p>{html.escape(tweet.text)}</p>
        <div class="source-actions">
          <a href="{html.escape(tweet.tweet_url)}">Open tweet</a>
          {links}
        </div>
        {quote_html}
      </div>
      {media_html}
      <div class="bookmark-rating private-feedback-surface">
        <p class="eyebrow">Private rating</p>
        <div class="rating-buttons">
          {rating_buttons}
        </div>
        <textarea data-note placeholder="Why this was or was not interesting">{html.escape(note)}</textarea>
        <p class="save-status" aria-live="polite"></p>
      </div>
    </article>
"""

    def _tweet_media_html(self, tweet: TweetContext) -> str:
        if not tweet.media:
            return ""
        items = []
        for media in tweet.media[:3]:
            if media.video_url:
                video_src = self._cache_media_url(media.video_url)
                poster_src = self._cache_media_url(media.url) if media.url else ""
                poster = f' poster="{html.escape(poster_src)}"' if poster_src else ""
                items.append(f'<video controls playsinline preload="metadata"{poster}><source src="{html.escape(video_src)}" type="video/mp4"><a href="{html.escape(video_src)}">Open video</a></video>')
            elif media.url:
                image_src = self._cache_media_url(media.url)
                items.append(f'<img src="{html.escape(image_src)}" alt="">')
        if not items:
            return ""
        return f'<div class="source-media">{"".join(items)}</div>'

    def _cache_media_url(self, url: str) -> str:
        if not self.cache_remote_media or not url.startswith(("http://", "https://")):
            return url

        parsed = urlparse(url)
        extension = Path(parsed.path).suffix.lower()
        if extension not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"}:
            extension = ".bin"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        media_dir = self.site_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        target = media_dir / f"{digest}{extension}"
        if target.exists() and target.stat().st_size > 0:
            return f"/media/{target.name}"

        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=30) as response:
                expected_size = int(response.headers.get("Content-Length") or "0")
                if expected_size > self.max_cached_media_bytes:
                    console.print(f"[yellow]Skipping large media asset: {url}[/yellow]")
                    return url
                bytes_written = 0
                with target.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        bytes_written += len(chunk)
                        if bytes_written > self.max_cached_media_bytes:
                            output.close()
                            target.unlink(missing_ok=True)
                            console.print(f"[yellow]Skipping large media asset: {url}[/yellow]")
                            return url
                        output.write(chunk)
        except Exception as error:
            target.unlink(missing_ok=True)
            console.print(f"[yellow]Could not cache media asset ({error}): {url}[/yellow]")
            return url

        return f"/media/{target.name}"

    def _feedback_panel(self, post: dict) -> str:
        feedback = post.get("feedback", {}).get("post_feedback", "")
        return f"""
<section class="private-feedback" data-post-slug="{html.escape(post["slug"])}">
  <div class="section-heading compact">
    <p class="eyebrow">Private feedback</p>
    <h2>Curation notes</h2>
  </div>
  <p class="feedback-help">Visible only on localhost. These notes and bookmark ratings are saved locally and included in the next writing prompt.</p>
  <textarea id="post-feedback" placeholder="What did this post get right? What should it do differently next time?">{html.escape(feedback)}</textarea>
  <button type="button" id="save-post-feedback">Save notes</button>
  <p class="save-status" id="post-feedback-status" aria-live="polite"></p>
</section>
"""

    def _feed(self, posts: list[dict]) -> str:
        items = []
        for post in posts[:20]:
            items.append(f"""
    <item>
      <title>{html.escape(post["title"])}</title>
      <link>{html.escape(post["url"])}</link>
      <guid>{html.escape(post["url"])}</guid>
      <description>{html.escape(post["summary"])}</description>
    </item>""")
        return f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0">
  <channel>
    <title>{html.escape(self.site_title)}</title>
    <link>{html.escape(self.base_url or "/")}</link>
    <description>{html.escape(self.site_description)}</description>
{''.join(items)}
  </channel>
</rss>
"""


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end].strip()
    body = text[end + 4 :].strip()
    metadata = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata, body


def markdown_to_html(markdown: str) -> str:
    try:
        import markdown as markdown_lib

        return markdown_lib.markdown(markdown, extensions=["extra", "sane_lists"])
    except Exception:
        escaped = html.escape(markdown)
        paragraphs = [f"<p>{para}</p>" for para in escaped.split("\n\n") if para.strip()]
        return "\n".join(paragraphs)


def format_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%B %d, %Y")
    except ValueError:
        return value[:10]


def compact_url(value: str) -> str:
    cleaned = value.replace("https://", "").replace("http://", "").rstrip("/")
    return cleaned[:44] + "..." if len(cleaned) > 47 else cleaned


def send_blog_notification(email_config: EmailConfig, post: BlogPost, post_url: str):
    """Send a notification-only email for a published blog post."""
    emailer = DigestEmailer(email_config)
    subject = emailer._build_subject(f"New post - {post.title}")
    body = "\n".join([
        "A new Bookmark Digest post is live.",
        "",
        post.title,
        post.summary,
        "",
        post_url,
    ])
    message = EmailMessage()
    message["From"] = email_config.from_address
    message["To"] = ", ".join(email_config.to_addresses)
    message["Subject"] = subject
    message.set_content(body)
    emailer._send_message(message)


def resolve_blog_paths(config: dict) -> dict[str, Path | str | int | bool]:
    blog_config = config.get("blog", {}) if config else {}
    def path_value(key: str, default: str) -> Path:
        value = Path(blog_config.get(key) or default)
        return value if value.is_absolute() else PROJECT_ROOT / value

    return {
        "state_path": path_value("state_path", "data/blog.sqlite3"),
        "content_dir": path_value("content_dir", "content/posts"),
        "site_dir": path_value("site_dir", "site"),
        "site_title": str(blog_config.get("site_title") or "Bookmark Digest"),
        "site_description": str(blog_config.get("site_description") or "Research notes from my X bookmarks."),
        "base_url": str(blog_config.get("base_url") or "http://localhost:8000"),
        "first_run_bookmarks": int(blog_config.get("first_run_bookmarks", 10)),
        "scan_limit": int(blog_config.get("scan_limit", 50)),
        "bird_command": str(blog_config.get("bird_command") or "npx --yes @steipete/bird"),
        "include_author_chain": bool(blog_config.get("include_author_chain", True)),
        "automatic_cover_image": bool(blog_config.get("automatic_cover_image", False)),
        "cache_remote_media": bool(blog_config.get("cache_remote_media", True)),
        "max_cached_media_mb": int(blog_config.get("max_cached_media_mb", 50)),
    }


FEEDBACK_SCRIPT = """
<script>
(() => {
  const isLocal = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
  if (!isLocal) return;

  document.querySelectorAll(".private-feedback, .private-feedback-surface").forEach((node) => {
    node.style.display = "block";
  });

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  document.querySelectorAll("[data-bookmark-id]").forEach((card) => {
    const bookmarkId = card.dataset.bookmarkId;
    const note = card.querySelector("[data-note]");
    const status = card.querySelector(".save-status");
    const buttons = Array.from(card.querySelectorAll("[data-rating]"));

    async function save(rating) {
      try {
        await postJSON("/api/bookmark-feedback", {
          bookmark_id: bookmarkId,
          rating,
          note: note ? note.value : "",
        });
        buttons.forEach((button) => {
          button.classList.toggle("active", Number(button.dataset.rating) === rating);
        });
        if (status) status.textContent = "Saved";
      } catch (_error) {
        if (status) status.textContent = "Could not save";
      }
    }

    buttons.forEach((button) => {
      button.addEventListener("click", () => save(Number(button.dataset.rating)));
    });
    if (note) {
      note.addEventListener("change", () => {
        const active = card.querySelector("[data-rating].active");
        save(active ? Number(active.dataset.rating) : 0);
      });
    }
  });

  const panel = document.querySelector(".private-feedback[data-post-slug]");
  if (!panel) return;
  const textarea = panel.querySelector("#post-feedback");
  const saveButton = panel.querySelector("#save-post-feedback");
  const status = panel.querySelector("#post-feedback-status");
  if (!textarea || !saveButton) return;

  async function hydrateFeedback() {
    try {
      const response = await fetch(`/api/feedback?slug=${encodeURIComponent(panel.dataset.postSlug)}`);
      if (!response.ok) return;
      const payload = await response.json();
      textarea.value = payload.post_feedback || "";
      Object.entries(payload.bookmarks || {}).forEach(([bookmarkId, item]) => {
        const card = document.querySelector(`[data-bookmark-id="${bookmarkId}"]`);
        if (!card) return;
        const note = card.querySelector("[data-note]");
        if (note) note.value = item.note || "";
        card.querySelectorAll("[data-rating]").forEach((button) => {
          button.classList.toggle("active", Number(button.dataset.rating) === Number(item.rating || 0));
        });
      });
    } catch (_error) {}
  }

  hydrateFeedback();

  saveButton.addEventListener("click", async () => {
    try {
      await postJSON("/api/post-feedback", {
        slug: panel.dataset.postSlug,
        feedback: textarea.value,
      });
      if (status) status.textContent = "Saved";
    } catch (_error) {
      if (status) status.textContent = "Could not save";
    }
  });
})();
</script>
"""


STYLES = """
:root {
  --ink: #171717;
  --muted: #63615d;
  --paper: #f6f8f7;
  --line: #c8d2ce;
  --accent: #0f766e;
  --accent-strong: #0b4f49;
  --panel: #ffffff;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: Georgia, Cambria, "Times New Roman", serif;
  line-height: 1.65;
}

a { color: var(--accent-strong); text-decoration-thickness: 0.08em; }

.site-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  border-bottom: 1px solid var(--line);
  padding: 18px clamp(20px, 5vw, 64px);
  background: rgba(246, 248, 247, 0.94);
  position: sticky;
  top: 0;
  z-index: 2;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  color: var(--ink);
  text-decoration: none;
  font-weight: 700;
  letter-spacing: 0;
}

.brand-mark {
  display: inline-grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border: 1px solid var(--ink);
  font-size: 0.78rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

nav { display: flex; gap: 18px; font-size: 0.95rem; }
nav a { color: var(--muted); text-decoration: none; }

.hero {
  min-height: 72vh;
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(280px, 0.95fr);
  gap: clamp(28px, 5vw, 72px);
  align-items: center;
  padding: clamp(44px, 8vw, 96px) clamp(20px, 5vw, 64px);
  border-bottom: 1px solid var(--line);
}

.hero h1 {
  max-width: 100%;
  margin: 0;
  font-size: clamp(3rem, 8vw, 7.25rem);
  line-height: 0.92;
  letter-spacing: 0;
}

.hero p:not(.eyebrow) {
  max-width: 680px;
  color: var(--muted);
  font-size: clamp(1.1rem, 2.2vw, 1.5rem);
}

.eyebrow,
.date {
  margin: 0 0 12px;
  color: var(--accent);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
  letter-spacing: 0;
  text-transform: uppercase;
}

.hero-image,
.post-cover,
.card-image {
  width: 100%;
  border: 1px solid var(--ink);
  object-fit: cover;
  background: var(--panel);
}

.hero-image {
  aspect-ratio: 4 / 3;
  max-height: 620px;
}

.archive {
  padding: clamp(40px, 7vw, 84px) clamp(20px, 5vw, 64px);
}

.section-heading {
  display: flex;
  align-items: end;
  justify-content: space-between;
  border-bottom: 1px solid var(--line);
  margin-bottom: 28px;
}

.section-heading h2 {
  margin: 0 0 14px;
  font-size: clamp(2rem, 4vw, 4rem);
  line-height: 1;
}

.post-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 22px;
}

.post-card {
  border: 1px solid var(--line);
  background: var(--panel);
}

.post-card-body { padding: 22px; }
.post-card h3 { margin: 0; font-size: 1.55rem; line-height: 1.15; }
.post-card h3 a { color: var(--ink); text-decoration: none; }
.post-card p:last-child { color: var(--muted); }
.card-image { aspect-ratio: 16 / 10; border-width: 0 0 1px; }

.post-shell {
  padding: clamp(36px, 7vw, 80px) clamp(20px, 5vw, 64px);
}

.post {
  max-width: 900px;
  margin: 0 auto;
}

.post h1 {
  margin: 0;
  font-size: clamp(2.6rem, 6vw, 6.4rem);
  line-height: 0.98;
  letter-spacing: 0;
}

.summary {
  color: var(--muted);
  font-size: 1.25rem;
  border-left: 4px solid var(--accent);
  padding-left: 18px;
}

.post-cover {
  aspect-ratio: 16 / 9;
  margin: 26px 0 44px;
}

.post-body {
  font-size: 1.08rem;
}

.post-body h2 {
  margin-top: 2.4em;
  padding-top: 0.8em;
  border-top: 1px solid var(--line);
  font-size: clamp(1.8rem, 3vw, 2.7rem);
  line-height: 1.05;
}

.post-body h3 { font-size: 1.35rem; }
.post-body img { max-width: 100%; border: 1px solid var(--line); }
.post-body blockquote {
  border-left: 4px solid var(--line);
  margin-left: 0;
  padding-left: 20px;
  color: var(--muted);
}

.source-bookmarks,
.private-feedback {
  margin-top: clamp(42px, 7vw, 76px);
  padding-top: 28px;
  border-top: 1px solid var(--line);
}

.section-heading.compact {
  align-items: start;
  margin-bottom: 18px;
}

.section-heading.compact h2 {
  font-size: clamp(1.8rem, 3vw, 2.6rem);
}

.source-list {
  display: grid;
  gap: 18px;
}

.source-card {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(220px, 0.9fr);
  gap: 20px;
  border: 1px solid var(--line);
  background: var(--panel);
  padding: 18px;
}

.source-copy p {
  margin-top: 0;
}

.source-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 0.95rem;
}

.source-media {
  display: grid;
  gap: 10px;
}

.source-media img,
.source-media video {
  width: 100%;
  max-height: 420px;
  border: 1px solid var(--line);
  background: var(--paper);
  object-fit: contain;
}

.quote-card {
  margin-top: 18px;
  padding: 14px;
  border-left: 4px solid var(--line);
  background: var(--paper);
}

.private-feedback,
.private-feedback-surface {
  display: none;
}

.bookmark-rating {
  grid-column: 1 / -1;
  border-top: 1px solid var(--line);
  padding-top: 14px;
}

.rating-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.rating-buttons button,
#save-post-feedback {
  border: 1px solid var(--ink);
  background: var(--panel);
  color: var(--ink);
  padding: 8px 12px;
  font: inherit;
  cursor: pointer;
}

.rating-buttons button.active,
#save-post-feedback:hover {
  background: var(--ink);
  color: var(--panel);
}

textarea {
  width: 100%;
  min-height: 92px;
  border: 1px solid var(--line);
  background: var(--panel);
  color: var(--ink);
  padding: 12px;
  font: inherit;
  resize: vertical;
}

.feedback-help,
.save-status {
  color: var(--muted);
}

.save-status {
  min-height: 1.4em;
  margin-bottom: 0;
  font-size: 0.95rem;
}

@media (max-width: 760px) {
  .site-header { align-items: flex-start; flex-direction: column; }
  .hero { grid-template-columns: 1fr; min-height: auto; }
  .hero h1 { max-width: 100%; }
  .source-card { grid-template-columns: 1fr; }
}
"""
