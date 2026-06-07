"""
Email delivery for bookmark digests.

Supports Resend API delivery and Gmail SMTP fallback.
"""

import base64
import json
import mimetypes
import os
import smtplib
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from .researcher import ResearchOutput


@dataclass
class EmailConfig:
    """Resolved email configuration."""

    enabled: bool = False
    provider: str = "resend"
    from_address: str = ""
    to_addresses: list[str] = field(default_factory=list)
    resend_api_key_env: str = "RESEND_API_KEY"
    resend_api_key: str = ""
    resend_api_url: str = "https://api.resend.com/emails"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username_env: str = "GMAIL_SMTP_USERNAME"
    smtp_password_env: str = "GMAIL_SMTP_APP_PASSWORD"
    attach_preferred: str = "pdf"       # pdf | markdown
    attach_fallback: str = "markdown"   # markdown | none
    send_on_empty: bool = True
    subject_prefix: str = "Bookmark Digest"
    smtp_username: str = ""
    smtp_password: str = ""


def _parse_recipients(value: object) -> list[str]:
    """Normalize recipient input into a clean list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def load_dotenv(path: Path = Path(".env")):
    """Load simple KEY=VALUE pairs from .env without adding a dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_email_config(
    config: dict,
    enabled_override: Optional[bool] = None,
    recipients_override: Optional[str] = None,
) -> EmailConfig:
    """Load and resolve email config from YAML + environment."""
    load_dotenv()
    email_section = config.get("email", {}) if config else {}

    email_config = EmailConfig(
        enabled=bool(email_section.get("enabled", False)),
        provider=str(email_section.get("provider", "resend")),
        from_address=str(email_section.get("from_address", "")),
        to_addresses=_parse_recipients(email_section.get("to_addresses", [])),
        resend_api_key_env=str(email_section.get("resend_api_key_env", "RESEND_API_KEY")),
        resend_api_url=str(email_section.get("resend_api_url", "https://api.resend.com/emails")),
        smtp_host=str(email_section.get("smtp_host", "smtp.gmail.com")),
        smtp_port=int(email_section.get("smtp_port", 587)),
        smtp_username_env=str(email_section.get("smtp_username_env", "GMAIL_SMTP_USERNAME")),
        smtp_password_env=str(email_section.get("smtp_password_env", "GMAIL_SMTP_APP_PASSWORD")),
        attach_preferred=str(email_section.get("attach_preferred", "pdf")),
        attach_fallback=str(email_section.get("attach_fallback", "markdown")),
        send_on_empty=bool(email_section.get("send_on_empty", True)),
        subject_prefix=str(email_section.get("subject_prefix", "Bookmark Digest")),
    )

    if enabled_override is not None:
        email_config.enabled = enabled_override

    if recipients_override:
        email_config.to_addresses = _parse_recipients(recipients_override)

    email_config.resend_api_key = os.getenv(email_config.resend_api_key_env, "")
    email_config.smtp_username = os.getenv(email_config.smtp_username_env, "")
    email_config.smtp_password = os.getenv(email_config.smtp_password_env, "")

    return email_config


