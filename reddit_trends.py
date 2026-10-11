#!/usr/bin/env python3
"""
Reddit popular posts (public RSS feed, Apify fallback) -> reddit_trends.json

  * Source: the public r/popular RSS feed, every run (free). Reddit ends RSS support on 13 Nov 2026.
    When the feed fails or comes back empty, the same top 25 come from an Apify Reddit actor instead:
      2nd tier  dami_studio/reddit-scraper, at most once every 4 hours (REDDIT_APIFY_EVERY_HOURS);
      3rd tier  harshmaur/reddit-scraper-pro ("No RSS"), at most once every 8 hours
                (REDDIT_APIFY3_EVERY_HOURS), tried only when the 2nd tier has just failed.
    In between, the JSON is left as it is. When each tier last ran is kept in the JSON.
  * Topic = the post title. Description = the post's own text when it has any.
  * Posts with no text (images, links) are described by Gemini. That is the ONLY Gemini call of
    the run: it also writes image search words for new posts, and it is skipped entirely when
    nothing needs either.
  * Reddit's feed isn't a trend rank, so rank = hours trending (longest = #1). Hours trending
    count from the date the post was ADDED to the JSON ("started_trending"), not from when the
    post was created; the post's creation time is kept as "published" for the "time ago" text.
  * Clicking a topic opens the post on Reddit.
  * One image per new topic from DuckDuckGo (the post's own thumbnail is the fallback).

Env vars:
  APIFY_TOKEN        needed only for the Apify fallback (same secret as social media)
  REDDIT_APIFY_ACTOR optional, fallback actor id (default dami_studio~reddit-scraper, $0.50 per 1,000 posts)
  REDDIT_APIFY_EVERY_HOURS  optional, minimum hours between Apify runs (default 4)
  REDDIT_APIFY_MAX_CHARGE_USD  optional, spending cap per 2nd-tier run (default 0.05)
  REDDIT_APIFY3_ACTOR / REDDIT_APIFY3_EVERY_HOURS / REDDIT_APIFY3_MAX_CHARGE_USD  optional, 3rd tier
  (defaults harshmaur~reddit-scraper-pro / 8 / 0.08)
  REDDIT_FEED        optional, RSS feed URL (default https://www.reddit.com/r/popular/.rss)
  REDDIT_TOP_N       optional, posts to keep (default 25)
  REDDIT_USER_AGENT  optional, Reddit asks for a descriptive user agent
  GEMINI_KEY         optional (only used for posts with no text)
  OUT_FILE           optional, default reddit_trends.json
"""
from __future__ import annotations

import html
import os
import re
import sys
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import trend_common as tc

FEEDS = [os.environ.get("REDDIT_FEED", "https://www.reddit.com/r/popular/.rss")]
if "old.reddit.com" not in FEEDS[0]:
    FEEDS.append(FEEDS[0].replace("://www.reddit.com", "://old.reddit.com"))
TOP_N = int(os.environ.get("REDDIT_TOP_N", "25"))
USER_AGENT = os.environ.get(
    "REDDIT_USER_AGENT",
    "TrendingTopicsDashboard/1.0 (personal GitHub Pages project; mzaiger/TrendingTopics)")
OUT_FILE = os.environ.get("OUT_FILE", "reddit_trends.json")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
# Fallback tiers, tried in order when the RSS feed fails. Each tier has its own minimum gap between runs
# (so the credit lasts) and a hard spending cap per run.
#   2nd: dami_studio, $0.50 per 1,000 posts, at most every 4 hours. Its keyless mode reads Reddit's
#        public feed, so it may stop working when Reddit ends RSS on 13 Nov 2026.
#   3rd: harshmaur "No RSS" actor (advertised as RSS-free), about $0.0425 a run (start $0.005 + 25 x
#        $0.0015), at most every 8 hours, and only when the 2nd tier has just failed.
APIFY_REDDIT_ACTOR = os.environ.get("REDDIT_APIFY_ACTOR", "dami_studio~reddit-scraper")
APIFY_EVERY_HOURS = float(os.environ.get("REDDIT_APIFY_EVERY_HOURS", "4"))
APIFY_REDDIT_MAX_CHARGE = os.environ.get("REDDIT_APIFY_MAX_CHARGE_USD", "0.05")
APIFY3_ACTOR = os.environ.get("REDDIT_APIFY3_ACTOR", "harshmaur~reddit-scraper-pro")
APIFY3_EVERY_HOURS = float(os.environ.get("REDDIT_APIFY3_EVERY_HOURS", "8"))
APIFY3_MAX_CHARGE = os.environ.get("REDDIT_APIFY3_MAX_CHARGE_USD", "0.08")

