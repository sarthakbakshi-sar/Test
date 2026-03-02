import math
from datetime import datetime
from .models import TrendingTopic

SOURCE_WEIGHTS = {
    "reddit_r_wallstreetbets":        2.5,
    "reddit_r_investing":             2.0,
    "reddit_r_stocks":                2.0,
    "reddit_r_personalfinance":       1.8,
    "reddit_r_financialindependence": 1.6,
    "reddit_r_economics":             1.5,
    "hackernews":                     2.2,
    "google_trends":                  3.0,
    "rss_marketwatch":                1.2,
    "rss_cnbc":                       1.2,
    "rss_reuters_business":           1.1,
    "rss_yahoo_finance":              1.0,
    "rss_investing_com":              1.0,
}

DEFAULT_SOURCE_WEIGHT = 1.0


def score_topics(topics: list[TrendingTopic]) -> list[TrendingTopic]:
    """
    Assigns a virality_score (0-100) to each topic.
    Formula: source_weight * log_normalized_score * recency_bonus
    Returns topics sorted highest virality first.
    """
    if not topics:
        return []

    max_raw = max(t.score for t in topics)
    max_raw = max(max_raw, 1.0)

    for topic in topics:
        weight = SOURCE_WEIGHTS.get(topic.source, DEFAULT_SOURCE_WEIGHT)

        log_score = math.log10(topic.score + 1)
        log_max = math.log10(max_raw + 1)
        normalized = (log_score / log_max) * 10.0 if log_max > 0 else 0.0

        age_seconds = (datetime.now() - topic.fetched_at).total_seconds()
        if age_seconds < 3600:
            recency = 1.5
        elif age_seconds < 21600:
            recency = 1.2
        else:
            recency = 1.0

        topic.virality_score = round(weight * normalized * recency, 2)

    return sorted(topics, key=lambda t: t.virality_score, reverse=True)


def pick_top_topics(topics: list[TrendingTopic], n: int = 3) -> list[TrendingTopic]:
    """
    Returns top N topics after scoring, enforcing source diversity
    (max 2 topics from the same source family).
    """
    scored = score_topics(topics)

    selected = []
    source_family_counts: dict[str, int] = {}

    for topic in scored:
        family = topic.source.split("_")[0]
        count = source_family_counts.get(family, 0)

        if count < 2:
            selected.append(topic)
            source_family_counts[family] = count + 1

        if len(selected) >= n:
            break

    # If diversity constraint left us short, fill from remaining
    if len(selected) < n:
        remaining = [t for t in scored if t not in selected]
        selected.extend(remaining[:n - len(selected)])

    return selected[:n]
