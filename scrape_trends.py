#!/usr/bin/env python3
"""
Get the US top-20 X (Twitter) trends from an Apify actor, gather context for each
(recent tweets from Sotwe + recent news headlines from Google News), ask Gemini (one call
for all topics) why they're trending, and write trends.json for index.html.

Each trend's link goes to twstalker.com/search/<topic>.

Env vars:
  APIFY_TOKEN         required - Apify API token
  GEMINI_KEY          required - same secret name as SportsDashboard
  APIFY_TRENDS        optional - trends to request per run (default 20; you pay per trend)
  APIFY_LOCATION      optional - location code (default US)
  APIFY_ACTOR         optional - actor id (default automation-lab~twitter-trends-scraper)
  APIFY_MAX_CHARGE_USD optional - hard spending cap per run (default 0.02)
  OUT_FILE            optional - output path (default trends.json)
  DEBUG_HTML_DIR      optional - save fetched Sotwe pages there for debugging
"""
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

TWSTALKER = "https://twstalker.com"
SOTWE = "https://www.sotwe.com"
NEWS_RSS = "https://news.google.com/rss/search"

TOP_N = 20
MAX_CHARS = 1500          # tweet text sent to Gemini per topic (all topics share one prompt)
MAX_HEADLINES = 5
PAGE_DELAY = 2.0          # seconds between requests to the same sites (be polite)

# Same chain as SportsDashboard/scripts/gemini_predictions.py, tried top to bottom.
# (gemini-2.0-flash-lite is left out: that script notes it was retired June 1, 2026.)
GEMINI_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
)
API_KEY = os.environ.get("GEMINI_KEY")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
APIFY_ACTOR = os.environ.get("APIFY_ACTOR", "automation-lab~twitter-trends-scraper")
APIFY_LOCATION = os.environ.get("APIFY_LOCATION", "US")
APIFY_TRENDS = int(os.environ.get("APIFY_TRENDS", "20"))
APIFY_MAX_CHARGE = os.environ.get("APIFY_MAX_CHARGE_USD", "0.02")
OUT_FILE = os.environ.get("OUT_FILE", "trends.json")
DEBUG_DIR = os.environ.get("DEBUG_HTML_DIR")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Where the tweets start and stop on a Sotwe topic page
START_MARKERS = ("Tweets including", "Top Tweets for")
END_MARKERS = ("Last Seen Hashtags", "Trends for you", "Most Popular Users")

NOISE_LINES = re.compile(
    r"^(\d+(\.\d+)?[KMB]?|Share|See More|Download (Image|Video|GIF)|"
    r"Ad-Free Browsing|Enjoy Sotwe.*|Subscribe now)$",
    re.IGNORECASE,
)


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
        out.append({
            "topic": topic,
            "query": query,
            "is_hashtag": bool(it.get("isHashtag")) or topic.startswith("#"),
            "volume": it.get("tweetVolume"),
            "url": f"{TWSTALKER}/search/{quote(query, safe='')}",
        })
        if len(out) == TOP_N:
            break
    return out


# --------------------------------------------------------------------- context: tweets

def sotwe_url(t: dict) -> str:
    q = t["query"].lstrip("#")
    kind = "hashtag" if t["is_hashtag"] else "search"
    return f"{SOTWE}/{kind}/{quote(q, safe='')}?lang=en"


def get_tweet_text(t: dict) -> str:
    """Readable text of the tweets on the topic's Sotwe page, trimmed for the LLM."""
    r = requests.get(sotwe_url(t), headers=HEADERS, timeout=30)
    r.raise_for_status()
    if DEBUG_DIR:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", t["topic"])[:60]
        with open(os.path.join(DEBUG_DIR, f"sotwe_{safe}.html"), "w", encoding="utf-8") as f:
            f.write(r.text)

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "footer"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)

    starts = [text.find(m) for m in START_MARKERS if m in text]
    if not starts:
        return ""          # no tweets section found - don't send page chrome to Gemini
    text = text[min(starts):]

    ends = [text.find(m) for m in END_MARKERS if m in text]
    if ends:
        text = text[:min(ends)]

    lines = [ln for ln in text.splitlines() if ln.strip() and not NOISE_LINES.match(ln.strip())]
    return "\n".join(lines)[:MAX_CHARS]