TASK = ("say in one or two sentences what the Reddit post is about, using only the title and "
        "details shown. Do not guess beyond the title.")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def fetch_feed() -> bytes:
    last = None
    for url in FEEDS:
        sep = "&" if "?" in url else "?"
        full = f"{url}{sep}limit={TOP_N}"
        try:
            r = requests.get(full, headers={"User-Agent": USER_AGENT}, timeout=30)
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            tc.warn(f"  {full} failed: {e}")
            last = e
    raise last  # type: ignore[misc]


def parse_feed(content: bytes) -> list[dict]:
    root = ET.fromstring(content)
    raw = []
    for pos, entry in enumerate(e for e in root if _local(e.tag) == "entry"):
        f = {"title": "", "id": "", "link": "", "updated": "", "published": "", "sub": "",
             "content": "", "thumb": ""}
        for c in entry:
            name = _local(c.tag)
            if name == "title":
                f["title"] = tc.clean_text(c.text)
            elif name == "id":
                f["id"] = (c.text or "").strip()
            elif name == "link" and not f["link"]:
                f["link"] = c.attrib.get("href", "")
            elif name in ("updated", "published"):
                f[name] = (c.text or "").strip()
            elif name == "category":
                f["sub"] = c.attrib.get("label") or ("r/" + c.attrib.get("term", ""))
            elif name == "content":
                f["content"] = c.text or ""
            elif name == "thumbnail":
                f["thumb"] = c.attrib.get("url", "")
        if not f["title"] or not f["link"]:
            continue

        # Post body: the "md" block holds the post's own text; link/image posts don't have one.
        body, domain, thumb = "", "", f["thumb"]
        if f["content"]:
            soup = BeautifulSoup(f["content"], "html.parser")
            md = soup.find("div", class_="md")
            if md:
                body = tc.clean_text(md.get_text(" ", strip=True))
            for a in soup.find_all("a"):
                if a.get_text(strip=True) == "[link]":
                    host = urlparse(a.get("href", "")).netloc.replace("www.", "")
                    if host and "reddit.com" not in host and "redd.it" not in host:
                        domain = host
            if not thumb:
                img = soup.find("img")
                thumb = img.get("src", "") if img else ""

        post_id = f["id"].split("_", 1)[-1] or tc.slugify(f["title"])
        sub = f["sub"].strip() or "reddit"
        desc = tc.shorten(body, 240)
        template = f"Popular post in {sub}" + (f" linking to {domain}." if domain else ".")
        raw.append({
            "id": post_id,
            "topic": f["title"],
            "description": desc,
            "description_source": "feed" if desc else "",
            "url": f["link"],
            "feed_pos": pos,
            "source_start": tc.parse_time(f["published"] or f["updated"]),
            "volume_unit": "hours",        # volume line = hours trending
            "extra": {"subreddit": sub, "domain": domain},
            "_image_hint": thumb if thumb.startswith("http") else None,
            "_material": f"Subreddit: {sub}\nLinked site: {domain or 'none'}\nPost title: {f['title']}",
            "_has_context": True,
            "_template": template,
        })
        if len(raw) == TOP_N:
            break
    tc.unique_ids(raw)
    return raw


def _plain(markdown: str) -> str:
    """Post text from Reddit's markdown as plain text (the RSS feed gives it already rendered)."""
    t = html.unescape(markdown or "")
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)          # [text](url) -> text
    t = re.sub(r"https?://\S+", "", t)                          # bare links
    t = re.sub(r"(?m)^\s{0,3}(#{1,6}|>|[-*+])\s+", "", t)       # headings, quotes, bullets
    t = re.sub(r"[*_~`]+", "", t)                                # bold / italic / code marks
    return tc.clean_text(t)


# --------------------------------------------------------------------------- Apify fallbacks

