import os
import re
import time
import logging
import anthropic
from .models import TrendingTopic, XThread, SubstackPost

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-4-6"
X_THREAD_MAX_TOKENS = 2000
SUBSTACK_MAX_TOKENS = 4000


def get_client() -> anthropic.Anthropic:
    """Initialize the Anthropic client. Raises EnvironmentError if API key is missing."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return anthropic.Anthropic(api_key=api_key)


def generate_x_thread(topic: TrendingTopic, client: anthropic.Anthropic) -> XThread:
    """
    Generates a viral X thread for the given trending topic.
    Retries once on API or parsing error.
    """
    prompt = _build_x_thread_prompt(topic)

    for attempt in range(2):
        try:
            message = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=X_THREAD_MAX_TOKENS,
                system=_x_thread_system_prompt(),
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = message.content[0].text
            tweets = _parse_x_thread_response(raw_text)
            tweets = [_safe_truncate(t, 280) for t in tweets]

            if len(tweets) < 5:
                raise ValueError(f"Only {len(tweets)} tweets generated, expected at least 5")

            return XThread(topic=topic, tweets=tweets)

        except anthropic.APIStatusError as e:
            if attempt == 0:
                logger.warning(f"Claude API error, retrying in 5s: {e}")
                time.sleep(5)
            else:
                raise RuntimeError(f"Claude API failed after 2 attempts: {e}") from e
        except ValueError as e:
            if attempt == 0:
                logger.warning(f"Thread parsing failed, retrying: {e}")
                time.sleep(2)
            else:
                raise


def generate_substack_post(topic: TrendingTopic, client: anthropic.Anthropic) -> SubstackPost:
    """
    Generates a Substack newsletter post for the given trending topic.
    Retries once on API or parsing error.
    """
    prompt = _build_substack_prompt(topic)

    for attempt in range(2):
        try:
            message = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=SUBSTACK_MAX_TOKENS,
                system=_substack_system_prompt(),
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = message.content[0].text
            title, body = _parse_substack_response(raw_text)

            word_count = len(body.split())
            if word_count < 400:
                raise ValueError(f"Post too short: {word_count} words")

            return SubstackPost(topic=topic, title=title, body=body, word_count=word_count)

        except anthropic.APIStatusError as e:
            if attempt == 0:
                logger.warning(f"Claude API error, retrying in 5s: {e}")
                time.sleep(5)
            else:
                raise RuntimeError(f"Claude API failed after 2 attempts: {e}") from e
        except ValueError as e:
            if attempt == 0:
                logger.warning(f"Post parsing failed, retrying: {e}")
                time.sleep(2)
            else:
                raise


def _x_thread_system_prompt() -> str:
    return """You are a top-tier finance content creator with 500K+ followers on X (formerly Twitter).
You specialize in making complex financial topics go viral by making them feel urgent, personal, and surprising.

Your writing rules:
- Write like a person, not a press release. Short sentences. Punchy. Direct.
- Every tweet must create curiosity or deliver value on its own.
- Use numbers and specific data points whenever possible — they drive engagement.
- The hook tweet is everything — it must stop the scroll.
- Never use jargon without immediately explaining it.
- Each tweet ends with either a new idea OR a reason to read the next tweet.
- You NEVER use hashtags (they look spammy and reduce reach).
- You NEVER use em-dashes (—). Use a period or a new sentence instead.
- Contrarian takes ("Everyone is wrong about X") outperform neutral takes.
- Format numbers for impact: "$2.3 trillion" not "2300000000000"."""


def _build_x_thread_prompt(topic: TrendingTopic) -> str:
    return f"""Generate a viral X (Twitter) thread about this trending finance topic:

TOPIC: {topic.title}
SOURCE: {topic.source}
CONTEXT: {topic.summary if topic.summary else "No additional context available."}
VIRALITY SCORE: {topic.virality_score:.1f}/100

THREAD STRUCTURE:

TWEET 1 (Hook - CRITICAL):
Bold claim, surprising stat, or counterintuitive insight. Must stop scrolling.
Use one of these proven formulas:
- "I [discovered/analyzed] X and what I found will change how you think about [Y]:"
- "The [institution/media] doesn't want you to know this about [topic]:"
- "[Surprising stat]% of people [behavior]. Here's what the smart money does instead:"
Max 240 characters. End with a colon or dash to tease the thread.

