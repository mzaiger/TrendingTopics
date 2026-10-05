#!/usr/bin/env python3
"""
Reddit popular posts (public RSS) -> reddit_trends.json

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
  REDDIT_FEED        optional, feed URL (default https://www.reddit.com/r/popular/.rss)
  REDDIT_TOP_N       optional, posts to keep (default 25)
  REDDIT_USER_AGENT  optional, Reddit asks for a descriptive user agent
  GEMINI_KEY         optional (only used for posts with no text)
  OUT_FILE           optional, default reddit_trends.json
"""
from __future__ import annotations

import os
import sys
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


def main() -> int:
    now = tc.utcnow()
    previous = tc.load_json(OUT_FILE)

    try:
        raw = parse_feed(fetch_feed())
    except (requests.RequestException, ET.ParseError) as e:
        tc.warn(f"Could not read the Reddit feed: {e}\n{OUT_FILE} was not changed. "
                "(Reddit often blocks cloud IPs; see README > Troubleshooting.)")
        return 1
    if not raw:
        tc.warn(f"The Reddit feed had no posts - {OUT_FILE} was not changed.")
        return 1
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
    tc.save_json(OUT_FILE, doc)
    tc.info(f"Wrote {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
