# Bookmark Digest

Bookmark Digest turns my X bookmarks into a small public blog.

Once a week it fetches new bookmarks, gathers surrounding tweet context with
Bird, asks Codex to write a post, renders a static site, and sends a Resend
email notification when a new post is published.

This is a personal automation project for `bookmark.ruari.dev`, not a hosted
SaaS product. The goal is a low-maintenance pipeline that helps me revisit the
technical links, demos, repos, videos, and threads I save during the week.

## What It Does

- Fetches new X bookmarks with the `bird` CLI.
- Handles the first run as the latest 10 bookmarks.
- Tracks processed bookmarks in SQLite so later runs only publish new items.
- Handles the edge case where the previous top bookmark was unbookmarked by
  stopping at the first already-processed bookmark.
- Captures linked URLs, quoted tweets, media, and bounded author-thread context.
- Uses Codex CLI to write a weekly Markdown blog post.
- Renders a static HTML site with RSS.
- Caches tweet images/videos into generated static files so media works without
  hotlinking X video URLs.
- Adds localhost-only feedback controls for rating bookmarks and leaving notes.
- Sends a Resend email notification after a new post is created.

## Product Shape

```text
X bookmarks
  -> Bird context fetcher
  -> SQLite checkpoint + preference state
  -> Codex blog writer
  -> Markdown posts
  -> Static HTML/RSS/media files
  -> Resend notification email
```

The public site is static. Private feedback and ratings only appear when served
locally, and are stored in SQLite for the next writing prompt.

## Requirements

- Python 3.11+
- Node/npm for `npx @steipete/bird`
- X cookies in `config/cookies.json`
- Codex CLI logged in locally
- Resend API key in `.env`

## Setup

```bash
git clone https://github.com/ruari8/bookmark-digest.git
cd bookmark-digest

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config/cookies.example.json config/cookies.json
cp .env.example .env
```

Add X cookies to `config/cookies.json`:

```json
{
  "auth_token": "your_auth_token_value",
  "ct0": "your_ct0_value"
}
```

Add your Resend key to `.env`:

```txt
RESEND_API_KEY=re_your_resend_api_key
```

`config/cookies.json` and `.env` are gitignored.

## Email

Local testing uses Resend's onboarding sender:

```yaml
from_address: "onboarding@resend.dev"
```

That works before `ruari.dev` is verified in Resend. After the domain is
verified, switch the sender to:

```yaml
from_address: "Bookmark Notes <bookmarks@ruari.dev>"
```

Send a test email without creating a post:

```bash
.venv/bin/python run.py --mode test-email
```

See [docs/email-setup.md](docs/email-setup.md) for the Resend setup notes.

## Usage

Preview what would be published without generating a post:

```bash
.venv/bin/python run.py --mode blog --dry-run
```

Generate a new blog post if there are new bookmarks:

```bash
.venv/bin/python run.py --mode blog
```

Rebuild the static site from existing Markdown posts:

```bash
.venv/bin/python run.py --mode rebuild-site
```

Serve locally with private feedback controls enabled:

```bash
.venv/bin/python run.py --mode serve --port 8000
```

Then open:

```txt
http://127.0.0.1:8000
```

## Scheduling

Run every Sunday at 9am:

```cron
0 9 * * 0 cd /path/to/bookmark-digest && .venv/bin/python run.py --mode blog >> data/cron.log 2>&1
```

The repo also includes a macOS launchd example in
`config/com.bookmark-digest.plist`.

## Generated Files

The generated site is intentionally gitignored:

```txt
site/
content/posts/*.md
data/blog.sqlite3
```

Those files are runtime/deployment state, not source code. For deployment, copy
or preserve them on the server and regenerate `site/` there.

## Tests

```bash
.venv/bin/python -m unittest discover -v
```

Coverage includes:

- Blog state/checkpointing.
- Missing-checkpoint fallback when a prior top bookmark was unbookmarked.
- Resend email payloads.
- Static site rendering.
- Media caching.
- Codex CLI invocation.

## Security Notes

- Do not commit `.env`.
- Do not commit `config/cookies.json`.
- Do not commit `data/` or `site/`.
- The public site is static; private feedback controls only appear on localhost.
- Resend API keys can be revoked without affecting a Google account.

## Limitations

- X bookmark access is brittle and depends on cookies plus Bird behavior.
- Codex CLI must be authenticated on the machine running the weekly job.
- Blog quality depends on the bookmark context and saved feedback signals.
- Resend domain verification is needed before sending from `bookmarks@ruari.dev`.
