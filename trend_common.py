#!/usr/bin/env python3
"""
Shared helpers for social_media_trends.py, google_trends.py and reddit_trends.py.

  * JSON load/save (atomic)
  * time parsing + "started trending" bookkeeping
  * ranking (platform rank, or "how long has it been trending") + rank change vs last run
  * DuckDuckGo image lookup (same approach as AddImageUrl.py: one session, jittered delays,
    backoff, per-run cache, and a bail-out when DuckDuckGo starts rate limiting)
  * one-call Gemini batch for topics that have no description of their own
"""
from __future__ import annotations

import html
import json
import os
import random
import re
import sys
import time
import warnings
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

try:  # bs4 warns when text "looks like" a URL/filename
    from bs4 import MarkupResemblesLocatorWarning
    warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
except ImportError:  # pragma: no cover
    pass

try:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
except ImportError:  # pragma: no cover - older, renamed package
    try:
        from duckduckgo_search import DDGS
        from duckduckgo_search.exceptions import (
            DuckDuckGoSearchException as DDGSException,
            RatelimitException,
            TimeoutException,
        )
    except ImportError:
        DDGS = None

        class DDGSException(Exception):
            pass

        class RatelimitException(DDGSException):
            pass

        class TimeoutException(DDGSException):
            pass


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# Same chain as SportsDashboard/scripts/gemini_predictions.py, tried top to bottom.
GEMINI_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
)


def info(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- basics

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: Any) -> Optional[datetime]:
    """RSS (RFC 822) or ISO 8601 timestamp -> aware UTC datetime, or None."""
    if not value:
        return None
    s = str(value).strip()
    dt = None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "topic"


def clean_text(s: Any) -> str:
    if not s:
        return ""
    s = html.unescape(str(s))
    s = BeautifulSoup(s, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", s).strip()


def shorten(text: str, limit: int = 240) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:- ")
    return cut + "…"


def fmt_count(n: float) -> str:
    n = float(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            v = n / div
            return f"{v:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def fmt_hours_trend(hours: float) -> str:
    """Whole hours, no decimals: "12hr trend" ("<1hr trend" under an hour)."""
    return "<1hr trend" if hours < 1 else f"{int(hours)}hr trend"


def parse_traffic(text: Any) -> Optional[float]:
    """Google's "approx traffic" ("200+", "10000+", "2K+", "1M+") -> a number, or None."""
    m = re.match(r"^\s*([\d.,]+)\s*([KMB]?)\s*\+?\s*$", str(text or ""), re.I)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return value * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()]


def load_json(path: str | Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path: str | Path, data: dict) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def unique_ids(raw_items: list[dict]) -> None:
    seen: dict[str, int] = {}
    for it in raw_items:
        base = it["id"]
        n = seen.get(base, 0)
        seen[base] = n + 1
        if n:
            it["id"] = f"{base}-{n + 1}"


# --------------------------------------------------------- ranking + history bookkeeping

MOVE_HOURS = 4          # rank movement is measured against the rank this many hours ago
HISTORY_KEEP_HOURS = 6  # how much per-topic rank history is stored in the JSON


def _rank_baseline(history: list, now: datetime):
    """Rank from about MOVE_HOURS ago: the newest history entry that is at least that old.
    A topic with no entry that old yet is compared with its oldest entry instead, so movement
    starts showing before it has been listed for a full 4 hours. Returns None if no history."""
    cutoff = now.timestamp() - MOVE_HOURS * 3600
    parsed = []
    for h in history or []:
        t = parse_time(h.get("t")) if isinstance(h, dict) else None
        r = h.get("r") if isinstance(h, dict) else None
        if t and isinstance(r, int):
            parsed.append((t, r))
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])
    old = [x for x in parsed if x[0].timestamp() <= cutoff]
    return (old[-1] if old else parsed[0])[1]


