#!/usr/bin/env python3
"""
Viral Finance Content Machine
Auto-generates viral X threads and Substack posts from trending finance topics.

Usage:
  python main.py              # Generate content, save to files (no X posting)
  python main.py --post       # Generate + auto-post threads to X
  python main.py --dry-run    # Generate and save files, but skip X posting
  python main.py --topics 5   # Generate for top 5 topics (default: 3)
  python main.py --verbose    # Enable debug logging
"""

import sys
import logging
import argparse
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import box
from rich.text import Text

load_dotenv()

from src.fetcher import fetch_all_topics
from src.scorer import pick_top_topics
from src.generator import get_client, generate_x_thread, generate_substack_post
from src.publisher import save_x_thread, save_substack_post, post_x_thread
from src.models import TrendingTopic, XThread, SubstackPost

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s", stream=sys.stderr)
logger = logging.getLogger(__name__)

console = Console()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="viral-finance",
        description="Auto-generate viral X threads and Substack posts from trending finance topics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                # Generate content and save to output/ (no X posting)
  python main.py --post         # Generate + auto-post to X (requires X API keys)
  python main.py --dry-run      # Generate and save content, but skip X posting
  python main.py --topics 5     # Generate for top 5 topics instead of 3
  python main.py --verbose      # Show debug logging
        """,
    )
    parser.add_argument("--post", action="store_true", default=False,
                        help="Auto-post generated threads to X (requires X API credentials in .env)")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Generate and save content to files, but skip posting to X")
    parser.add_argument("--topics", type=int, default=3, metavar="N",
                        help="Number of top topics to generate content for (default: 3)")
    parser.add_argument("--verbose", action="store_true", default=False,
                        help="Enable verbose debug logging")
    return parser.parse_args()


def print_banner():
    console.print(Panel(
        Text.assemble(
            ("Viral Finance Content Machine\n", "bold cyan"),
            ("Auto-generates viral X threads + Substack posts\n", "dim"),
            (f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "dim"),
        ),
        box=box.DOUBLE_EDGE,
        border_style="cyan",
        padding=(1, 4),
    ))
    console.print()


def print_topics_table(topics: list[TrendingTopic], title: str = "Top Trending Finance Topics"):
    table = Table(title=title, box=box.ROUNDED, border_style="blue", show_lines=True)
    table.add_column("#", style="bold", width=3, justify="right")
    table.add_column("Topic", style="white", max_width=48)
    table.add_column("Source", style="cyan", width=22)
    table.add_column("Virality", style="green", width=12, justify="right")

    for i, topic in enumerate(topics, 1):
        bar_len = int(topic.virality_score / 10)
        bar = "█" * bar_len + "░" * (10 - bar_len)
        table.add_row(
            str(i),
            topic.title[:48],
            topic.source[:22],
            f"{bar} {topic.virality_score:.0f}",
        )

    console.print(table)
    console.print()


def run_with_spinner(description: str, fn, *args, **kwargs):
    """Run a function while showing a Rich spinner. Returns the result."""
    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  console=console, transient=True) as progress:
        progress.add_task(description, total=None)
        return fn(*args, **kwargs)


def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    print_banner()

    # ── Phase 1: Fetch ─────────────────────────────────────────────
    console.print("[bold]Phase 1:[/bold] Fetching trending finance topics...\n")

    try:
        all_topics = run_with_spinner("  Scanning sources...", fetch_all_topics)
    except Exception as e:
        console.print(f"[bold red]Fatal error during fetch:[/bold red] {e}")
        sys.exit(1)

    if not all_topics:
        console.print("[bold red]No topics fetched. Check your internet connection.[/bold red]")
        sys.exit(1)

    console.print(f"[green]Found {len(all_topics)} topics across all sources[/green]\n")

    # ── Phase 2: Score & rank ───────────────────────────────────────
    console.print("[bold]Phase 2:[/bold] Scoring and ranking topics...\n")
    top_topics = pick_top_topics(all_topics, n=args.topics)
    print_topics_table(top_topics)

    # ── Phase 3: Generate ───────────────────────────────────────────
    console.print("[bold]Phase 3:[/bold] Generating content with Claude...\n")

    try:
        claude_client = get_client()
    except EnvironmentError as e:
        console.print(f"[bold red]Configuration error:[/bold red] {e}")
        sys.exit(1)

    generated_threads: list[XThread] = []
    generated_posts: list[SubstackPost] = []

    for i, topic in enumerate(top_topics, 1):
        console.print(f"[bold]Topic {i}/{len(top_topics)}:[/bold] {topic.title[:60]}...")

        try:
            thread = run_with_spinner("  Generating X thread...", generate_x_thread, topic, claude_client)
            generated_threads.append(thread)
            console.print(f"  [green]X thread ready ({thread.tweet_count} tweets)[/green]")
        except Exception as e:
            console.print(f"  [red]X thread failed:[/red] {e}")

        try:
            post = run_with_spinner("  Generating Substack post...", generate_substack_post, topic, claude_client)
            generated_posts.append(post)
            console.print(f"  [green]Substack post ready ({post.word_count} words)[/green]")
        except Exception as e:
            console.print(f"  [red]Substack post failed:[/red] {e}")

        console.print()

    # ── Phase 4: Save files ─────────────────────────────────────────
    console.print("[bold]Phase 4:[/bold] Saving output files...\n")

    saved_thread_paths = []
    saved_post_paths = []

    for thread in generated_threads:
        try:
            path = save_x_thread(thread)
            saved_thread_paths.append(path)
            console.print(f"  [green]Thread saved:[/green] {path}")
        except Exception as e:
            console.print(f"  [red]Save failed:[/red] {e}")

    for post in generated_posts:
        try:
            path = save_substack_post(post)
            saved_post_paths.append(path)
            console.print(f"  [green]Newsletter saved:[/green] {path}")
        except Exception as e:
            console.print(f"  [red]Save failed:[/red] {e}")

    console.print()

    # ── Phase 5: Post to X (optional) ──────────────────────────────
    if args.post and not args.dry_run and generated_threads:
        console.print("[bold]Phase 5:[/bold] Posting to X...\n")

        for thread in generated_threads:
            console.print(f"  Posting: {thread.topic.title[:50]}...")
            try:
                post_ids = post_x_thread(thread)
                first_id = post_ids[0]
                console.print(f"  [green]Posted![/green] https://twitter.com/i/web/status/{first_id}")
            except EnvironmentError as e:
                console.print(f"  [bold red]Credential error:[/bold red] {e}")
                break
            except RuntimeError as e:
                console.print(f"  [red]Post failed:[/red] {e}")

        console.print()

    # ── Summary ─────────────────────────────────────────────────────
    table = Table(title="Run Complete", box=box.SIMPLE_HEAVY, border_style="green")
    table.add_column("", style="bold")
    table.add_column("", style="green")

    table.add_row("Topics fetched", str(len(all_topics)))
    table.add_row("Topics selected", str(len(top_topics)))
    table.add_row("X threads generated", str(len(generated_threads)))
    table.add_row("Substack posts generated", str(len(generated_posts)))
    table.add_row("Files saved", str(len(saved_thread_paths) + len(saved_post_paths)))
    if args.post:
        table.add_row("Threads posted to X", str(sum(1 for t in generated_threads if t.posted)))

    console.print(table)
    console.print()
    console.print(
        "[bold green]Done![/bold green] Check "
        "[cyan]output/threads/[/cyan] and [cyan]output/newsletters/[/cyan] for your content."
    )

    if not args.post or args.dry_run:
        console.print("\n[dim]To auto-post threads to X, add your X API keys to .env and run with [cyan]--post[/cyan][/dim]")


if __name__ == "__main__":
    main()
