"""
Content Parser

Extracts article content from URLs found in bookmarked tweets.
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import httpx
import trafilatura
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .scraper import Tweet

console = Console()


@dataclass
class ExtractedContent:
    """Content extracted from a URL."""
    url: str
    title: Optional[str] = None
    content: Optional[str] = None
    content_type: str = "article"  # article, github, arxiv, youtube, other
    error: Optional[str] = None


@dataclass
class ParsedBookmark:
    """A bookmark with all content extracted."""
    tweet: Tweet
    extracted_content: list[ExtractedContent] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        """Check if any content was successfully extracted."""
        return any(c.content for c in self.extracted_content)

    @property
    def summary_text(self) -> str:
        """Get combined text for AI processing."""
        parts = [f"Tweet by @{self.tweet.author_username}:\n{self.tweet.text}"]

        if self.tweet.quote_tweet:
            qt = self.tweet.quote_tweet
            parts.append(f"\nQuoted tweet by @{qt.get('author_username', 'unknown')}:\n{qt.get('text', '')}")

        for content in self.extracted_content:
            if content.content:
                parts.append(f"\n---\nLinked content from {content.url}:\nTitle: {content.title or 'N/A'}\n{content.content}")

        return "\n".join(parts)


class ContentParser:
    """Parses and extracts content from tweets and linked URLs."""

    # URL patterns for special handling
    GITHUB_PATTERN = re.compile(r"github\.com/([^/]+)/([^/]+)")
    ARXIV_PATTERN = re.compile(r"arxiv\.org/(abs|pdf)/(\d+\.\d+)")
    YOUTUBE_PATTERN = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]+)")

    def __init__(self, timeout: int = 30, max_articles: int = 50):
        self.timeout = timeout
        self.max_articles = max_articles
        self.client: Optional[httpx.AsyncClient] = None

    async def _init_client(self):
        """Initialize HTTP client."""
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                },
            )

    async def _close_client(self):
        """Close HTTP client."""
        if self.client:
            await self.client.aclose()
            self.client = None

    def _identify_content_type(self, url: str) -> str:
        """Identify the type of content from URL."""
        if self.GITHUB_PATTERN.search(url):
            return "github"
        if self.ARXIV_PATTERN.search(url):
            return "arxiv"
        if self.YOUTUBE_PATTERN.search(url):
            return "youtube"
        return "article"

    async def _extract_github(self, url: str) -> ExtractedContent:
        """Extract content from GitHub repository."""
        match = self.GITHUB_PATTERN.search(url)
        if not match:
            return ExtractedContent(url=url, error="Invalid GitHub URL")

        owner, repo = match.groups()
        # Clean repo name (remove .git suffix, query params, etc.)
        repo = repo.split("?")[0].split("#")[0].rstrip(".git")

        # Fetch README via raw.githubusercontent.com
        readme_urls = [
            f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md",
            f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.md",
            f"https://raw.githubusercontent.com/{owner}/{repo}/main/readme.md",
            f"https://raw.githubusercontent.com/{owner}/{repo}/master/readme.md",
        ]

        for readme_url in readme_urls:
            try:
                response = await self.client.get(readme_url)
                if response.status_code == 200:
                    content = response.text
                    # Truncate very long READMEs
                    if len(content) > 10000:
                        content = content[:10000] + "\n\n[README truncated...]"

                    return ExtractedContent(
                        url=url,
                        title=f"{owner}/{repo}",
                        content=content,
                        content_type="github",
                    )
            except Exception:
                continue

        # If no README found, just return basic info
        return ExtractedContent(
            url=url,
            title=f"{owner}/{repo}",
            content=f"GitHub repository: {owner}/{repo}\nURL: {url}",
            content_type="github",
        )

    async def _extract_arxiv(self, url: str) -> ExtractedContent:
        """Extract abstract from arXiv paper."""
        match = self.ARXIV_PATTERN.search(url)
        if not match:
            return ExtractedContent(url=url, error="Invalid arXiv URL")

        paper_id = match.group(2)
        abs_url = f"https://arxiv.org/abs/{paper_id}"

        try:
            response = await self.client.get(abs_url)
            response.raise_for_status()

            # Use trafilatura to extract the page content
            downloaded = trafilatura.extract(
                response.text,
                include_comments=False,
                include_tables=False,
            )

            if downloaded:
                return ExtractedContent(
                    url=url,
                    title=f"arXiv:{paper_id}",
                    content=downloaded,
                    content_type="arxiv",
                )
        except Exception as e:
            return ExtractedContent(url=url, content_type="arxiv", error=str(e))

        return ExtractedContent(
            url=url,
            title=f"arXiv:{paper_id}",
            content=f"arXiv paper: {paper_id}\nURL: {abs_url}",
            content_type="arxiv",
        )

    async def _extract_youtube(self, url: str) -> ExtractedContent:
        """Extract info from YouTube video (title and description only)."""
        match = self.YOUTUBE_PATTERN.search(url)
        if not match:
            return ExtractedContent(url=url, error="Invalid YouTube URL")

        video_id = match.group(1)

        try:
            # Fetch the video page to get title/description
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
            response = await self.client.get(watch_url)
            response.raise_for_status()

            # Try to extract title from page
            title_match = re.search(r'<title>([^<]+)</title>', response.text)
            title = title_match.group(1).replace(" - YouTube", "").strip() if title_match else None

            # Try to extract description
            desc_match = re.search(r'"shortDescription":"([^"]+)"', response.text)
            description = desc_match.group(1) if desc_match else None
            if description:
                # Unescape newlines
                description = description.replace("\\n", "\n")

            content = f"YouTube Video: {title or video_id}\n"
            if description:
                content += f"\nDescription:\n{description[:2000]}"

            return ExtractedContent(
                url=url,
                title=title,
                content=content,
                content_type="youtube",
            )
        except Exception as e:
            return ExtractedContent(
                url=url,
                title=f"YouTube: {video_id}",
                content=f"YouTube video ID: {video_id}\nURL: {url}",
                content_type="youtube",
                error=str(e),
            )

    async def _extract_article(self, url: str) -> ExtractedContent:
        """Extract article content using trafilatura."""
        try:
            response = await self.client.get(url)
            response.raise_for_status()

            # Extract with trafilatura
            downloaded = trafilatura.extract(
                response.text,
                include_comments=False,
                include_tables=True,
                include_links=False,
                output_format="txt",
            )

            if not downloaded:
                return ExtractedContent(
                    url=url,
                    content_type="article",
                    error="Could not extract content",
                )

            # Try to get title
            title = None
            title_match = re.search(r'<title>([^<]+)</title>', response.text, re.IGNORECASE)
            if title_match:
                title = title_match.group(1).strip()

            # Truncate very long articles
            if len(downloaded) > 15000:
                downloaded = downloaded[:15000] + "\n\n[Article truncated...]"

            return ExtractedContent(
                url=url,
                title=title,
                content=downloaded,
                content_type="article",
            )
        except httpx.HTTPStatusError as e:
            return ExtractedContent(url=url, content_type="article", error=f"HTTP {e.response.status_code}")
        except Exception as e:
            return ExtractedContent(url=url, content_type="article", error=str(e))

    async def extract_url(self, url: str) -> ExtractedContent:
        """Extract content from a single URL."""
        content_type = self._identify_content_type(url)

        if content_type == "github":
            return await self._extract_github(url)
        elif content_type == "arxiv":
            return await self._extract_arxiv(url)
        elif content_type == "youtube":
            return await self._extract_youtube(url)
        else:
            return await self._extract_article(url)

    async def parse_bookmarks(self, tweets: list[Tweet]) -> list[ParsedBookmark]:
        """Parse all bookmarks and extract linked content."""
        await self._init_client()

        parsed = []
        total_urls = sum(len(t.urls) for t in tweets)

        console.print(f"[blue]Parsing {len(tweets)} bookmarks with {total_urls} linked URLs...[/blue]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Extracting content...", total=len(tweets))

            for tweet in tweets:
                bookmark = ParsedBookmark(tweet=tweet)

                # Extract content from each URL
                for url in tweet.urls[:5]:  # Limit URLs per tweet
                    progress.update(task, description=f"Extracting: {urlparse(url).netloc}...")
                    content = await self.extract_url(url)
                    bookmark.extracted_content.append(content)

                parsed.append(bookmark)
                progress.advance(task)

        await self._close_client()

        # Summary
        successful = sum(1 for p in parsed if p.has_content)
        console.print(f"[green]Parsed {len(parsed)} bookmarks, {successful} with extracted content[/green]")

        return parsed


def parse_bookmarks(tweets: list[Tweet], timeout: int = 30) -> list[ParsedBookmark]:
    """
    Convenience function to parse bookmarks.

    Args:
        tweets: List of Tweet objects from scraper
        timeout: Request timeout in seconds

    Returns:
        List of ParsedBookmark objects with extracted content
    """
    parser = ContentParser(timeout=timeout)
    return asyncio.run(parser.parse_bookmarks(tweets))


if __name__ == "__main__":
    # Test content extraction
    async def test():
        parser = ContentParser()
        await parser._init_client()

        test_urls = [
            "https://github.com/anthropics/anthropic-sdk-python",
            "https://arxiv.org/abs/2303.08774",
        ]

        for url in test_urls:
            content = await parser.extract_url(url)
            console.print(f"\n[bold]{url}[/bold]")
            console.print(f"Type: {content.content_type}")
            console.print(f"Title: {content.title}")
            if content.content:
                console.print(f"Content preview: {content.content[:200]}...")
            if content.error:
                console.print(f"[red]Error: {content.error}[/red]")

        await parser._close_client()

    asyncio.run(test())
