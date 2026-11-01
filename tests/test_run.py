import unittest
from unittest.mock import patch

from click.testing import CliRunner

import run
from src.emailer import EmailConfig


def enabled_email():
    return EmailConfig(enabled=True, from_address="a@x.dev", to_addresses=["b@x.dev"], resend_api_key="re_test")


class RunCliTests(unittest.TestCase):
    def test_default_mode_is_blog_and_failures_send_an_alert(self):
        with patch.object(run, "load_config", return_value={}), \
             patch.object(run, "load_email_config", return_value=enabled_email()), \
             patch.object(run, "run_blog_pipeline", side_effect=RuntimeError("model_not_found")), \
             patch.object(run, "send_failure_alert") as alert:
            result = CliRunner().invoke(run.main, [])

        self.assertEqual(result.exit_code, 1)
        alert.assert_called_once()
        self.assertIn("model_not_found", str(alert.call_args.args[1]))

    def test_dry_run_failures_do_not_email(self):
        with patch.object(run, "load_config", return_value={}), \
             patch.object(run, "load_email_config", return_value=enabled_email()), \
             patch.object(run, "run_blog_pipeline", side_effect=RuntimeError("boom")), \
             patch.object(run, "send_failure_alert") as alert:
            result = CliRunner().invoke(run.main, ["--dry-run"])
        self.assertEqual(result.exit_code, 1)
        alert.assert_not_called()

    def test_test_email_mode_sends_notification(self):
        with patch.object(run, "load_config", return_value={"blog": {"base_url": "http://localhost:8000"}}), \
             patch.object(run, "load_email_config", return_value=enabled_email()), \
             patch.object(run, "send_blog_notification") as send_mock:
            result = CliRunner().invoke(run.main, ["--mode", "test-email"])
        self.assertEqual(result.exit_code, 0, result.output)
        send_mock.assert_called_once()

    def test_codex_from_config_reads_ai_section(self):
        codex = run.codex_from_config({"ai": {"model": "gpt-6.1-sol", "reasoning_effort": "high", "web_search": False}})
        self.assertEqual((codex.model, codex.reasoning_effort, codex.search), ("gpt-6.1-sol", "high", False))


if __name__ == "__main__":
    unittest.main()
