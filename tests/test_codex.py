import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from src.codex import CodexCLI


class CodexCLITests(unittest.TestCase):
    def test_live_search_is_set_through_exec_config(self):
        command = CodexCLI(model="gpt-6.1-sol").command(Path("/tmp/out.txt"))
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertNotIn("--search", command)  # TUI-only flag; exec ignores it
        self.assertIn('web_search="live"', command)
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertEqual(command[-1], "-")

    def test_search_can_be_disabled(self):
        command = CodexCLI(model="m", search=False).command(Path("/tmp/out.txt"))
        self.assertIn('web_search="disabled"', command)
        self.assertNotIn('web_search="live"', command)

    def test_run_returns_last_message_and_sends_prompt_on_stdin(self):
        def fake_run(command, **kwargs):
            out = Path(command[command.index("--output-last-message") + 1])
            out.write_text("# Title\n\nBody\n")
            self.assertEqual(kwargs["input"], "PROMPT")
            return subprocess.CompletedProcess(command, 0, stdout="noise", stderr="")

        with patch("src.codex.subprocess.run", side_effect=fake_run):
            self.assertEqual(CodexCLI(model="m").run("PROMPT"), "# Title\n\nBody")

    def test_run_raises_with_detail_on_failure(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="model_not_found")
        with patch("src.codex.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "model_not_found"):
                CodexCLI(model="m").run("PROMPT")

    def test_run_raises_when_no_final_message(self):
        ok = subprocess.CompletedProcess([], 0, stdout="chatter", stderr="")
        with patch("src.codex.subprocess.run", return_value=ok):
            with self.assertRaisesRegex(RuntimeError, "no final message"):
                CodexCLI(model="m").run("PROMPT")


if __name__ == "__main__":
    unittest.main()
