import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.emailer import EmailConfig, DigestEmailer, load_email_config, send_empty_status_email


class EmailerTests(unittest.TestCase):
    def test_load_email_config_resolves_env_and_yaml(self):
        config = {
            "email": {
                "enabled": True,
                "from_address": "bot@example.com",
                "to_addresses": ["me@example.com"],
                "smtp_username_env": "TEST_SMTP_USER",
                "smtp_password_env": "TEST_SMTP_PASS",
                "attach_preferred": "pdf",
                "attach_fallback": "markdown",
            }
        }

        with patch.dict("os.environ", {"TEST_SMTP_USER": "bot@example.com", "TEST_SMTP_PASS": "secret"}, clear=False):
            resolved = load_email_config(config)

        self.assertTrue(resolved.enabled)
        self.assertEqual(resolved.smtp_username, "bot@example.com")
        self.assertEqual(resolved.smtp_password, "secret")
        self.assertEqual(resolved.to_addresses, ["me@example.com"])

    def test_attachment_selection_prefers_pdf_then_fallback_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            md_path = tmpdir / "digest.md"
            pdf_path = tmpdir / "digest.pdf"
            md_path.write_text("markdown")

            config = EmailConfig(
                enabled=True,
                from_address="bot@example.com",
                to_addresses=["me@example.com"],
                attach_preferred="pdf",
                attach_fallback="markdown",
            )
            emailer = DigestEmailer(config)

            # PDF missing -> markdown fallback
            selected = emailer.resolve_attachment_path({"markdown": md_path, "pdf": pdf_path})
            self.assertEqual(selected, md_path)

            # PDF present -> PDF preferred
            pdf_path.write_bytes(b"%PDF")
            selected = emailer.resolve_attachment_path({"markdown": md_path, "pdf": pdf_path})
            self.assertEqual(selected, pdf_path)

    @patch("smtplib.SMTP")
    def test_send_empty_status_email_uses_smtp(self, smtp_cls: MagicMock):
        smtp = smtp_cls.return_value.__enter__.return_value
        config = EmailConfig(
            enabled=True,
            from_address="bot@example.com",
            to_addresses=["me@example.com"],
            smtp_username="bot@example.com",
            smtp_password="secret",
        )

        send_empty_status_email(config, reason="No new bookmarks")

        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("bot@example.com", "secret")
        smtp.send_message.assert_called_once()

    @patch("smtplib.SMTP", side_effect=RuntimeError("smtp failed"))
    def test_send_empty_status_email_raises_on_smtp_failure(self, _smtp_cls: MagicMock):
        config = EmailConfig(
            enabled=True,
            from_address="bot@example.com",
            to_addresses=["me@example.com"],
            smtp_username="bot@example.com",
            smtp_password="secret",
        )

        with self.assertRaises(RuntimeError):
            send_empty_status_email(config, reason="No new bookmarks")


if __name__ == "__main__":
    unittest.main()
