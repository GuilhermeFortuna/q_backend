"""News RSS service unit tests (WO60)."""

from __future__ import annotations

import base64

import pytest

from q_backend.api.services import news as news_service

SAMPLE_FEED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Older Article</title>
      <link>https://example.com/older</link>
      <description>Older summary</description>
      <pubDate>Wed, 03 Jan 2024 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Newer Article</title>
      <link>https://example.com/newer</link>
      <description><![CDATA[<img src="https://example.com/img.jpg" />Newer summary]]></description>
      <pubDate>Thu, 04 Jan 2024 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

SECOND_FEED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>CNBC Headline</title>
      <link>https://www.cnbc.com/article-1</link>
      <description>CNBC summary</description>
      <pubDate>Fri, 05 Jan 2024 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def _feed_fetch_map(*feeds: tuple[str, bytes]):
    mapping = dict(feeds)

    def fetch(url: str, timeout: float) -> bytes:
        if url in mapping:
            return mapping[url]
        if url.startswith("https://example.com/") or url.startswith("https://www.cnbc.com/"):
            return b"<html><head><title>Article Title</title></head><body><p>Full article body content that is long enough to pass the generic paragraph filter comfortably for testing purposes here.</p></body></html>"
        raise KeyError(url)

    return fetch


def test_latest_articles_parses_sorts_caps_and_builds_base64_ids():
    feed_url = news_service.NEWS_FEEDS[0]["url"]
    articles = news_service.latest_articles(
        limit=25,
        fetch=_feed_fetch_map((feed_url, SAMPLE_FEED_XML)),
    )

    assert len(articles) == 2
    assert articles[0]["title"] == "Newer Article"
    assert articles[1]["title"] == "Older Article"
    assert articles[0]["id"] == base64.urlsafe_b64encode(b"https://example.com/newer").decode("utf-8")
    assert articles[0]["imageUrl"] == "https://example.com/img.jpg"


def test_latest_articles_skips_failing_feed():
    good_url = news_service.NEWS_FEEDS[0]["url"]
    bad_url = news_service.NEWS_FEEDS[1]["url"]

    def fetch(url: str, timeout: float) -> bytes:
        if url == good_url:
            return SAMPLE_FEED_XML
        if url == bad_url:
            raise OSError("network down")
        raise KeyError(url)

    articles = news_service.latest_articles(limit=25, fetch=fetch)
    assert len(articles) == 2
    assert all(article["source"] == "Valor Finanças" for article in articles)


def test_latest_articles_respects_limit():
    feed_url = news_service.NEWS_FEEDS[0]["url"]
    articles = news_service.latest_articles(
        limit=1,
        fetch=_feed_fetch_map((feed_url, SAMPLE_FEED_XML)),
    )
    assert len(articles) == 1
    assert articles[0]["title"] == "Newer Article"


def test_get_article_round_trips_article_id():
    link = "https://example.com/newer"
    article_id = base64.urlsafe_b64encode(link.encode("utf-8")).decode("utf-8")
    feed_url = news_service.NEWS_FEEDS[0]["url"]

    article = news_service.get_article(
        article_id,
        fetch=_feed_fetch_map((feed_url, SAMPLE_FEED_XML)),
    )

    assert article["id"] == article_id
    assert article["title"] == "Newer Article"
    assert article["summary"] == "Newer summary"
    assert "Full article body content" in article["content"]


def test_get_article_rejects_invalid_id():
    with pytest.raises(ValueError, match="Invalid article ID format"):
        news_service.get_article("not-valid-base64!!!")


def test_get_article_uses_cnbc_source_name_for_non_valor_links():
    link = "https://www.cnbc.com/article-1"
    article_id = base64.urlsafe_b64encode(link.encode("utf-8")).decode("utf-8")
    cnbc_feed = news_service.NEWS_FEEDS[3]["url"]

    article = news_service.get_article(
        article_id,
        fetch=_feed_fetch_map((cnbc_feed, SECOND_FEED_XML)),
    )

    assert article["source"] == "CNBC"
    assert article["title"] == "CNBC Headline"