def _run_actor(actor: str, body: dict, max_charge: str) -> list:
    r = requests.post(
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
        params={"maxTotalChargeUsd": max_charge},                 # hard cap on what one run can cost
        headers={"Authorization": f"Bearer {APIFY_TOKEN}", "Content-Type": "application/json"},
        json=body,
        timeout=240,
    )
    r.raise_for_status()
    items = r.json()
    if not isinstance(items, list):
        raise ValueError(f"Unexpected Apify response: {str(items)[:200]}")
    return items


def fetch_apify() -> list:
    """2nd tier: the top TOP_N of r/popular (hot). dedupeAcrossRuns is OFF on purpose: with it on the
    actor only returns posts it has not delivered before, so the list would shrink every run."""
    return _run_actor(APIFY_REDDIT_ACTOR,
                      {"sources": ["r/popular"], "sort": "hot", "maxPostsCount": TOP_N,
                       "maxPostsPerSubreddit": TOP_N, "dedupeAcrossRuns": False},
                      APIFY_REDDIT_MAX_CHARGE)


def fetch_apify3() -> list:
    """3rd tier: r/popular through a start URL. Comments and community rows are switched off because
    every stored row is billed."""
    return _run_actor(APIFY3_ACTOR,
                      {"startUrls": [{"url": "https://www.reddit.com/r/popular/"}],
                       "maxPostsCount": TOP_N, "maxCommentsCount": 0, "maxCommentsPerPost": 0,
                       "maxCommunitiesCount": 0, "crawlCommentsPerPost": False, "includeNSFW": False},
                      APIFY3_MAX_CHARGE)


def parse_apify(items: list) -> list[dict]:
    """Same item shape as parse_feed(), built from an Apify Reddit actor's rows. Field names from
    both fallback actors are read (title/url/selftext/createdUtc vs postUrl/body/createdAt), and
    rows that are not posts (comments, communities) are skipped."""
    raw = []
    for d in (x for x in items if isinstance(x, dict)):
        if d.get("dataType") not in (None, "post"):
            continue
        title = tc.clean_text(d.get("title"))
        link = d.get("postUrl") or d.get("url") or d.get("permalink") or ""
        if link.startswith("/"):
            link = "https://www.reddit.com" + link
        if not title or not link or d.get("over18") or d.get("over_18"):
            continue
        sub = (d.get("communityName") or d.get("subreddit") or d.get("parsedCommunityName") or "").strip()
        sub = sub if sub.startswith("r/") else ("r/" + sub if sub else "reddit")
        body = _plain(d.get("selftext") or d.get("body") or d.get("narration") or "")
        created = d.get("createdUtc") or d.get("created_utc")
        started = (datetime.fromtimestamp(created, tz=timezone.utc)
                   if isinstance(created, (int, float)) else tc.parse_time(d.get("createdAt")))
        host = (d.get("domain") or "").replace("www.", "")
        domain = host if host and not host.startswith("self.") and "reddit.com" not in host \
            and "redd.it" not in host else ""
        thumb = d.get("thumbnail") or ""
        if not str(thumb).startswith("http"):
            imgs = d.get("images")
            thumb = imgs[0] if isinstance(imgs, list) and imgs and isinstance(imgs[0], str) else ""
        desc = tc.shorten(body, 240)
        post_id = str(d.get("parsedId") or d.get("id") or "").split("_", 1)[-1] or tc.slugify(title)
        raw.append({
            "id": post_id,
            "topic": title,
            "description": desc,
            "description_source": "feed" if desc else "",
            "url": link,
            "feed_pos": len(raw),
            "source_start": started,
            "volume_unit": "hours",
            "extra": {"subreddit": sub, "domain": domain},
            "_image_hint": thumb if str(thumb).startswith("http") else None,
            "_material": f"Subreddit: {sub}\nLinked site: {domain or 'none'}\nPost title: {title}",
            "_has_context": True,
            "_template": f"Popular post in {sub}" + (f" linking to {domain}." if domain else "."),
        })
        if len(raw) == TOP_N:
            break
    tc.unique_ids(raw)
    return raw


def _due(last_iso, hours: float, now: datetime) -> bool:
    """True when at least `hours` have passed since the recorded time (5 minutes of slack)."""
    last = tc.parse_time(last_iso)
    return last is None or (now - last).total_seconds() >= hours * 3600 - 300


