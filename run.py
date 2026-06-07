#!/usr/bin/env python3
"""
Bookmark Digest - Main Entry Point

Automated pipeline that reads X bookmarks, researches topics, and generates
a learning document (with optional podcast conversion).
"""

import json
import sys
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import click
import yaml
from rich.console import Console
from rich.panel import Panel

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from src.scraper import get_new_bookmarks, Tweet
from src.parser import parse_bookmarks
from src.researcher import research_bookmarks
from src.generator import generate_document
from src.audio import generate_audio, print_notebooklm_instructions
from src.emailer import load_email_config, send_digest_email, send_empty_status_email
from src.blog import (
    BlogState,
    BlogPost,
    BirdContextFetcher,
    CodexBlogWriter,
    StaticSiteBuilder,
    context_to_brief,
    resolve_blog_paths,
    send_blog_notification,
)

console = Console()

# Paths
PROJECT_ROOT = Path(__file__).parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"


def load_config() -> dict:
    """Load configuration from YAML file."""
    config_path = CONFIG_DIR / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def load_processed_ids() -> list[str]:
    """Load list of already-processed bookmark IDs."""
    processed_path = DATA_DIR / "processed.json"
    if processed_path.exists():
        with open(processed_path) as f:
            data = json.load(f)
            return data.get("processed_ids", [])
    return []


def save_processed_ids(ids: list[str], existing_ids: list[str]):
    """Save updated list of processed bookmark IDs."""
    # Merge new IDs with existing, keep most recent at front
    all_ids = ids + [i for i in existing_ids if i not in ids]
    # Keep only last 1000 IDs to prevent unbounded growth
    all_ids = all_ids[:1000]

    processed_path = DATA_DIR / "processed.json"
    processed_path.parent.mkdir(parents=True, exist_ok=True)

    with open(processed_path, "w") as f:
        json.dump({
            "processed_ids": all_ids,
            "last_run": datetime.now().isoformat(),
        }, f, indent=2)


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
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

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


def _new_site_builder(config: dict, state: Optional[BlogState] = None) -> tuple[dict, StaticSiteBuilder]:
    """Build a StaticSiteBuilder from resolved blog config."""
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


def rebuild_blog_site(config: dict):
    """Render the static blog from existing post Markdown files."""
    paths, builder = _new_site_builder(config)
    index_path = builder.render_all()
    console.print(f"[green]Rendered blog site[/green]: {index_path}")
    console.print(f"Site directory: {paths['site_dir']}")


def serve_blog_site(config: dict, port: int):
    """Serve the generated static blog locally."""
    paths = resolve_blog_paths(config)
    state = BlogState(paths["state_path"])
    _, builder = _new_site_builder(config, state=state)
    site_dir = paths["site_dir"]
    if not (site_dir / "index.html").exists():
        builder.render_all()

    handler = partial(BlogSiteRequestHandler, directory=str(site_dir), state_path=paths["state_path"])
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    console.print(f"[green]Serving blog locally[/green]: http://127.0.0.1:{port}")
    console.print("Press Ctrl+C to stop.")
    server.serve_forever()


def send_test_blog_email(config: dict, email_config):
    """Send a notification-only test email without generating a post."""
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
        send_blog_notification(
            email_config=email_config,
            post=post,
            post_url=f"{str(paths['base_url']).rstrip('/')}/",
        )
    except Exception as error:
        raise click.ClickException(f"Email test failed: {error}") from error
    recipients = ", ".join(email_config.to_addresses)
    console.print(f"[green]Test email sent[/green] to {recipients}")


