#!/usr/bin/env python3
"""
X Reply Engine — daily refresh script.

Runs from GitHub Actions cron (01:00 UTC = 06:30 AM IST, Mon-Fri).
Scrapes 109 X accounts via Apify, filters, drafts 2 reply variants per qualifying
tweet using GPT-4.1-mini with Master Reply Prompt v2, writes index.html (GitHub Pages
auto-rebuilds). No email is sent; Rahul reads the queue in the web app.

Required env vars:
    APIFY_TOKEN
    OPENAI_API_KEY
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ============================================================================
# CONFIG
# ============================================================================

APIFY_TOKEN = os.environ["APIFY_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

APIFY_ACTOR = "61RPP7dywgiy0JPD0"  # apidojo/tweet-scraper
OPENAI_MODEL = "gpt-4.1-mini"  # cheap, fast, equally good for this task
HOURS_LOOKBACK = 48  # widened from 24h — Tier 3 accounts don't post daily
MAX_TWEETS_FETCH = 1200  # raised for ~392-account list (was 800 at 109 accounts)

# Tier budgets (replies per day, with buffer for SKIPs)
TIER_BUDGET = {1: 8, 2: 14, 3: 10}

# Engagement floor (lower for Tier 3 to surface smaller-account fresh tweets)
ENG_FLOOR = {1: 50, 2: 15, 3: 5}

# Mute keywords — pre-ranking filter per the 2026 algorithm's MutedKeywordFilter
MUTE_KEYWORDS = [
    "modi", "rahul gandhi", "bjp", "congress", "trump", "biden",
    "election", "muslim", "hindu rashtra", "pakistan", "kashmir",
    "petrol price", "diesel price", "fuel hike", "religion",
    "crypto pump", "shitcoin", "doge to the moon", "memecoin",
]

# Target accounts now live in handles.json (locked "core" + auto-managed "synced").
# The weekly sync-following workflow maintains the "synced" list from @rahul_reddy's follows.
def _load_handles():
    path = Path(__file__).parent / "handles.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    out, seen = [], set()
    for row in (data.get("core", []) + data.get("synced", [])):
        h = row.get("handle", "").strip()
        lo = h.lower()
        if not h or lo in seen:
            continue
        seen.add(lo)
        t = row.get("tier", 3)
        out.append((h, t if t in (1, 2, 3) else 3))
    return out

HANDLES = _load_handles()
TIER_LOOKUP = {h.lower(): t for h, t in HANDLES}

# Master Reply Prompt v2 — LOCKED
MASTER_PROMPT = """You are drafting Twitter/X reply suggestions for Rahul Srinivas, a Bangalore-based founder building an edtech company. Rahul posts the best ones manually — your output never goes live without his approval.

# WHO RAHUL IS
Founder. 36 months in. Bootstrapped. Has helped 70,000+ paid learners. Built his startup's masterclass app on Lovable in 6 weeks with VdoCipher DRM. Runs in-person Forge writing programs in Dehradun and Bangalore. Operates Meta and Google Ads on real budgets. Knows Indian buyer psychology cold.

He is an operator, not a thought leader. He posts receipts, not theories.

# RECEIPTS HE MAY CITE (the ONLY facts you may use)
- 70,000+ paid learners on his platform (refer as "we" / "my startup" / "the cohort I'm building" — NEVER name the brand)
- Indian buyer psychology: "Indian buyers price-signal trust, not affordability"
- Free webinars: ~4% show-up. Paid ₹100 micropaywall: ~38% show-up.
- Built masterclass app on Lovable in 6 weeks with VdoCipher DRM
- Runs writers' programs in Dehradun and Bangalore (3,000+ photos)
- Performance marketing at small-medium budgets in India
- Bootstrapped, Bangalore-based, 36 months in

NEVER invent stats. NEVER cite courses he doesn't teach (no chess, no fitness, no finance, no languages — only writing programs, creator masterclasses, skill cohorts).

