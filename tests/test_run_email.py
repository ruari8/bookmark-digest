import unittest
from unittest.mock import patch

try:
    from click.testing import CliRunner
    import run
    from src.emailer import EmailConfig
    _CLICK_TEST_DEPS_AVAILABLE = True
except ModuleNotFoundError:
    _CLICK_TEST_DEPS_AVAILABLE = False


@unittest.skipUnless(_CLICK_TEST_DEPS_AVAILABLE, "click/rich dependencies not installed")
class RunEmailFailureTests(unittest.TestCase):
    def test_empty_run_email_failure_exits_nonzero(self):
        runner = CliRunner()
        email_config = EmailConfig(
            enabled=True,
            send_on_empty=True,
            from_address="bot@example.com",
            to_addresses=["me@example.com"],
            smtp_username="bot@example.com",
            smtp_password="secret",
        )

        with patch.object(run, "load_config", return_value={}), \
             patch.object(run, "load_processed_ids", return_value=[]), \
             patch.object(run, "get_new_bookmarks", return_value=[]), \
             patch.object(run, "load_email_config", return_value=email_config), \
             patch.object(run, "send_empty_status_email", side_effect=RuntimeError("smtp failed")):
            result = runner.invoke(run.main, ["--email"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Failed to send status email", result.output)


if __name__ == "__main__":
    unittest.main()
