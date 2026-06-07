import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.blog import (
    BirdContextFetcher,
    BlogPost,
    BlogState,
    BookmarkContext,
    StaticSiteBuilder,
    TweetContext,
    parse_tweet_context,
)


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
                            {"expanded_url": "https://x.com/ignored/status/1"},
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

            self.assertIn("Bookmarked sources", html)
            self.assertIn("A demo video worth watching.", html)
            self.assertIn("More like this.", html)
            self.assertIn("Lead with demos.", state.preference_brief())

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

            with patch("src.blog.urlopen", return_value=FakeResponse(b"video")):
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


if __name__ == "__main__":
    unittest.main()
