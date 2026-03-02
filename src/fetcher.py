import os
import time
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from .models import TrendingTopic

logger = logging.getLogger(__name__)

RSS_FEEDS = {
    "MarketWatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "Reuters_Business": "https://feeds.reuters.com/reuters/businessNews",
    "Yahoo_Finance": "https://finance.yahoo.com/news/rssindex",
    "Investing_com": "https://www.investing.com/rss/news.rss",
}

REDDIT_SUBREDDITS = [
    "investing",
    "personalfinance",
    "wallstreetbets",
    "stocks",
    "financialindependence",
    "Economics",
]

HN_API_URL = "https://hn.algolia.com/api/v1/search"

GOOGLE_TRENDS_KEYWORDS = [
    "stock market",
    "inflation",
    "interest rates",
    "cryptocurrency",
    "personal finance",
]

FINANCE_KEYWORDS = {
    "stock", "market", "invest", "finance", "money", "bank", "crypto",
    "bitcoin", "economy", "inflation", "interest rate", "fed", "federal",
    "nasdaq", "s&p", "dow", "etf", "bond", "yield", "earnings", "ipo",
    "recession", "gdp", "debt", "budget", "tax", "wealth", "portfolio",
    "hedge", "fund", "equity", "dividend", "mortgage", "loan", "credit",
    "retirement", "401k", "ira", "trading", "forex", "commodity", "oil",
    "gold", "silver", "dollar", "euro", "rate", "fiscal", "monetary",
}


def fetch_all_topics(console=None) -> list[TrendingTopic]:
    """
    Main entry point. Calls all fetchers and merges results.
    Each fetcher is isolated — failures are logged and skipped.
    Returns a deduplicated list of TrendingTopic objects.
    """
    all_topics: list[TrendingTopic] = []

    fetchers = [
        ("RSS Feeds", _fetch_rss_topics),
        ("HackerNews", _fetch_hackernews_topics),
        ("Reddit", _fetch_reddit_topics),
        ("Google Trends", _fetch_google_trends_topics),
    ]

    for name, fetcher_fn in fetchers:
        try:
            if console:
                console.print(f"  [dim]Fetching from {name}...[/dim]")
            topics = fetcher_fn()
            all_topics.extend(topics)
            if console:
                console.print(f"  [green]Got {len(topics)} topics from {name}[/green]")
        except Exception as e:
            logger.warning(f"Fetcher '{name}' failed: {e}")
            if console:
                console.print(f"  [yellow]Warning: {name} unavailable ({e})[/yellow]")

    # Deduplicate by first 60 chars of lowercased title
    seen: set[str] = set()
    unique: list[TrendingTopic] = []
    for topic in all_topics:
        key = topic.title.lower().strip()[:60]
        if key not in seen:
            seen.add(key)
            unique.append(topic)

    return unique


def _fetch_rss_topics() -> list[TrendingTopic]:
    """Parse RSS feeds from major finance news sources using requests + stdlib XML."""
    topics = []
    headers = {"User-Agent": "ViralFinanceBot/1.0 RSS Reader"}

    for source_name, feed_url in RSS_FEEDS.items():
        try:
            response = requests.get(feed_url, headers=headers, timeout=10)
            response.raise_for_status()
            root = ET.fromstring(response.content)

            # Handle both RSS 2.0 (<channel><item>) and Atom (<entry>) formats
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = root.findall(".//item") or root.findall(".//atom:entry", ns)

            for item in items[:5]:
                # RSS 2.0 fields
                title_el = item.find("title") or item.find("atom:title", ns)
                link_el = item.find("link") or item.find("atom:link", ns)
                desc_el = (item.find("description") or item.find("summary")
                           or item.find("atom:summary", ns))

                title = (title_el.text or "").strip() if title_el is not None else ""
                summary = (desc_el.text or "")[:300].strip() if desc_el is not None else ""

                if link_el is not None:
                    link = link_el.get("href") or link_el.text or ""
                else:
                    link = ""

                if not title or not _is_finance_related(title + " " + summary):
                    continue

                topics.append(TrendingTopic(
                    title=title,
                    source=f"rss_{source_name.lower()}",
                    score=1.0,
                    url=link.strip(),
                    summary=summary,
                ))
        except Exception as e:
            logger.debug(f"RSS feed {source_name} failed: {e}")
            continue

    return topics


