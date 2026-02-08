"""
Email delivery for bookmark digests.

Supports Gmail SMTP delivery with attachment preference/fallback.
"""

import mimetypes
import os
import smtplib
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .researcher import ResearchOutput


@dataclass
class EmailConfig:
    """Resolved email configuration."""

    enabled: bool = False
    provider: str = "gmail_smtp"
    from_address: str = ""
    to_addresses: list[str] = field(default_factory=list)
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


def load_email_config(
    config: dict,
    enabled_override: Optional[bool] = None,
    recipients_override: Optional[str] = None,
) -> EmailConfig:
    """Load and resolve email config from YAML + environment."""
    email_section = config.get("email", {}) if config else {}

    email_config = EmailConfig(
        enabled=bool(email_section.get("enabled", False)),
        provider=str(email_section.get("provider", "gmail_smtp")),
        from_address=str(email_section.get("from_address", "")),
        to_addresses=_parse_recipients(email_section.get("to_addresses", [])),
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

    email_config.smtp_username = os.getenv(email_config.smtp_username_env, "")
    email_config.smtp_password = os.getenv(email_config.smtp_password_env, "")

    return email_config


class DigestEmailer:
    """Sends digest emails using SMTP."""

    def __init__(self, config: EmailConfig):
        self.config = config

    def _validate(self):
        """Validate required config fields before sending."""
        if self.config.provider != "gmail_smtp":
            raise ValueError(f"Unsupported email provider: {self.config.provider}")
        if not self.config.from_address:
            raise ValueError("Email from_address is not configured")
        if not self.config.to_addresses:
            raise ValueError("No email recipients configured")
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

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.config.smtp_username, self.config.smtp_password)
            smtp.send_message(message)

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
