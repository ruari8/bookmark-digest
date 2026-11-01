import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.emailer import EmailConfig, load_dotenv, load_email_config, resend_payload, send_email


class EmailerTests(unittest.TestCase):
    def test_load_email_config_resolves_yaml_env_and_overrides(self):
        config = {"email": {"enabled": True, "from_address": "a@x.dev", "to_addresses": ["b@x.dev"]}}
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test"}, clear=False), patch("src.emailer.load_dotenv"):
            resolved = load_email_config(config, enabled_override=False, recipients_override="c@x.dev, d@x.dev")
        self.assertFalse(resolved.enabled)
        self.assertEqual(resolved.to_addresses, ["c@x.dev", "d@x.dev"])
        self.assertEqual(resolved.resend_api_key, "re_test")

    def test_payload_prefixes_subject_and_includes_html(self):
        config = EmailConfig(from_address="a@x.dev", to_addresses=["b@x.dev"], subject_prefix="Bookmark Notes")
        payload = resend_payload(config, "Weekly run failed", "text", "<p>html</p>")
        self.assertEqual(payload["subject"], "Bookmark Notes - Weekly run failed")
        self.assertEqual(payload["html"], "<p>html</p>")

    def test_send_email_posts_to_resend(self):
        config = EmailConfig(from_address="a@x.dev", to_addresses=["b@x.dev"], resend_api_key="re_test")
        response = MagicMock(status=200)
        response.__enter__.return_value = response
        with patch("src.emailer.urlopen", return_value=response) as urlopen_mock:
            send_email(config, "Subject", "Body")
        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer re_test")
        self.assertEqual(json.loads(request.data)["text"], "Body")

    def test_send_email_requires_api_key(self):
        config = EmailConfig(from_address="a@x.dev", to_addresses=["b@x.dev"])
        with self.assertRaisesRegex(ValueError, "RESEND_API_KEY"):
            send_email(config, "Subject", "Body")

    def test_load_dotenv_does_not_override_existing_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("BD_TEST_A=from_file\nBD_TEST_B='quoted'\n")
            with patch.dict(os.environ, {"BD_TEST_A": "from_env"}, clear=False):
                load_dotenv(path)
                self.assertEqual(os.environ["BD_TEST_A"], "from_env")
                self.assertEqual(os.environ["BD_TEST_B"], "quoted")
            os.environ.pop("BD_TEST_B", None)


if __name__ == "__main__":
    unittest.main()
