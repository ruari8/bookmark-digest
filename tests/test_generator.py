import tempfile
import unittest
from pathlib import Path

try:
    from src.generator import DocumentGenerator, generate_document
    from src.researcher import ResearchOutput

    _DEPS_AVAILABLE = True
except ModuleNotFoundError:
    _DEPS_AVAILABLE = False


@unittest.skipUnless(_DEPS_AVAILABLE, "runtime dependencies not installed (rich)")
class DocumentGeneratorTests(unittest.TestCase):
    def _research(self) -> ResearchOutput:
        return ResearchOutput(
            executive_summary="A short summary.",
            topics=[],
            further_reading=[],
        )

    def test_save_markdown_uses_template_and_creates_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "nested"
            generator = DocumentGenerator(
                output_dir=output_dir,
                filename_template="digest-{date}.md",
            )

            path = generator.save_markdown(self._research())

            self.assertTrue(path.exists())
            self.assertEqual(path.parent, output_dir)
            self.assertTrue(path.name.startswith("digest-"))
            self.assertTrue(path.name.endswith(".md"))
            self.assertIn("A short summary.", path.read_text())

    def test_generate_document_accepts_filename_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_document(
                self._research(),
                output_dir=Path(tmp),
                filename_template="custom-{date}.md",
            )

            markdown_path = result["markdown"]

            self.assertIsNotNone(markdown_path)
            self.assertTrue(markdown_path.exists())
            self.assertTrue(markdown_path.name.startswith("custom-"))


if __name__ == "__main__":
    unittest.main()