def apify_fallback(previous: dict, now: datetime) -> tuple:
    """Run the Apify fallbacks when the RSS feed gave nothing. Returns (raw_posts, state, source) where
    source is "apify2" or "apify3" (or "" when nothing was fetched).

    state holds when each tier last ran (kept in the JSON so the minimum gaps survive between
    runs, even when every tier fails):
      apify_last_run / apify_last_ok   2nd tier (default every 4 hours)
      apify3_last_run                  3rd tier (default every 8 hours), tried only when the 2nd tier
                                       just failed or its last run failed, so you never pay for both"""
    prev = previous or {}
    state = {k: prev[k] for k in ("apify_last_run", "apify_last_ok", "apify3_last_run") if k in prev}

    tier2_failed = state.get("apify_last_ok") is False
    if _due(state.get("apify_last_run"), APIFY_EVERY_HOURS, now):
        state["apify_last_run"] = tc.iso(now)
        try:
            raw = parse_apify(fetch_apify())
        except (requests.RequestException, ValueError) as e:
            tc.warn(f"  Apify Reddit fallback (2nd) {APIFY_REDDIT_ACTOR} failed: {e}")
            raw = []
        state["apify_last_ok"] = bool(raw)
        if raw:
            tc.info(f"Reddit posts came from the 2nd-tier Apify actor ({APIFY_REDDIT_ACTOR})")
            return raw, state, "apify2"
        tier2_failed = True

    if tier2_failed and APIFY3_ACTOR and _due(state.get("apify3_last_run"), APIFY3_EVERY_HOURS, now):
        state["apify3_last_run"] = tc.iso(now)
        try:
            raw = parse_apify(fetch_apify3())
        except (requests.RequestException, ValueError) as e:
            tc.warn(f"  Apify Reddit fallback (3rd) {APIFY3_ACTOR} failed: {e}")
            raw = []
        if raw:
            tc.info(f"Reddit posts came from the 3rd-tier Apify actor ({APIFY3_ACTOR})")
            return raw, state, "apify3"
    return [], state, ""


def main() -> int:
    now = tc.utcnow()
    previous = tc.load_json(OUT_FILE)

    raw, state, source = [], {}, "rss"
    try:
        raw = parse_feed(fetch_feed())
    except (requests.RequestException, ET.ParseError) as e:
        tc.warn(f"Could not read the Reddit RSS feed: {e}")
    if not raw:
        if not APIFY_TOKEN:
            tc.warn(f"No posts from the RSS feed and APIFY_TOKEN is not set, so {OUT_FILE} was not changed.")
            return 1
        raw, state, source = apify_fallback(previous, now)
        if not raw:
            if previous and state != {k: previous[k] for k in state if k in previous}:
                # Remember that the fallbacks were just tried, so a failing actor is not retried every hour.
                tc.save_json(OUT_FILE, {**previous, **state})
            tc.warn(f"No Reddit posts this run (RSS down; Apify fallbacks not due or empty), so the posts "
                    f"in {OUT_FILE} are left as they are until the next slot.")
            return 0
    tc.info(f"Got {len(raw)} Reddit posts")

    # Description: post text -> last run -> Gemini -> template.
    # ONE Gemini call covers both the posts with no text and the image search words for new posts.
    tc.reuse_previous(raw, previous)
    finder = tc.ImageFinder()
    leftovers = [x for x in raw if not x["description"]]
    image_targets = tc.items_needing_images(raw, previous, finder.max_lookups) if finder.enabled else []
    tc.gemini_enrich(leftovers, image_targets, TASK)
    tc.apply_templates(raw)

    items = tc.finalize(raw, previous, rank_mode="duration", now=now)
    tc.apply_images(items, previous, finder)
    doc = tc.build_doc("reddit", "Reddit", items, rank_basis="time_trending",
                       region="United States", source_url=FEEDS[0])
    # Keep the fallback timers (from this run, or carried over from the last file) in the JSON.
    for k in ("apify_last_run", "apify_last_ok", "apify3_last_run"):
        v = state.get(k, (previous or {}).get(k))
        if v is not None:
            doc[k] = v
    doc["data_source"] = source        # "rss" / "apify2" / "apify3": schedule.py slows social media down when dearer
    tc.save_json(OUT_FILE, doc)
    tc.info(f"Wrote {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
