#!/usr/bin/env python3
"""
Google trending searches (public "Daily Search Trends" RSS) -> google_trends.json

  * Google's feed only ever holds its 10 newest trends, so this script keeps a rolling window:
    every topic the feed has shown in the last GOOGLE_KEEP_HOURS (default 24) stays in the file,
    even after it drops out of the feed. Run it often and you get a full day's list.
  * Topic + short description come straight from the feed (Google's own news snippet, or the top
    news headline). Gemini is only used - in ONE call - for a topic with no description at all.
  * Rank = search volume (Google's "approx traffic", highest = #1; ties -> longer trending first).
  * Clicking a topic opens a Google search for it.
  * One image per new topic from DuckDuckGo (Google's own picture is the fallback).

Env vars:
  GOOGLE_TRENDS_GEO  optional, country code (default US)
  GOOGLE_KEEP_HOURS  optional, how long a topic stays after it leaves the feed (default 24)
  GEMINI_KEY         optional (only used for topics with no description / image search words)
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
KEEP_HOURS = float(os.environ.get("GOOGLE_KEEP_HOURS", "24"))
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
        traffic_value = tc.parse_traffic(traffic)
        if traffic_value is not None:
            volume = f"{tc.fmt_count(traffic_value)}{'+' if traffic.strip().endswith('+') else ''} searches"
        else:
            volume = f"{traffic} searches" if traffic else None
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
            "volume": volume,
            "volume_value": traffic_value,
            "volume_unit": "searches",
            "extra": {"news_source": news[0]["source"] if news else ""},
            "_image_hint": _text(item, "picture") or None,
            "_material": f"Search term: {topic}\nNews about it:\n{material}",
            "_has_context": bool(news),
            "_template": "Trending on Google Search right now.",
        })
    tc.unique_ids(raw)
    return raw


def carry_over(previous: dict, fresh_ids: set, now) -> list[dict]:
    """Topics that left the feed but were seen within the last KEEP_HOURS stay in the list."""
    out = []
    for p in previous.get("items") or []:
        if not isinstance(p, dict) or p.get("id") in fresh_ids or not p.get("topic"):
            continue
        seen = tc.parse_time(p.get("last_seen")) or tc.parse_time(p.get("first_seen"))
        if not seen or (now - seen).total_seconds() > KEEP_HOURS * 3600:
            continue
        out.append({
            "id": p["id"], "topic": p["topic"],
            "description": p.get("description") or "",
            "description_source": p.get("description_source") or "",
            "url": p.get("url") or "",
            "feed_pos": 1000 + len(out),
            "source_start": tc.parse_time(p.get("started_trending")),
            "volume": p.get("volume"), "volume_value": p.get("volume_value"),
            "volume_unit": "searches",
            "extra": p.get("extra") or {},
            "last_seen": seen,
        })
    return out


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

    # A topic that is still (or again) in the feed keeps the biggest search volume we've seen.
    prev_by_id = {p.get("id"): p for p in (previous.get("items") or []) if isinstance(p, dict)}
    for x in raw:
        p = prev_by_id.get(x["id"])
        if p and (p.get("volume_value") or 0) > (x.get("volume_value") or 0):
            x["volume_value"], x["volume"] = p["volume_value"], p.get("volume")

    kept = carry_over(previous, {x["id"] for x in raw}, now)
    if kept:
        tc.info(f"Keeping {len(kept)} topic(s) from the last {KEEP_HOURS:g}h that left the feed.")

    items = tc.finalize(raw + kept, previous, rank_mode="volume", now=now)
    finder = tc.ImageFinder()
    tc.plan_image_queries(items, previous, finder)      # topic + description -> better search words
    tc.apply_images(items, previous, finder)
    doc = tc.build_doc("google", "Google", items, rank_basis="search_volume",
                       region=GEO, source_url=FEED_URL)
    doc["keep_hours"] = KEEP_HOURS
    tc.save_json(OUT_FILE, doc)
    tc.info(f"Wrote {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
