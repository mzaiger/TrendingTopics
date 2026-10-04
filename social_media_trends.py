#!/usr/bin/env python3
"""
Social media trends: US top 20 -> social_media_trends.json

  1. Trend list from an Apify actor (one call, promoted/paid trends dropped).
  2. Context for each topic: recent posts (Sotwe) + last-24h news headlines (Google News RSS).
  3. ONE Gemini call writes a short description for every topic.
  4. Rank = the platform's own rank. Rank change vs the last run, and the date it first
     started trending, are kept in the JSON.
  5. One image per new topic from DuckDuckGo.
  Each topic links to a twstalker.com search.

Env vars:
  APIFY_TOKEN          required
  GEMINI_KEY           required (same secret name as SportsDashboard)
  APIFY_TRENDS         optional, trends to request (default 20; you pay per trend)
  APIFY_LOCATION       optional, default US
  APIFY_ACTOR          optional, actor id (default below)
  APIFY_MAX_CHARGE_USD optional, hard spending cap per run (default 0.02)
  OUT_FILE             optional, default social_media_trends.json
  (image settings: see trend_common.ImageFinder - SKIP_IMAGES, IMAGE_MAX_LOOKUPS, DDG_PROXY ...)
"""
from __future__ import annotations

import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

import trend_common as tc

TWSTALKER = "https://twstalker.com"
SOTWE = "https://www.sotwe.com"
NEWS_RSS = "https://news.google.com/rss/search"

TOP_N = 20
MAX_CHARS = 1500          # post text sent to Gemini per topic (all topics share one prompt)
MAX_HEADLINES = 5
PAGE_DELAY = 2.0          # seconds between requests to the same sites (be polite)

# The one place the Apify actor id appears (the id itself is fixed by Apify).
APIFY_ACTOR = os.environ.get("APIFY_ACTOR", "automation-lab~twitter-trends-scraper")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
APIFY_LOCATION = os.environ.get("APIFY_LOCATION", "US")
APIFY_TRENDS = int(os.environ.get("APIFY_TRENDS", "20"))
APIFY_MAX_CHARGE = os.environ.get("APIFY_MAX_CHARGE_USD", "0.02")
OUT_FILE = os.environ.get("OUT_FILE", "social_media_trends.json")

# Where the posts start and stop on a Sotwe topic page
START_MARKERS = ("Tweets including", "Top Tweets for")
END_MARKERS = ("Last Seen Hashtags", "Trends for you", "Most Popular Users")
NOISE_LINES = re.compile(
    r"^(\d+(\.\d+)?[KMB]?|Share|See More|Download (Image|Video|GIF)|"
    r"Ad-Free Browsing|Enjoy Sotwe.*|Subscribe now)$",
    re.IGNORECASE,
)

TASK = ("explain in one or two sentences why it is trending right now on social media in the "
        "United States, using the recent posts and news headlines shown.")


# --------------------------------------------------------------------------- trends

def get_trends() -> list[dict]:
    """Top trends from the Apify actor, promoted ones removed, in rank order."""
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
    r = requests.post(
        url,
        params={"maxTotalChargeUsd": APIFY_MAX_CHARGE},   # hard cap on what one run can cost
        headers={"Authorization": f"Bearer {APIFY_TOKEN}", "Content-Type": "application/json"},
        json={"locations": [APIFY_LOCATION], "maxTrendsPerLocation": APIFY_TRENDS},
        timeout=180,
    )
    r.raise_for_status()
    items = r.json()
    if not isinstance(items, list):
        raise ValueError(f"Unexpected Apify response: {str(items)[:200]}")

    out, seen = [], set()
    for it in sorted(items, key=lambda x: x.get("rank") or 999):
        if it.get("isPromoted"):
            continue
        topic = (it.get("name") or "").strip()
        query = (it.get("query") or topic).strip()
        key = topic.lower()
        if not topic or key in seen:
            continue
        seen.add(key)
        vol = it.get("tweetVolume")
        out.append({
            "topic": topic,
            "query": query,
            "is_hashtag": bool(it.get("isHashtag")) or topic.startswith("#"),
            "volume": f"{tc.fmt_count(vol)} posts" if isinstance(vol, (int, float)) and vol else None,
            "url": f"{TWSTALKER}/search/{quote(query, safe='')}",
        })
        if len(out) == TOP_N:
            break
    return out


# ----------------------------------------------------------------- context: posts

def sotwe_url(t: dict) -> str:
    q = t["query"].lstrip("#")
    kind = "hashtag" if t["is_hashtag"] else "search"
    return f"{SOTWE}/{kind}/{quote(q, safe='')}?lang=en"


