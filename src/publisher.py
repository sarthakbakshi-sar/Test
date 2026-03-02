import os
import re
import time
import logging
import tweepy
from datetime import datetime
from pathlib import Path
from .models import XThread, SubstackPost

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("output")
THREADS_DIR = OUTPUT_DIR / "threads"
NEWSLETTERS_DIR = OUTPUT_DIR / "newsletters"


def save_x_thread(thread: XThread) -> Path:
    """Save the X thread to a timestamped markdown file. Returns the file path."""
    THREADS_DIR.mkdir(parents=True, exist_ok=True)

    slug = _make_slug(thread.topic.title)
    timestamp = thread.generated_at.strftime("%Y%m%d_%H%M%S")
    filepath = THREADS_DIR / f"{timestamp}_{slug}.md"
    filepath.write_text(_format_thread_markdown(thread), encoding="utf-8")

    logger.info(f"Thread saved: {filepath}")
    return filepath


def save_substack_post(post: SubstackPost) -> Path:
    """Save the Substack post to a timestamped markdown file. Returns the file path."""
    NEWSLETTERS_DIR.mkdir(parents=True, exist_ok=True)

    slug = _make_slug(post.title)
    timestamp = post.generated_at.strftime("%Y%m%d_%H%M%S")
    filepath = NEWSLETTERS_DIR / f"{timestamp}_{slug}.md"
    filepath.write_text(_format_newsletter_markdown(post), encoding="utf-8")

    logger.info(f"Newsletter saved: {filepath}")
    return filepath


def post_x_thread(thread: XThread) -> list[str]:
    """
    Post the X thread to Twitter/X via Tweepy API v2.
    Each tweet is posted as a reply to the previous one to form a chain.
    Returns list of posted tweet IDs.
    Raises EnvironmentError if credentials are missing.
    Raises RuntimeError if any tweet fails.
    """
    required = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise EnvironmentError(
            f"Missing X API credentials: {', '.join(missing)}. "
            "Add them to your .env file. See .env.example for details."
        )

    client = tweepy.Client(
        consumer_key=os.getenv("X_API_KEY"),
        consumer_secret=os.getenv("X_API_SECRET"),
        access_token=os.getenv("X_ACCESS_TOKEN"),
        access_token_secret=os.getenv("X_ACCESS_TOKEN_SECRET"),
        bearer_token=os.getenv("X_BEARER_TOKEN"),
    )

    posted_ids = []
    previous_tweet_id = None

    for i, tweet_text in enumerate(thread.tweets):
        try:
            kwargs = {"text": tweet_text}
            if previous_tweet_id:
                kwargs["in_reply_to_tweet_id"] = previous_tweet_id

            response = client.create_tweet(**kwargs)
            tweet_id = str(response.data["id"])
            posted_ids.append(tweet_id)
            previous_tweet_id = tweet_id

            logger.info(f"Posted tweet {i + 1}/{len(thread.tweets)}: {tweet_id}")

            # Sleep 2s between tweets to respect rate limits (17 tweets/15min on Basic tier)
            if i < len(thread.tweets) - 1:
                time.sleep(2)

        except tweepy.TweepyException as e:
            error_msg = f"Failed to post tweet {i + 1}: {e}"
            logger.error(error_msg)
            if posted_ids:
                logger.warning(f"Partial thread: {len(posted_ids)}/{len(thread.tweets)} tweets posted")
            raise RuntimeError(error_msg) from e

    thread.posted = True
    thread.post_ids = posted_ids
    return posted_ids


def _format_thread_markdown(thread: XThread) -> str:
    """Format XThread as a readable markdown file."""
    lines = [
        f"# X Thread: {thread.topic.title}",
        f"",
        f"**Generated:** {thread.generated_at.strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Source:** {thread.topic.source}  ",
        f"**Virality Score:** {thread.topic.virality_score:.1f}/100  ",
        f"**Tweets:** {thread.tweet_count}  ",
        f"",
        f"---",
        f"",
        f"## Thread",
        f"",
    ]

    for i, tweet in enumerate(thread.tweets, 1):
        if i == 1:
            label = "Hook"
        elif i == len(thread.tweets):
            label = "CTA"
        else:
            label = f"Tweet {i}"
        char_note = f"({len(tweet)} chars)"
        lines += [f"### {label} {char_note}", f"", tweet, f"", "---", ""]

    if thread.posted and thread.post_ids:
        first_id = thread.post_ids[0]
        lines += [
            f"## Post Status",
            f"",
            f"**Posted:** Yes  ",
            f"**Thread URL:** https://twitter.com/i/web/status/{first_id}  ",
            f"",
        ]

    return "\n".join(lines)


def _format_newsletter_markdown(post: SubstackPost) -> str:
    """Format SubstackPost as a clean markdown file ready for copy-paste into Substack."""
    lines = [
        f"---",
        f'title: "{post.title}"',
        f"generated: {post.generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"source: {post.topic.source}",
        f"virality_score: {post.topic.virality_score:.1f}",
        f"word_count: {post.word_count}",
        f"---",
        f"",
        f"# {post.title}",
        f"",
        post.body,
    ]
    return "\n".join(lines)


def _make_slug(text: str, max_len: int = 40) -> str:
    """Convert a title into a URL-safe filename slug."""
    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_len]
