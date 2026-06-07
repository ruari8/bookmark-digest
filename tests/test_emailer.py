import unittest
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.emailer import EmailConfig, DigestEmailer, load_dotenv, load_email_config, send_empty_status_email


class FakeHTTPResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"id":"email-id"}'


class EmailerTests(unittest.TestCase):
    def test_load_email_config_resolves_env_and_yaml(self):
        config = {
            "email": {
                "enabled": True,
                "provider": "resend",
                "from_address": "bot@example.com",
                "to_addresses": ["me@example.com"],
                "resend_api_key_env": "TEST_RESEND_KEY",
                "smtp_username_env": "TEST_SMTP_USER",
                "smtp_password_env": "TEST_SMTP_PASS",
                "attach_preferred": "pdf",
                "attach_fallback": "markdown",
            }
        }

        with patch.dict(
            "os.environ",
            {
                "TEST_RESEND_KEY": "re_secret",
                "TEST_SMTP_USER": "bot@example.com",
                "TEST_SMTP_PASS": "secret",
            },
            clear=False,
        ):
            resolved = load_email_config(config)

        self.assertTrue(resolved.enabled)
        self.assertEqual(resolved.provider, "resend")
        self.assertEqual(resolved.resend_api_key, "re_secret")
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
                provider="gmail_smtp",
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
            provider="gmail_smtp",
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
            provider="gmail_smtp",
            from_address="bot@example.com",
            to_addresses=["me@example.com"],
            smtp_username="bot@example.com",
            smtp_password="secret",
        )

        with self.assertRaises(RuntimeError):
            send_empty_status_email(config, reason="No new bookmarks")

    @patch("src.emailer.urlopen", return_value=FakeHTTPResponse())
    def test_send_empty_status_email_uses_resend_api(self, urlopen_mock: MagicMock):
        config = EmailConfig(
            enabled=True,
            provider="resend",
            from_address="Bookmark Notes <bookmarks@ruari.dev>",
            to_addresses=["me@example.com"],
            resend_api_key="re_secret",
        )

        send_empty_status_email(config, reason="No new bookmarks")

        request = urlopen_mock.call_args[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["from"], "Bookmark Notes <bookmarks@ruari.dev>")
        self.assertEqual(payload["to"], ["me@example.com"])
        self.assertIn("No new bookmarks", payload["subject"])
        self.assertIn("No new bookmarks", payload["text"])
        self.assertEqual(request.headers["Authorization"], "Bearer re_secret")

    def test_resend_requires_api_key(self):
        config = EmailConfig(
            enabled=True,
            provider="resend",
            from_address="Bookmark Notes <bookmarks@ruari.dev>",
            to_addresses=["me@example.com"],
            resend_api_key="",
        )

        with self.assertRaises(ValueError):
            send_empty_status_email(config, reason="No new bookmarks")

    def test_load_dotenv_reads_resend_key_without_overriding_existing_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text('RESEND_API_KEY="from-file"\nOTHER_KEY=abc\n')

            with patch.dict(os.environ, {"OTHER_KEY": "existing"}, clear=False):
                os.environ.pop("RESEND_API_KEY", None)
                load_dotenv(env_path)

                self.assertEqual(os.environ["RESEND_API_KEY"], "from-file")
                self.assertEqual(os.environ["OTHER_KEY"], "existing")


if __name__ == "__main__":
    unittest.main()
