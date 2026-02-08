#!/usr/bin/env python3
"""
Bookmark Digest - Main Entry Point

Automated pipeline that reads X bookmarks, researches topics, and generates
a learning document (with optional podcast conversion).
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

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


@click.command()
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
    default="markdown",
    help="Output format for the digest",
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
def main(
    since: Optional[str],
    max_bookmarks: int,
    output_format: str,
    tts: bool,
    notebooklm: bool,
    force: bool,
    dry_run: bool,
    email_enabled: Optional[bool],
    email_to: Optional[str],
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
    email_config = load_email_config(
        config,
        enabled_override=email_enabled,
        recipients_override=email_to,
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
    )

    # Step 3: AI Research
    console.print("\n[bold]Step 3: AI Research & Synthesis[/bold]")
    ai_config = config.get("ai", {})
    research = research_bookmarks(
        parsed,
        provider=ai_config.get("provider", "anthropic"),
        model=ai_config.get("model", "claude-sonnet-4-20250514"),
        claude_cli_tools=ai_config.get("claude_cli_tools", None),
        claude_cli_permission_mode=ai_config.get("claude_cli_permission_mode", "bypassPermissions"),
        claude_cli_dangerously_skip_permissions=ai_config.get("claude_cli_dangerously_skip_permissions", True),
    )

    # Step 4: Generate document
    console.print("\n[bold]Step 4: Generating document[/bold]")
    output_config = config.get("output", {})
    output_dir = Path(output_config.get("directory", "../")).resolve()
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_config.get("directory", "..")

    results = generate_document(
        research,
        output_dir=output_dir,
        format=output_format,
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
