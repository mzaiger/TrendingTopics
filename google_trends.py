#!/usr/bin/env python3
"""
Google trending searches (public RSS) -> google_trends.json

  * Topic + short description come straight from the feed (Google's own news snippet, or the top
    news headline). Gemini is only used - in ONE call - for a topic with no description at all.
  * Google doesn't rank the list, so rank = how long a topic has been trending (longest = #1),
    worked out from the "started_trending" date stored in the JSON.
  * Clicking a topic opens a Google search for it.
  * One image per new topic from DuckDuckGo (Google's own picture is the fallback).

Env vars:
  GOOGLE_TRENDS_GEO  optional, country code (default US)
  GEMINI_KEY         optional (only used for topics with no description)
  OUT_FILE           optional, default google_trends.json
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import requests

import trend_common as tc

GEO = os.environ.get("GOOGLE_TRENDS_GEO", "US").upper()
FEED_URL = f"https://trends.google.com/trending/rss?geo={GEO}"
OUT_FILE = os.environ.get("OUT_FILE", "google_trends.json")

TASK = ("explain in one or two sentences why people are searching for it on Google right now, "
        "using only the news material shown.")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kids(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name]


def _text(el, name: str) -> str:
    for c in _kids(el, name):
        return (c.text or "").strip()
    return ""


def describe(news: list[dict]) -> str:
    """Google's snippet if it has one, else the top headline(s)."""
    for n in news:
        if n["snippet"]:
            return tc.shorten(n["snippet"], 240)
    if not news:
        return ""
    first = news[0]
    text = first["title"] + (f" ({first['source']})" if first["source"] else "")
    if len(news) > 1 and len(text) < 150:
        text += f" Also: {news[1]['title']}"
    return tc.shorten(text, 260)


def parse_feed(content: bytes) -> list[dict]:
    root = ET.fromstring(content)
    raw = []
    for pos, item in enumerate(root.iter("item")):
        topic = tc.clean_text(_text(item, "title"))
        if not topic:
            continue
        news = []
        for ni in _kids(item, "news_item"):
            news.append({
                "title": tc.clean_text(_text(ni, "news_item_title")),
                "snippet": tc.clean_text(_text(ni, "news_item_snippet")),
                "url": _text(ni, "news_item_url"),
                "source": tc.clean_text(_text(ni, "news_item_source")),
            })
        news = [n for n in news if n["title"] or n["snippet"]]
        traffic = _text(item, "approx_traffic")
        desc = describe(news)
        material = "\n".join(
            f"- {n['title']}" + (f" ({n['source']})" if n["source"] else "")
            + (f": {n['snippet']}" if n["snippet"] else "")
            for n in news[:4]) or "(no news items in the feed)"
        raw.append({
            "id": tc.slugify(topic),
            "topic": topic,
            "description": desc,
            "description_source": "feed" if desc else "",
            "url": f"https://www.google.com/search?q={quote_plus(topic)}",
            "feed_pos": pos,
            "source_start": tc.parse_time(_text(item, "pubDate")),
            "volume": f"{traffic} searches" if traffic else None,
            "extra": {"news_source": news[0]["source"] if news else ""},
            "_image_query": topic,
            "_image_hint": _text(item, "picture") or None,
            "_material": f"Search term: {topic}\nNews about it:\n{material}",
            "_has_context": bool(news),
            "_template": "Trending on Google Search right now.",
        })
    tc.unique_ids(raw)
    return raw


def main() -> int:
    now = tc.utcnow()
    previous = tc.load_json(OUT_FILE)

    try:
        r = requests.get(FEED_URL, headers=tc.HEADERS, timeout=30)
        r.raise_for_status()
        raw = parse_feed(r.content)
    except (requests.RequestException, ET.ParseError) as e:
        tc.warn(f"Could not read the Google Trends feed: {e}\n{OUT_FILE} was not changed.")
        return 1
    if not raw:
        tc.warn(f"The Google Trends feed had no items - {OUT_FILE} was not changed.")
        return 1
    tc.info(f"Got {len(raw)} Google trends: {', '.join(r['topic'] for r in raw)}")

    # Description: feed -> last run -> Gemini (one call, only the leftovers) -> generic template
    tc.reuse_previous(raw, previous)
    leftovers = [x for x in raw if not x["description"]]
    if leftovers:
        tc.gemini_fill([x for x in leftovers if x["_has_context"]], TASK)
    tc.apply_templates(raw)

    items = tc.finalize(raw, previous, rank_by_duration=True, now=now)
    tc.apply_images(items, previous, tc.ImageFinder())
    doc = tc.build_doc("google", "Google", items, rank_basis="time_trending",
                       region=GEO, source_url=FEED_URL)
    tc.save_json(OUT_FILE, doc)
    tc.info(f"Wrote {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