def run_blog_pipeline(
    config: dict,
    max_bookmarks: int,
    dry_run: bool,
    email_config,
):
    """Fetch new bookmarks, generate a static blog post, and persist the checkpoint."""
    paths = resolve_blog_paths(config)
    state = BlogState(paths["state_path"])
    paths, builder = _new_site_builder(config, state=state)
    last_seen_id = state.get("last_seen_bookmark_id")

    first_run_count = int(paths["first_run_bookmarks"])
    scan_limit = int(paths["scan_limit"])
    requested_count = max_bookmarks if max_bookmarks > 0 else (scan_limit if last_seen_id else first_run_count)

    console.print("\n[bold]Step 1: Fetching bookmark context[/bold]")
    fetcher = BirdContextFetcher(
        cookies_path=CONFIG_DIR / "cookies.json",
        command=str(paths["bird_command"]),
    )
    bookmarks = fetcher.fetch_bookmarks(
        max_count=requested_count if not last_seen_id else first_run_count,
        since_id=last_seen_id,
        scan_limit=requested_count,
        include_author_chain=bool(paths["include_author_chain"]),
        stop_ids=state.processed_ids(),
    )

    processed_ids = state.processed_ids()
    bookmarks = [bundle for bundle in bookmarks if bundle.bookmark.id not in processed_ids]

    if not bookmarks:
        console.print("[yellow]No new bookmarks to publish.[/yellow]")
        return

    console.print(f"[green]Found {len(bookmarks)} new bookmarks[/green]")
    if dry_run:
        for bundle in bookmarks:
            tweet = bundle.bookmark
            console.print(f"  - @{tweet.author_username}: {tweet.text[:90]}...")
        return

    console.print("\n[bold]Step 2: Writing blog post with Codex[/bold]")
    ai_config = config.get("ai", {})
    writer = CodexBlogWriter(
        model=ai_config.get("model", "gpt-5.5"),
        reasoning_effort=ai_config.get("codex_cli_reasoning_effort", "low"),
        search=ai_config.get("codex_cli_search", True),
        sandbox=ai_config.get("codex_cli_sandbox", "read-only"),
        ignore_user_config=ai_config.get("codex_cli_ignore_user_config", True),
        automatic_cover_image=bool(paths["automatic_cover_image"]),
    )
    post = writer.write_post(
        context_to_brief(bookmarks, preference_brief=state.preference_brief()),
        bookmarks,
    )

    console.print("\n[bold]Step 3: Rendering static blog[/bold]")
    state.save_bookmark_contexts(post.slug, bookmarks)
    markdown_path, html_path = builder.write_post(post)
    state.save_post(post, markdown_path=markdown_path, html_path=html_path)
    post_url = f"{str(paths['base_url']).rstrip('/')}/posts/{post.slug}/"

    if email_config.enabled:
        console.print("\n[bold]Step 4: Sending blog notification email[/bold]")
        send_blog_notification(email_config, post, post_url)
        console.print("[green]Notification email sent[/green]")

    console.print("\n" + "-" * 50)
    console.print("[bold green]Blog post generated[/bold green]")
    console.print(f"  Title: {post.title}")
    console.print(f"  Markdown: {markdown_path}")
    console.print(f"  HTML: {html_path}")
    console.print(f"  URL: {post_url}")


