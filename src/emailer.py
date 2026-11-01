"""Email notifications through the Resend API."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).parent.parent
RESEND_API_URL = "https://api.resend.com/emails"


@dataclass
class EmailConfig:
    enabled: bool = False
    from_address: str = ""
    to_addresses: list[str] = field(default_factory=list)
    subject_prefix: str = "Bookmark Notes"
    resend_api_key_env: str = "RESEND_API_KEY"
    resend_api_key: str = ""


def _parse_recipients(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def load_dotenv(path: Path = PROJECT_ROOT / ".env"):
    """Load KEY=VALUE pairs from .env without overriding the real environment."""
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def load_email_config(
    config: dict,
    enabled_override: Optional[bool] = None,
    recipients_override: Optional[str] = None,
) -> EmailConfig:
    load_dotenv()
    section = (config or {}).get("email", {}) or {}
    email_config = EmailConfig(
        enabled=bool(section.get("enabled", False)),
        from_address=str(section.get("from_address", "")),
        to_addresses=_parse_recipients(section.get("to_addresses", [])),
        subject_prefix=str(section.get("subject_prefix", "Bookmark Notes")),
        resend_api_key_env=str(section.get("resend_api_key_env", "RESEND_API_KEY")),
    )
    if enabled_override is not None:
        email_config.enabled = enabled_override
    if recipients_override:
        email_config.to_addresses = _parse_recipients(recipients_override)
    email_config.resend_api_key = os.getenv(email_config.resend_api_key_env, "")
    return email_config


def resend_payload(config: EmailConfig, subject: str, text: str, html: str = "") -> dict:
    prefix = config.subject_prefix.strip()
    payload = {
        "from": config.from_address,
        "to": config.to_addresses,
        "subject": f"{prefix} - {subject}" if prefix else subject,
        "text": text,
    }
    if html:
        payload["html"] = html
    return payload


def send_email(config: EmailConfig, subject: str, text: str, html: str = ""):
    """Send one email. Raises on missing config or a non-2xx Resend response."""
    if not config.from_address:
        raise ValueError("email.from_address is not configured")
    if not config.to_addresses:
        raise ValueError("No email recipients configured")
    if not config.resend_api_key:
        raise ValueError(f"Missing Resend API key env var: {config.resend_api_key_env}")

    request = Request(
        RESEND_API_URL,
        data=json.dumps(resend_payload(config, subject, text, html)).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.resend_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "bookmark-digest/2.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            if not 200 <= response.status < 300:
                body = response.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Resend API failed with HTTP {response.status}: {body}")
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Resend API failed with HTTP {error.code}: {body}") from error
