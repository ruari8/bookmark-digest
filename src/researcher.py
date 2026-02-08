"""
AI Researcher

Clusters content by topic and generates adaptive-depth research using AI.
"""

import json
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from rich.console import Console

from .parser import ParsedBookmark

console = Console()


@dataclass
class TopicCluster:
    """A cluster of related bookmarks on a topic."""
    name: str
    description: str
    bookmarks: list[ParsedBookmark]
    complexity: str = "medium"  # simple, medium, complex
    research: Optional[str] = None


@dataclass
class ResearchOutput:
    """Complete research output from AI."""
    executive_summary: str
    topics: list[TopicCluster]
    further_reading: list[dict]  # {"title": str, "url": str}


class AIResearcher:
    """Generates AI research from parsed bookmarks."""

    GROUNDING_RULES = """Grounding rules (IMPORTANT):
- Prefer the provided bookmarked content (tweets + extracted linked content) as your primary evidence.
- If you use tools (e.g. WebSearch/WebFetch), you MUST:
  1) only make claims supported by what you actually saw
  2) include a short "Sources" section with URLs for any non-trivial factual claims
- Do NOT invent facts. If evidence is insufficient or contradictory, say so explicitly.
"""

    CLUSTERING_PROMPT = """You are organizing bookmarked content into coherent topics for a weekly research digest.

{grounding_rules}

Analyze the following bookmarked tweets and their linked content. Group them into 3-7 logical topic clusters.
For each cluster, determine if the topic is:
- "simple": Familiar concept, quick summary sufficient (1-2 paragraphs)
- "medium": Interesting topic, moderate depth needed (3-5 paragraphs)
- "complex": Novel/technical topic, deep dive warranted (1+ pages with examples)

Return a JSON object with this structure:
{
  "clusters": [
    {
      "name": "Short topic name",
      "description": "One sentence describing what this cluster covers",
      "complexity": "simple|medium|complex",
      "bookmark_indices": [0, 3, 5]  // indices of bookmarks in this cluster
    }
  ]
}

Bookmarks to organize:
"""

    RESEARCH_PROMPT = """You are a knowledgeable researcher creating a learning digest.

{grounding_rules}

Write a comprehensive research section for this topic: "{topic_name}"
Topic description: {topic_description}
Complexity level: {complexity}

Based on the following bookmarked content:

{content}

Guidelines based on complexity:
- simple: 1-2 paragraph summary. Key takeaways only.
- medium: 3-5 paragraphs. Explain concepts, provide context, note implications.
- complex: Full deep-dive (500-1000 words). Include:
  - Detailed explanation of concepts
  - Technical details where relevant
  - Real-world examples or applications
  - Connections to broader trends
  - Potential implications

Write in a clear, educational tone. Use markdown formatting.
Focus on teaching the reader, not just summarizing the tweets.
If there are linked articles, incorporate their insights.

Output the research content directly (no JSON, just markdown text)."""

    SUMMARY_PROMPT = """Based on the following topic summaries from this week's bookmarks, write a brief executive summary (2-3 paragraphs) that:
{grounding_rules}

1. Highlights the main themes across all topics
2. Notes any interesting patterns or connections
3. Suggests which topics deserve the reader's attention first

Topics covered:
{topics}

Write the executive summary directly in markdown."""

    def __init__(
        self,
        provider: str = "anthropic",
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 8192,
        claude_cli_tools: Optional[str] = None,
        claude_cli_permission_mode: Optional[str] = "bypassPermissions",
        claude_cli_dangerously_skip_permissions: bool = True,
    ):
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        self.claude_cli_tools = claude_cli_tools
        self.claude_cli_permission_mode = claude_cli_permission_mode
        self.claude_cli_dangerously_skip_permissions = claude_cli_dangerously_skip_permissions
        self._client = None

    def _call_claude_cli(self, prompt: str) -> str:
        """
        Call Claude Code CLI in non-interactive mode.

        This is intended for automation using a Claude subscription (no API key).
        Tool/permission behavior is controlled by `claude_cli_tools`,
        `claude_cli_permission_mode`, and `claude_cli_dangerously_skip_permissions`.
        """
        command: list[str] = [
            "claude",
            "-p",
            "--model",
            self.model,
            "--output-format",
            "text",
            "--no-session-persistence",
        ]

        # Tools:
        # - None: omit flag entirely (Claude Code default tool set; "all tools")
        # - "default": explicit default tool set
        # - "": disable all tools
        if self.claude_cli_tools is not None:
            command.extend(["--tools", self.claude_cli_tools])

        if self.claude_cli_permission_mode is not None:
            command.extend(["--permission-mode", self.claude_cli_permission_mode])

        # Fully unattended automation: bypass permission prompts.
        if self.claude_cli_dangerously_skip_permissions:
            command.append("--allow-dangerously-skip-permissions")
            command.append("--dangerously-skip-permissions")

        try:
            result = subprocess.run(
                command + [prompt],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as e:
            raise RuntimeError("claude CLI not found. Install Claude Code and ensure `claude` is on PATH.") from e
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            stdout = (e.stdout or "").strip()
            detail = stderr or stdout or str(e)
            raise RuntimeError(f"claude CLI call failed: {detail}") from e

        return (result.stdout or "").strip()

    def _get_client(self):
        """Get or create the AI client."""
        if self._client is None:
            if self.provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic()
            elif self.provider == "openai":
                import openai
                self._client = openai.OpenAI()
            else:
                raise ValueError(f"Unknown provider: {self.provider}")
        return self._client

    def _call_ai(self, prompt: str, max_tokens: Optional[int] = None) -> str:
        """Make an AI API call."""
        if self.provider in ("claude_cli", "claude"):
            return self._call_claude_cli(prompt)

        client = self._get_client()
        tokens = max_tokens or self.max_tokens

        if self.provider == "anthropic":
            response = client.messages.create(
                model=self.model,
                max_tokens=tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        elif self.provider == "openai":
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

    def _prepare_bookmark_summary(self, bookmark: ParsedBookmark, index: int) -> str:
        """Prepare a bookmark summary for clustering."""
        lines = [
            f"[{index}] @{bookmark.tweet.author_username}: {bookmark.tweet.text[:500]}"
        ]
        for content in bookmark.extracted_content:
            if content.title:
                lines.append(f"    Link: {content.title} ({content.content_type})")
        return "\n".join(lines)

    def cluster_bookmarks(self, bookmarks: list[ParsedBookmark]) -> list[TopicCluster]:
        """Cluster bookmarks into topics using AI."""
        if not bookmarks:
            return []

        console.print("[blue]Clustering bookmarks by topic...[/blue]")

        # Prepare summaries for clustering
        summaries = []
        for i, bookmark in enumerate(bookmarks):
            summaries.append(self._prepare_bookmark_summary(bookmark, i))

        prompt = self.CLUSTERING_PROMPT.format(grounding_rules=self.GROUNDING_RULES) + "\n\n" + "\n\n".join(summaries)

        try:
            response = self._call_ai(prompt, max_tokens=2000)

            # Parse JSON response
            # Handle potential markdown code blocks
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                response = response.split("```")[1].split("```")[0]

            data = json.loads(response)

            clusters = []
            for cluster_data in data.get("clusters", []):
                cluster_bookmarks = [
                    bookmarks[i]
                    for i in cluster_data.get("bookmark_indices", [])
                    if i < len(bookmarks)
                ]
                if cluster_bookmarks:
                    clusters.append(TopicCluster(
                        name=cluster_data.get("name", "Unnamed Topic"),
                        description=cluster_data.get("description", ""),
                        bookmarks=cluster_bookmarks,
                        complexity=cluster_data.get("complexity", "medium"),
                    ))

            console.print(f"[green]Created {len(clusters)} topic clusters[/green]")
            return clusters

        except Exception as e:
            console.print(f"[yellow]Warning: Clustering failed ({e}), using single cluster[/yellow]")
            # Fallback: single cluster with all bookmarks
            return [TopicCluster(
                name="This Week's Bookmarks",
                description="All bookmarked content from this week",
                bookmarks=bookmarks,
                complexity="medium",
            )]

    def research_topic(self, cluster: TopicCluster) -> str:
        """Generate research content for a topic cluster."""
        console.print(f"[blue]Researching: {cluster.name} ({cluster.complexity})...[/blue]")

        # Prepare content for research
        content_parts = []
        for bookmark in cluster.bookmarks:
            content_parts.append(bookmark.summary_text)

        content = "\n\n---\n\n".join(content_parts)

        # Truncate if too long
        if len(content) > 50000:
            content = content[:50000] + "\n\n[Content truncated for length...]"

        prompt = self.RESEARCH_PROMPT.format(
            grounding_rules=self.GROUNDING_RULES,
            topic_name=cluster.name,
            topic_description=cluster.description,
            complexity=cluster.complexity,
            content=content,
        )

        # Adjust token limit based on complexity
        token_limits = {
            "simple": 1000,
            "medium": 2500,
            "complex": 5000,
        }
        max_tokens = token_limits.get(cluster.complexity, 2500)

        research = self._call_ai(prompt, max_tokens=max_tokens)
        cluster.research = research
        return research

    def generate_summary(self, clusters: list[TopicCluster]) -> str:
        """Generate executive summary of all topics."""
        console.print("[blue]Generating executive summary...[/blue]")

        topics_text = "\n\n".join([
            f"## {c.name}\n{c.description}\nComplexity: {c.complexity}\n\n{c.research[:500]}..."
            for c in clusters
            if c.research
        ])

        prompt = self.SUMMARY_PROMPT.format(
            grounding_rules=self.GROUNDING_RULES,
            topics=topics_text,
        )
        return self._call_ai(prompt, max_tokens=1500)

    def collect_links(self, clusters: list[TopicCluster]) -> list[dict]:
        """Collect all unique links for further reading."""
        seen_urls = set()
        links = []

        for cluster in clusters:
            for bookmark in cluster.bookmarks:
                for content in bookmark.extracted_content:
                    if content.url not in seen_urls and content.title:
                        seen_urls.add(content.url)
                        links.append({
                            "title": content.title,
                            "url": content.url,
                            "type": content.content_type,
                            "topic": cluster.name,
                        })

        return links

    def research(self, bookmarks: list[ParsedBookmark]) -> ResearchOutput:
        """Run the full research pipeline."""
        if not bookmarks:
            return ResearchOutput(
                executive_summary="No new bookmarks to process this week.",
                topics=[],
                further_reading=[],
            )

        # Step 1: Cluster bookmarks
        clusters = self.cluster_bookmarks(bookmarks)

        # Step 2: Research each topic
        for cluster in clusters:
            self.research_topic(cluster)

        # Step 3: Generate executive summary
        summary = self.generate_summary(clusters)

        # Step 4: Collect links
        links = self.collect_links(clusters)

        console.print("[green]Research complete![/green]")

        return ResearchOutput(
            executive_summary=summary,
            topics=clusters,
            further_reading=links,
        )


def research_bookmarks(
    bookmarks: list[ParsedBookmark],
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-20250514",
    claude_cli_tools: Optional[str] = "default",
    claude_cli_permission_mode: str = "bypassPermissions",
    claude_cli_dangerously_skip_permissions: bool = True,
) -> ResearchOutput:
    """
    Convenience function to run research on bookmarks.

    Args:
        bookmarks: List of ParsedBookmark objects
        provider: AI provider (anthropic, openai, or claude_cli)
        model: Model name to use

    Returns:
        ResearchOutput with all research content
    """
    researcher = AIResearcher(
        provider=provider,
        model=model,
        claude_cli_tools=claude_cli_tools,
        claude_cli_permission_mode=claude_cli_permission_mode,
        claude_cli_dangerously_skip_permissions=claude_cli_dangerously_skip_permissions,
    )
    return researcher.research(bookmarks)


if __name__ == "__main__":
    # Test with mock data
    from .scraper import Tweet

    mock_tweet = Tweet(
        id="1",
        text="Interesting thread on AI agents and their capabilities",
        author_username="testuser",
        author_name="Test User",
        created_at="2024-01-20",
        urls=["https://example.com/article"],
        media_urls=[],
    )

    mock_bookmark = ParsedBookmark(tweet=mock_tweet)

    researcher = AIResearcher()
    clusters = researcher.cluster_bookmarks([mock_bookmark])
    print(f"Clusters: {clusters}")
