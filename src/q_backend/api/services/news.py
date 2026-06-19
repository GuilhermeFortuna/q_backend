"""RSS news aggregation with an injectable HTTP fetch seam for tests."""

from __future__ import annotations

import base64
import email.utils
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

FetchFn = Callable[[str, float], bytes]

NEWS_FEEDS = (
    {"url": "https://valor.globo.com/rss/valor/financas/", "source": "Valor Finanças"},
    {"url": "https://valor.globo.com/rss/valor/empresas/", "source": "Valor Empresas"},
    {"url": "https://valor.globo.com/rss/valor/agronegocios/", "source": "Valor Agro"},
    {"url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "source": "CNBC"},
)

FEED_LIST_TIMEOUT = 3.0
FEED_DETAIL_TIMEOUT = 4.0
ARTICLE_SCRAPE_TIMEOUT = 6.0


def _default_fetch(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _article_id_for_link(link: str) -> str:
    return base64.urlsafe_b64encode(link.encode("utf-8")).decode("utf-8")


def _decode_article_id(article_id: str) -> str:
    return base64.urlsafe_b64decode(article_id.encode("utf-8")).decode("utf-8")


def _clean_description(description: str) -> tuple[str, str | None]:
    image_url = None
    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description)
    if img_match:
        image_url = img_match.group(1)

    clean_description = re.sub(r"<img[^>]+>", "", description)
    clean_description = re.sub(r"<br\s*/?>", "", clean_description).strip()
    return clean_description, image_url


def _parse_pub_date(pub_date_str: str) -> str:
    try:
        return email.utils.parsedate_to_datetime(pub_date_str).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _parse_feed_xml(
    xml_data: bytes,
    *,
    source: str,
    seen_urls: set[str] | None = None,
) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    seen = seen_urls if seen_urls is not None else set()

    root = ET.fromstring(xml_data)
    items = root.findall(".//item")

    for item in items:
        title = item.find("title").text if item.find("title") is not None else ""
        link = item.find("link").text if item.find("link") is not None else ""
        description = (
            item.find("description").text if item.find("description") is not None else ""
        )
        pub_date_str = item.find("pubDate").text if item.find("pubDate") is not None else ""

        if not title or not link or link in seen:
            continue

        seen.add(link)
        clean_description, image_url = _clean_description(description)
        pub_date_iso = _parse_pub_date(pub_date_str)

        articles.append(
            {
                "id": _article_id_for_link(link),
                "title": title,
                "source": source,
                "publishedAt": pub_date_iso,
                "summary": clean_description,
                "content": clean_description,
                "imageUrl": image_url,
            }
        )

    return articles


def _find_feed_item_metadata(
    xml_data: bytes,
    url: str,
) -> tuple[str, str, str, str | None] | None:
    root = ET.fromstring(xml_data)
    items = root.findall(".//item")

    for item in items:
        link = item.find("link").text if item.find("link") is not None else ""
        if link != url:
            continue

        title = item.find("title").text if item.find("title") is not None else ""
        description = (
            item.find("description").text if item.find("description") is not None else ""
        )
        pub_date_str = item.find("pubDate").text if item.find("pubDate") is not None else ""

        description, image_url = _clean_description(description)
        pub_date_iso = datetime.now(timezone.utc).isoformat()
        try:
            dt = email.utils.parsedate_to_datetime(pub_date_str)
            pub_date_iso = dt.isoformat()
        except Exception:
            pass

        return title, description, pub_date_iso, image_url

    return None


def _scrape_article_content(url: str, html: str, *, is_valor: bool) -> tuple[str, str | None, str]:
    content = ""
    image_url = None
    title = ""

    if is_valor:
        paragraphs = re.findall(
            r"<p[^>]*content-text__container[^>]*>(.*?)</p>", html, re.DOTALL
        )
        clean_paragraphs = []
        for paragraph in paragraphs:
            p_clean = re.sub(r"<[^>]+>", "", paragraph).strip()
            p_clean = (
                p_clean.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
                .replace("&apos;", "'")
            )
            if len(p_clean) > 40:
                clean_paragraphs.append(p_clean)
        if clean_paragraphs:
            content = "\n\n".join(clean_paragraphs)

    if not content:
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL)
        clean_paragraphs = []
        bad_words = [
            "livestream",
            "sign in",
            "create free account",
            "watch live",
            "privacy policy",
            "terms of service",
            "all rights reserved",
            "cnbc.com",
            "subscribe to",
            "inscreva-se",
            "todos os direitos reservados",
            "leia mais",
        ]

        for paragraph in paragraphs:
            p_clean = re.sub(r"<[^>]+>", "", paragraph).strip()
            p_clean = (
                p_clean.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
                .replace("&apos;", "'")
            )

            if (
                len(p_clean) > 85
                and not any(bw in p_clean.lower() for bw in bad_words)
                and not any(
                    x in p_clean
                    for x in ["var ", "window.", "document.", "function()", "adsbygoogle"]
                )
            ):
                clean_paragraphs.append(p_clean)
        if clean_paragraphs:
            content = "\n\n".join(clean_paragraphs)

    og_img = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
    )
    if og_img:
        image_url = og_img.group(1)
    else:
        tw_img = re.search(
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
            html,
        )
        if tw_img:
            image_url = tw_img.group(1)

    if not title:
        title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE)
        if title_match:
            title = (
                title_match.group(1)
                .replace(" - CNBC", "")
                .replace(" | Valor Econômico", "")
                .strip()
            )

    return content, image_url, title


