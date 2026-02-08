"""
X/Twitter Bookmark Scraper

Uses cookie-based authentication to fetch bookmarks from X's internal API.
"""

import json
import asyncio
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional

import httpx
from rich.console import Console

console = Console()

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"


@dataclass
class Tweet:
    """Represents a bookmarked tweet."""
    id: str
    text: str
    author_username: str
    author_name: str
    created_at: str
    urls: list[str]
    media_urls: list[str]
    quote_tweet: Optional[dict] = None
    reply_to_id: Optional[str] = None
    bookmark_timestamp: Optional[str] = None


def save_raw_tweets(tweets: list[Tweet], filename: Optional[str] = None):
    """Save tweet data for debugging/reference."""
    if not filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"bookmarks_{timestamp}.json"

    raw_dir = DATA_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    filepath = raw_dir / filename
    with open(filepath, "w") as f:
        json.dump([asdict(t) for t in tweets], f, indent=2)

    console.print(f"[dim]Saved raw data to {filepath}[/dim]")


class BirdBookmarkScraper:
    """Fetches bookmarks using the bird CLI (primary scraper)."""

    def __init__(self, cookies_path: Optional[Path] = None):
        self.cookies_path = cookies_path or CONFIG_DIR / "cookies.json"
        self.cookies = self._load_optional_cookies()

    def _load_optional_cookies(self) -> Optional[dict]:
        """Load cookies if available so bird can run without browser profile setup."""
        if not self.cookies_path.exists():
            return None

        try:
            with open(self.cookies_path) as f:
                cookies = json.load(f)
        except Exception:
            return None

        required = ("auth_token", "ct0")
        if all(
            key in cookies
            and isinstance(cookies[key], str)
            and cookies[key]
            and not cookies[key].startswith("YOUR_")
            for key in required
        ):
            return {key: cookies[key] for key in required}
        return None

    def _build_command(self, max_count: int, since_id: Optional[str]) -> list[str]:
        """Build bird CLI command."""
        command = ["bird", "bookmarks", "--json", "--plain"]

        # If we need to stop at a since_id, fetch all pages and stop locally.
        if since_id or max_count == 0:
            command.append("--all")
        else:
            command.extend(["-n", str(max_count)])

        # Prefer explicit cookie auth (if present) over browser-profile auth.
        if self.cookies:
            command.extend(["--auth-token", self.cookies["auth_token"], "--ct0", self.cookies["ct0"]])

        return command

    @staticmethod
    def _extract_urls(tweet: dict) -> list[str]:
        """Extract non-X URLs from a bird tweet payload."""
        urls = []

        def maybe_add(url: str):
            if not url:
                return
            if url.startswith("https://twitter.com/") or url.startswith("https://x.com/"):
                return
            urls.append(url)

        for item in tweet.get("urls", []):
            if isinstance(item, str):
                maybe_add(item)
            elif isinstance(item, dict):
                maybe_add(item.get("expanded_url") or item.get("url") or "")

        for container in (tweet, tweet.get("legacy", {})):
            for entity in container.get("entities", {}).get("urls", []):
                maybe_add(entity.get("expanded_url") or entity.get("url") or "")

        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                deduped.append(url)
        return deduped

    @staticmethod
    def _extract_media_urls(tweet: dict) -> list[str]:
        """Extract media URLs from a bird tweet payload."""
        media_urls = []

        def add_media_url(url: str):
            if url:
                media_urls.append(url)

        for media in tweet.get("media", []):
            if isinstance(media, str):
                add_media_url(media)
            elif isinstance(media, dict):
                add_media_url(
                    media.get("media_url_https")
                    or media.get("media_url")
                    or media.get("url")
                    or ""
                )
                variants = media.get("video_info", {}).get("variants", [])
                mp4_variants = [v for v in variants if v.get("content_type") == "video/mp4"]
                if mp4_variants:
                    best = max(mp4_variants, key=lambda v: v.get("bitrate", 0))
                    add_media_url(best.get("url", ""))

        legacy_media = tweet.get("legacy", {}).get("extended_entities", {}).get("media", [])
        for media in legacy_media:
            if media.get("type") == "photo":
                add_media_url(media.get("media_url_https", ""))
            elif media.get("type") == "video":
                variants = media.get("video_info", {}).get("variants", [])
                mp4_variants = [v for v in variants if v.get("content_type") == "video/mp4"]
                if mp4_variants:
                    best = max(mp4_variants, key=lambda v: v.get("bitrate", 0))
                    add_media_url(best.get("url", ""))

        return [url for url in dict.fromkeys(media_urls) if url]

    @staticmethod
    def _parse_quote_tweet(tweet: dict) -> Optional[dict]:
        """Parse quoted tweet if present."""
        quoted = tweet.get("quoted_tweet") or tweet.get("quotedTweet")
        if not isinstance(quoted, dict):
            return None

        quoted_author = quoted.get("author", {}) if isinstance(quoted.get("author"), dict) else {}
        quoted_legacy = quoted.get("legacy", {}) if isinstance(quoted.get("legacy"), dict) else {}

        return {
            "id": str(quoted.get("id") or quoted.get("rest_id") or ""),
            "text": quoted.get("text") or quoted.get("full_text") or quoted_legacy.get("full_text") or "",
            "author_username": (
                quoted_author.get("username")
                or quoted_author.get("screen_name")
                or quoted.get("author_username")
                or ""
            ),
            "author_name": quoted_author.get("name") or quoted.get("author_name") or "",
        }

    def _parse_tweet(self, tweet: dict) -> Optional[Tweet]:
        """Normalize bird tweet payload to local Tweet model."""
        tweet_id = str(tweet.get("id") or tweet.get("id_str") or tweet.get("rest_id") or "")
        if not tweet_id:
            return None

        author = tweet.get("author", {}) if isinstance(tweet.get("author"), dict) else {}
        legacy = tweet.get("legacy", {}) if isinstance(tweet.get("legacy"), dict) else {}
        text = tweet.get("text") or tweet.get("full_text") or tweet.get("fullText") or legacy.get("full_text") or ""

        return Tweet(
            id=tweet_id,
            text=text,
            author_username=(
                author.get("username")
                or author.get("screen_name")
                or tweet.get("author_username")
                or tweet.get("username")
                or ""
            ),
            author_name=author.get("name") or tweet.get("author_name") or "",
            created_at=tweet.get("created_at") or tweet.get("createdAt") or legacy.get("created_at") or "",
            urls=self._extract_urls(tweet),
            media_urls=self._extract_media_urls(tweet),
            quote_tweet=self._parse_quote_tweet(tweet),
            reply_to_id=(
                tweet.get("in_reply_to_status_id_str")
                or tweet.get("reply_to_id")
                or legacy.get("in_reply_to_status_id_str")
            ),
            bookmark_timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _extract_tweet_items(payload: object) -> list[dict]:
        """Extract tweet objects from bird JSON output."""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if isinstance(payload, dict):
            for key in ("tweets", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            # Some bird commands return a single object
            if payload.get("id") or payload.get("rest_id"):
                return [payload]

        raise ValueError("Unexpected bird output format")

    def fetch_bookmarks(
        self,
        max_count: int = 0,
        since_id: Optional[str] = None,
    ) -> list[Tweet]:
        """Fetch bookmarks from bird."""
        command = self._build_command(max_count=max_count, since_id=since_id)
        console.print("[blue]Fetching bookmarks using bird CLI...[/blue]")

        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as e:
            raise RuntimeError("bird CLI not installed") from e
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            raise RuntimeError(f"bird command failed: {stderr or e}") from e

        stdout = (result.stdout or "").strip()
        if not stdout:
            return []

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError("bird returned non-JSON output") from e

        items = self._extract_tweet_items(payload)
        tweets = []
        for item in items:
            tweet = self._parse_tweet(item)
            if not tweet:
                continue

            if since_id and tweet.id == since_id:
                break

            tweets.append(tweet)

            if max_count > 0 and len(tweets) >= max_count:
                break

        console.print(f"[green]Fetched {len(tweets)} bookmarks via bird[/green]")
        return tweets


class BookmarkScraper:
    """Scrapes bookmarks from X using cookie authentication."""

    # X API endpoints
    BOOKMARKS_URL = "https://x.com/i/api/graphql/yzqS-gKDfwCL0JNSX_VBow/Bookmarks"

    # Required headers for X API
    HEADERS = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
        "content-type": "application/json",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
    }

    # GraphQL query features
    FEATURES = {
        "graphql_timeline_v2_bookmark_timeline": True,
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "articles_preview_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "creator_subscriptions_quote_tweet_preview_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "rweb_video_timestamps_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
    }

    def __init__(self, cookies_path: Optional[Path] = None):
        """Initialize scraper with cookies."""
        self.cookies_path = cookies_path or CONFIG_DIR / "cookies.json"
        self.cookies = self._load_cookies()
        self.client: Optional[httpx.AsyncClient] = None

    def _load_cookies(self) -> dict:
        """Load cookies from JSON file."""
        if not self.cookies_path.exists():
            raise FileNotFoundError(
                f"Cookies file not found: {self.cookies_path}\n"
                "Copy cookies.example.json to cookies.json and add your X session cookies."
            )

        with open(self.cookies_path) as f:
            cookies = json.load(f)

        required = ["auth_token", "ct0"]
        missing = [k for k in required if k not in cookies or cookies[k].startswith("YOUR_")]
        if missing:
            raise ValueError(f"Missing or invalid cookies: {missing}")

        return cookies

    def _get_headers(self) -> dict:
        """Get headers with CSRF token."""
        headers = self.HEADERS.copy()
        headers["x-csrf-token"] = self.cookies["ct0"]
        return headers

    def _get_cookie_string(self) -> str:
        """Format cookies for header."""
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if not k.startswith("_"))

    async def _init_client(self):
        """Initialize HTTP client."""
        if self.client is None:
            self.client = httpx.AsyncClient(
                headers=self._get_headers(),
                cookies=self.cookies,
                timeout=30.0,
                follow_redirects=True,
            )

    async def _close_client(self):
        """Close HTTP client."""
        if self.client:
            await self.client.aclose()
            self.client = None

    def _parse_tweet(self, tweet_result: dict) -> Optional[Tweet]:
        """Parse tweet data from API response."""
        try:
            # Handle different result types
            if tweet_result.get("__typename") == "TweetWithVisibilityResults":
                tweet_result = tweet_result.get("tweet", {})

            if tweet_result.get("__typename") != "Tweet":
                return None

            legacy = tweet_result.get("legacy", {})
            user = tweet_result.get("core", {}).get("user_results", {}).get("result", {})
            user_legacy = user.get("legacy", {})

            # Extract URLs from entities
            urls = []
            for url_entity in legacy.get("entities", {}).get("urls", []):
                expanded = url_entity.get("expanded_url", "")
                if expanded and not expanded.startswith("https://twitter.com/") and not expanded.startswith("https://x.com/"):
                    urls.append(expanded)

            # Extract media URLs
            media_urls = []
            for media in legacy.get("extended_entities", {}).get("media", []):
                if media.get("type") == "photo":
                    media_urls.append(media.get("media_url_https", ""))
                elif media.get("type") == "video":
                    variants = media.get("video_info", {}).get("variants", [])
                    # Get highest quality video
                    mp4_variants = [v for v in variants if v.get("content_type") == "video/mp4"]
                    if mp4_variants:
                        best = max(mp4_variants, key=lambda v: v.get("bitrate", 0))
                        media_urls.append(best.get("url", ""))

            # Handle quote tweets
            quote_tweet = None
            quoted = tweet_result.get("quoted_status_result", {}).get("result", {})
            if quoted:
                quote_legacy = quoted.get("legacy", {})
                quote_user = quoted.get("core", {}).get("user_results", {}).get("result", {}).get("legacy", {})
                quote_tweet = {
                    "id": quoted.get("rest_id", ""),
                    "text": quote_legacy.get("full_text", ""),
                    "author_username": quote_user.get("screen_name", ""),
                    "author_name": quote_user.get("name", ""),
                }

            return Tweet(
                id=tweet_result.get("rest_id", ""),
                text=legacy.get("full_text", ""),
                author_username=user_legacy.get("screen_name", ""),
                author_name=user_legacy.get("name", ""),
                created_at=legacy.get("created_at", ""),
                urls=urls,
                media_urls=media_urls,
                quote_tweet=quote_tweet,
                reply_to_id=legacy.get("in_reply_to_status_id_str"),
                bookmark_timestamp=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            console.print(f"[yellow]Warning: Failed to parse tweet: {e}[/yellow]")
            return None

    async def fetch_bookmarks(
        self,
        max_count: int = 0,
        since_id: Optional[str] = None,
    ) -> list[Tweet]:
        """
        Fetch bookmarks from X.

        Args:
            max_count: Maximum number of bookmarks to fetch (0 = no limit)
            since_id: Only fetch bookmarks newer than this tweet ID

        Returns:
            List of Tweet objects
        """
        await self._init_client()

        tweets = []
        cursor = None
        stop_fetching = False

        console.print("[blue]Fetching bookmarks from X...[/blue]")

        while not stop_fetching:
            # Build query parameters
            variables = {
                "count": 20,
                "includePromotedContent": False,
            }
            if cursor:
                variables["cursor"] = cursor

            params = {
                "variables": json.dumps(variables),
                "features": json.dumps(self.FEATURES),
            }

            try:
                response = await self.client.get(self.BOOKMARKS_URL, params=params)
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    console.print("[red]Authentication failed. Your cookies may have expired.[/red]")
                    console.print("[yellow]Please update config/cookies.json with fresh cookies from your browser.[/yellow]")
                raise
            except Exception as e:
                console.print(f"[red]Error fetching bookmarks: {e}[/red]")
                raise

            # Parse response
            timeline = data.get("data", {}).get("bookmark_timeline_v2", {}).get("timeline", {})
            instructions = timeline.get("instructions", [])

            entries = []
            for instruction in instructions:
                if instruction.get("type") == "TimelineAddEntries":
                    entries = instruction.get("entries", [])
                    break

            if not entries:
                break

            new_cursor = None
            for entry in entries:
                entry_id = entry.get("entryId", "")

                # Handle cursor entries
                if entry_id.startswith("cursor-bottom"):
                    new_cursor = entry.get("content", {}).get("value")
                    continue

                if not entry_id.startswith("tweet-"):
                    continue

                # Extract tweet
                tweet_result = (
                    entry.get("content", {})
                    .get("itemContent", {})
                    .get("tweet_results", {})
                    .get("result", {})
                )

                tweet = self._parse_tweet(tweet_result)
                if tweet:
                    # Check if we've reached our stopping point
                    if since_id and tweet.id == since_id:
                        stop_fetching = True
                        break

                    tweets.append(tweet)

                    # Check max count
                    if max_count > 0 and len(tweets) >= max_count:
                        stop_fetching = True
                        break

            cursor = new_cursor
            if not cursor:
                break

            console.print(f"[dim]Fetched {len(tweets)} bookmarks so far...[/dim]")

        await self._close_client()
        console.print(f"[green]Fetched {len(tweets)} bookmarks[/green]")

        return tweets

    def save_raw_data(self, tweets: list[Tweet], filename: Optional[str] = None):
        """Save raw tweet data for debugging/reference."""
        save_raw_tweets(tweets, filename=filename)


def get_new_bookmarks(
    processed_ids: list[str],
    max_count: int = 0,
    days_limit: int = 0,
) -> list[Tweet]:
    """
    Fetch only new bookmarks that haven't been processed.

    Args:
        processed_ids: List of already-processed tweet IDs
        max_count: Maximum bookmarks to fetch
        days_limit: Only fetch bookmarks from last N days

    Returns:
        List of new Tweet objects
    """
    # Get the most recent processed ID to use as stopping point
    since_id = processed_ids[0] if processed_ids else None

    # Primary fetch path: bird CLI
    try:
        all_tweets = BirdBookmarkScraper().fetch_bookmarks(
            max_count=max_count,
            since_id=since_id,
        )
    except Exception as bird_error:
        console.print(f"[yellow]bird fetch failed ({bird_error}). Falling back to built-in scraper...[/yellow]")
        scraper = BookmarkScraper()
        all_tweets = asyncio.run(scraper.fetch_bookmarks(
            max_count=max_count,
            since_id=since_id,
        ))

    # Filter out already processed
    processed_set = set(processed_ids)
    new_tweets = [t for t in all_tweets if t.id not in processed_set]

    # Filter by date if specified
    if days_limit > 0:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_limit)
        filtered = []
        for tweet in new_tweets:
            try:
                # Parse X's date format: "Wed Oct 10 20:19:24 +0000 2018"
                tweet_date = datetime.strptime(
                    tweet.created_at,
                    "%a %b %d %H:%M:%S %z %Y"
                )
            except ValueError:
                # Try ISO-like timestamps from alternate scrapers
                try:
                    from dateutil import parser as date_parser
                    tweet_date = date_parser.parse(tweet.created_at)
                except Exception:
                    # If we can't parse the date, include it anyway
                    filtered.append(tweet)
                    continue

            try:
                if tweet_date >= cutoff:
                    filtered.append(tweet)
            except TypeError:
                # Naive datetime fallback
                if tweet_date.replace(tzinfo=timezone.utc) >= cutoff:
                    filtered.append(tweet)
        new_tweets = filtered

    # Save raw data
    if new_tweets:
        save_raw_tweets(new_tweets)

    console.print(f"[green]Found {len(new_tweets)} new bookmarks to process[/green]")
    return new_tweets


if __name__ == "__main__":
    # Test the scraper
    tweets = asyncio.run(BookmarkScraper().fetch_bookmarks(max_count=5))
    for tweet in tweets:
        console.print(f"\n[@{tweet.author_username}]: {tweet.text[:100]}...")
        if tweet.urls:
            console.print(f"  Links: {tweet.urls}")
