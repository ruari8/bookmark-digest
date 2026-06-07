import unittest
from unittest.mock import patch, MagicMock

try:
    from src.researcher import AIResearcher
    _DEPS_AVAILABLE = True
except ModuleNotFoundError:
    _DEPS_AVAILABLE = False


@unittest.skipUnless(_DEPS_AVAILABLE, "runtime dependencies not installed (rich)")
class ResearcherCliTests(unittest.TestCase):
    @patch("subprocess.run")
    def test_codex_cli_invocation_uses_subscription_login_path(self, run_mock: MagicMock):
        run_mock.return_value = MagicMock(stdout="OK\n")
        researcher = AIResearcher(
            provider="codex_cli",
            model="gpt-5.5",
            codex_cli_search=True,
            codex_cli_sandbox="read-only",
            codex_cli_profile="research",
        )
        out = researcher._call_ai("Reply OK")

        self.assertEqual(out, "OK")
        args = run_mock.call_args[0][0]
        self.assertEqual(args[:2], ["codex", "exec"])
        self.assertIn("--ephemeral", args)
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--search", args)
        self.assertIn("--output-last-message", args)
        self.assertIn("--model", args)
        model_index = args.index("--model") + 1
        self.assertEqual(args[model_index], "gpt-5.5")
        sandbox_index = args.index("--sandbox") + 1
        self.assertEqual(args[sandbox_index], "read-only")
        self.assertNotIn("--profile", args)
        self.assertEqual(args[-1], "-")
        self.assertEqual(run_mock.call_args.kwargs["input"], "Reply OK")

    @patch("subprocess.run")
    def test_codex_cli_invocation_can_use_profile_when_user_config_is_enabled(self, run_mock: MagicMock):
        run_mock.return_value = MagicMock(stdout="OK\n")
        researcher = AIResearcher(
            provider="codex_cli",
            model="gpt-5.5",
            codex_cli_profile="research",
            codex_cli_ignore_user_config=False,
        )
        out = researcher._call_ai("Reply OK")

        self.assertEqual(out, "OK")
        args = run_mock.call_args[0][0]
        self.assertNotIn("--ignore-user-config", args)
        profile_index = args.index("--profile") + 1
        self.assertEqual(args[profile_index], "research")

    @patch("subprocess.run")
    def test_codex_cli_invocation_can_disable_search_and_model_flag(self, run_mock: MagicMock):
        run_mock.return_value = MagicMock(stdout="OK\n")
        researcher = AIResearcher(
            provider="codex_cli",
            model="",
            codex_cli_search=False,
        )
        out = researcher._call_ai("Reply OK")

        self.assertEqual(out, "OK")
        args = run_mock.call_args[0][0]
        self.assertNotIn("--search", args)
        self.assertNotIn("--model", args)

    @patch("subprocess.run")
    def test_codex_cli_invocation_can_set_reasoning_effort(self, run_mock: MagicMock):
        run_mock.return_value = MagicMock(stdout="OK\n")
        researcher = AIResearcher(
            provider="codex_cli",
            model="gpt-5.5",
            codex_cli_reasoning_effort="low",
        )
        out = researcher._call_ai("Reply OK")

        self.assertEqual(out, "OK")
        args = run_mock.call_args[0][0]
        config_index = args.index("-c") + 1
        self.assertEqual(args[config_index], 'model_reasoning_effort="low"')

    @patch("subprocess.run")
    def test_claude_cli_invocation_uses_opus_and_expected_flags(self, run_mock: MagicMock):
        run_mock.return_value = MagicMock(stdout="OK\n")
        researcher = AIResearcher(
            provider="claude_cli",
            model="opus",
            claude_cli_tools=None,  # Omits --tools flag (Claude Code default tool set)
            claude_cli_permission_mode="bypassPermissions",
            claude_cli_dangerously_skip_permissions=True,
        )
        out = researcher._call_ai("Reply OK")

        self.assertEqual(out, "OK")
        args = run_mock.call_args[0][0]
        self.assertIn("claude", args[0])
        self.assertIn("--model", args)
        model_index = args.index("--model") + 1
        self.assertEqual(args[model_index], "opus")
        self.assertNotIn("--tools", args)
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertIn("--permission-mode", args)

    @patch("subprocess.run")
    def test_claude_cli_invocation_allows_web_tools_when_configured(self, run_mock: MagicMock):
        run_mock.return_value = MagicMock(stdout="OK\n")
        researcher = AIResearcher(
            provider="claude_cli",
            model="opus",
            claude_cli_tools="WebSearch,WebFetch",
            claude_cli_permission_mode="bypassPermissions",
            claude_cli_dangerously_skip_permissions=False,
        )
        out = researcher._call_ai("Reply OK")

        self.assertEqual(out, "OK")
        args = run_mock.call_args[0][0]
        self.assertIn("--tools", args)
        tools_index = args.index("--tools") + 1
        self.assertEqual(args[tools_index], "WebSearch,WebFetch")
        self.assertNotIn("--dangerously-skip-permissions", args)


if __name__ == "__main__":
    unittest.main()