TWEET 2 through TWEET 10 (Body):
Each tweet delivers ONE clear insight, fact, or action step.
Include a specific number or data point in each.
Start each with its number: "2/" "3/" etc.
180-260 characters each.

TWEET 11 (CTA):
Summarize the key takeaway in one line.
Ask a question OR tell them to follow for more daily finance insights.
Max 240 characters.

OUTPUT FORMAT - Return ONLY tweets with "TWEET N:" prefix, one per line:
TWEET 1: [hook text]
TWEET 2: [body tweet text]
...
TWEET 11: [CTA text]

Generate between 10 and 12 tweets total."""


def _substack_system_prompt() -> str:
    return """You are a successful Substack finance writer whose newsletters get 50%+ open rates.
Your writing style: authoritative but accessible, analytical but never dry, always actionable.

Rules:
- Readers are smart but not finance experts. Meet them where they are.
- Open with a hook that makes this feel urgent and personally relevant.
- Every section ends with a "so what" connecting to the reader's life.
- Include 2-3 specific numbers, stats, or data points per section.
- Write in second person ("you") for intimacy.
- Paragraphs are max 3 sentences. No walls of text.
- Use ## for section headings.
- Use bullet points sparingly — max one list per post, max 5 items."""


def _build_substack_prompt(topic: TrendingTopic) -> str:
    return f"""Write a Substack newsletter post about this trending finance topic:

TOPIC: {topic.title}
SOURCE: {topic.source}
CONTEXT: {topic.summary if topic.summary else "No additional context available."}
VIRALITY SCORE: {topic.virality_score:.1f}/100

REQUIRED STRUCTURE:

TITLE (on its own line, prefixed with "TITLE:"):
Punchy, specific, 50-80 characters.
Formula examples:
- "Why [X] Is About to Change [Y] Forever"
- "The [X] Strategy That [Made/Saved] People $[N]"
- "What [Entity]'s [Action] Means For Your Money"

BODY (600-900 words, in markdown):

[Opening — no heading, just 2-3 punchy paragraphs]
Start with a scene, surprising stat, or question that makes this feel personal.
Introduce the topic and why it's exploding right now.

## [Section 1: The Context]
What's actually happening? Factual grounding with specific numbers, dates, actors.

## [Section 2: Why It Matters To You]
Connect the macro event to the reader's personal finances.
Be direct: "If you have [X], here's what this means."

## [Section 3: What To Do]
2-3 concrete, specific actions. Not "diversify." Instead: specific allocations, tools, timing.

## The Bottom Line
3-4 sentences summarizing the key insight. End with a memorable one-liner.

## Join the Conversation
2-3 sentences asking readers to reply, share, or follow for weekly analysis.

OUTPUT FORMAT:
TITLE: [your title here]
[full markdown body starting on next line]"""


def _parse_x_thread_response(raw_text: str) -> list[str]:
    """Parse Claude response into list of tweet strings."""
    pattern = re.compile(r"^TWEET\s+\d+:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
    matches = pattern.findall(raw_text)

    if len(matches) >= 5:
        return [m.strip() for m in matches if m.strip()]

    # Fallback: split by lines and filter
    lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
    tweets = [
        line for line in lines
        if not re.match(r"^(TWEET|tweet|Tweet)\s*\d+:?\s*$", line)
        and len(line) > 20
    ]
    return tweets


def _parse_substack_response(raw_text: str) -> tuple[str, str]:
    """Parse Claude response into (title, body) tuple."""
    lines = raw_text.strip().split("\n")
    title = ""
    body_start_idx = 0

    for i, line in enumerate(lines):
        if line.strip().upper().startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()
            body_start_idx = i + 1
            break

    if not title:
        for i, line in enumerate(lines):
            if line.strip():
                title = line.strip().lstrip("#").strip()
                body_start_idx = i + 1
                break

    body = "\n".join(lines[body_start_idx:]).strip()
    return title, body


def _safe_truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len chars at a word boundary."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len - 3].rsplit(" ", 1)[0]
    return truncated + "..."