# VOICE RULES
- No em dashes. Use commas, colons, periods, hyphens.
- No "Great post!", "Love this!", "100%", "This!", "Spot on!"
- No "As a founder...", "In my experience..."
- No exclamation marks unless the tweet is celebratory
- No emoji unless the tweet uses them
- No empty hedging. Humble hedging is fine ("I keep noticing", "the version that worked for me").
- Max 3 lines. Most replies 1-2 lines.
- Specific over general.
- Confident not combative.
- India-native voice. ₹ not $. Tier 1/2 cities references where it fits.
- Short sentences.
- Don't quote the tweet back at the author.
- NEVER reference politics, religion, named politicians, hot-button cultural figures, crypto memes.
- NEVER produce a reply any reasonable viewer would block, mute, or report.
- NEVER name "LevelUp" or any brand variant. Use "my startup", "we", "I".
- NEVER fabricate stats not in the receipts library.
- NEVER use thought-leader phrasings: "Worth distinguishing", "The truth is", "Most founders treat", "Most people miss". Replace with "I keep noticing", "the version that worked for me", "what I keep seeing".
- CONCRETE OR SKIP. Every reply needs one specific real number, situation, named phenomenon, or pointed question.
- VARY YOUR RECEIPTS. Do not lean on the same stat or anecdote across replies. Spread across the receipts library; when no fresh concrete receipt fits, use SHARPENER or HONEST_QUESTION instead of forcing a repeated number.

# 4 REPLY MODES
1. RECEIPT — share a specific Rahul experience from the receipts library. Concrete required.
2. SHARPENER — take their idea one click deeper, personal observation framing.
3. HONEST_QUESTION — specific question that invites author reply back.
4. HUMAN_REPLY — for off-niche tweets (weather, travel, life events, book reflections, sports without team loyalty). One-line person-on-the-timeline.

# REPLY OR SKIP
REPLY (mode 1-3) if in Rahul's professional lane AND meets at least 3 of: posted <24h ago, has 10-200 replies, topic overlaps receipts, author has 5K+ followers, constructive tone, Rahul can add something specific.
HUMAN_REPLY (mode 4) for off-niche but not political/religious/mute-risky tweets.
SKIP if: pure link, drowned (>200 replies), political/religious/mute-keyword-risky, dunking, milestone celebration, can't fit a concrete object, requires fabricating a stat.

# OUTPUT — strict JSON only, no other text:
{
  "decision": "REPLY" | "HUMAN_REPLY" | "SKIP",
  "skip_reason": "string (only if SKIP)",
  "variants": [
    {"mode": "RECEIPT|SHARPENER|HONEST_QUESTION|HUMAN_REPLY", "text": "max 280 chars"},
    {"mode": "different from variant 1", "text": "..."}
  ],
  "why_this_matters": "one line"
}

ALWAYS exactly 2 variants when decision is REPLY or HUMAN_REPLY. Different modes preferred."""


def load_learnings():
    """Read learnings.jsonl (written by the worker /log endpoint) and summarize
    Rahul's actual mode preferences per tier, to gently bias drafting."""
    from collections import Counter
    path = Path(__file__).parent / "learnings.jsonl"
    if not path.exists():
        return ""
    per_tier = {1: Counter(), 2: Counter(), 3: Counter()}
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("action") not in ("posted", "reply_click"):
            continue
        tier = e.get("tier")
        mode = (e.get("mode") or "").upper()
        if tier in per_tier and mode:
            per_tier[tier][mode] += 1
            n += 1
    if n < 5:
        return ""
    out = ["# RAHUL'S OBSERVED PREFERENCES",
           "(from %d of his real picks; when two modes both fit, lean toward what he actually posts)" % n]
    for tier in (1, 2, 3):
        top = per_tier[tier].most_common(2)
        if top:
            out.append("- Tier %d: " % tier + ", ".join("%s (%d)" % (m, c) for m, c in top))
    return "\n".join(out)


# ============================================================================
# HELPERS
# ============================================================================

def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def http_json(url, data=None, headers=None, method=None, timeout=120):
    headers = headers or {}
    headers.setdefault("User-Agent", "x-reply-engine/1.0")
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ============================================================================
# 1. APIFY SCRAPE
# ============================================================================

