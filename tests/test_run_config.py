import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from click.testing import CliRunner
    import run
    from src.emailer import EmailConfig
    from src.researcher import ResearchOutput
    from src.scraper import Tweet

    _DEPS_AVAILABLE = True
except ModuleNotFoundError:
    _DEPS_AVAILABLE = False


@unittest.skipUnless(_DEPS_AVAILABLE, "click/rich dependencies not installed")
class RunConfigTests(unittest.TestCase):
    def test_output_config_is_used_when_cli_format_is_omitted(self):
        runner = CliRunner()
        captured = {}
        tweet = Tweet(
            id="1",
            text="A bookmarked link",
            author_username="ruari",
            author_name="Ruari",
            created_at="2026-06-06",
            urls=[],
            media_urls=[],
        )
        research = ResearchOutput(
            executive_summary="Summary",
            topics=[],
            further_reading=[],
        )

        def fake_generate_document(research_output, output_dir, format, filename_template):
            captured["research"] = research_output
            captured["output_dir"] = output_dir
            captured["format"] = format
            captured["filename_template"] = filename_template
            return {"markdown": Path("/tmp/digest.md"), "pdf": None}

        config = {
            "output": {
                "directory": "digests",
                "format": "both",
                "filename_template": "custom-{date}.md",
            }
        }

        with patch.object(run, "load_config", return_value=config), \
             patch.object(run, "load_processed_ids", return_value=[]), \
             patch.object(run, "get_new_bookmarks", return_value=[tweet]), \
             patch.object(run, "parse_bookmarks", return_value=[]), \
             patch.object(run, "research_bookmarks", return_value=research), \
             patch.object(run, "generate_document", side_effect=fake_generate_document), \
             patch.object(run, "save_processed_ids"), \
             patch.object(run, "load_email_config", return_value=EmailConfig(enabled=False)):
            result = runner.invoke(run.main, [])

        self.assertEqual(result.exit_code, 0)
        self.assertIs(captured["research"], research)
        self.assertEqual(captured["format"], "both")
        self.assertEqual(captured["filename_template"], "custom-{date}.md")
        self.assertEqual(captured["output_dir"], (run.PROJECT_ROOT / "digests").resolve())


if __name__ == "__main__":
    unittest.main()
