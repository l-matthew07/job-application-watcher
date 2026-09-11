"""Public-sentiment scraper for company mentions on X (Twitter).

Pulls recent public posts matching per-company queries through the X API v2
recent-search endpoint, filters out spam/irrelevant hits, scores each post for
sentiment, and rolls the results up per company.

See ``twitter_sentiment.cli`` for the entrypoint.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