def get_post_text(t: dict) -> str:
    """Readable text of the posts on the topic's Sotwe page, trimmed for the LLM."""
    r = requests.get(sotwe_url(t), headers=tc.HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "footer"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)

    starts = [text.find(m) for m in START_MARKERS if m in text]
    if not starts:
        return ""          # no posts section found - don't send page chrome to Gemini
    text = text[min(starts):]
    ends = [text.find(m) for m in END_MARKERS if m in text]
    if ends:
        text = text[:min(ends)]
    lines = [ln for ln in text.splitlines() if ln.strip() and not NOISE_LINES.match(ln.strip())]
    return "\n".join(lines)[:MAX_CHARS]


# ----------------------------------------------------------------- context: news

def get_headlines(t: dict) -> str:
    """Last-24h Google News headlines for the topic, one per line."""
    q = t["query"].lstrip("#")
    r = requests.get(
        NEWS_RSS,
        params={"q": f"{q} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"},
        headers=tc.HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    lines = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()   # Google appends " - Publisher"
        if title:
            lines.append(f"- {title}")
        if len(lines) >= MAX_HEADLINES:
            break
    return "\n".join(lines)


# ------------------------------------------------------------------------------ main

def main() -> int:
    missing = [n for n, v in (("APIFY_TOKEN", APIFY_TOKEN), ("GEMINI_KEY", os.environ.get("GEMINI_KEY")))
               if not v]
    if missing:
        tc.warn(f"Missing env var(s): {', '.join(missing)}")
        return 1

    now = tc.utcnow()
    previous = tc.load_json(OUT_FILE)

    try:
        trends = get_trends()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        hint = {401: "the Apify token looks invalid",
                403: "the Apify token isn't allowed to run this actor",
                402: "the Apify free credit may be used up for this month"
                }.get(code, "see the response body below")
        body = e.response.text[:200] if e.response is not None else ""
        tc.warn(f"Apify returned HTTP {code} ({hint}). {body}\n{OUT_FILE} was not changed.")
        return 1
    except (requests.RequestException, ValueError) as e:
        tc.warn(f"Could not get trends from Apify: {e}\n{OUT_FILE} was not changed.")
        return 1
    if len(trends) < 5:
        tc.warn(f"Only got {len(trends)} trends - aborting without writing.")
        return 1
    tc.info(f"Got {len(trends)} trends: {', '.join(t['topic'] for t in trends)}")

    # 1) Gather context for every topic
    sotwe_blocked = False
    raw = []
    for pos, t in enumerate(trends):
        tc.info(f"[{pos + 1}/{len(trends)}] {t['topic']}")
        posts, news = "", ""
        time.sleep(PAGE_DELAY)
        if not sotwe_blocked:
            try:
                posts = get_post_text(t)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 403:
                    sotwe_blocked = True
                    tc.warn("  Sotwe blocked this IP (403); using news headlines only from here on.")
                else:
                    tc.warn(f"  Sotwe fetch failed: {e}")
            except requests.RequestException as e:
                tc.warn(f"  Sotwe fetch failed: {e}")
        try:
            news = get_headlines(t)
        except (requests.RequestException, ET.ParseError) as e:
            tc.warn(f"  News fetch failed: {e}")

        raw.append({
            "id": tc.slugify(t["topic"]),
            "topic": t["topic"],
            "description": "",
            "description_source": "",
            "url": t["url"],
            "feed_pos": pos,
            "source_start": None,          # the platform doesn't say; first_seen is used
            "volume": t["volume"],
            "extra": {},
            "_image_query": t["query"].lstrip("#"),
            "_has_context": len(posts) + len(news) > 40,
            "_material": f"Recent posts:\n{posts or '(none available)'}\n\n"
                         f"Recent news headlines:\n{news or '(none available)'}",
        })
    tc.unique_ids(raw)

    # 2) One Gemini call for every topic that has something to summarize
    model = tc.gemini_fill([r for r in raw if r["_has_context"]], TASK)
    carried = tc.reuse_previous(raw, previous)       # a failed call shouldn't blank the page
    if carried:
        tc.info(f"Kept {carried} description(s) from the previous run.")

    # 3) Rank, history, images, write
    items = tc.finalize(raw, previous, rank_by_duration=False, now=now)
    tc.apply_images(items, previous, tc.ImageFinder())
    doc = tc.build_doc("social_media", "Social Media", items, rank_basis="platform_rank",
                       region="United States",
                       source_url=f"Apify {APIFY_ACTOR.replace('~', '/')} ({APIFY_LOCATION})",
                       models=list(tc.GEMINI_MODELS))
    tc.save_json(OUT_FILE, doc)
    tc.info(f"Wrote {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
