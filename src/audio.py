"""
Audio Generator

Converts research digest to audio using TTS APIs.
"""

import re
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .researcher import ResearchOutput

console = Console()

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent


class AudioGenerator:
    """Generates audio from research output using TTS."""

    # OpenAI TTS voices
    OPENAI_VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]

    # Maximum characters per TTS request (OpenAI limit is 4096)
    MAX_CHUNK_SIZE = 4000

    def __init__(
        self,
        provider: str = "openai",
        voice: str = "alloy",
        output_dir: Optional[Path] = None,
    ):
        self.provider = provider
        self.voice = voice
        self.output_dir = output_dir or PROJECT_ROOT.parent  # Default: learn/ folder
        self._client = None

    def _get_client(self):
        """Get or create the TTS client."""
        if self._client is None:
            if self.provider == "openai":
                import openai
                self._client = openai.OpenAI()
            elif self.provider == "elevenlabs":
                # ElevenLabs requires API key
                try:
                    from elevenlabs import ElevenLabs
                    self._client = ElevenLabs()
                except ImportError:
                    raise ImportError("Install elevenlabs: pip install elevenlabs")
            else:
                raise ValueError(f"Unknown TTS provider: {self.provider}")
        return self._client

    def _prepare_text_for_speech(self, research: ResearchOutput) -> str:
        """Convert research output to speech-friendly text."""
        parts = []

        # Introduction
        parts.append("Welcome to your weekly bookmark digest.")
        parts.append("")

        # Executive summary
        parts.append("Here's an overview of this week's highlights.")
        parts.append(self._clean_for_speech(research.executive_summary))
        parts.append("")

        # Topics
        for i, topic in enumerate(research.topics, 1):
            parts.append(f"Topic {i}: {topic.name}.")
            parts.append(topic.description)
            parts.append("")

            if topic.research:
                # Clean up markdown for speech
                clean_research = self._clean_for_speech(topic.research)
                parts.append(clean_research)
                parts.append("")

        # Outro
        parts.append("That's all for this week's digest. Happy learning!")

        return "\n\n".join(parts)

    def _clean_for_speech(self, text: str) -> str:
        """Clean markdown text for speech synthesis."""
        # Remove markdown headers
        text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)

        # Remove markdown links, keep text
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

        # Remove bold/italic markers
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"\*([^*]+)\*", r"\1", text)
        text = re.sub(r"__([^_]+)__", r"\1", text)
        text = re.sub(r"_([^_]+)_", r"\1", text)

        # Remove code blocks
        text = re.sub(r"```[^`]*```", "code block omitted", text, flags=re.DOTALL)
        text = re.sub(r"`([^`]+)`", r"\1", text)

        # Convert bullet points to natural speech
        text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.MULTILINE)

        # Remove horizontal rules
        text = re.sub(r"^---+$", "", text, flags=re.MULTILINE)

        # Clean up multiple newlines
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Remove URLs
        text = re.sub(r"https?://\S+", "", text)

        return text.strip()

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into chunks for TTS API limits."""
        chunks = []
        current_chunk = ""

        # Split by paragraphs
        paragraphs = text.split("\n\n")

        for para in paragraphs:
            if len(current_chunk) + len(para) + 2 < self.MAX_CHUNK_SIZE:
                current_chunk += para + "\n\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = para + "\n\n"

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks

    def _generate_openai_audio(self, text: str, output_path: Path) -> Path:
        """Generate audio using OpenAI TTS."""
        client = self._get_client()

        chunks = self._chunk_text(text)
        audio_data = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Generating audio...", total=len(chunks))

            for i, chunk in enumerate(chunks):
                progress.update(task, description=f"Processing chunk {i+1}/{len(chunks)}...")

                response = client.audio.speech.create(
                    model="tts-1",
                    voice=self.voice,
                    input=chunk,
                    response_format="mp3",
                )

                audio_data.append(response.content)
                progress.advance(task)

        # Combine audio chunks
        combined = b"".join(audio_data)
        output_path.write_bytes(combined)

        return output_path

    def _generate_elevenlabs_audio(self, text: str, output_path: Path) -> Path:
        """Generate audio using ElevenLabs."""
        client = self._get_client()

        chunks = self._chunk_text(text)
        audio_data = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Generating audio...", total=len(chunks))

            for i, chunk in enumerate(chunks):
                progress.update(task, description=f"Processing chunk {i+1}/{len(chunks)}...")

                audio = client.generate(
                    text=chunk,
                    voice=self.voice,
                    model="eleven_multilingual_v2",
                )

                audio_data.append(audio)
                progress.advance(task)

        # Combine and save
        with open(output_path, "wb") as f:
            for chunk in audio_data:
                f.write(chunk)

        return output_path

    def generate(
        self,
        research: ResearchOutput,
        filename: Optional[str] = None,
    ) -> Optional[Path]:
        """
        Generate audio from research output.

        Args:
            research: ResearchOutput from researcher
            filename: Optional output filename (default: weekly-digest-{date}.mp3)

        Returns:
            Path to generated audio file, or None if generation failed
        """
        if filename is None:
            from datetime import datetime
            date_str = datetime.now().strftime("%Y-%m-%d")
            filename = f"weekly-digest-{date_str}.mp3"

        output_path = self.output_dir / filename

        console.print(f"[blue]Generating audio with {self.provider} ({self.voice} voice)...[/blue]")

        # Prepare text
        text = self._prepare_text_for_speech(research)

        # Log text length
        console.print(f"[dim]Prepared {len(text)} characters for speech[/dim]")

        try:
            if self.provider == "openai":
                result = self._generate_openai_audio(text, output_path)
            elif self.provider == "elevenlabs":
                result = self._generate_elevenlabs_audio(text, output_path)
            else:
                raise ValueError(f"Unknown provider: {self.provider}")

            console.print(f"[green]Saved audio: {result}[/green]")
            return result

        except Exception as e:
            console.print(f"[red]Audio generation failed: {e}[/red]")
            return None


def generate_audio(
    research: ResearchOutput,
    provider: str = "openai",
    voice: str = "alloy",
    output_dir: Optional[Path] = None,
) -> Optional[Path]:
    """
    Convenience function to generate audio.

    Args:
        research: ResearchOutput from researcher
        provider: TTS provider ("openai" or "elevenlabs")
        voice: Voice to use
        output_dir: Output directory (default: learn/ folder)

    Returns:
        Path to generated audio file, or None if failed
    """
    generator = AudioGenerator(provider=provider, voice=voice, output_dir=output_dir)
    return generator.generate(research)


def print_notebooklm_instructions(markdown_path: Path):
    """Print instructions for using NotebookLM for podcast generation."""
    console.print("\n[bold cyan]NotebookLM Podcast Generation[/bold cyan]")
    console.print("─" * 50)
    console.print(f"\n1. Open [link=https://notebooklm.google.com]NotebookLM[/link]")
    console.print(f"2. Create a new notebook")
    console.print(f"3. Upload your digest: [bold]{markdown_path}[/bold]")
    console.print(f"4. Click 'Generate Audio Overview' in the notebook")
    console.print(f"5. Download the generated podcast\n")
    console.print("─" * 50)


if __name__ == "__main__":
    # Test with mock research output
    from .researcher import ResearchOutput, TopicCluster

    mock_research = ResearchOutput(
        executive_summary="This week we explored AI developments and software engineering best practices.",
        topics=[
            TopicCluster(
                name="AI Agents",
                description="Autonomous AI systems are advancing rapidly",
                bookmarks=[],
                complexity="medium",
                research="AI agents can now perform complex multi-step tasks. Key developments include improved tool use and better reasoning capabilities.",
            ),
        ],
        further_reading=[],
    )

    generator = AudioGenerator()
    text = generator._prepare_text_for_speech(mock_research)
    print("Prepared text for speech:")
    print(text)