# ---------------------------------------------------------------------- context: news

def get_headlines(t: dict) -> str:
    """Last-24h Google News headlines for the topic, one per line."""
    q = t["query"].lstrip("#")
    r = requests.get(
        NEWS_RSS,
        params={"q": f"{q} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"},
        headers=HEADERS,
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


# ---------------------------------------------------------------------------- Gemini

PROMPT = """You are summarizing why topics are trending on X (Twitter) in the United States.

Below are {count} topics. Each has recent tweets (newest first) and/or news headlines from the last 24 hours.
For EACH topic, write ONE or TWO short sentences (max 40 words) explaining why it is trending right now.

Rules:
- Use only what is shown under that topic. Do not invent facts or add background you can't see there.
- Do not mix up topics: each reason must come from its own topic's material.
- If something is unconfirmed, say "reportedly" or "unconfirmed".
- Ignore spam, ads, insults and unrelated arguments. Never repeat slurs or personal attacks.
- If the reason isn't clear from the material, say so plainly.
- Plain language, no hashtags, no emojis, no usernames.

Return JSON only, with exactly one entry per topic id:
{{"reasons": [{{"id": 1, "reason": "..."}}, {{"id": 2, "reason": "..."}}]}}

{blocks}"""


class ModelUnavailable(Exception):
    pass


def build_prompt(entries: list[dict]) -> str:
    blocks = []
    for e in entries:
        blocks.append(
            f"=== TOPIC id={e['id']}: {e['topic']} ===\n"
            f"Tweets:\n{e['tweets'] or '(none available)'}\n"
            f"Headlines:\n{e['news'] or '(none available)'}"
        )
    return PROMPT.format(count=len(entries), blocks="\n\n".join(blocks))


def _parse_reasons(raw: str) -> dict[int, str] | None:
    """{id: reason} from the model's JSON, or None if it isn't usable."""
    try:
        data = json.loads(raw)
        rows = data["reasons"] if isinstance(data, dict) else data
        out = {}
        for row in rows:
            reason = str(row["reason"]).strip()
            if reason:
                out[int(row["id"])] = reason
        return out or None
    except (KeyError, ValueError, TypeError):
        return None


def _call_model(model: str, prompt: str) -> dict[int, str] | None:
    """Ask one model. Returns {id: reason}, or None if the reply was unusable.
    Raises ModelUnavailable on any 4xx (rate limit, quota, retired model)
    so the caller can fall through to the next model in the chain."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": API_KEY, "Content-Type": "application/json"}

    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=120)
        except requests.RequestException as e:
            print(f"  {model}: request error ({e})", file=sys.stderr)
            time.sleep(2 ** attempt * 3)
            continue
        if 400 <= r.status_code < 500:
            raise ModelUnavailable(f"{r.status_code} {r.reason}: {r.text[:150]}")
        if r.status_code >= 500:
            time.sleep(2 ** attempt * 3)
            continue
        try:
            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, ValueError, TypeError) as e:
            # e.g. blocked by safety filters, or no candidates returned
            print(f"  {model}: unreadable response ({e})", file=sys.stderr)
            return None
        reasons = _parse_reasons(raw)
        if reasons is None:
            print(f"  {model}: reply wasn't the expected JSON", file=sys.stderr)
        return reasons
    return None


def ask_gemini(entries: list[dict]) -> tuple[dict[int, str], str | None]:
    """One call for all topics. Returns ({id: reason}, model_used).
    Moves to the next model in GEMINI_MODELS on a 4xx or an unusable reply."""
    prompt = build_prompt(entries)
    for model in GEMINI_MODELS:
        try:
            reasons = _call_model(model, prompt)
        except ModelUnavailable as e:
            print(f"  {model} unavailable, trying next: {e}", file=sys.stderr)
            continue
        if reasons:
            return reasons, model
        print(f"  {model} gave no usable reasons, trying next.", file=sys.stderr)
    return {}, None


# ------------------------------------------------------------------------------ main

def load_previous_reasons() -> dict[str, str]:
    """{topic (lowercase): reason} from the existing trends.json, if there is one."""
    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            old = json.load(f)
        return {x["topic"].lower(): x["reason"] for x in old.get("trends", []) if x.get("reason")}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def main() -> int:
    missing = [n for n, v in (("APIFY_TOKEN", APIFY_TOKEN), ("GEMINI_KEY", API_KEY)) if not v]
    if missing:
        print(f"Missing env var(s): {', '.join(missing)}", file=sys.stderr)
        return 1

    try:
        trends = get_trends()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        hint = {
            401: "the Apify token looks invalid",
            403: "the Apify token isn't allowed to run this actor",
            402: "the Apify free credit may be used up for this month",
        }.get(code, "see the response body below")
        body = e.response.text[:200] if e.response is not None else ""
        print(f"Apify returned HTTP {code} ({hint}). {body}\ntrends.json was not changed.",
              file=sys.stderr)
        return 1
    except (requests.RequestException, ValueError) as e:
        print(f"Could not get trends from Apify: {e}\ntrends.json was not changed.", file=sys.stderr)
        return 1
    if len(trends) < 5:
        print(f"Only got {len(trends)} trends - aborting without writing.", file=sys.stderr)
        return 1
    print(f"Got {len(trends)} trends: {', '.join(t['topic'] for t in trends)}")

    # 1) Gather context for every topic
    sotwe_blocked = False
    contexts = {}
    for rank, t in enumerate(trends, start=1):
        topic = t["topic"]
        print(f"[{rank}/{len(trends)}] {topic}")
        tweets, news = "", ""
        time.sleep(PAGE_DELAY)

        if not sotwe_blocked:
            try:
                tweets = get_tweet_text(t)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 403:
                    sotwe_blocked = True
                    print("  Sotwe blocked this IP (403); using news headlines only from here on.",
                          file=sys.stderr)
                else:
                    print(f"  Sotwe fetch failed: {e}", file=sys.stderr)
            except requests.RequestException as e:
                print(f"  Sotwe fetch failed: {e}", file=sys.stderr)
        try:
            news = get_headlines(t)
        except (requests.RequestException, ET.ParseError) as e:
            print(f"  News fetch failed: {e}", file=sys.stderr)
        contexts[rank] = (tweets, news)

    # 2) One Gemini call for every topic that has something to summarize
    entries = [
        {"id": rank, "topic": trends[rank - 1]["topic"], "tweets": tw, "news": nw}
        for rank, (tw, nw) in contexts.items()
        if len(tw) + len(nw) > 40
    ]
    reasons, model_used = {}, None
    if entries:
        print(f"Asking Gemini about {len(entries)} topics in one call...")
        reasons, model_used = ask_gemini(entries)
        print(f"Got {len(reasons)} reasons" + (f" from {model_used}" if model_used else ""))
    else:
        print("No topic had enough context; skipping Gemini.", file=sys.stderr)

    # 3) Write trends.json. If Gemini gave nothing for a topic that was also in the last
    #    run, keep its previous reason so one failed call doesn't blank the whole page.
    previous = load_previous_reasons()
    results = []
    for rank, t in enumerate(trends, start=1):
        tweets, news = contexts[rank]
        reason = reasons.get(rank)
        carried = False
        if not reason and t["topic"].lower() in previous:
            reason, carried = previous[t["topic"].lower()], True
        results.append({
            "rank": rank,
            "topic": t["topic"],
            "reason": reason,
            "reason_carried_over": carried,
            "model": model_used if (reason and not carried) else None,
            "volume": t["volume"],
            "sources": [n for n, v in (("tweets", tweets), ("news", news)) if v],
            "search_url": t["url"],
        })

    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "region": "United States",
        "source": f"Apify {APIFY_ACTOR.replace('~', '/')} ({APIFY_LOCATION})",
        "models": list(GEMINI_MODELS),
        "trends": results,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Wrote {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
