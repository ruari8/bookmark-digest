"""
Document Generator

Compiles research into formatted markdown and optional PDF.
"""

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console

from .researcher import ResearchOutput

console = Console()

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent


class DocumentGenerator:
    """Generates markdown and PDF documents from research output."""

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        filename_template: str = "weekly-digest-{date}.md",
    ):
        self.output_dir = output_dir or PROJECT_ROOT.parent  # Default: learn/ folder
        self.filename_template = filename_template

    def _generate_filename(self, extension: str = "md") -> str:
        """Generate filename with current date."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = self.filename_template.format(date=date_str)
        if not filename.endswith(f".{extension}"):
            filename = filename.rsplit(".", 1)[0] + f".{extension}"
        return filename

    def generate_markdown(self, research: ResearchOutput) -> str:
        """Generate markdown content from research output."""
        date_str = datetime.now().strftime("%B %d, %Y")

        sections = []

        # Header
        sections.append(f"""# Weekly Bookmark Digest
*Generated on {date_str}*

---

## Executive Summary

{research.executive_summary}

---
""")

        # Topic sections
        for topic in research.topics:
            complexity_badge = {
                "simple": "Quick Read",
                "medium": "Moderate",
                "complex": "Deep Dive",
            }.get(topic.complexity, "")

            sections.append(f"""## {topic.name}

*{topic.description}* | **{complexity_badge}**

{topic.research or "No research content available."}

---
""")

        # Further reading
        if research.further_reading:
            links_by_topic = {}
            for link in research.further_reading:
                topic = link.get("topic", "Other")
                if topic not in links_by_topic:
                    links_by_topic[topic] = []
                links_by_topic[topic].append(link)

            sections.append("## Further Reading\n")

            for topic, links in links_by_topic.items():
                sections.append(f"\n### {topic}\n")
                for link in links:
                    sections.append(f"- [{link['title']}]({link['url']}) ({link['type']})")
                sections.append("")

        # Footer
        sections.append("""
---

*This digest was automatically generated from your X bookmarks using [Bookmark Digest](https://github.com/youruser/bookmark-digest).*
""")

        return "\n".join(sections)

    def save_markdown(self, research: ResearchOutput, filename: Optional[str] = None) -> Path:
        """Save research as markdown file."""
        if filename is None:
            filename = self._generate_filename("md")

        filepath = self.output_dir / filename
        content = self.generate_markdown(research)

        filepath.write_text(content)
        console.print(f"[green]Saved markdown: {filepath}[/green]")

        return filepath

    def generate_pdf(self, markdown_path: Path, output_path: Optional[Path] = None) -> Optional[Path]:
        """
        Convert markdown to PDF using pandoc.

        Requires pandoc to be installed: brew install pandoc
        """
        if output_path is None:
            output_path = markdown_path.with_suffix(".pdf")

        # Check if pandoc is available
        try:
            subprocess.run(["pandoc", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            console.print("[yellow]pandoc not found. Install with: brew install pandoc[/yellow]")
            console.print("[yellow]Skipping PDF generation.[/yellow]")
            return None

        try:
            # Generate PDF with pandoc
            subprocess.run([
                "pandoc",
                str(markdown_path),
                "-o", str(output_path),
                "--pdf-engine=pdflatex",
                "-V", "geometry:margin=1in",
                "-V", "fontsize=11pt",
            ], check=True, capture_output=True)

            console.print(f"[green]Saved PDF: {output_path}[/green]")
            return output_path

        except subprocess.CalledProcessError as e:
            # Try with weasyprint as fallback
            try:
                subprocess.run([
                    "pandoc",
                    str(markdown_path),
                    "-o", str(output_path),
                    "--pdf-engine=weasyprint",
                ], check=True, capture_output=True)

                console.print(f"[green]Saved PDF: {output_path}[/green]")
                return output_path

            except subprocess.CalledProcessError:
                console.print(f"[red]PDF generation failed: {e.stderr.decode() if e.stderr else 'Unknown error'}[/red]")
                return None

    def save(
        self,
        research: ResearchOutput,
        format: str = "markdown",
        filename: Optional[str] = None,
    ) -> dict[str, Optional[Path]]:
        """
        Save research output in specified format(s).

        Args:
            research: ResearchOutput from researcher
            format: "markdown", "pdf", or "both"
            filename: Optional custom filename (without extension)

        Returns:
            Dictionary with paths to generated files
        """
        results = {"markdown": None, "pdf": None}

        # Always generate markdown first
        md_filename = filename + ".md" if filename else None
        md_path = self.save_markdown(research, md_filename)
        results["markdown"] = md_path

        # Generate PDF if requested
        if format in ("pdf", "both"):
            pdf_path = self.generate_pdf(md_path)
            results["pdf"] = pdf_path

            # If only PDF was requested, optionally remove markdown
            # (keeping it for now as it's useful)

        return results


def generate_document(
    research: ResearchOutput,
    output_dir: Optional[Path] = None,
    format: str = "markdown",
) -> dict[str, Optional[Path]]:
    """
    Convenience function to generate documents.

    Args:
        research: ResearchOutput from researcher
        output_dir: Directory to save files (default: learn/ folder)
        format: Output format ("markdown", "pdf", or "both")

    Returns:
        Dictionary with paths to generated files
    """
    generator = DocumentGenerator(output_dir=output_dir)
    return generator.save(research, format=format)


if __name__ == "__main__":
    # Test with mock research output
    from .researcher import ResearchOutput, TopicCluster

    mock_research = ResearchOutput(
        executive_summary="This week's bookmarks covered AI developments and software engineering practices.",
        topics=[
            TopicCluster(
                name="AI Agents",
                description="Developments in autonomous AI systems",
                bookmarks=[],
                complexity="complex",
                research="## Overview\n\nAI agents are becoming increasingly capable...\n\n## Key Developments\n\n1. Multi-agent systems\n2. Tool use improvements",
            ),
        ],
        further_reading=[
            {"title": "Example Article", "url": "https://example.com", "type": "article", "topic": "AI Agents"},
        ],
    )

    generator = DocumentGenerator()
    content = generator.generate_markdown(mock_research)
    print(content)
