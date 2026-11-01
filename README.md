# Bookmark Notes

Turns my X bookmarks into a small weekly paper at `bookmark.ruari.dev`.

Every Sunday it fetches the week's new bookmarks with Bird, has Codex research
and write an issue (with live web search for deep dives), renders a static site
with RSS, and emails me a link. If the run fails, it emails me the error instead.

```text
X bookmarks -> Bird (bookmarks, author threads, linked posts)
            -> SQLite checkpoint + my ratings/notes
            -> Codex CLI writer (web search on, no shell)
            -> Markdown issue -> static HTML + RSS + cached media
            -> Resend email (new issue, or failure alert)
```

## Requirements

- Python 3.11+
- Node/npm for `npx @steipete/bird`
- X cookies in `config/cookies.json` (`auth_token`, `ct0`)
- Codex CLI logged in (`codex login`) with access to the model in `config/config.yaml`
- `RESEND_API_KEY` in `.env`

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/cookies.example.json config/cookies.json   # add auth_token and ct0
cp .env.example .env                                 # add RESEND_API_KEY
```

`config/cookies.json` and `.env` are gitignored.

## Usage

```bash
.venv/bin/python run.py                     # weekly run: fetch, write, render, email
.venv/bin/python run.py --dry-run           # list what would go into the next issue
.venv/bin/python run.py --mode rebuild-site # re-render every issue from content/posts
.venv/bin/python run.py --mode serve        # local preview with private rating controls
.venv/bin/python run.py --mode test-email
```

Cron (Sunday 09:00):

```cron
0 9 * * 0 cd /path/to/bookmark-digest && .venv/bin/python run.py >> data/cron.log 2>&1
```

## The writer

`ai.model` in `config/config.yaml` picks the Codex model. When a model is
retired, the run fails with `model_not_found` and you get an alert email; run
`codex debug models` to see what the account can use now.

Codex runs with `-c web_search="live"` and a read-only sandbox. (`codex --search`
only affects the interactive TUI; `codex exec` defaults to cached search.) It cannot
run shell commands, so all X context (author threads, quoted posts, and linked
x.com posts) is fetched by the pipeline before Codex starts.

## Private feedback

`--mode serve` shows rating buttons and note fields on each bookmark. Ratings
and notes are stored in `data/blog.sqlite3` and fed into the next issue's prompt
so it leans toward what I found interesting. They are never written into the
static HTML; the local page loads them from `/api/feedback`.

## Generated files

`site/`, `content/posts/*.md` and `data/` are runtime state on the server and
are gitignored.

## Tests

```bash
.venv/bin/python -m unittest discover -v
```
