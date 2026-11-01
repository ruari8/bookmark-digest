"""
Static site renderer for Bookmark Notes.

Design concept: a weekly broadsheet. Each post is a numbered issue with a
masthead, dateline and standfirst; the bookmarks behind it are printed as a
numbered list of clippings. One ink colour, one red accent, hairline rules,
no cards or gradients.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from rich.console import Console

from .blog import (
    BlogPost,
    BlogState,
    TweetContext,
    _extract_summary,
    _extract_title,
    _strip_leading_h1,
)

console = Console()

ASSET_VERSION = "20261101-1"
SAFE_URL_SCHEMES = {"http", "https", "mailto"}
URL_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.\-]*):")


class StaticSiteBuilder:
    """Renders generated Markdown posts to a static site."""

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

    # ------------------------------------------------------------------ build

    def write_post(self, post: BlogPost) -> tuple[Path, Path]:
        self.content_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = self.content_dir / f"{post.slug}.md"
        existed = markdown_path.exists()
        markdown_path.write_text(with_front_matter(post))
        try:
            self.render_all()
        except Exception:
            # Never leave an unrenderable post behind: every later render
            # re-reads all posts, so it would block future issues too.
            if not existed:
                markdown_path.unlink(missing_ok=True)
            raise
        return markdown_path, self.site_dir / "posts" / post.slug / "index.html"

    def render_all(self) -> Path:
        self.site_dir.mkdir(parents=True, exist_ok=True)
        paths = sorted(self.content_dir.glob("*.md"))  # oldest first: slugs start with a date
        posts = [self._load_post(path, issue=index) for index, path in enumerate(paths, 1)]
        posts.reverse()  # newest first from here on

        (self.site_dir / "styles.css").write_text(STYLES)
        for post in posts:
            post_dir = self.site_dir / "posts" / post["slug"]
            post_dir.mkdir(parents=True, exist_ok=True)
            (post_dir / "index.html").write_text(self._post_page(post))
        (self.site_dir / "posts").mkdir(parents=True, exist_ok=True)
        (self.site_dir / "posts" / "index.html").write_text(self._archive_page(posts))
        (self.site_dir / "index.html").write_text(self._front_page(posts))
        (self.site_dir / "feed.xml").write_text(self._feed(posts))
        return self.site_dir / "index.html"

    def _load_post(self, path: Path, issue: int) -> dict:
        metadata, body = parse_front_matter(path.read_text())
        slug = metadata.get("slug") or path.stem
        title = metadata.get("title") or _extract_title(body)
        body = _strip_leading_h1(body)
        standfirst, body = split_standfirst(body)
        return {
            "slug": slug,
            "issue": issue,
            "title": title,
            "summary": plain_text(metadata.get("summary") or _extract_summary(standfirst or body)),
            "standfirst_html": inline_markdown(standfirst) if standfirst else "",
            "date": metadata.get("date") or "",
            "cover_image": metadata.get("cover_image") or "",
            "bookmark_count": int(metadata.get("bookmark_count") or 0),
            "sections": [
                line[3:].strip()
                for line in body.splitlines()
                if line.startswith("## ") and line[3:].strip().lower() not in {"source trail", "sources"}
            ],
            "minutes": max(1, round(len(body.split()) / 230)),
            "html": markdown_to_html(body),
            "url": f"{self.base_url}/posts/{slug}/",
            "bookmarks": self.state.bookmarks_for_post(slug) if self.state else [],
        }

    # ---------------------------------------------------------------- layout

    def _layout(self, title: str, description: str, body: str, url: str = "", masthead: bool = False) -> str:
        has_feedback = "data-bookmark-id=" in body or 'class="private-feedback"' in body
        header = "" if masthead else f"""
  <header class="topbar">
    <a class="topbar-title" href="/">{html.escape(self.site_title)}</a>
    <nav><a href="/posts/">Issues</a><a href="/feed.xml">RSS</a></nav>
  </header>"""
        canonical = f'\n  <link rel="canonical" href="{html.escape(url)}">' if url else ""
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <meta name="description" content="{html.escape(description)}">
  <meta property="og:title" content="{html.escape(title)}">
  <meta property="og:description" content="{html.escape(description)}">
  <meta property="og:type" content="{'article' if url else 'website'}">
  <meta name="twitter:card" content="summary">{canonical}
  <meta name="color-scheme" content="light dark">
  <link rel="alternate" type="application/rss+xml" title="{html.escape(self.site_title)}" href="/feed.xml">
  <link rel="stylesheet" href="/styles.css?v={ASSET_VERSION}">
</head>
<body>{header}
{body}
  <footer class="colophon">
    <p>{html.escape(self.site_title)} is put together every Sunday from the posts Ruari bookmarked on X that week.</p>
    <p><a href="/posts/">All issues</a> &middot; <a href="/feed.xml">RSS</a></p>
  </footer>
{FEEDBACK_SCRIPT if has_feedback else ""}
</body>
</html>
"""

    def _masthead(self, latest: Optional[dict]) -> str:
        dateline = ""
        if latest:
            dateline = (
                f"<span>No. {latest['issue']}</span>"
                f"<span>{html.escape(format_date(latest['date'], weekday=True))}</span>"
                f"<span>{html.escape(self.site_description)}</span>"
            )
        return f"""
  <header class="masthead">
    <h1 class="masthead-title"><a href="/">{html.escape(self.site_title)}</a></h1>
    <p class="dateline">{dateline}</p>
  </header>"""

    # ----------------------------------------------------------------- pages

    def _front_page(self, posts: list[dict]) -> str:
        latest = posts[0] if posts else None
        lead = '<p class="empty">The first issue is on its way.</p>'
        if latest:
            contents = "".join(f"<li>{html.escape(section)}</li>" for section in latest["sections"])
            contents_html = f"""
      <aside class="lead-contents">
        <h2 class="label">In this issue</h2>
        <ol>{contents}</ol>
      </aside>""" if contents else ""
            lead = f"""
    <article class="lead">
      <div class="lead-story">
        <p class="kicker">This week &middot; {self._issue_meta(latest)}</p>
        <h2 class="lead-headline"><a href="/posts/{html.escape(latest['slug'])}/">{html.escape(latest['title'])}</a></h2>
        <p class="standfirst">{html.escape(latest['summary'])}</p>
        <p class="read-on"><a href="/posts/{html.escape(latest['slug'])}/">Read issue No. {latest['issue']} &rarr;</a></p>
      </div>{contents_html}
    </article>"""
        back = self._issue_list(posts[1:])
        back_html = f"""
    <section class="back-issues">
      <h2 class="label">Back issues</h2>
      {back}
    </section>""" if posts[1:] else ""
        body = f"""{self._masthead(latest)}
  <main class="front">{lead}{back_html}
  </main>"""
        return self._layout(self.site_title, self.site_description, body, masthead=True)

    def _archive_page(self, posts: list[dict]) -> str:
        body = f"""
  <main class="archive">
    <h1 class="page-title">Every issue</h1>
    {self._issue_list(posts) if posts else '<p class="empty">No issues yet.</p>'}
  </main>"""
        return self._layout(f"Issues - {self.site_title}", self.site_description, body)

    def _issue_list(self, posts: list[dict]) -> str:
        rows = "".join(
            f"""
      <li>
        <span class="issue-no">No. {post['issue']}</span>
        <time datetime="{html.escape(post['date'])}">{html.escape(format_date(post['date']))}</time>
        <div>
          <h3><a href="/posts/{html.escape(post['slug'])}/">{html.escape(post['title'])}</a></h3>
          <p>{html.escape(post['summary'])}</p>
        </div>
      </li>"""
            for post in posts
        )
        return f'<ol class="issue-list">{rows}\n    </ol>'

    def _issue_meta(self, post: dict) -> str:
        parts = [f"No. {post['issue']}", html.escape(format_date(post["date"]))]
        if post["bookmark_count"]:
            parts.append(f"{post['bookmark_count']} bookmarks")
        parts.append(f"{post['minutes']} min read")
        return " &middot; ".join(parts)

    def _post_page(self, post: dict) -> str:
        cover = ""
        if post["cover_image"]:
            cover = f'<img class="cover" src="{html.escape(self._cache_media_url(post["cover_image"]))}" alt="">'
        body = f"""
  <main>
    <article class="issue">
      <header class="issue-head">
        <p class="kicker">{self._issue_meta(post)}</p>
        <h1>{html.escape(post['title'])}</h1>
        <p class="standfirst">{post['standfirst_html'] or html.escape(post['summary'])}</p>
      </header>
      {cover}
      <div class="issue-body">
{post['html']}
      </div>
      {self._clippings(post)}
      {self._feedback_panel(post)}
    </article>
  </main>"""
        return self._layout(f"{post['title']} - {self.site_title}", post["summary"], body, url=post["url"])

    # ------------------------------------------------------------- clippings

    def _clippings(self, post: dict) -> str:
        bookmarks = post["bookmarks"]
        if not bookmarks:
            return ""
        items = "\n".join(self._clipping(tweet, number) for number, tweet in enumerate(bookmarks, 1))
        return f"""
      <section class="clippings" aria-labelledby="clippings-title">
        <h2 class="label" id="clippings-title">The clippings &middot; {len(bookmarks)} bookmarks behind this issue</h2>
        <ol>
{items}
        </ol>
      </section>"""

    def _clipping(self, tweet: TweetContext, number: int) -> str:
        links = "".join(
            f'<a href="{html.escape(url)}">{html.escape(compact_url(url))}</a>' for url in tweet.urls
        )
        quote = ""
        if tweet.quote_tweet:
            q = tweet.quote_tweet
            quote = f"""
            <blockquote class="clip-quote">
              <p><a href="{html.escape(q.tweet_url)}">@{html.escape(q.author_username)}</a> {html.escape(display_text(q.text))}</p>
              {self._media_html(q)}
            </blockquote>"""
        # Ratings and notes are private. They are never baked into the static
        # HTML; the localhost-only script fetches them from /api/feedback.
        buttons = "".join(
            f'<button type="button" data-rating="{value}">{label}</button>'
            for value, label in [(-1, "Low"), (1, "Useful"), (2, "Strong"), (3, "Top")]
        )
        return f"""
          <li class="clip" data-bookmark-id="{html.escape(tweet.id)}">
            <span class="clip-no">{number:02d}</span>
            <div class="clip-body">
              <p class="clip-meta"><a href="{html.escape(tweet.tweet_url)}">@{html.escape(tweet.author_username)}</a> &middot; {html.escape(format_date(tweet.created_at))}</p>
              <p class="clip-text">{html.escape(display_text(tweet.text))}</p>
              {f'<p class="clip-links">{links}</p>' if links else ''}{quote}
              {self._media_html(tweet)}
              <div class="private-feedback-surface">
                <div class="rating-buttons">{buttons}</div>
                <textarea data-note aria-label="Private note on clipping {number:02d} by @{html.escape(tweet.author_username)}" placeholder="Why this was or was not interesting"></textarea>
                <p class="save-status" aria-live="polite"></p>
              </div>
            </div>
          </li>"""

    def _media_html(self, tweet: TweetContext) -> str:
        items = []
        for media in tweet.media[:3]:
            if media.video_url:
                src = self._cache_media_url(media.video_url)
                poster = f' poster="{html.escape(self._cache_media_url(media.url))}"' if media.url else ""
                items.append(
                    f'<video controls playsinline preload="none"{poster}>'
                    f'<source src="{html.escape(src)}" type="video/mp4"><a href="{html.escape(src)}">Open video</a></video>'
                )
            elif media.url:
                items.append(f'<img src="{html.escape(self._cache_media_url(media.url))}" alt="" loading="lazy">')
        return f'<div class="clip-media">{"".join(items)}</div>' if items else ""

    def _feedback_panel(self, post: dict) -> str:
        return f"""
      <section class="private-feedback" data-post-slug="{html.escape(post['slug'])}">
        <h2 class="label">Private curation notes</h2>
        <p class="feedback-help">Only visible on localhost. Notes and ratings are stored locally and fed into the next issue's prompt.</p>
        <label class="label" for="post-feedback">Notes for the next issue</label>
        <textarea id="post-feedback" placeholder="What did this issue get right? What should the next one do differently?"></textarea>
        <button type="button" id="save-post-feedback">Save notes</button>
        <p class="save-status" id="post-feedback-status" aria-live="polite"></p>
      </section>"""

    # ------------------------------------------------------------------ feed

    def _feed(self, posts: list[dict]) -> str:
        items = []
        for post in posts[:20]:
            pub_date = ""
            parsed = parse_date(post["date"])
            if parsed:
                pub_date = f"\n      <pubDate>{format_datetime(parsed)}</pubDate>"
            items.append(f"""
    <item>
      <title>{html.escape(post['title'])}</title>
      <link>{html.escape(post['url'])}</link>
      <guid>{html.escape(post['url'])}</guid>{pub_date}
      <description>{html.escape(post['summary'])}</description>
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

    # ----------------------------------------------------------------- media

    def _cache_media_url(self, url: str) -> str:
        """Download remote media into site/media so pages never hotlink X."""
        if not self.cache_remote_media or not url.startswith(("http://", "https://")):
            return url

        extension = Path(urlparse(url).path).suffix.lower()
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
                if int(response.headers.get("Content-Length") or "0") > self.max_cached_media_bytes:
                    console.print(f"[yellow]Skipping large media asset: {url}[/yellow]")
                    return url
                written = 0
                with target.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        written += len(chunk)
                        if written > self.max_cached_media_bytes:
                            break
                        output.write(chunk)
            if written > self.max_cached_media_bytes:
                target.unlink(missing_ok=True)
                console.print(f"[yellow]Skipping large media asset: {url}[/yellow]")
                return url
        except Exception as error:
            target.unlink(missing_ok=True)
            console.print(f"[yellow]Could not cache media asset ({error}): {url}[/yellow]")
            return url
        return f"/media/{target.name}"


# --------------------------------------------------------------------- helpers


def with_front_matter(post: BlogPost) -> str:
    front_matter = {
        "title": post.title,
        "slug": post.slug,
        "date": datetime.now(timezone.utc).isoformat(),
        "summary": post.summary,
        "cover_image": post.cover_image,
        "latest_bookmark_id": post.latest_bookmark_id,
        "bookmark_count": str(len(post.bookmark_ids)),
        "bookmark_ids": ",".join(post.bookmark_ids),
    }
    lines = ["---", *(f"{key}: {value}" for key, value in front_matter.items()), "---", "", post.markdown.strip(), ""]
    return "\n".join(lines)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    metadata = {}
    for line in text[4:end].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    return metadata, text[end + 4 :].strip()


def markdown_to_html(markdown_text: str) -> str:
    """Render model-written Markdown with raw HTML disabled and unsafe links dropped.

    Post bodies are written by a model that read untrusted tweets and web
    pages, so a prompt-injected <script> or javascript: link must not reach
    the public site.
    """
    import markdown as markdown_lib
    from markdown.treeprocessors import Treeprocessor

    class DropUnsafeUrls(Treeprocessor):
        def run(self, root):
            for element in root.iter():
                for attribute in ("href", "src"):
                    value = element.get(attribute)
                    if value is not None and not is_safe_url(value):
                        del element.attrib[attribute]

    md = markdown_lib.Markdown(extensions=["tables", "fenced_code", "sane_lists", "smarty"])
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    md.treeprocessors.register(DropUnsafeUrls(md), "drop_unsafe_urls", 0)
    return md.convert(markdown_text)


def is_safe_url(value: str) -> bool:
    """True for http(s)/mailto URLs and scheme-less relative links.

    Checks the URL the browser will see: HTML entities decoded (repeatedly, to
    catch double encoding) and whitespace/control characters removed, so
    `javascript&#58;...` or `jav\tascript:` cannot slip through. Never raises.
    """
    decoded = value
    for _ in range(3):
        unescaped = html.unescape(decoded)
        if unescaped == decoded:
            break
        decoded = unescaped
    normalized = re.sub(r"[\x00-\x20\x7f]+", "", decoded).lower()
    if normalized.startswith("//"):
        return True  # protocol-relative: resolves to http(s)
    match = URL_SCHEME_RE.match(normalized)
    if match:
        return match.group(1) in SAFE_URL_SCHEMES
    # No valid scheme. A colon before any / ? # would still be read as a scheme.
    return ":" not in re.split(r"[/?#]", normalized, maxsplit=1)[0]


def split_standfirst(body: str) -> tuple[str, str]:
    """Lift the opening plain paragraph out of the body to use as the standfirst.

    The writer is asked for a two-sentence standfirst under the headline, and
    older posts open with a summary paragraph. Either way, showing it as the
    standfirst and again as the first body paragraph would repeat it.
    """
    first, _, rest = body.strip().partition("\n\n")
    if not rest.strip() or not first.strip() or first.lstrip()[0] in "#!-*+>|`[0123456789":
        return "", body
    return first.strip(), rest.strip()


def inline_markdown(text: str) -> str:
    """Render one paragraph of Markdown to inline HTML (no wrapping <p>)."""
    rendered = markdown_to_html(text).strip()
    if rendered.startswith("<p>") and rendered.endswith("</p>"):
        rendered = rendered[3:-4]
    return rendered


def plain_text(text: str) -> str:
    """Strip Markdown emphasis and code markers from short summaries."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"\*\*|\*|`", "", text).strip()


def parse_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    for parser in (
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
        lambda v: datetime.strptime(v, "%a %b %d %H:%M:%S %z %Y"),  # X's created_at format
    ):
        try:
            return parser(value)
        except ValueError:
            continue
    return None


def format_date(value: str, weekday: bool = False) -> str:
    parsed = parse_date(value)
    if not parsed:
        return value[:10]
    text = f"{parsed.day} {parsed:%B %Y}"
    return f"{parsed:%A} {text}" if weekday else text


def display_text(text: str) -> str:
    """Drop t.co shortlinks from tweet text; the expanded links are listed separately."""
    return re.sub(r"\s*https://t\.co/\w+", "", text).strip() or text


def compact_url(value: str) -> str:
    cleaned = re.sub(r"^https?://(www\.)?", "", value, flags=re.I).rstrip("/")
    return cleaned[:44] + "..." if len(cleaned) > 47 else cleaned


FEEDBACK_SCRIPT = """
<script>
(() => {
  if (!["localhost", "127.0.0.1", "::1"].includes(window.location.hostname)) return;
  document.documentElement.classList.add("local-feedback");

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  function setActive(card, rating) {
    card.querySelectorAll("[data-rating]").forEach((button) => {
      const on = Number(button.dataset.rating) === Number(rating || 0);
      button.classList.toggle("active", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  document.querySelectorAll("[data-bookmark-id]").forEach((card) => {
    const note = card.querySelector("[data-note]");
    const status = card.querySelector(".save-status");
    async function save(rating) {
      try {
        await postJSON("/api/bookmark-feedback", {
          bookmark_id: card.dataset.bookmarkId,
          rating,
          note: note ? note.value : "",
        });
        setActive(card, rating);
        if (status) status.textContent = "Saved";
      } catch (_error) {
        if (status) status.textContent = "Could not save";
      }
    }
    card.querySelectorAll("[data-rating]").forEach((button) => {
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
  const status = panel.querySelector("#post-feedback-status");

  fetch(`/api/feedback?slug=${encodeURIComponent(panel.dataset.postSlug)}`)
    .then((response) => (response.ok ? response.json() : null))
    .then((payload) => {
      if (!payload) return;
      textarea.value = payload.post_feedback || "";
      Object.entries(payload.bookmarks || {}).forEach(([bookmarkId, item]) => {
        const card = document.querySelector(`[data-bookmark-id="${CSS.escape(bookmarkId)}"]`);
        if (!card) return;
        const note = card.querySelector("[data-note]");
        if (note) note.value = item.note || "";
        setActive(card, item.rating);
      });
    })
    .catch(() => {});

  panel.querySelector("#save-post-feedback").addEventListener("click", async () => {
    try {
      await postJSON("/api/post-feedback", { slug: panel.dataset.postSlug, feedback: textarea.value });
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
  --paper: #f5f2ea;
  --ink: #1b1a17;
  --muted: #5f5b52;
  --rule: #1b1a17;
  --hairline: #d6d0c2;
  --red: #9e2a1f;
  --serif: "Iowan Old Style", "Charter", "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif;
  --display: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
  --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
  --measure: 40rem;
}

@media (prefers-color-scheme: dark) {
  :root {
    --paper: #151412;
    --ink: #ebe6da;
    --muted: #a29d91;
    --rule: #ebe6da;
    --hairline: #3a3731;
    --red: #e07a67;
  }
}

* { box-sizing: border-box; }

html { -webkit-text-size-adjust: 100%; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font: 1.125rem/1.6 var(--serif);
  font-kerning: normal;
  text-rendering: optimizeLegibility;
}

a { color: inherit; text-decoration-color: var(--red); text-decoration-thickness: 1px; text-underline-offset: 0.18em; }
a:hover { color: var(--red); }

.label, .kicker, .dateline, .clip-no, .clip-meta, .issue-no, time, .topbar, .colophon {
  font-family: var(--mono);
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

/* ---- top bar (inner pages) */
.topbar {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 1rem;
  max-width: 72rem;
  margin: 0 auto;
  padding: 1.1rem clamp(1.1rem, 4vw, 2.5rem) 0.8rem;
  border-bottom: 1px solid var(--rule);
}
.topbar-title { font-family: var(--display); font-size: 1.15rem; font-weight: 700; letter-spacing: 0; text-transform: none; text-decoration: none; }
.topbar nav { display: flex; gap: 1.25rem; }
.topbar nav a { text-decoration: none; color: var(--muted); }

/* ---- masthead (front page) */
.masthead {
  max-width: 72rem;
  margin: 0 auto;
  padding: clamp(1.75rem, 5vw, 3.25rem) clamp(1.1rem, 4vw, 2.5rem) 0;
  text-align: center;
}
.masthead-title {
  margin: 0;
  font-family: var(--display);
  font-weight: 700;
  font-size: clamp(2.9rem, 10vw, 6.75rem);
  line-height: 0.95;
  letter-spacing: -0.02em;
}
.masthead-title a { text-decoration: none; }
.dateline {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 0.4rem 1.5rem;
  margin: clamp(1rem, 3vw, 1.6rem) 0 0;
  padding: 0.55rem 0;
  border-top: 3px solid var(--rule);
  border-bottom: 1px solid var(--rule);
  box-shadow: 0 -6px 0 -5px var(--rule);
  color: var(--muted);
}

/* ---- front page */
.front, .archive, main { max-width: 72rem; margin: 0 auto; padding: 0 clamp(1.1rem, 4vw, 2.5rem); }

.lead {
  display: grid;
  grid-template-columns: minmax(0, 2.2fr) minmax(14rem, 1fr);
  gap: clamp(1.5rem, 4vw, 3.5rem);
  padding: clamp(1.75rem, 5vw, 3.25rem) 0;
  border-bottom: 1px solid var(--rule);
}
.kicker { margin: 0 0 0.9rem; color: var(--red); }
.lead-headline {
  margin: 0;
  font-family: var(--display);
  font-size: clamp(2.1rem, 5.4vw, 4.1rem);
  line-height: 1.02;
  letter-spacing: -0.015em;
  text-wrap: balance;
}
.lead-headline a { text-decoration: none; }
.standfirst { margin: 1.1rem 0 0; max-width: 38rem; font-size: clamp(1.15rem, 2vw, 1.35rem); line-height: 1.45; font-style: italic; color: var(--muted); text-wrap: pretty; }
.read-on { margin: 1.4rem 0 0; font-weight: 700; }

.lead-contents { border-left: 1px solid var(--hairline); padding-left: clamp(1rem, 2.5vw, 1.75rem); }
.lead-contents .label { margin: 0.2rem 0 0.9rem; color: var(--muted); font-weight: 400; }
.lead-contents ol { margin: 0; padding: 0; list-style: none; counter-reset: contents; }
.lead-contents li { counter-increment: contents; display: grid; grid-template-columns: 1.6rem 1fr; padding: 0.55rem 0; border-top: 1px solid var(--hairline); font-size: 1rem; line-height: 1.35; }
.lead-contents li::before { content: counter(contents); font-family: var(--mono); font-size: 0.75rem; color: var(--red); padding-top: 0.2rem; }

.back-issues { padding: clamp(1.75rem, 4vw, 2.75rem) 0 1rem; }
.back-issues > .label, .page-title { margin: 0 0 0.5rem; color: var(--muted); font-weight: 400; }
.page-title { font-family: var(--display); font-size: clamp(2.2rem, 6vw, 3.6rem); color: var(--ink); letter-spacing: -0.015em; text-transform: none; margin: clamp(1.75rem, 5vw, 3rem) 0 1rem; }

.issue-list { list-style: none; margin: 0; padding: 0; }
.issue-list li {
  display: grid;
  grid-template-columns: 4.5rem 9.5rem minmax(0, 1fr);
  gap: 0 1.25rem;
  align-items: baseline;
  padding: 1.05rem 0;
  border-top: 1px solid var(--hairline);
}
.issue-list li:first-child { border-top-color: var(--rule); }
.issue-no { color: var(--red); }
.issue-list time { color: var(--muted); }
.issue-list h3 { margin: 0; font-family: var(--display); font-size: 1.3rem; line-height: 1.2; text-wrap: balance; }
.issue-list h3 a { text-decoration: none; }
.issue-list p { margin: 0.3rem 0 0; color: var(--muted); font-size: 1rem; line-height: 1.45; max-width: 46rem; }

/* ---- issue page */
.issue { max-width: var(--measure); margin: 0 auto; padding: clamp(2rem, 6vw, 4rem) 0 2rem; }
.issue-head { padding-bottom: 1.6rem; border-bottom: 1px solid var(--rule); margin-bottom: 2rem; }
.issue-head h1 {
  margin: 0;
  font-family: var(--display);
  font-size: clamp(2.2rem, 6.4vw, 3.6rem);
  line-height: 1.03;
  letter-spacing: -0.015em;
  text-wrap: balance;
}
.cover { width: 100%; margin: 0 0 2rem; border: 1px solid var(--hairline); }

.issue-body > p:first-child::first-letter {
  float: left;
  margin: 0.06em 0.09em 0 0;
  font-family: var(--display);
  font-size: 3.6em;
  line-height: 0.82;
  font-weight: 700;
  color: var(--red);
}
.issue-body p, .issue-body li { text-wrap: pretty; }
.issue-body h2 {
  margin: 2.6rem 0 0.9rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--rule);
  font-family: var(--display);
  font-size: 1.6rem;
  line-height: 1.15;
  letter-spacing: -0.01em;
  text-wrap: balance;
}
.issue-body h3 { margin: 2rem 0 0.5rem; font-size: 1.2rem; }
.issue-body blockquote { margin: 1.5rem 0; padding: 0 0 0 1.1rem; border-left: 2px solid var(--red); font-style: italic; }
.issue-body img { max-width: 100%; height: auto; border: 1px solid var(--hairline); }
.issue-body code { font-family: var(--mono); font-size: 0.85em; background: color-mix(in srgb, var(--hairline) 45%, transparent); padding: 0.05em 0.3em; }
.issue-body pre { overflow-x: auto; padding: 1rem; border: 1px solid var(--hairline); font-size: 0.85rem; }
.issue-body pre code { background: none; padding: 0; }
.issue-body table { border-collapse: collapse; width: 100%; font-size: 0.95rem; }
.issue-body th, .issue-body td { border-top: 1px solid var(--hairline); padding: 0.4rem 0.6rem 0.4rem 0; text-align: left; vertical-align: top; }
.issue-body a { overflow-wrap: anywhere; }

/* ---- clippings */
.clippings { margin-top: 3.5rem; padding-top: 0.75rem; border-top: 3px solid var(--rule); }
.clippings > .label { margin: 0 0 0.5rem; color: var(--muted); font-weight: 400; }
.clippings ol { list-style: none; margin: 0; padding: 0; }
.clip { display: grid; grid-template-columns: 2.25rem minmax(0, 1fr); gap: 0 0.75rem; padding: 1.1rem 0; border-top: 1px solid var(--hairline); }
.clip-no { color: var(--red); padding-top: 0.2rem; }
.clip-meta { margin: 0 0 0.35rem; color: var(--muted); }
.clip-meta a { text-decoration: none; }
.clip-text { margin: 0; font-size: 1rem; line-height: 1.5; overflow-wrap: anywhere; }
.clip-links { display: flex; flex-wrap: wrap; gap: 0.25rem 1rem; margin: 0.5rem 0 0; font-family: var(--mono); font-size: 0.75rem; }
.clip-quote { margin: 0.75rem 0 0; padding-left: 0.9rem; border-left: 1px solid var(--rule); font-size: 0.95rem; color: var(--muted); }
.clip-quote p { margin: 0; overflow-wrap: anywhere; }
.clip-media { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.75rem; }
.clip-media img, .clip-media video { max-width: 100%; max-height: 18rem; width: auto; border: 1px solid var(--hairline); background: var(--hairline); }

/* ---- private feedback (localhost only) */
.private-feedback, .private-feedback-surface { display: none; }
.local-feedback .private-feedback, .local-feedback .private-feedback-surface { display: block; }
.private-feedback-surface { margin-top: 0.75rem; padding-top: 0.75rem; border-top: 1px dashed var(--hairline); }
.private-feedback { margin-top: 3rem; padding-top: 0.75rem; border-top: 3px solid var(--rule); }
.feedback-help, .save-status { color: var(--muted); font-size: 0.9rem; }
.save-status { min-height: 1.3em; margin: 0.25rem 0 0; }
.rating-buttons { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.5rem; }
.rating-buttons button, #save-post-feedback {
  font: 0.72rem/1 var(--mono);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  padding: 0.5rem 0.7rem;
  border: 1px solid var(--rule);
  background: transparent;
  color: var(--ink);
  cursor: pointer;
}
.rating-buttons button.active, #save-post-feedback:hover { background: var(--ink); color: var(--paper); }
textarea { width: 100%; min-height: 5rem; padding: 0.6rem; border: 1px solid var(--hairline); background: transparent; color: var(--ink); font: 0.95rem/1.45 var(--serif); resize: vertical; }

/* ---- footer */
.colophon {
  max-width: 72rem;
  margin: 3rem auto 0;
  padding: 1.25rem clamp(1.1rem, 4vw, 2.5rem) 2.5rem;
  border-top: 1px solid var(--rule);
  color: var(--muted);
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 0.5rem 2rem;
  line-height: 1.6;
}
.colophon p { margin: 0; }
.empty { color: var(--muted); padding: 2rem 0; }

@media (max-width: 760px) {
  body { font-size: 1.0625rem; }
  .issue-body p, .issue-body li { hyphens: auto; }
  .lead { grid-template-columns: 1fr; }
  .lead-contents { border-left: 0; padding-left: 0; border-top: 1px solid var(--rule); padding-top: 1rem; }
  .issue-list li { grid-template-columns: 1fr; gap: 0.15rem; }
  .issue-list li > span, .issue-list li > time { display: inline; }
  .dateline { justify-content: center; }
  .dateline span:last-child { flex-basis: 100%; }
}

@media print {
  .topbar nav, .private-feedback, .private-feedback-surface, .clip-media video { display: none !important; }
  body { background: #fff; color: #000; }
}
"""
