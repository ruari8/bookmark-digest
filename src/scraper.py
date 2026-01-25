"""
X/Twitter Bookmark Scraper

Uses cookie-based authentication to fetch bookmarks from X's internal API.
"""

import json
import asyncio
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
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"bookmarks_{timestamp}.json"

        raw_dir = DATA_DIR / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        filepath = raw_dir / filename
        with open(filepath, "w") as f:
            json.dump([asdict(t) for t in tweets], f, indent=2)

        console.print(f"[dim]Saved raw data to {filepath}[/dim]")


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
    scraper = BookmarkScraper()

    # Get the most recent processed ID to use as stopping point
    since_id = processed_ids[0] if processed_ids else None

    # Fetch bookmarks
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
                if tweet_date >= cutoff:
                    filtered.append(tweet)
            except ValueError:
                # If we can't parse the date, include it anyway
                filtered.append(tweet)
        new_tweets = filtered

    # Save raw data
    if new_tweets:
        scraper.save_raw_data(new_tweets)

    console.print(f"[green]Found {len(new_tweets)} new bookmarks to process[/green]")
    return new_tweets


if __name__ == "__main__":
    # Test the scraper
    tweets = asyncio.run(BookmarkScraper().fetch_bookmarks(max_count=5))
    for tweet in tweets:
        console.print(f"\n[@{tweet.author_username}]: {tweet.text[:100]}...")
        if tweet.urls:
            console.print(f"  Links: {tweet.urls}")