def _next_history(history: list, now: datetime, rank: int) -> list:
    """Add this run's rank, drop entries older than HISTORY_KEEP_HOURS (but always keep the
    newest entry that is at least MOVE_HOURS old, because it is the comparison point)."""
    cutoff_keep = now.timestamp() - HISTORY_KEEP_HOURS * 3600
    cutoff_move = now.timestamp() - MOVE_HOURS * 3600
    parsed = []
    for h in history or []:
        t = parse_time(h.get("t")) if isinstance(h, dict) else None
        r = h.get("r") if isinstance(h, dict) else None
        if t and isinstance(r, int):
            parsed.append((t, r))
    parsed.sort(key=lambda x: x[0])
    old = [x for x in parsed if x[0].timestamp() <= cutoff_move]
    anchor = old[-1] if old else None
    kept = [x for x in parsed if x[0].timestamp() >= cutoff_keep or x is anchor]
    kept = [x for x in kept if (now - x[0]).total_seconds() > 300]   # re-runs within 5 min replace
    kept.append((now, rank))
    return [{"t": iso(t), "r": r} for t, r in kept]


def finalize(raw_items: list[dict], previous_doc: dict, *, rank_mode: str,
             now: datetime) -> list[dict]:
    """Turn raw feed items into the final JSON items.

    raw item keys: id, topic, description, description_source, url, feed_pos (0-based order in
    the feed), source_start (the feed's own date for the item, or None), volume / volume_value /
    volume_unit, last_seen (datetime, optional), extra (dict), and any private keys starting with
    "_" (used by later steps, stripped before saving).

    Two different dates are kept:
      started_trending / first_seen  the moment the topic was ADDED to the JSON (first run that
                                     saw it). Hours trending, the time filters and the "Newest"
                                     sort all use this one.
      published                      the feed's own date (Reddit post time, Google's start time).
                                     The page shows it as the "time ago" text.

    rank_mode:
      "feed"      the feed's own order (social media)
      "duration"  longest on the list first, i.e. most hours trending (Reddit)
      "volume"    biggest volume_value first, ties -> longer on the list (Google)

    rank_change = rank 4 hours ago - new rank (positive = moved up). The rank 4 hours ago comes from
    "rank_history" (one entry per run, kept for a few hours) stored on every item.
    For volume_unit "hours" the volume is the whole hours trending as of this run.
    """
    prev_items = {x.get("id"): x for x in (previous_doc.get("items") or []) if isinstance(x, dict)}
    had_previous = bool(prev_items)

    staged = []
    for it in raw_items:
        p = prev_items.get(it["id"])
        added = (parse_time(p.get("first_seen")) if p else None) or now
        staged.append((it, p, added))

    if rank_mode == "duration":
        staged.sort(key=lambda s: (s[2], s[0].get("feed_pos", 0)))
    elif rank_mode == "volume":
        staged.sort(key=lambda s: (-(s[0].get("volume_value") or 0), s[2], s[0].get("feed_pos", 0)))
    else:
        staged.sort(key=lambda s: s[0].get("feed_pos", 0))

    out = []
    for rank, (it, p, added) in enumerate(staged, start=1):
        history = (p.get("rank_history") if p else None) or []
        prev_rank = _rank_baseline(history, now) if p else None
        if p and prev_rank is None:
            prev_rank = p.get("rank") if isinstance(p.get("rank"), int) else None
        hours = int(max(0.0, (now - added).total_seconds()) // 3600)
        unit = it.get("volume_unit") or ""
        if unit == "hours":
            volume_value, volume = hours, fmt_hours_trend(hours)
        else:
            volume_value, volume = it.get("volume_value"), it.get("volume")
        published = it.get("source_start")
        final = {
            "id": it["id"],
            "rank": rank,
            "previous_rank": prev_rank,
            "rank_4h_ago": prev_rank,
            "rank_history": _next_history(history, now, rank),
            "rank_change": (prev_rank - rank) if isinstance(prev_rank, int) else None,
            "is_new": bool(had_previous and p is None),
            "topic": it["topic"],
            "description": it.get("description") or "",
            "description_source": it.get("description_source") or "",
            "url": it.get("url") or "",
            "image_url": "",
            "image_source": "",
            "started_trending": iso(added),
            "first_seen": iso(added),
            "published": iso(min(published, now)) if published else None,
            "last_seen": iso(it.get("last_seen") or now),
            "hours_trending": hours,
            "volume": volume,
            "volume_value": volume_value,
            "volume_unit": unit,
            "extra": it.get("extra") or {},
        }
        for k, v in it.items():          # keep private "_..." keys for the image step
            if k.startswith("_"):
                final[k] = v
        out.append(final)
    return out


def build_doc(source: str, label: str, items: list[dict], *, rank_basis: str,
              region: str, source_url: str, models: Optional[list[str]] = None) -> dict:
    for it in items:
        for k in [k for k in it if k.startswith("_")]:
            del it[k]
    doc = {
        "source": source,
        "label": label,
        "updated": iso(utcnow()),
        "region": region,
        "source_url": source_url,
        "rank_basis": rank_basis,
        "count": len(items),
        "items": items,
    }
    if models:
        doc["models"] = models
    return doc


# --------------------------------------------------------------- descriptions + Gemini

def reuse_previous(raw_items: list[dict], previous_doc: dict) -> int:
    """Fill empty descriptions from the last run (never re-use a generic template)."""
    prev = {x.get("id"): x for x in (previous_doc.get("items") or []) if isinstance(x, dict)}
    n = 0
    for it in raw_items:
        if it.get("description"):
            continue
        p = prev.get(it["id"])
        if p and p.get("description") and p.get("description_source") != "template":
            it["description"] = p["description"]
            it["description_source"] = p.get("description_source") or "carried_over"
            n += 1
    return n


def apply_templates(raw_items: list[dict]) -> None:
    for it in raw_items:
        if not it.get("description") and it.get("_template"):
            it["description"] = it["_template"]
            it["description_source"] = "template"


class ModelUnavailable(Exception):
    pass


def _parse_topics(raw: str) -> Optional[dict[int, dict]]:
    """{id: {"description": ..., "image_query": ...}} from the model's JSON, or None."""
    try:
        data = json.loads(raw)
        rows = data["topics"] if isinstance(data, dict) else data
        out = {}
        for row in rows:
            entry = {}
            d = str(row.get("description") or "").strip()
            q = str(row.get("image_query") or "").strip()
            if d:
                entry["description"] = d
            if q:
                entry["image_query"] = q
            if entry:
                out[int(row["id"])] = entry
        return out or None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _call_model(model: str, prompt: str, api_key: str) -> Optional[dict[int, dict]]:
    """One model. Returns {id: {...}}, or None if the reply was unusable.
    Raises ModelUnavailable on any 4xx so the caller can fall through to the next model."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=120)
        except requests.RequestException as e:
            warn(f"  {model}: request error ({e})")
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
            warn(f"  {model}: unreadable response ({e})")   # e.g. blocked by safety filters
            return None
        out = _parse_topics(raw)
        if out is None:
            warn(f"  {model}: reply wasn't the expected JSON")
        return out
    return None


def _gemini_batch(prompt: str) -> tuple[dict[int, dict], Optional[str]]:
    """The one Gemini request of a run (with the model fallback chain on 4xx / unusable replies,
    which only happens when a model fails). Returns ({id: {...}}, model_used)."""
    api_key = os.environ.get("GEMINI_KEY")
    if not api_key:
        warn("GEMINI_KEY not set - skipping Gemini.")
        return {}, None
    for model in GEMINI_MODELS:
        try:
            result = _call_model(model, prompt, api_key)
        except ModelUnavailable as e:
            warn(f"  {model} unavailable, trying next: {e}")
            continue
        if result:
            return result, model
        warn(f"  {model} gave no usable answer, trying next.")
    return {}, None


_QUERY_STOP = {
    "the", "this", "that", "these", "those", "here", "there", "after", "before", "with", "from",
    "what", "when", "where", "why", "how", "who", "will", "would", "could", "should", "also",
    "reportedly", "unconfirmed", "trending", "popular", "latest", "today", "week", "says",
    "said", "amid", "over", "about", "into", "than", "their", "they", "your", "have", "been",
}


def heuristic_query(topic: str, description: str = "") -> str:
    """Fallback image query when Gemini isn't available: the topic plus up to two distinctive
    capitalised words from its description (usually a name)."""
    t = re.sub(r"\s+", " ", (topic or "").lstrip("#")).strip()
    known = {w.lower() for w in re.findall(r"[A-Za-z0-9']+", t)}
    extras: list[str] = []
    for w in re.findall(r"\b[A-Z][A-Za-z'\u2019-]{3,}\b", description or ""):
        wl = w.lower()
        if wl in known or wl in _QUERY_STOP or wl in (e.lower() for e in extras):
            continue
        extras.append(w)
        if len(extras) == 2:
            break
    return " ".join([t] + extras)[:120].strip()


def items_needing_images(raw_items: list[dict], previous_doc: dict, limit: int) -> list[dict]:
    """Items that still need a picture (no DuckDuckGo image saved from an earlier run)."""
    prev = {x.get("id"): x for x in (previous_doc.get("items") or []) if isinstance(x, dict)}
    out = []
    for it in raw_items:
        old = prev.get(it["id"])
        if old and old.get("image_url") and old.get("image_source") == "duckduckgo":
            continue
        out.append(it)
        if len(out) >= limit:
            break
    return out


def _enrich_prompt(entries: list[dict], task: str) -> str:
    blocks = []
    for e in entries:
        needed = [name for name, flag in (("description", e["need_desc"]), ("image_query", e["need_query"])) if flag]
        lines = [f"=== TOPIC id={e['id']}: {e['topic']} ===", "Needed: " + ", ".join(needed)]
        if e["description"]:
            lines.append(f"Existing description (context only): {e['description']}")
        if e["material"]:
            lines.append("Material:\n" + e["material"])
        blocks.append("\n".join(lines))
    return (
        "You write short text for a trending-topics page.\n\n"
        f"There are {len(entries)} topics below. Each says what is Needed:\n"
        f"- description: {task} Write ONE or TWO short sentences (max 40 words).\n"
        "- image_query: ONE short query (2 to 7 words) that will find a photo of the topic's main "
        "subject on an image search engine. The topic is the subject of the picture; use the "
        "description/material only to pick the right meaning (which person, team, event, product or "
        "place). Prefer proper names. For a match-up like \"A vs B\" include both sides. Do not add "
        "words like photo, image, picture, news or today. No hashtags or quotes.\n\n"
        "Rules:\n"
        "- Use only the material shown under that topic. Do not invent facts or add background.\n"
        "- Do not mix up topics: each answer must come from its own topic's material.\n"
        "- If something is unconfirmed, say \"reportedly\" or \"unconfirmed\".\n"
        "- Ignore spam, ads, insults and unrelated arguments. Never repeat slurs or personal attacks.\n"
        "- If the material doesn't make a description clear, say so plainly.\n"
        "- Plain language. No hashtags, emojis or usernames. Never name the social network a post "
        "came from; just say \"social media\".\n\n"
        "Return JSON only, one entry per topic id, including a field ONLY when it is Needed:\n"
        "{\"topics\": [{\"id\": 1, \"description\": \"...\", \"image_query\": \"...\"}]}\n\n"
        + "\n\n".join(blocks)
    )


def gemini_enrich(desc_targets: list[dict], image_targets: list[dict], task: str) -> Optional[str]:
    """The ONE Gemini call of a run.

    desc_targets   items that need a description (they need a "_material" string)
    image_targets  items that need a picture -> gets "_image_query" (topic + description in, best
                   search words out). A keyword fallback is set first, in case Gemini can't help.
    Items can be in both lists. Returns the model used, or None if no call was made / it failed.
    """
    for t in image_targets:
        t["_image_query"] = heuristic_query(t["topic"], t.get("description", ""))
    desc_ids = {id(t) for t in desc_targets}
    img_ids = set() if os.environ.get("IMAGE_QUERY_GEMINI", "1") == "0" else {id(t) for t in image_targets}

    union, seen = [], set()
    for t in list(desc_targets) + list(image_targets):
        if id(t) in seen or (id(t) not in desc_ids and id(t) not in img_ids):
            continue
        seen.add(id(t))
        union.append(t)
    if not union:
        return None

    entries = [{
        "id": i, "topic": t["topic"], "material": t.get("_material", ""),
        "description": t.get("description", ""),
        "need_desc": id(t) in desc_ids, "need_query": id(t) in img_ids,
    } for i, t in enumerate(union, start=1)]
    info(f"Asking Gemini once: {len(desc_ids)} description(s) + {len(img_ids)} image query(ies) "
         f"for {len(entries)} topic(s)...")
    result, model = _gemini_batch(_enrich_prompt(entries, task))
    for i, t in enumerate(union, start=1):
        r = result.get(i) or {}
        if id(t) in desc_ids and r.get("description"):
            t["description"] = shorten(r["description"], 300)
            t["description_source"] = "gemini"
        if id(t) in img_ids and r.get("image_query"):
            q = re.sub(r"\s+", " ", r["image_query"]).replace('"', "").strip()
            if q:
                t["_image_query"] = q[:120]
    if result:
        info(f"Got {len(result)} answer(s) from {model}")
    return model


# ----------------------------------------------------------------- DuckDuckGo images

_TITLE_STOP = {"the", "a", "an", "of", "and", "vs", "v", "in", "on", "at", "to", "for", "with",
               "is", "are", "by", "from", "new", "news", "photo", "image", "pictures", "picture",
               "official", "live"}
_JUNK_WORDS = ("logo", "icon", "clipart", "clip art", "vector", "wallpaper", "template", "mockup")
_WATERMARK_HOSTS = ("alamy.", "shutterstock.", "gettyimages.", "dreamstime.", "istockphoto.",
                    "depositphotos.", "123rf.", "pinterest.")


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(t) > 1 and t not in _TITLE_STOP}


def pick_best_image(results: list[dict], topic: str, query: str) -> Optional[str]:
    """Choose the most relevant of DuckDuckGo's candidates.

    The search engine only matches keywords, so we check each candidate's own title/source against
    the topic (weighted most) and the query, prefer decent-sized, normal-shaped pictures, and avoid
    logos / clip art / stock-photo watermarks. Returns None if nothing shares a word with the topic.
    """
    topic_t = _tokens(topic)
    extra_t = _tokens(query) - topic_t
    best_url, best_score, best_hit = None, -1e9, False
    for idx, r in enumerate(results):
        url = r.get("image") or ""
        if not url.startswith("http"):
            continue
        title = f"{r.get('title') or ''} {r.get('source') or ''}"
        title_t = _tokens(title)
        topic_cov = len(topic_t & title_t) / max(1, len(topic_t))
        extra_cov = len(extra_t & title_t) / max(1, len(extra_t)) if extra_t else 0.0
        score = 4.0 * topic_cov + 2.0 * extra_cov
        w, h = r.get("width"), r.get("height")
        if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
            if min(w, h) < 200:
                score -= 3.0
            elif w >= 500:
                score += 0.5
            score += 0.5 if 0.6 <= w / h <= 2.4 else -1.0
        low = f"{title} {url}".lower()
        if any(j in low for j in _JUNK_WORDS) and not any(j in topic.lower() for j in _JUNK_WORDS):
            score -= 1.5
        if url.lower().split("?")[0].endswith(".svg"):
            score -= 3.0
        if any(hst in url.lower() for hst in _WATERMARK_HOSTS):
            score -= 0.5
        score -= idx * 0.05            # DuckDuckGo's own order breaks ties
        if score > best_score:
            best_url, best_score, best_hit = url, score, (topic_cov > 0 or extra_cov > 0)
    return best_url if best_hit else None


def _retryable(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in (
        "malformed headers", "error sending request", "connection", "connecterror",
        "readerror", "remoteprotocolerror", "temporarily unavailable", "timeout",
        "502", "503", "504",
    ))


class ImageFinder:
    """Looks up one image per new topic on DuckDuckGo Images.

    Rate-limit friendliness (same ideas as AddImageUrl.py): a single DDGS session, a random
    delay between requests, exponential backoff with jitter, a per-run cache, a cap on lookups
    per run, and a bail-out after a few failures in a row (the next run simply tries again).
    """

    def __init__(self) -> None:
        self.enabled = os.environ.get("SKIP_IMAGES", "") != "1"
        self.max_lookups = int(os.environ.get("IMAGE_MAX_LOOKUPS", "20"))
        self.min_delay = float(os.environ.get("IMAGE_MIN_DELAY", "2.0"))
        self.max_delay = float(os.environ.get("IMAGE_MAX_DELAY", "5.0"))
        self.max_retries = int(os.environ.get("IMAGE_MAX_RETRIES", "3"))
        self.abort_after = int(os.environ.get("IMAGE_ABORT_AFTER", "3"))
        self.proxy = os.environ.get("DDG_PROXY") or None
        self.region = os.environ.get("IMAGE_REGION", "us-en")
        self.safesearch = os.environ.get("IMAGE_SAFESEARCH", "moderate")
        self.timeout = 20
        self.candidates = int(os.environ.get("IMAGE_CANDIDATES", "12"))
        self.lookups = 0
        self.failures_in_a_row = 0
        self.blocked = False
        self.cache: dict[str, Optional[str]] = {}
        self._client_obj = None
        if self.enabled and DDGS is None:
            warn("ddgs isn't installed (pip install ddgs) - skipping image lookups.")
            self.enabled = False

    def _client(self):
        if self._client_obj is None:
            self._client_obj = DDGS(proxy=self.proxy, timeout=self.timeout)
        return self._client_obj

    def _search(self, query: str) -> tuple[list[dict], str]:
        backoff = 4.0
        reason = "retry_exhausted"
        for attempt in range(1, self.max_retries + 1):
            try:
                results = list(self._client().images(
                    query, region=self.region, safesearch=self.safesearch,
                    max_results=self.candidates))
                return results, ("ok" if results else "no_results")
            except RatelimitException:
                reason = "ratelimit"
            except TimeoutException:
                reason = "timeout"
            except Exception as e:  # DDGSException and anything the HTTP layer raises
                reason = f"error: {e}"
                if not _retryable(str(e)):
                    return [], reason
            if attempt < self.max_retries:
                wait = min(backoff + random.uniform(0, backoff * 0.5), 60.0)
                warn(f"  image search {reason} on {query!r} (attempt {attempt}/{self.max_retries}) "
                     f"- retrying in {wait:.0f}s")
                time.sleep(wait)
                backoff = min(backoff * 2, 60.0)
        return [], reason

    def find(self, query: str, topic: Optional[str] = None) -> Optional[str]:
        q = re.sub(r"\s+", " ", query or "").replace('"', "").strip()[:200]
        if not q or not self.enabled or self.blocked:
            return None
        key = f"{q.lower()}|{(topic or '').lower()}"
        if key in self.cache:
            return self.cache[key]
        if self.lookups >= self.max_lookups:
            return None
        self.lookups += 1
        results, reason = self._search(q)
        url = pick_best_image(results, topic or q, q) if results else None
        if results and not url:
            reason = "no_match"
        if results or reason == "no_results":
            self.failures_in_a_row = 0
            self.cache[key] = url
        else:
            self.failures_in_a_row += 1
            if self.failures_in_a_row >= self.abort_after:
                self.blocked = True
                warn("DuckDuckGo looks rate limited from this IP - skipping the remaining image "
                     "lookups this run. They'll be retried on the next run.")
        info(f"  image: {q!r} -> {url or reason}")
        time.sleep(random.uniform(self.min_delay, self.max_delay))
        return url


def apply_images(items: list[dict], previous_doc: dict, finder: ImageFinder) -> None:
    """image_url for each item: reuse last run's DuckDuckGo image, else look one up, else fall
    back to an image the feed itself provided ("_image_hint")."""
    prev = {x.get("id"): x for x in (previous_doc.get("items") or []) if isinstance(x, dict)}
    for it in items:
        old = prev.get(it["id"])
        if old and old.get("image_url") and old.get("image_source") == "duckduckgo":
            it["image_url"], it["image_source"] = old["image_url"], "duckduckgo"
            continue
        query = it.get("_image_query") or heuristic_query(it["topic"], it.get("description", ""))
        url = finder.find(query, topic=it["topic"])
        if url:
            it["image_url"], it["image_source"] = url, "duckduckgo"
        elif it.get("_image_hint"):
            it["image_url"], it["image_source"] = it["_image_hint"], "feed"
        elif old and old.get("image_url"):
            it["image_url"], it["image_source"] = old["image_url"], old.get("image_source") or "feed"
    info(f"Images: {sum(1 for i in items if i['image_url'])}/{len(items)} topics have one "
         f"({finder.lookups} DuckDuckGo lookup(s) this run).")
