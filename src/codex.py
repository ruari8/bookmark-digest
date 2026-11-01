"""Thin wrapper around `codex exec` for unattended, single-shot writing jobs."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


@dataclass
class CodexCLI:
    """Runs one non-interactive Codex turn and returns its final message.

    Uses the machine's `codex login` session (ChatGPT/Codex subscription).
    """

    model: str
    reasoning_effort: str = "medium"
    search: bool = True
    sandbox: str = "read-only"
    ignore_user_config: bool = True
    binary: str = "codex"
    timeout_seconds: int = 1800

    def command(self, output_path: Path) -> list[str]:
        command = [self.binary]
        # `--search` is a global flag; `codex exec --search` is rejected.
        if self.search:
            command.append("--search")
        command += [
            "exec",
            "--ephemeral",
            "--color", "never",
            "--sandbox", self.sandbox,
            "--cd", str(PROJECT_ROOT),
            "--skip-git-repo-check",
            "--model", self.model,
            "-c", f'model_reasoning_effort="{self.reasoning_effort}"',
            "--output-last-message", str(output_path),
        ]
        if self.ignore_user_config:
            command.append("--ignore-user-config")
        # Prompt goes over stdin to keep bookmark content out of argv.
        command.append("-")
        return command

    def run(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="bookmark-digest-codex-") as tmp:
            output_path = Path(tmp) / "last-message.txt"
            try:
                result = subprocess.run(
                    self.command(output_path),
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
            except FileNotFoundError as error:
                raise RuntimeError(f"{self.binary} not found on PATH") from error
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(f"codex exec failed (exit {result.returncode}): {detail[-4000:]}")
            final = output_path.read_text().strip() if output_path.exists() else ""
        if not final:
            raise RuntimeError("codex exec produced no final message")
        return final