@click.command()
@click.option(
    "--mode",
    type=click.Choice(["digest", "blog", "serve", "rebuild-site", "test-email"]),
    default="digest",
    show_default=True,
    help="Run the legacy digest, generate a blog post, serve the site, or rebuild static files.",
)
@click.option(
    "--since",
    type=str,
    help="Only process bookmarks since this date (YYYY-MM-DD)",
)
@click.option(
    "--max-bookmarks",
    type=int,
    default=0,
    help="Maximum number of bookmarks to process (0 = no limit)",
)
@click.option(
    "--output-format",
    type=click.Choice(["markdown", "pdf", "both"]),
    default=None,
    help="Output format for the digest (defaults to config output.format)",
)
@click.option(
    "--tts/--no-tts",
    default=False,
    help="Generate audio using TTS",
)
@click.option(
    "--notebooklm/--no-notebooklm",
    default=False,
    help="Print NotebookLM instructions for podcast generation",
)
@click.option(
    "--force",
    is_flag=True,
    help="Process all bookmarks, ignoring processed IDs",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Fetch bookmarks but don't generate documents",
)
@click.option(
    "--email/--no-email",
    "email_enabled",
    default=None,
    help="Send digest email (overrides config)",
)
@click.option(
    "--email-to",
    type=str,
    default=None,
    help="Override email recipients (comma-separated)",
)
@click.option(
    "--port",
    type=int,
    default=8000,
    show_default=True,
    help="Local port for --mode serve.",
)
def main(
    mode: str,
    since: Optional[str],
    max_bookmarks: int,
    output_format: Optional[str],
    tts: bool,
    notebooklm: bool,
    force: bool,
    dry_run: bool,
    email_enabled: Optional[bool],
    email_to: Optional[str],
    port: int,
):
    """
    Generate a research digest from your X bookmarks.

    This tool fetches your recent X bookmarks, extracts linked content,
    uses AI to cluster topics and generate research, and outputs a
    formatted document.

    \b
    Setup:
    1. Copy config/cookies.example.json to config/cookies.json
    2. Add your X session cookies (auth_token, ct0) from browser DevTools
    3. Run: python run.py

    \b
    Example usage:
        python run.py                        # Process new bookmarks
        python run.py --force                # Reprocess all bookmarks
        python run.py --output-format pdf    # Generate PDF
        python run.py --tts                  # Generate audio
        python run.py --email                # Send digest email
    """
    console.print(Panel.fit(
        "[bold blue]Bookmark Digest[/bold blue]\n"
        "Personal AI Research Pipeline",
        border_style="blue",
    ))

    # Load config
    config = load_config()

    if mode == "serve":
        serve_blog_site(config, port=port)
        return

    if mode == "rebuild-site":
        rebuild_blog_site(config)
        return

    email_config = load_email_config(
        config,
        enabled_override=email_enabled,
        recipients_override=email_to,
    )

    if mode == "blog":
        run_blog_pipeline(
            config=config,
            max_bookmarks=max_bookmarks,
            dry_run=dry_run,
            email_config=email_config,
        )
        return

    if mode == "test-email":
        send_test_blog_email(config=config, email_config=email_config)
        return

    output_config = config.get("output", {})
    if output_format is None:
        output_format = output_config.get("format") or "markdown"
    if output_format not in {"markdown", "pdf", "both"}:
        raise click.ClickException(
            "Invalid output.format in config/config.yaml. Use markdown, pdf, or both."
        )

    # Load processed IDs
    processed_ids = [] if force else load_processed_ids()

    # Get scraper settings
    scraper_config = config.get("scraper", {})
    if max_bookmarks == 0:
        max_bookmarks = scraper_config.get("max_bookmarks", 0)
    days_limit = scraper_config.get("days_limit", 7)

    # Override days_limit if --since is provided
    if since:
        from datetime import datetime as dt
        since_date = dt.strptime(since, "%Y-%m-%d")
        days_limit = (dt.now() - since_date).days + 1

    # Step 1: Fetch bookmarks
    console.print("\n[bold]Step 1: Fetching bookmarks[/bold]")
    try:
        tweets = get_new_bookmarks(
            processed_ids=processed_ids,
            max_count=max_bookmarks,
            days_limit=days_limit,
        )
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        console.print("\n[yellow]Setup instructions:[/yellow]")
        console.print("1. Open x.com in your browser and log in")
        console.print("2. Open DevTools (F12) → Network tab")
        console.print("3. Refresh the page and click any request to x.com")
        console.print("4. Find 'Cookie' in Request Headers")
        console.print("5. Copy the values for 'auth_token' and 'ct0'")
        console.print(f"6. Add them to {CONFIG_DIR / 'cookies.json'}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Failed to fetch bookmarks: {e}[/red]")
        sys.exit(1)

    if not tweets:
        console.print("[yellow]No new bookmarks to process.[/yellow]")
        if email_config.enabled and email_config.send_on_empty:
            console.print("[bold]Sending status email (no new bookmarks)[/bold]")
            try:
                send_empty_status_email(
                    email_config=email_config,
                    reason="No new bookmarks",
                )
                console.print("[green]Status email sent[/green]")
            except Exception as e:
                console.print(f"[red]Failed to send status email: {e}[/red]")
                sys.exit(1)
        sys.exit(0)

    console.print(f"[green]Found {len(tweets)} new bookmarks[/green]")

    if dry_run:
        console.print("\n[yellow]Dry run - skipping document generation[/yellow]")
        for tweet in tweets[:10]:
            console.print(f"  - @{tweet.author_username}: {tweet.text[:60]}...")
        if len(tweets) > 10:
            console.print(f"  ... and {len(tweets) - 10} more")
        sys.exit(0)

    # Step 2: Parse content
    console.print("\n[bold]Step 2: Parsing linked content[/bold]")
    parser_config = config.get("parser", {})
    parsed = parse_bookmarks(
        tweets,
        timeout=parser_config.get("fetch_timeout", 30),
        max_articles=parser_config.get("max_articles", 50),
    )

    # Step 3: AI Research
    console.print("\n[bold]Step 3: AI Research & Synthesis[/bold]")
    ai_config = config.get("ai", {})
    research = research_bookmarks(
        parsed,
        provider=ai_config.get("provider", "codex_cli"),
        model=ai_config.get("model", "gpt-5.5"),
        claude_cli_tools=ai_config.get("claude_cli_tools", None),
        claude_cli_permission_mode=ai_config.get("claude_cli_permission_mode", "bypassPermissions"),
        claude_cli_dangerously_skip_permissions=ai_config.get("claude_cli_dangerously_skip_permissions", False),
        codex_cli_search=ai_config.get("codex_cli_search", True),
        codex_cli_sandbox=ai_config.get("codex_cli_sandbox", "read-only"),
        codex_cli_profile=ai_config.get("codex_cli_profile", None),
        codex_cli_ignore_user_config=ai_config.get("codex_cli_ignore_user_config", True),
        codex_cli_reasoning_effort=ai_config.get("codex_cli_reasoning_effort", None),
    )

    # Step 4: Generate document
    console.print("\n[bold]Step 4: Generating document[/bold]")
    output_dir_value = Path(output_config.get("directory") or "../")
    if output_dir_value.is_absolute():
        output_dir = output_dir_value
    else:
        output_dir = PROJECT_ROOT / output_dir_value
    output_dir = output_dir.resolve()

    results = generate_document(
        research,
        output_dir=output_dir,
        format=output_format,
        filename_template=output_config.get("filename_template") or "weekly-digest-{date}.md",
    )

    # Step 5: Audio (optional)
    if tts:
        console.print("\n[bold]Step 5: Generating audio[/bold]")
        audio_config = config.get("audio", {})
        generate_audio(
            research,
            provider=audio_config.get("provider", "openai"),
            voice=audio_config.get("voice", "alloy"),
            output_dir=output_dir,
        )

    # NotebookLM instructions
    if notebooklm and results.get("markdown"):
        print_notebooklm_instructions(results["markdown"])

    # Step 6: Email delivery (optional)
    if email_config.enabled:
        console.print("\n[bold]Step 6: Sending email[/bold]")
        try:
            attachment = send_digest_email(
                email_config=email_config,
                research=research,
                results=results,
                bookmark_count=len(tweets),
            )
            recipients = ", ".join(email_config.to_addresses)
            console.print(f"[green]Email sent to {recipients} ({attachment.name})[/green]")
        except FileNotFoundError as e:
            console.print(f"[red]Email attachment error: {e}[/red]")
            try:
                send_empty_status_email(
                    email_config=email_config,
                    reason="Digest email failed",
                    details=str(e),
                )
            except Exception as status_error:
                console.print(f"[yellow]Also failed to send status email: {status_error}[/yellow]")
            sys.exit(1)
        except Exception as e:
            console.print(f"[red]Email delivery failed: {e}[/red]")
            sys.exit(1)

    # Save processed IDs
    new_ids = [t.id for t in tweets]
    save_processed_ids(new_ids, processed_ids)

    # Summary
    console.print("\n" + "─" * 50)
    console.print("[bold green]Done![/bold green]")
    if results.get("markdown"):
        console.print(f"  Markdown: {results['markdown']}")
    if results.get("pdf"):
        console.print(f"  PDF: {results['pdf']}")
    console.print(f"  Processed {len(tweets)} bookmarks across {len(research.topics)} topics")


if __name__ == "__main__":
    main()