def scrape_apify():
    log(f"Scraping Apify for {len(HANDLES)} handles, last {HOURS_LOOKBACK}h, max {MAX_TWEETS_FETCH} tweets")
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    body = {
        "twitterHandles": [h for h, _ in HANDLES],
        "maxItems": MAX_TWEETS_FETCH,
        "sort": "Latest",
        "tweetLanguage": "en",
    }
    tweets = http_json(url, data=body, timeout=180)
    log(f"Apify returned {len(tweets)} items")
    return [t for t in tweets if isinstance(t, dict) and isinstance(t.get("author"), dict)]


# ============================================================================
# 2. FILTER
# ============================================================================

def passes_filter(t, now):
    try:
        dt = datetime.strptime(t["createdAt"], "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        return False, "bad date"
    hours_old = (now - dt).total_seconds() / 3600
    if hours_old > HOURS_LOOKBACK:
        return False, "stale"
    if t.get("isReply") == "True" or t.get("isRetweet") == "True":
        return False, "reply/retweet"
    if t.get("lang") and t["lang"] != "en":
        return False, "non-english"
    text = (t.get("fullText") or t.get("text") or "").strip()
    if len(text) < 30:
        return False, "too short"
    if text.startswith("RT @"):
        return False, "RT prefix"
    likes = int(t.get("likeCount", 0))
    replies = int(t.get("replyCount", 0))
    retweets = int(t.get("retweetCount", 0))
    if replies > 200:
        return False, "drowned"
    engagement = likes + 5 * replies + 3 * retweets
    # Tier-aware engagement floor: lower for T3, higher for T1
    handle = (t.get("author", {}).get("userName") or "").lower()
    tier = TIER_LOOKUP.get(handle, 3)
    if engagement < ENG_FLOOR.get(tier, 10):
        return False, f"engagement below T{tier} floor"
    lower = text.lower()
    for kw in MUTE_KEYWORDS:
        if kw in lower:
            return False, f"mute-keyword: {kw}"
    return True, None


def rank_and_allocate(tweets):
    by_tier = {1: [], 2: [], 3: []}
    seen_authors = set()
    for t in tweets:
        h = t["author"]["userName"].lower()
        tier = TIER_LOOKUP.get(h)
        if not tier:
            continue
        by_tier[tier].append(t)
    for tier in [1, 2, 3]:
        by_tier[tier].sort(
            key=lambda x: -(
                int(x.get("likeCount", 0)) + 5 * int(x.get("replyCount", 0)) + 3 * int(x.get("retweetCount", 0))
            )
        )
    selected = []
    # Author Diversity Scorer: max 1 reply per author per day
    for tier in [1, 2, 3]:
        budget = TIER_BUDGET[tier] + 3  # +3 buffer for SKIPped variants
        for t in by_tier[tier]:
            h = t["author"]["userName"].lower()
            if h in seen_authors:
                continue
            selected.append(t)
            seen_authors.add(h)
            budget -= 1
            if budget <= 0:
                break
    return selected


# ============================================================================
# 3. OPENAI DRAFTS
# ============================================================================

def draft_reply(tweet, learned=""):
    handle = tweet["author"]["userName"]
    name = tweet["author"].get("name", "")
    text = tweet.get("fullText") or tweet.get("text", "")
    likes = tweet.get("likeCount", 0)
    replies = tweet.get("replyCount", 0)
    user_prompt = (
        f"Tweet to evaluate:\n"
        f"Author: @{handle} ({name})\n"
        f"Likes: {likes}, Replies: {replies}\n"
        f'Text: """\n{text}\n"""\n\n'
        f"Apply the rules. Output ONLY the JSON object specified."
    )
    body = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": MASTER_PROMPT + (("\n\n" + learned) if learned else "")},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    try:
        r = http_json("https://api.openai.com/v1/chat/completions", data=body, headers=headers, timeout=90)
        content = r["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        log(f"  draft error on {handle}: {e}")
        return {"decision": "SKIP", "skip_reason": f"draft error: {e}", "variants": []}


# ============================================================================
# 4. HTML GENERATION
# ============================================================================

def build_data_object(drafts, now):
    out = []
    for d in drafts:
        t = d["_tweet"]
        try:
            created_dt = datetime.strptime(t["createdAt"], "%a %b %d %H:%M:%S %z %Y")
            created_iso = created_dt.isoformat()
        except Exception:
            created_iso = None
        out.append({
            "tweetId": t["id"],
            "tier": TIER_LOOKUP.get(t["author"]["userName"].lower(), 3),
            "decision": d.get("decision", "SKIP"),
            "handle": t["author"]["userName"],
            "name": t["author"].get("name", ""),
            "followers": int(t["author"].get("followers", 0) or 0),
            "createdAt": created_iso,
            "likes": int(t.get("likeCount", 0)),
            "replies": int(t.get("replyCount", 0)),
            "retweets": int(t.get("retweetCount", 0)),
            "text": t.get("fullText") or t.get("text", ""),
            "url": t.get("url"),
            "why": d.get("why_this_matters"),
            "skip_reason": d.get("skip_reason"),
            "variants": d.get("variants", []),
        })
    return out


def render_html(data_obj, generated_at, ready_count, skip_count):
    data_json = json.dumps({
        "generatedAt": generated_at.isoformat(),
        "accounts": len(HANDLES),
        "tweets": data_obj,
    }, ensure_ascii=False)
    template = Path(__file__).parent / "index.template.html"
    if template.exists():
        return template.read_text().replace("__DATA_INJECTION__", data_json)
    # Fallback: build inline (the template file is committed alongside this script)
    raise RuntimeError("index.template.html missing")


# ============================================================================
# MAIN
# ============================================================================

def main():
    now = datetime.now(timezone.utc)

    raw = scrape_apify()

    fresh = []
    for t in raw:
        ok, reason = passes_filter(t, now)
        if ok:
            fresh.append(t)
    log(f"After filter: {len(fresh)} (from {len(raw)})")

    selected = rank_and_allocate(fresh)
    log(f"After rank+allocate (1 per author): {len(selected)}")

    drafts = []
    learned = load_learnings()
    if learned:
        log("Applying learned preferences from past picks")
    for i, t in enumerate(selected):
        log(f"  Drafting {i+1}/{len(selected)}: @{t['author']['userName']}")
        d = draft_reply(t, learned)
        d["_tweet"] = t
        drafts.append(d)

    ready = [d for d in drafts if d.get("decision") != "SKIP"]
    skipped = [d for d in drafts if d.get("decision") == "SKIP"]
    by_tier = {1: 0, 2: 0, 3: 0}
    for d in ready:
        tier = TIER_LOOKUP.get(d["_tweet"]["author"]["userName"].lower(), 3)
        by_tier[tier] += 1

    data_obj = build_data_object(drafts, now)

    # Sort by hottest (velocity) for default rendering
    def velocity(d):
        if not d.get("createdAt"):
            return 0
        dt = datetime.fromisoformat(d["createdAt"])
        hours = max(0.5, (now - dt).total_seconds() / 3600)
        return ((d["likes"] or 0) + 5 * (d["replies"] or 0) + 3 * (d["retweets"] or 0)) / hours

    data_obj.sort(key=lambda d: -velocity(d))
    top_hot = [d for d in data_obj if d.get("decision") != "SKIP"][:5]

    html = render_html(data_obj, now, len(ready), len(skipped))
    out_path = Path(__file__).parent / "index.html"
    out_path.write_text(html, encoding="utf-8")
    log(f"Wrote {out_path} ({len(html):,} bytes)")

    # Save state for the next run + UI consumption
    state = {
        "lastRefresh": now.isoformat(),
        "ready": len(ready),
        "skipped": len(skipped),
        "by_tier": by_tier,
        "hot_5": [{"handle": d["handle"], "text": d["text"][:200]} for d in top_hot],
    }
    Path(__file__).parent.joinpath("state.json").write_text(json.dumps(state, indent=2))

    log("DONE")


if __name__ == "__main__":
    main()
