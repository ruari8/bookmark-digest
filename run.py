#!/usr/bin/env python3
"""
Bookmark Notes: turn this week's X bookmarks into a static blog issue.

    python run.py                    # weekly run: fetch, write, render, email
    python run.py --dry-run          # list what would go into the next issue
    python run.py --mode rebuild-site
    python run.py --mode serve       # local preview with private feedback controls
    python run.py --mode test-email
"""

import json
import sys
import traceback
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import click
import yaml
from rich.console import Console

from src.blog import (
    BirdContextFetcher,
    BlogPost,
    BlogState,
    CodexBlogWriter,
    context_to_brief,
    resolve_blog_paths,
    send_blog_notification,
    send_failure_alert,
)
from src.codex import CodexCLI
from src.emailer import EmailConfig, load_email_config
from src.site import StaticSiteBuilder

console = Console()

PROJECT_ROOT = Path(__file__).parent
CONFIG_DIR = PROJECT_ROOT / "config"


def load_config() -> dict:
    config_path = CONFIG_DIR / "config.yaml"
    if config_path.exists():
        return yaml.safe_load(config_path.read_text()) or {}
    return {}


def codex_from_config(config: dict) -> CodexCLI:
    ai = config.get("ai", {}) or {}
    model = ai.get("model")
    if not model:
        raise click.ClickException("Set ai.model in config/config.yaml")
    return CodexCLI(
        model=str(model),
        reasoning_effort=str(ai.get("reasoning_effort", "medium")),
        search=bool(ai.get("web_search", True)),
        sandbox=str(ai.get("sandbox", "read-only")),
        ignore_user_config=bool(ai.get("ignore_user_config", True)),
        timeout_seconds=int(ai.get("timeout_seconds", 1800)),
    )


def new_site_builder(config: dict, state: Optional[BlogState] = None) -> tuple[dict, StaticSiteBuilder]:
    paths = resolve_blog_paths(config)
    if state is None and Path(paths["state_path"]).exists():
        state = BlogState(paths["state_path"])
    builder = StaticSiteBuilder(
        content_dir=paths["content_dir"],
        site_dir=paths["site_dir"],
        site_title=paths["site_title"],
        site_description=paths["site_description"],
        base_url=paths["base_url"],
        state=state,
        cache_remote_media=bool(paths["cache_remote_media"]),
        max_cached_media_bytes=int(paths["max_cached_media_mb"]) * 1024 * 1024,
    )
    return paths, builder


