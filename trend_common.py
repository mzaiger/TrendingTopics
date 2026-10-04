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

def finalize(raw_items: list[dict], previous_doc: dict, *, rank_by_duration: bool,
             now: datetime) -> list[dict]:
    """Turn raw feed items into the final JSON items.

    raw item keys: id, topic, description, description_source, url, feed_pos (0-based order in
    the feed), source_start (datetime or None), volume (str or None), extra (dict), and any
    private keys starting with "_" (used by later steps, stripped before saving).

    started_trending = the earlier of (what the feed says, the first time we saw the topic).
    rank = the feed's own order, or - when rank_by_duration - longest-trending first.
    rank_change = previous rank - new rank (positive = moved up).
    """
    prev_items = {x.get("id"): x for x in (previous_doc.get("items") or []) if isinstance(x, dict)}
    had_previous = bool(prev_items)

    staged = []
    for it in raw_items:
        p = prev_items.get(it["id"])
        first_seen = (parse_time(p.get("first_seen")) if p else None) or now
        start = first_seen
        src_start = it.get("source_start")
        if src_start:
            start = min(start, min(src_start, now))
        staged.append((it, p, first_seen, start))

    if rank_by_duration:
        staged.sort(key=lambda s: (s[3], s[0].get("feed_pos", 0)))
    else:
        staged.sort(key=lambda s: s[0].get("feed_pos", 0))

    out = []
    for rank, (it, p, first_seen, start) in enumerate(staged, start=1):
        prev_rank = p.get("rank") if p else None
        final = {
            "id": it["id"],
            "rank": rank,
            "previous_rank": prev_rank,
            "rank_change": (prev_rank - rank) if isinstance(prev_rank, int) else None,
            "is_new": bool(had_previous and p is None),
            "topic": it["topic"],
            "description": it.get("description") or "",
            "description_source": it.get("description_source") or "",
            "url": it.get("url") or "",
            "image_url": "",
            "image_source": "",
            "started_trending": iso(start),
            "first_seen": iso(first_seen),
            "volume": it.get("volume"),
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


def _gemini_prompt(entries: list[dict], task: str) -> str:
    blocks = []
    for e in entries:
        blocks.append(f"=== TOPIC id={e['id']}: {e['topic']} ===\n{e['material']}")
    return (
        "You write short descriptions for a trending-topics page.\n\n"
        f"There are {len(entries)} topics below. For EACH topic: {task}\n"
        "Write ONE or TWO short sentences (max 40 words) per topic.\n\n"
        "Rules:\n"
        "- Use only the material shown under that topic. Do not invent facts or add background.\n"
        "- Do not mix up topics: each description must come from its own topic's material.\n"
        "- If something is unconfirmed, say \"reportedly\" or \"unconfirmed\".\n"
        "- Ignore spam, ads, insults and unrelated arguments. Never repeat slurs or personal attacks.\n"
        "- If the material doesn't make it clear, say so plainly.\n"
        "- Plain language. No hashtags, emojis or usernames. Never name the social network a post "
        "came from; just say \"social media\".\n\n"
        "Return JSON only, with exactly one entry per topic id:\n"
        "{\"descriptions\": [{\"id\": 1, \"description\": \"...\"}, {\"id\": 2, \"description\": \"...\"}]}\n\n"
        + "\n\n".join(blocks)
    )


class ModelUnavailable(Exception):
    pass


def _parse_descriptions(raw: str) -> Optional[dict[int, str]]:
    try:
        data = json.loads(raw)
        rows = data["descriptions"] if isinstance(data, dict) else data
        out = {}
        for row in rows:
            text = str(row["description"]).strip()
            if text:
                out[int(row["id"])] = text
        return out or None
    except (KeyError, ValueError, TypeError):
        return None


def _call_model(model: str, prompt: str, api_key: str) -> Optional[dict[int, str]]:
    """One model. Returns {id: text}, or None if the reply was unusable.
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
        out = _parse_descriptions(raw)
        if out is None:
            warn(f"  {model}: reply wasn't the expected JSON")
        return out
    return None


def gemini_fill(targets: list[dict], task: str) -> Optional[str]:
    """ONE Gemini call for every item in `targets` (items need a "_material" string).
    Sets description + description_source="gemini". Returns the model used, or None."""
    if not targets:
        return None
    api_key = os.environ.get("GEMINI_KEY")
    if not api_key:
        warn("GEMINI_KEY not set - skipping Gemini.")
        return None
    entries = [{"id": i, "topic": t["topic"], "material": t["_material"]}
               for i, t in enumerate(targets, start=1)]
    prompt = _gemini_prompt(entries, task)
    info(f"Asking Gemini about {len(entries)} topic(s) in one call...")
    for model in GEMINI_MODELS:
        try:
            result = _call_model(model, prompt, api_key)
        except ModelUnavailable as e:
            warn(f"  {model} unavailable, trying next: {e}")
            continue
        if result:
            for i, t in enumerate(targets, start=1):
                if result.get(i):
                    t["description"] = shorten(result[i], 300)
                    t["description_source"] = "gemini"
            info(f"Got {len(result)} description(s) from {model}")
            return model
        warn(f"  {model} gave no usable descriptions, trying next.")
    return None


# ----------------------------------------------------------------- DuckDuckGo images

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

    def _search(self, query: str) -> tuple[Optional[str], str]:
        backoff = 4.0
        reason = "retry_exhausted"
        for attempt in range(1, self.max_retries + 1):
            try:
                results = self._client().images(
                    query, region=self.region, safesearch=self.safesearch, max_results=5)
                for r in results:
                    url = r.get("image")
                    if url and url.startswith("http"):
                        return url, "ok"
                return None, "no_results"
            except RatelimitException:
                reason = "ratelimit"
            except TimeoutException:
                reason = "timeout"
            except Exception as e:  # DDGSException and anything the HTTP layer raises
                reason = f"error: {e}"
                if not _retryable(str(e)):
                    return None, reason
            if attempt < self.max_retries:
                wait = min(backoff + random.uniform(0, backoff * 0.5), 60.0)
                warn(f"  image search {reason} on {query!r} (attempt {attempt}/{self.max_retries}) "
                     f"- retrying in {wait:.0f}s")
                time.sleep(wait)
                backoff = min(backoff * 2, 60.0)
        return None, reason

    def find(self, query: str) -> Optional[str]:
        q = re.sub(r"\s+", " ", query or "").replace('"', "").strip()[:200]
        if not q or not self.enabled or self.blocked:
            return None
        key = q.lower()
        if key in self.cache:
            return self.cache[key]
        if self.lookups >= self.max_lookups:
            return None
        self.lookups += 1
        url, reason = self._search(q)
        if url or reason == "no_results":
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
        url = finder.find(it.get("_image_query") or it["topic"])
        if url:
            it["image_url"], it["image_source"] = url, "duckduckgo"
        elif it.get("_image_hint"):
            it["image_url"], it["image_source"] = it["_image_hint"], "feed"
        elif old and old.get("image_url"):
            it["image_url"], it["image_source"] = old["image_url"], old.get("image_source") or "feed"
    info(f"Images: {sum(1 for i in items if i['image_url'])}/{len(items)} topics have one "
         f"({finder.lookups} DuckDuckGo lookup(s) this run).")