def _fetch_hackernews_topics() -> list[TrendingTopic]:
    """Query HN Algolia API for finance posts from the last 48 hours."""
    topics = []
    try:
        params = {
            "query": "finance money investing stock market",
            "tags": "story",
            "numericFilters": f"points>50,created_at_i>{int(time.time()) - 172800}",
            "hitsPerPage": 20,
        }
        response = requests.get(HN_API_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        for hit in data.get("hits", []):
            title = hit.get("title", "").strip()
            points = hit.get("points", 0)
            url = hit.get("url", "") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"

            if not title or not _is_finance_related(title):
                continue

            topics.append(TrendingTopic(
                title=title,
                source="hackernews",
                score=float(points),
                url=url,
                summary=f"HackerNews discussion with {points} upvotes",
            ))
    except requests.RequestException as e:
        raise RuntimeError(f"HackerNews API request failed: {e}") from e

    return topics


def _fetch_reddit_topics() -> list[TrendingTopic]:
    """
    Fetch hot posts from finance subreddits.
    Uses PRAW if credentials are present, falls back to public JSON API.
    """
    client_id = os.getenv("REDDIT_CLIENT_ID")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET")

    if client_id and client_secret:
        return _fetch_reddit_with_praw(client_id, client_secret)
    else:
        return _fetch_reddit_public_json()


def _fetch_reddit_with_praw(client_id: str, client_secret: str) -> list[TrendingTopic]:
    """Fetch Reddit posts using PRAW (authenticated)."""
    import praw
    reddit = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent="ViralFinanceBot/1.0",
    )
    topics = []
    for sub_name in REDDIT_SUBREDDITS:
        try:
            subreddit = reddit.subreddit(sub_name)
            for post in subreddit.hot(limit=5):
                if post.stickied:
                    continue
                selftext = (post.selftext or "")[:300].strip()
                topics.append(TrendingTopic(
                    title=post.title.strip(),
                    source=f"reddit_r_{sub_name.lower()}",
                    score=float(post.score),
                    url=f"https://reddit.com{post.permalink}",
                    summary=selftext or f"r/{sub_name} post with {post.score} upvotes",
                ))
        except Exception as e:
            logger.debug(f"Failed fetching r/{sub_name}: {e}")
            continue
    return topics


def _fetch_reddit_public_json() -> list[TrendingTopic]:
    """Fetch Reddit hot posts using public JSON API (no credentials needed)."""
    topics = []
    headers = {"User-Agent": "ViralFinanceBot/1.0"}
    for sub_name in REDDIT_SUBREDDITS[:3]:  # Limit to 3 to avoid rate limiting
        try:
            url = f"https://www.reddit.com/r/{sub_name}/hot.json?limit=5"
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()

            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                if post.get("stickied"):
                    continue
                title = post.get("title", "").strip()
                score = float(post.get("score", 0))
                permalink = post.get("permalink", "")
                selftext = (post.get("selftext", "") or "")[:300].strip()

                if not title:
                    continue

                topics.append(TrendingTopic(
                    title=title,
                    source=f"reddit_r_{sub_name.lower()}",
                    score=score,
                    url=f"https://reddit.com{permalink}",
                    summary=selftext or f"r/{sub_name} post with {int(score)} upvotes",
                ))
            time.sleep(1)  # Respect Reddit rate limits
        except Exception as e:
            logger.debug(f"Reddit public JSON failed for r/{sub_name}: {e}")
            continue
    return topics


def _fetch_google_trends_topics() -> list[TrendingTopic]:
    """
    Use pytrends to get rising finance-related search queries.
    Has aggressive rate limits — sleeps 1s between requests and limits to 3 keywords.
    """
    try:
        from pytrends.request import TrendReq
    except ImportError:
        raise RuntimeError("pytrends not installed")

    topics = []
    pytrends = TrendReq(hl="en-US", tz=360, timeout=(10, 25))

    for keyword in GOOGLE_TRENDS_KEYWORDS[:3]:
        try:
            pytrends.build_payload([keyword], timeframe="now 1-d", geo="US")
            related = pytrends.related_queries()

            if keyword not in related or related[keyword] is None:
                time.sleep(1)
                continue

            rising_df = related[keyword].get("rising")
            if rising_df is None or rising_df.empty:
                time.sleep(1)
                continue

            for _, row in rising_df.head(3).iterrows():
                query_text = str(row.get("query", "")).strip()
                value = float(row.get("value", 0))

                if not query_text or not _is_finance_related(query_text):
                    continue

                topics.append(TrendingTopic(
                    title=f"{query_text} (trending on Google)",
                    source="google_trends",
                    score=value,
                    url=f"https://trends.google.com/trends/explore?q={query_text.replace(' ', '+')}",
                    summary=f"Rising Google search trend related to '{keyword}', interest score: {value}",
                ))

            time.sleep(1)
        except Exception as e:
            logger.debug(f"Google Trends failed for '{keyword}': {e}")
            time.sleep(1)
            continue

    return topics


def _is_finance_related(text: str) -> bool:
    """Check if text contains finance-related keywords."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in FINANCE_KEYWORDS)
