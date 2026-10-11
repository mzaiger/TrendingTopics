#!/usr/bin/env python3
"""
How often the social media workflow really runs, chosen so the Apify bill stays under the free $5.

The Apps Script wakes the social media workflow every 30 minutes. social_media_trends.py then asks this
module "is a run due?" and exits early (free, no Apify call) when it is not. The answer depends on two
things that change the monthly bill:

  * which Twitter/X actor is working   "primary" (cheap) or "fallback" (about 8x the price per run)
  * where Reddit's data came from      "rss" (free), "apify2" (4-hourly), "apify3" (8-hourly, dearest)

For each of the 6 combinations the interval is the FASTEST one in INTERVALS whose total monthly cost
(Twitter + Reddit) is at most BUDGET - MARGIN. Run `python schedule.py` to print the matrix.
Edit the prices below if Apify changes them; the matrix follows.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import trend_common as tc

BUDGET = 5.00            # Apify's free monthly credit
MARGIN = 0.30            # head-room kept under the budget (prices drift, month lengths differ)
DAYS = 30
INTERVALS = [30, 60, 120, 240, 480]      # minutes; 30 is the fastest (the Apps Script ticks every 30)
TREND_COUNT = 20
POSTS = 25

# ---- prices (US dollars), from the actors' pages
TWITTER_RUN = {
    "primary": TREND_COUNT * 0.00005,                  # lurkapi: $0.05 per 1,000 trends, no start fee
    "fallback": 0.003 + TREND_COUNT * 0.00023,         # automation-lab, Free plan: start + per trend
}
REDDIT_MONTH = {
    "rss": 0.0,
    "apify2": (24 / 4) * DAYS * POSTS * 0.0005,        # dami_studio: $0.50 per 1,000, every 4 hours
    "apify3": (24 / 8) * DAYS * (0.005 + POSTS * 0.0015),  # harshmaur pro: start + $0.0015 per post, every 8 hours
}
TWITTER_STATES = ("primary", "fallback")
REDDIT_STATES = ("rss", "apify2", "apify3")


def twitter_month(state: str, interval_min: int) -> float:
    return TWITTER_RUN[state] * (24 * 60 / interval_min) * DAYS


def total_month(twitter: str, reddit: str, interval_min: int) -> float:
    return twitter_month(twitter, interval_min) + REDDIT_MONTH[reddit]


def interval_for(twitter: str, reddit: str) -> int:
    """Fastest allowed interval that keeps the month under BUDGET - MARGIN (slowest if none does)."""
    for minutes in INTERVALS:
        if total_month(twitter, reddit, minutes) <= BUDGET - MARGIN:
            return minutes
    return INTERVALS[-1]


MATRIX = {(t, r): interval_for(t, r) for t in TWITTER_STATES for r in REDDIT_STATES}


# ---- reading the current state from the JSON files
def twitter_state(social_doc: Optional[dict]) -> str:
    return "fallback" if (social_doc or {}).get("actor_used") == "fallback" else "primary"


def reddit_state(reddit_doc: Optional[dict]) -> str:
    """rss / apify2 / apify3. If the 2nd-tier actor's last run failed, the 3rd tier is next, so that
    (dearer) state is assumed."""
    doc = reddit_doc or {}
    src = doc.get("data_source")
    if src in (None, "rss"):
        return "rss"
    return "apify3" if (src == "apify3" or doc.get("apify_last_ok") is False) else "apify2"


def plan(social_doc: Optional[dict], reddit_doc: Optional[dict], now: datetime) -> dict:
    """What the schedule says right now: the two states, the interval, and whether a run is due."""
    t, r = twitter_state(social_doc), reddit_state(reddit_doc)
    minutes = interval_for(t, r)
    forced = os.environ.get("SOCIAL_FORCE_INTERVAL_MINUTES")
    if forced and forced.isdigit():
        minutes = int(forced)
    last = tc.parse_time((social_doc or {}).get("updated"))
    elapsed = None if last is None else (now - last).total_seconds() / 60
    due = last is None or elapsed >= minutes - 5          # 5 minutes of slack for trigger jitter
    return {"twitter": t, "reddit": r, "interval_minutes": minutes, "due": due,
            "minutes_since_last": None if elapsed is None else round(elapsed),
            "month_cost": round(total_month(t, r, minutes), 2)}


def _label(minutes: int) -> str:
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h"


def print_matrix() -> None:
    print(f"Social media interval by state (target <= ${BUDGET - MARGIN:.2f} of ${BUDGET:.2f}/month)\n")
    print(f"{'Twitter / Reddit':<24}" + "".join(f"{r:>20}" for r in REDDIT_STATES))
    for t in TWITTER_STATES:
        row = f"{t:<24}"
        for r in REDDIT_STATES:
            m = MATRIX[(t, r)]
            row += f"{_label(m) + '  $%.2f' % total_month(t, r, m):>20}"
        print(row)
    print("\nReddit alone per month: " + ", ".join(f"{k} ${v:.2f}" for k, v in REDDIT_MONTH.items()))
    print("Twitter per run: " + ", ".join(f"{k} ${v:.4f}" for k, v in TWITTER_RUN.items()))


if __name__ == "__main__":
    print_matrix()