def latest_articles(limit: int = 25, *, fetch: FetchFn = _default_fetch) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for feed in NEWS_FEEDS:
        try:
            xml_data = fetch(feed["url"], FEED_LIST_TIMEOUT)
            articles.extend(
                _parse_feed_xml(xml_data, source=feed["source"], seen_urls=seen_urls)
            )
        except Exception as exc:
            logger.error("Failed to fetch news from feed %s: %s", feed["url"], exc)

    articles.sort(key=lambda item: item["publishedAt"], reverse=True)
    return articles[:limit]


def get_article(article_id: str, *, fetch: FetchFn = _default_fetch) -> dict[str, Any]:
    try:
        url = _decode_article_id(article_id)
    except Exception as exc:
        raise ValueError("Invalid article ID format.") from exc

    is_valor = "valor.globo.com" in url
    source_name = "Valor Econômico" if is_valor else "CNBC"

    title = ""
    description = ""
    pub_date_iso = datetime.now(timezone.utc).isoformat()
    image_url = None

    for feed in NEWS_FEEDS:
        try:
            xml_data = fetch(feed["url"], FEED_DETAIL_TIMEOUT)
            metadata = _find_feed_item_metadata(xml_data, url)
            if metadata is not None:
                title, description, pub_date_iso, image_url = metadata
                break
        except Exception as exc:
            logger.error("Error checking RSS metadata during detail fetch: %s", exc)

    content = ""
    try:
        html = fetch(url, ARTICLE_SCRAPE_TIMEOUT).decode("utf-8")
        scraped_content, scraped_image, scraped_title = _scrape_article_content(
            url, html, is_valor=is_valor
        )
        content = scraped_content
        if not image_url:
            image_url = scraped_image
        if not title:
            title = scraped_title
    except Exception as exc:
        logger.error("Failed to scrape article content for %s: %s", url, exc)

    if not content:
        content = (
            description
            if description
            else "Unable to fetch article body. Please read the full article on the publisher website."
        )
    if not title:
        title = "News Article"

    return {
        "id": article_id,
        "title": title,
        "source": source_name,
        "publishedAt": pub_date_iso,
        "summary": description if description else title,
        "content": content,
        "imageUrl": image_url,
    }
