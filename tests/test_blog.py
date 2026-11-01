import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.blog import (
    BirdContextFetcher,
    BlogPost,
    BlogState,
    BookmarkContext,
    TweetContext,
    build_blog_prompt,
    clean_model_markdown,
    context_to_brief,
    parse_tweet_context,
)
from src.site import StaticSiteBuilder, markdown_to_html


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.headers = {"Content-Length": str(len(body))}
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1):
        if self.offset >= len(self.body):
            return b""
        if size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeBirdFetcher(BirdContextFetcher):
    def __init__(self, payload):
        self.payload = payload
        self.command = []
        self.cookies_path = Path("unused")

    def _load_cookie_env(self):
        return {}

    def _run(self, args):
        return self.payload


def tweet_payload(tweet_id: str, username: str = "demo") -> dict:
    return {
        "id": tweet_id,
        "author": {"username": username, "name": username.title()},
        "text": f"Tweet {tweet_id}",
        "_raw": {
            "legacy": {
                "full_text": f"Tweet {tweet_id}",
                "created_at": "Sun Jun 07 10:00:00 +0000 2026",
            }
        },
    }


class BlogPipelineTests(unittest.TestCase):
    def test_parse_tweet_context_extracts_urls_media_and_quote(self):
        tweet = parse_tweet_context({
            "id": "100",
            "author": {"username": "ruari", "name": "Ruari"},
            "_raw": {
                "legacy": {
                    "full_text": "Interesting project",
                    "created_at": "Sun Jun 07 10:00:00 +0000 2026",
                    "entities": {
                        "urls": [
                            {"expanded_url": "https://example.com/project"},
                            {"expanded_url": "https://x.com/linked/status/1"},
                        ]
                    },
                    "extended_entities": {
                        "media": [
                            {
                                "type": "photo",
                                "media_url_https": "https://pbs.twimg.com/media/photo.jpg",
                            },
                            {
                                "type": "video",
                                "media_url_https": "https://pbs.twimg.com/media/video.jpg",
                                "video_info": {
                                    "variants": [
                                        {"content_type": "video/mp4", "bitrate": 256000, "url": "https://video/low.mp4"},
                                        {"content_type": "video/mp4", "bitrate": 832000, "url": "https://video/high.mp4"},
                                    ]
                                },
                            },
                        ]
                    },
                },
                "quoted_status_result": {
                    "result": {
                        "rest_id": "99",
                        "core": {
                            "user_results": {
                                "result": {
                                    "legacy": {
                                        "screen_name": "quoted",
                                        "name": "Quoted Author",
                                    }
                                }
                            }
                        },
                        "legacy": {
                            "full_text": "Quoted context",
                            "created_at": "Sun Jun 07 09:00:00 +0000 2026",
                        },
                    }
                },
            },
        })

        self.assertEqual(tweet.id, "100")
        self.assertEqual(tweet.author_username, "ruari")
        self.assertEqual(tweet.urls, ["https://example.com/project"])
        self.assertEqual(tweet.linked_tweet_ids, ["1"])
        self.assertEqual(tweet.media[0].url, "https://pbs.twimg.com/media/photo.jpg")
        self.assertEqual(tweet.media[1].video_url, "https://video/high.mp4")
        self.assertIsNotNone(tweet.quote_tweet)
        self.assertEqual(tweet.quote_tweet.id, "99")
        self.assertEqual(tweet.quote_tweet.author_username, "quoted")

    def test_state_and_static_site_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = BlogState(root / "data" / "blog.sqlite3")
            builder = StaticSiteBuilder(
                content_dir=root / "content" / "posts",
                site_dir=root / "site",
                site_title="Bookmark Notes",
                site_description="Weekly notes.",
                base_url="http://localhost:8000",
                cache_remote_media=False,
            )
            post = BlogPost(
                title="Testing The Blog Pipeline",
                slug="2026-06-07-testing-the-blog-pipeline",
                summary="A short generated summary.",
                markdown="# Testing The Blog Pipeline\n\nThis is the body.",
                bookmark_ids=["100", "99"],
                latest_bookmark_id="100",
                cover_image="https://pbs.twimg.com/media/photo.jpg",
            )

            markdown_path, html_path = builder.write_post(post)
            state.save_post(post, markdown_path=markdown_path, html_path=html_path)

            self.assertTrue(markdown_path.exists())
            self.assertTrue(html_path.exists())
            self.assertTrue((root / "site" / "index.html").exists())
            self.assertEqual(state.get("last_seen_bookmark_id"), "100")
            self.assertEqual(state.processed_ids(), {"100", "99"})
            self.assertEqual(state.list_posts()[0]["slug"], post.slug)
            self.assertIn("Testing The Blog Pipeline", html_path.read_text())
            self.assertEqual(html_path.read_text().count("<h1>"), 1)
            self.assertIn("No. 1", (root / "site" / "index.html").read_text())

    def test_feedback_and_source_cards_render_from_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = BlogState(root / "data" / "blog.sqlite3")
            post = BlogPost(
                title="Preference Test",
                slug="preference-test",
                summary="Summary.",
                markdown="# Preference Test\n\nBody.",
                bookmark_ids=["100"],
                latest_bookmark_id="100",
            )
            tweet = TweetContext(
                id="100",
                text="A demo video worth watching.",
                author_username="demo",
                author_name="Demo",
                created_at="2026-06-07T10:00:00+00:00",
                tweet_url="https://x.com/demo/status/100",
                urls=["https://example.com/demo"],
            )
            state.save_bookmark_contexts(post.slug, [BookmarkContext(bookmark=tweet)])
            state.set_bookmark_feedback("100", 2, "More like this.")
            state.set_post_feedback(post.slug, "Lead with demos.")

            builder = StaticSiteBuilder(
                content_dir=root / "content" / "posts",
                site_dir=root / "site",
                site_title="Bookmark Notes",
                site_description="Weekly notes.",
                base_url="http://localhost:8000",
                state=state,
            )
            markdown_path, html_path = builder.write_post(post)
            state.save_post(post, markdown_path=markdown_path, html_path=html_path)
            html = html_path.read_text()

            self.assertIn("The clippings", html)
            self.assertIn("A demo video worth watching.", html)
            # Private notes must never be baked into public static HTML.
            self.assertNotIn("More like this.", html)
            self.assertNotIn("Lead with demos.", html)
            self.assertNotIn('class="active"', html)
            self.assertIn("Lead with demos.", state.preference_brief())
            self.assertIn("More like this.", state.preference_brief())

    def test_media_cache_rewrites_remote_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = StaticSiteBuilder(
                content_dir=root / "content" / "posts",
                site_dir=root / "site",
                site_title="Bookmark Notes",
                site_description="Weekly notes.",
                base_url="http://localhost:8000",
            )

            with patch("src.site.urlopen", return_value=FakeResponse(b"video")):
                local_url = builder._cache_media_url("https://video.twimg.com/path/demo.mp4?tag=1")

            self.assertTrue(local_url.startswith("/media/"))
            self.assertTrue((root / "site" / local_url.lstrip("/")).exists())

    def test_fetch_stops_at_processed_fallback_when_since_id_missing(self):
        fetcher = FakeBirdFetcher([
            tweet_payload("new-2"),
            tweet_payload("new-1"),
            tweet_payload("processed-2"),
            tweet_payload("old-unprocessed"),
        ])

        bookmarks = fetcher.fetch_bookmarks(
            max_count=10,
            since_id="removed-last-seen",
            scan_limit=50,
            include_author_chain=False,
            stop_ids={"processed-2", "processed-1"},
        )

        self.assertEqual([bundle.bookmark.id for bundle in bookmarks], ["new-2", "new-1"])


