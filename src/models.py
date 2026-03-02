from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TrendingTopic:
    """A single trending finance topic discovered from any source."""
    title: str
    source: str          # "reddit", "hackernews", "rss_*", "google_trends"
    score: float = 0.0   # Raw engagement score from source
    virality_score: float = 0.0  # Computed virality score (0.0 to 100.0)
    url: str = ""
    summary: str = ""
    fetched_at: datetime = field(default_factory=datetime.now)

    def __repr__(self):
        return f"TrendingTopic(title={self.title!r}, source={self.source}, virality={self.virality_score:.1f})"


@dataclass
class XThread:
    """A fully-generated X (Twitter) thread. tweets[0] is the hook, tweets[-1] is the CTA."""
    topic: TrendingTopic
    tweets: list[str]
    generated_at: datetime = field(default_factory=datetime.now)
    posted: bool = False
    post_ids: list[str] = field(default_factory=list)

    @property
    def hook(self) -> str:
        return self.tweets[0] if self.tweets else ""

    @property
    def cta(self) -> str:
        return self.tweets[-1] if len(self.tweets) > 1 else ""

    @property
    def body_tweets(self) -> list[str]:
        return self.tweets[1:-1] if len(self.tweets) > 2 else []

    @property
    def tweet_count(self) -> int:
        return len(self.tweets)


@dataclass
class SubstackPost:
    """A fully-generated Substack newsletter post."""
    topic: TrendingTopic
    title: str
    body: str            # Full markdown body
    word_count: int = 0
    generated_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        if self.body and self.word_count == 0:
            self.word_count = len(self.body.split())
