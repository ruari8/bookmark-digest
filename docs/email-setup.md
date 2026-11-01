# Email Setup

Bookmark Notes sends blog notifications through Resend.

## What To Do Now

You are on Resend onboarding. That is fine.

For local testing, use Resend's onboarding sender:

```txt
onboarding@resend.dev
```

That means you do not need Cloudflare/DNS yet. Domain verification is only needed later if you want the email to come from:

```txt
bookmarks@ruari.dev
```

## API Key

Put the key in `.env` at the repo root:

```sh
cp .env.example .env
```

Then edit `.env`:

```txt
RESEND_API_KEY=re_your_actual_resend_api_key
```

`.env` is gitignored. Do not put the API key in `config/config.yaml`.

## Test

Send a test email without generating a blog post:

```sh
.venv/bin/python run.py --mode test-email
```

## Weekly Run

The normal weekly command is:

```sh
.venv/bin/python run.py
```

The schedule should run that at 9am on Sunday.

## Later: Use ruari.dev As Sender

When you want the nicer sender address:

1. Add `ruari.dev` as a domain in Resend.
2. Add the DNS records Resend gives you in Cloudflare.
3. Wait until Resend marks the domain verified.
4. Change `config/config.yaml`:

```yaml
from_address: "Bookmark Notes <bookmarks@ruari.dev>"
```