class LinkedTweetFetcher(FakeBirdFetcher):
    def __init__(self):
        super().__init__([])
        self.reads = []

    def read_tweet(self, tweet_id):
        self.reads.append(tweet_id)
        if tweet_id == "broken":
            raise RuntimeError("bird failed")
        return parse_tweet_context(tweet_payload(tweet_id, "linked"))


class WriterInputTests(unittest.TestCase):
    def test_attach_linked_tweets_skips_quotes_tolerates_failures_and_respects_limit(self):
        quote = TweetContext(id="q", text="", author_username="", author_name="", created_at="", tweet_url="")
        first = TweetContext(
            id="1", text="see", author_username="a", author_name="", created_at="", tweet_url="",
            quote_tweet=quote, linked_tweet_ids=["q", "broken", "200"],
        )
        second = TweetContext(
            id="2", text="also", author_username="b", author_name="", created_at="", tweet_url="",
            linked_tweet_ids=["300", "400"],
        )
        bundles = [BookmarkContext(bookmark=first), BookmarkContext(bookmark=second)]
        fetcher = LinkedTweetFetcher()

        fetcher.attach_linked_tweets(bundles, limit=3)

        self.assertEqual(fetcher.reads, ["broken", "200", "300"])
        self.assertEqual([t.id for t in bundles[0].linked_tweets], ["200"])
        self.assertEqual([t.id for t in bundles[1].linked_tweets], ["300"])
        brief = context_to_brief(bundles)
        self.assertIn("- Linked tweet:", brief)
        self.assertIn("Tweet 200", brief)

    def test_prompt_requires_coverage_and_mentions_search_only_when_enabled(self):
        with_search = build_blog_prompt("BRIEF", search=True)
        self.assertIn("live web search", with_search)
        self.assertIn("## Also noted", with_search)
        self.assertIn("Every bookmark in the brief must appear", with_search)
        self.assertTrue(with_search.rstrip().endswith("BRIEF"))
        self.assertNotIn("python -m src.blog_tools", with_search)
        self.assertIn("Web search is off", build_blog_prompt("BRIEF", search=False))

    def test_clean_model_markdown_drops_preamble_and_fences(self):
        self.assertEqual(clean_model_markdown("Sure, here it is.\n\n```markdown\n# Title\n\nBody\n```"), "# Title\n\nBody")
        self.assertEqual(clean_model_markdown("```\n# Title\nBody\n```"), "# Title\nBody")
        kept = "# Title\n\n```py\nx = 1\n```"
        self.assertEqual(clean_model_markdown(kept), kept)


class MarkdownSafetyTests(unittest.TestCase):
    def test_raw_html_and_unsafe_links_are_neutralised(self):
        rendered = markdown_to_html(
            "Hello <script>alert(1)</script>\n\n<iframe src='x'></iframe>\n\n"
            "[bad](javascript:alert(1)) [good](https://example.com) [rel](/posts/a/)"
        )
        self.assertNotIn("<script", rendered)
        self.assertNotIn("<iframe", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertIn('href="https://example.com"', rendered)
        self.assertIn('href="/posts/a/"', rendered)


if __name__ == "__main__":
    unittest.main()