class BlogSiteRequestHandler(SimpleHTTPRequestHandler):
    """Static file handler with localhost-only feedback endpoints."""

    def __init__(self, *args, state_path: Path, **kwargs):
        self.state_path = state_path
        super().__init__(*args, **kwargs)

    def _send_json(self, status: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
            raise ValueError("Request body too large")
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/feedback":
            slug = parse_qs(parsed.query).get("slug", [""])[0]
            if not slug:
                self._send_json(400, {"error": "Missing slug"})
                return
            self._send_json(200, BlogState(self.state_path).feedback_payload(slug))
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            state = BlogState(self.state_path)
            if parsed.path == "/api/bookmark-feedback":
                state.set_bookmark_feedback(
                    bookmark_id=str(payload.get("bookmark_id") or ""),
                    rating=int(payload.get("rating", 0)),
                    note=str(payload.get("note") or ""),
                )
                self._send_json(200, {"ok": True})
                return
            if parsed.path == "/api/post-feedback":
                state.set_post_feedback(
                    post_slug=str(payload.get("slug") or ""),
                    feedback=str(payload.get("feedback") or ""),
                )
                self._send_json(200, {"ok": True})
                return
        except Exception as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(404, {"error": "Not found"})


def rebuild_blog_site(config: dict):
    paths, builder = new_site_builder(config)
    index_path = builder.render_all()
    console.print(f"[green]Rendered blog site[/green]: {index_path}")


def serve_blog_site(config: dict, port: int):
    paths = resolve_blog_paths(config)
    state = BlogState(paths["state_path"])
    _, builder = new_site_builder(config, state=state)
    if not (paths["site_dir"] / "index.html").exists():
        builder.render_all()
    handler = partial(BlogSiteRequestHandler, directory=str(paths["site_dir"]), state_path=paths["state_path"])
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    console.print(f"[green]Serving blog locally[/green]: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    server.serve_forever()


def send_test_blog_email(config: dict, email_config: EmailConfig):
    paths = resolve_blog_paths(config)
    post = BlogPost(
        title="Bookmark Notes email test",
        slug="email-test",
        summary="This is a test email from the Bookmark Notes blog pipeline.",
        markdown="",
        bookmark_ids=[],
        latest_bookmark_id="",
    )
    try:
        send_blog_notification(email_config, post, f"{str(paths['base_url']).rstrip('/')}/")
    except Exception as error:
        raise click.ClickException(f"Email test failed: {error}") from error
    console.print(f"[green]Test email sent[/green] to {', '.join(email_config.to_addresses)}")


def run_blog_pipeline(config: dict, max_bookmarks: int, dry_run: bool, email_config: EmailConfig):
    """Fetch new bookmarks, write and render an issue, then checkpoint and notify."""
    paths = resolve_blog_paths(config)
    state = BlogState(paths["state_path"])
    paths, builder = new_site_builder(config, state=state)
    last_seen_id = state.get("last_seen_bookmark_id")
    first_run_count = int(paths["first_run_bookmarks"])
    scan_limit = max_bookmarks if max_bookmarks > 0 else int(paths["scan_limit"])

    console.print("\n[bold]Step 1: Fetching bookmark context[/bold]")
    fetcher = BirdContextFetcher(cookies_path=CONFIG_DIR / "cookies.json", command=str(paths["bird_command"]))
    processed_ids = state.processed_ids()
    bookmarks = fetcher.fetch_bookmarks(
        max_count=max_bookmarks if (max_bookmarks > 0 and not last_seen_id) else first_run_count,
        since_id=last_seen_id,
        scan_limit=scan_limit,
        include_author_chain=bool(paths["include_author_chain"]),
        stop_ids=processed_ids,
    )
    bookmarks = [bundle for bundle in bookmarks if bundle.bookmark.id not in processed_ids]
    if not bookmarks:
        console.print("[yellow]No new bookmarks to publish.[/yellow]")
        return

    console.print(f"[green]Found {len(bookmarks)} new bookmarks[/green]")
    if dry_run:
        for bundle in bookmarks:
            console.print(f"  - @{bundle.bookmark.author_username}: {bundle.bookmark.text[:90]}")
        return

    linked = fetcher.attach_linked_tweets(bookmarks, limit=int(paths["max_linked_tweets"]))
    if linked:
        console.print(f"Fetched {linked} linked tweets for extra context")

    codex = codex_from_config(config)
    console.print(f"\n[bold]Step 2: Writing the issue with {codex.model}[/bold] (web search {'on' if codex.search else 'off'})")
    writer = CodexBlogWriter(codex, automatic_cover_image=bool(paths["automatic_cover_image"]))
    post = writer.write_post(context_to_brief(bookmarks, preference_brief=state.preference_brief()), bookmarks)

    console.print("\n[bold]Step 3: Rendering static site[/bold]")
    state.save_bookmark_contexts(post.slug, bookmarks)
    markdown_path, html_path = builder.write_post(post)
    state.save_post(post, markdown_path=markdown_path, html_path=html_path)
    post_url = f"{str(paths['base_url']).rstrip('/')}/posts/{post.slug}/"

    if email_config.enabled:
        console.print("\n[bold]Step 4: Sending notification email[/bold]")
        try:
            send_blog_notification(email_config, post, post_url)
        except Exception as error:
            raise RuntimeError(f"Issue published at {post_url}, but the notification email failed: {error}") from error

    console.print(f"\n[bold green]Published[/bold green] {post.title}\n  {post_url}")


@click.command()
@click.option(
    "--mode",
    type=click.Choice(["blog", "serve", "rebuild-site", "test-email"]),
    default="blog",
    show_default=True,
)
@click.option("--max-bookmarks", type=int, default=0, help="Override blog.scan_limit for this run.")
@click.option("--dry-run", is_flag=True, help="List new bookmarks without writing or publishing.")
@click.option("--email/--no-email", "email_enabled", default=None, help="Override email.enabled.")
@click.option("--email-to", type=str, default=None, help="Override recipients (comma-separated).")
@click.option("--port", type=int, default=8000, show_default=True, help="Port for --mode serve.")
def main(mode: str, max_bookmarks: int, dry_run: bool, email_enabled: Optional[bool], email_to: Optional[str], port: int):
    """Turn this week's X bookmarks into a Bookmark Notes issue."""
    config = load_config()

    if mode == "serve":
        serve_blog_site(config, port=port)
        return
    if mode == "rebuild-site":
        rebuild_blog_site(config)
        return

    email_config = load_email_config(config, enabled_override=email_enabled, recipients_override=email_to)
    if mode == "test-email":
        send_test_blog_email(config, email_config)
        return

    try:
        run_blog_pipeline(config, max_bookmarks=max_bookmarks, dry_run=dry_run, email_config=email_config)
    except Exception as error:
        traceback.print_exc()
        if email_config.enabled and not dry_run:
            try:
                send_failure_alert(email_config, error, log_hint="Details: data/cron.log on the VPS.")
                console.print("[yellow]Sent failure alert email[/yellow]")
            except Exception as alert_error:
                console.print(f"[red]Could not send failure alert: {alert_error}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