class DigestEmailer:
    """Sends digest emails."""

    def __init__(self, config: EmailConfig):
        self.config = config

    def _validate(self):
        """Validate required config fields before sending."""
        if self.config.provider not in {"resend", "gmail_smtp"}:
            raise ValueError(f"Unsupported email provider: {self.config.provider}")
        if not self.config.from_address:
            raise ValueError("Email from_address is not configured")
        if not self.config.to_addresses:
            raise ValueError("No email recipients configured")
        if self.config.provider == "resend":
            if not self.config.resend_api_key:
                raise ValueError(f"Missing Resend API key env var: {self.config.resend_api_key_env}")
        if self.config.provider == "gmail_smtp":
            if not self.config.smtp_username:
                raise ValueError(f"Missing SMTP username env var: {self.config.smtp_username_env}")
            if not self.config.smtp_password:
                raise ValueError(f"Missing SMTP password env var: {self.config.smtp_password_env}")

    def _build_subject(self, suffix: str) -> str:
        prefix = self.config.subject_prefix.strip() or "Bookmark Digest"
        return f"{prefix} - {suffix}"

    @staticmethod
    def _build_base_message(from_address: str, to_addresses: list[str], subject: str, body: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = from_address
        message["To"] = ", ".join(to_addresses)
        message["Subject"] = subject
        message.set_content(body)
        return message

    @staticmethod
    def _attach_file(message: EmailMessage, path: Path):
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {path}")

        mime_type, _ = mimetypes.guess_type(path.name)
        if mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"

        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    def _send_message(self, message: EmailMessage):
        self._validate()

        if self.config.provider == "resend":
            self._send_resend_message(message)
            return

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.config.smtp_username, self.config.smtp_password)
            smtp.send_message(message)

    def _send_resend_message(self, message: EmailMessage):
        payload = self._resend_payload(message)
        data = json.dumps(payload).encode("utf-8")
        request = Request(
            self.config.resend_api_url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.config.resend_api_key}",
                "Content-Type": "application/json",
                "User-Agent": "bookmark-digest/1.0",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=30) as response:
                if response.status < 200 or response.status >= 300:
                    body = response.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"Resend API failed with HTTP {response.status}: {body}")
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Resend API failed with HTTP {error.code}: {body}") from error

    def _resend_payload(self, message: EmailMessage) -> dict:
        payload = {
            "from": message["From"],
            "to": self.config.to_addresses,
            "subject": message["Subject"],
            "text": self._message_text(message),
        }

        attachments = []
        for attachment in message.iter_attachments():
            filename = attachment.get_filename() or "attachment"
            attachments.append({
                "filename": filename,
                "content": base64.b64encode(attachment.get_payload(decode=True) or b"").decode("ascii"),
            })
        if attachments:
            payload["attachments"] = attachments

        return payload

    @staticmethod
    def _message_text(message: EmailMessage) -> str:
        if message.is_multipart():
            part = message.get_body(preferencelist=("plain",))
            if part:
                return part.get_content()
            return ""
        return message.get_content()

    def resolve_attachment_path(self, results: dict[str, Optional[Path]]) -> Optional[Path]:
        """
        Select attachment path based on preferred + fallback policy.
        """
        preferred_key = self.config.attach_preferred
        fallback_key = self.config.attach_fallback

        candidate_keys = []
        if preferred_key in ("pdf", "markdown"):
            candidate_keys.append(preferred_key)
        if fallback_key in ("pdf", "markdown") and fallback_key != preferred_key:
            candidate_keys.append(fallback_key)

        for key in candidate_keys:
            value = results.get(key)
            if not value:
                continue
            path = Path(value)
            if path.exists():
                return path

        return None

    def send_digest_email(
        self,
        research: "ResearchOutput",
        results: dict[str, Optional[Path]],
        bookmark_count: int,
    ) -> Path:
        """Send digest email with preferred attachment."""
        attachment = self.resolve_attachment_path(results)
        if attachment is None:
            raise FileNotFoundError("No digest attachment available (pdf/markdown)")

        date_str = datetime.now().strftime("%Y-%m-%d")
        subject = self._build_subject(f"Weekly Digest - {date_str}")
        body = "\n".join([
            f"Your weekly digest is ready ({date_str}).",
            "",
            f"Bookmarks processed: {bookmark_count}",
            f"Topics generated: {len(research.topics)}",
            f"Attachment: {attachment.name}",
            "",
            "Output paths:",
            f"- Markdown: {results.get('markdown') or 'not generated'}",
            f"- PDF: {results.get('pdf') or 'not generated'}",
        ])

        message = self._build_base_message(
            from_address=self.config.from_address,
            to_addresses=self.config.to_addresses,
            subject=subject,
            body=body,
        )
        self._attach_file(message, attachment)
        self._send_message(message)
        return attachment

    def send_empty_status_email(self, reason: str = "No new bookmarks", details: Optional[str] = None):
        """Send status-only email (used for empty runs and failure alerts)."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        subject = self._build_subject(f"{reason} - {date_str}")
        lines = [
            f"Bookmark Digest status update ({date_str}).",
            "",
            reason,
        ]
        if details:
            lines.extend(["", f"Details: {details}"])

        message = self._build_base_message(
            from_address=self.config.from_address,
            to_addresses=self.config.to_addresses,
            subject=subject,
            body="\n".join(lines),
        )
        self._send_message(message)


def send_digest_email(
    email_config: EmailConfig,
    research: "ResearchOutput",
    results: dict[str, Optional[Path]],
    bookmark_count: int,
) -> Path:
    """Convenience wrapper for digest email sending."""
    return DigestEmailer(email_config).send_digest_email(
        research=research,
        results=results,
        bookmark_count=bookmark_count,
    )


def send_empty_status_email(
    email_config: EmailConfig,
    reason: str = "No new bookmarks",
    details: Optional[str] = None,
):
    """Convenience wrapper for status emails."""
    DigestEmailer(email_config).send_empty_status_email(reason=reason, details=details)
