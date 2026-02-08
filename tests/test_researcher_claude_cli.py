import unittest
from unittest.mock import patch, MagicMock

try:
    from src.researcher import AIResearcher
    _DEPS_AVAILABLE = True
except ModuleNotFoundError:
    _DEPS_AVAILABLE = False


@unittest.skipUnless(_DEPS_AVAILABLE, "runtime dependencies not installed (rich)")
class ResearcherClaudeCliTests(unittest.TestCase):
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
