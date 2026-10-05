#!/usr/bin/env python3
"""
Google trending searches (public "Daily Search Trends" RSS) -> google_trends.json

  * Google's feed only ever holds its 10 newest trends, so this script keeps a rolling window:
    every topic stays in the file for GOOGLE_KEEP_HOURS (default 48) after it was added, even once
    it drops out of the feed. Older topics are truncated. The page shows the top 20 by search volume.
  * Topic + short description come straight from the feed (Google's own news snippet, or the top
    news headline). Gemini is only used for a topic with no description - and it is the ONLY Gemini
    call of the run (it also writes image search words for new topics).
  * Rank = search volume (Google's "approx traffic", highest = #1; ties -> longer on the list first).
  * Hours trending / time filters count from the date a topic was ADDED to the JSON; Google's own
    start time is kept as "published" for the "time ago" text.
  * Clicking a topic opens a Google search for it.
  * One image per new topic from DuckDuckGo (Google's own picture is the fallback).

Env vars:
  GOOGLE_TRENDS_GEO  optional, country code (default US)
  GOOGLE_KEEP_HOURS  optional, hours a topic is kept after it was added (default 48)
  GOOGLE_TOP_N       optional, how many topics the page shows, by search volume (default 20)
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
KEEP_HOURS = float(os.environ.get("GOOGLE_KEEP_HOURS", "48"))
TOP_N = int(os.environ.get("GOOGLE_TOP_N", "20"))
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
    """Topics that left the feed stay until they are KEEP_HOURS old (counted from when they were
    added to the JSON); older ones are truncated."""
    out = []
    for p in previous.get("items") or []:
        if not isinstance(p, dict) or p.get("id") in fresh_ids or not p.get("topic"):
            continue
        added = tc.parse_time(p.get("first_seen")) or tc.parse_time(p.get("started_trending"))
        if not added or (now - added).total_seconds() > KEEP_HOURS * 3600:
            continue
        seen = tc.parse_time(p.get("last_seen")) or added
        out.append({
            "id": p["id"], "topic": p["topic"],
            "description": p.get("description") or "",
            "description_source": p.get("description_source") or "",
            "url": p.get("url") or "",
            "feed_pos": 1000 + len(out),
            "source_start": tc.parse_time(p.get("published")),
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

    # Description: feed -> last run -> Gemini -> generic template.
    # ONE Gemini call covers both the leftovers with no description and the image search words.
    tc.reuse_previous(raw, previous)
    finder = tc.ImageFinder()
    leftovers = [x for x in raw if not x["description"] and x["_has_context"]]
    image_targets = tc.items_needing_images(raw, previous, finder.max_lookups) if finder.enabled else []
    tc.gemini_enrich(leftovers, image_targets, TASK)
    tc.apply_templates(raw)

    # A topic that is still (or again) in the feed keeps the biggest search volume we've seen.
    prev_by_id = {p.get("id"): p for p in (previous.get("items") or []) if isinstance(p, dict)}
    for x in raw:
        p = prev_by_id.get(x["id"])
        if p and (p.get("volume_value") or 0) > (x.get("volume_value") or 0):
            x["volume_value"], x["volume"] = p["volume_value"], p.get("volume")

    kept = carry_over(previous, {x["id"] for x in raw}, now)
    if kept:
        tc.info(f"Keeping {len(kept)} earlier topic(s) (under {KEEP_HOURS:g}h old) that left the feed.")

    items = tc.finalize(raw + kept, previous, rank_mode="volume", now=now)
    tc.apply_images(items, previous, finder)
    doc = tc.build_doc("google", "Google", items, rank_basis="search_volume",
                       region=GEO, source_url=FEED_URL)
    doc["keep_hours"] = KEEP_HOURS
    doc["display_limit"] = TOP_N         # the page shows only the top N by search volume
    tc.save_json(OUT_FILE, doc)
    tc.info(f"Wrote {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
