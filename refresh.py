#!/usr/bin/env python3
"""
X Reply Engine — daily refresh script (v2).

Runs from GitHub Actions cron (01:00 UTC = 06:30 AM IST, Mon-Fri).
Scrapes ~390 X accounts via Apify, filters, then:
  1. TRIAGE (cheap model): score topic-fit + tags + human_ok per tweet.
  2. SELECT: rank by engagement-velocity * early-bonus * fit; kill the HUMAN_REPLY
     flood structurally (on-niche pool fit>=5; off-niche human pool hard-capped 4/day).
  3. DRAFT (strong model, GPT-5.5 primary): 2-4 varied variants, topic-relevant
     receipts injected per call, no hardcoded receipt library.
  4. VALIDATE: programmatic voice/safety checks, one retry, drop still-failing variants.
Writes index.html from index.template.html (__DATA_INJECTION__). Rahul posts manually.

Required env vars:
    APIFY_TOKEN
    OPENAI_API_KEY      (drafting + triage; GPT-5.x chain with gpt-4.1 fallback)

Env modes:
    DRY_RUN=1           -> no state/metrics mutation; write index to $DRY_RUN_OUT;
                           print a summary block. Learnings 'offered' line goes to a
                           temp copy, not the real file.
    DRY_RUN_OUT=path    -> where DRY_RUN writes the rendered index (default dry-index.html)
    SMOKE_HANDLES=h1,h2 -> restrict scrape to these handles, small maxItems.
    MIDDAY=1            -> cheaper afternoon run (T12_FETCH=500, T3_FETCH=300).
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
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

APIFY_ACTOR = "61RPP7dywgiy0JPD0"  # apidojo/tweet-scraper

# Models
# All-OpenAI stack (single provider by owner's choice). GPT-5.x models take
# max_completion_tokens (not max_tokens) and run with reasoning_effort "none"
# so replies stay fast/cheap; openai_chat() handles the per-family params.
TRIAGE_MODEL_CHAIN = ("gpt-5.4-mini", "gpt-4.1-mini")
DRAFT_MODEL_CHAIN = ("gpt-5.5", "gpt-5.4", "gpt-4.1")

# Env modes
DRY_RUN = os.environ.get("DRY_RUN") == "1"
DRY_RUN_OUT = os.environ.get("DRY_RUN_OUT") or str(Path(__file__).parent / "dry-index.html")
MIDDAY = os.environ.get("MIDDAY") == "1"
SMOKE_HANDLES = [h.strip() for h in os.environ.get("SMOKE_HANDLES", "").split(",") if h.strip()]

HOURS_LOOKBACK = 48  # widened from 24h — Tier 3 accounts don't post daily
MAX_TWEETS_FETCH = 1200  # legacy total ceiling (kept for reference / 1200-era safety)
if MIDDAY:
    T12_FETCH = 500
    T3_FETCH = 300
else:
    T12_FETCH = 1000  # latest tweets across T1/T2 handles
    T3_FETCH = 700    # dedicated T3 pass so rising-voice long tail is not crowded out
SMOKE_FETCH = 150     # per-batch cap when SMOKE_HANDLES is set

SELF_HANDLE = "rahul_reddy"  # Rahul's own account, for growth + reply tracking

# Off-niche HUMAN_REPLY items admitted to the queue per day (structural flood cap)
HUMAN_REPLY_DAY_CAP = 4
# Receipt cooldown: don't reuse a receipt used within this many days
RECEIPT_COOLDOWN_DAYS = 7
# Minimum ready items required to overwrite the live index.html. A successful-but-
# empty scrape (all filtered/skipped) must not blank the queue: stale beats blank.
MIN_READY_FLOOR = 1

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

# Banned openers / phrases the validator rejects (case-insensitive substring)
BANNED_PHRASES = [
    "great post", "love this", "100%", "this!", "spot on", "as a founder",
    "in my experience", "worth distinguishing", "the truth is",
    "most founders treat", "most people miss",
]


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


def load_receipts():
    """Load receipts.json (authored by the receipts workstream). Returns the list of
    receipt dicts. If missing, returns [] (drafting still works, just receipt-free)."""
    path = Path(__file__).parent / "receipts.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rs = data.get("receipts", [])
        return [r for r in rs if isinstance(r, dict) and r.get("id")]
    except Exception as e:
        log(f"receipts.json load failed: {e}")
        return []


# ============================================================================
# PROMPT v3 — identity, voice, safety. Receipts injected per call (NOT hardcoded).
# ============================================================================

MASTER_PROMPT = """You are drafting Twitter/X reply suggestions for Rahul Srinivas, a Bangalore-based founder building an edtech company. Rahul posts the best ones manually — your output never goes live without his approval.

# WHO RAHUL IS
Founder. 36 months in. Bootstrapped. Has helped 70,000+ paid learners. Built his startup's masterclass app on Lovable in 6 weeks with VdoCipher DRM. Runs in-person Forge writing programs in Dehradun and Bangalore. Operates Meta and Google Ads on real Indian budgets. Knows Indian buyer psychology cold.

He is an operator, not a thought leader. He posts receipts, not theories.

# RECEIPTS YOU MAY CITE
You will be given a short list of RELEVANT RECEIPTS below (facts, stats, stories Rahul can honestly claim). Those are the ONLY specific facts about Rahul you may state. If none of them fit the tweet, DO NOT force one — a sharp specific observation or a genuine question is better than a shoehorned stat. NEVER invent a number, a program, or an outcome that is not in a provided receipt. NEVER cite things he doesn't do (no chess, fitness, finance, languages — only writing programs, creator masterclasses, skill cohorts).

# VOICE RULES
- No em dashes (—). Use commas, colons, periods, hyphens.
- No "Great post", "Love this", "100%", "This!", "Spot on".
- No "As a founder...", "In my experience...".
- No thought-leader phrasings: "Worth distinguishing", "The truth is", "Most founders treat", "Most people miss". Replace with "I keep noticing", "the version that worked for me", "what I keep seeing".
- No exclamation marks unless the tweet is celebratory. No emoji unless the tweet uses them.
- Humble hedging is fine ("I keep noticing", "the version that worked for me").
- Max 3 lines. Most replies 1-2 lines. Short sentences. Specific over general.
- Confident not combative. India-native voice. Rupee not dollar where money comes up.
- Don't quote the tweet back at the author.
- NEVER reference politics, religion, named politicians, hot-button cultural figures, crypto memes.
- NEVER produce a reply any reasonable viewer would block, mute, or report.
- NEVER name "LevelUp" or any brand variant. Use "my startup", "we", "I".
- Concrete beats generic: a sharp specific observation or a pointed question is fine. NEVER force a receipt that doesn't fit — prefer none.

# REPLY MODES
1. RECEIPT — cite one specific provided receipt as Rahul's own experience.
2. SHARPENER — take their idea one click deeper, personal observation framing.
3. HONEST_QUESTION — a specific question that invites the author to reply back.
4. HUMAN_REPLY — a light one-line person-on-the-timeline reply (for off-niche or as a warm alternative).

# YOUR TASK
Draft 2 to 4 reply variants for the tweet. Rules for the variant set:
- The modes must DIFFER from each other.
- At most ONE HUMAN_REPLY variant, and only include it if the tweet is on-niche enough that a human note adds warmth; for clearly off-niche tweets a single HUMAN_REPLY is fine.
- Exactly one variant must be the SHORT PUNCHY version (<= 120 chars).
- For each variant report which receipt id you used (from the provided list), or null if none.
- Every variant <= 280 characters.

# OUTPUT — strict JSON only, no other text, no code fences:
{
  "decision": "REPLY" | "HUMAN_REPLY" | "SKIP",
  "skip_reason": "string (only if SKIP)",
  "variants": [
    {"mode": "RECEIPT|SHARPENER|HONEST_QUESTION|HUMAN_REPLY", "text": "...", "receipt_used": "receipt-id or null"}
  ],
  "why_this_matters": "one line"
}"""


# ============================================================================
# HELPERS
# ============================================================================

def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def http_json(url, data=None, headers=None, method=None, timeout=120):
    headers = headers or {}
    headers.setdefault("User-Agent", "x-reply-engine/2.0")
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _strip_json(text):
    """Pull a JSON object/array out of a model response, tolerating code fences."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    # If there's leading/trailing prose, grab the first {...} or [...] block.
    if not (t.startswith("{") or t.startswith("[")):
        m = re.search(r"(\{.*\}|\[.*\])", t, re.DOTALL)
        if m:
            t = m.group(1)
    return json.loads(t)


def openai_chat(model, system, user, max_tokens=900, temperature=0.7, json_mode=True, timeout=90):
    if not OPENAI_API_KEY:
        raise RuntimeError("no OPENAI_API_KEY")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    if model.startswith(("gpt-5", "o3", "o4")):
        # GPT-5 / o-series: max_tokens is rejected; reasoning off keeps replies
        # fast and avoids burning the completion budget on hidden reasoning.
        body["max_completion_tokens"] = max_tokens
        body["reasoning_effort"] = "none"
    else:
        body["max_tokens"] = max_tokens
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    try:
        r = http_json("https://api.openai.com/v1/chat/completions", data=body, headers=headers, timeout=timeout)
    except urllib.error.HTTPError as e:
        # Some model revisions reject non-default temperature; retry once without it.
        if e.code == 400 and "temperature" in str(e.read()[:500]):
            body.pop("temperature", None)
            r = http_json("https://api.openai.com/v1/chat/completions", data=body, headers=headers, timeout=timeout)
        else:
            raise
    return r["choices"][0]["message"]["content"]


# token accounting (rough, for the DRY_RUN cost estimate) ---------------------
_TOK = {"triage_in": 0, "triage_out": 0, "draft_in": 0, "draft_out": 0}

def _est_tokens(s):
    return max(1, int(len(s or "") / 4))


# ============================================================================
# 1. APIFY SCRAPE
# ============================================================================

class ScrapeError(RuntimeError):
    """Raised when the Apify scrape yields no usable data at all (every batch
    failed after retries). main() treats this as fatal-but-safe: exit non-zero
    WITHOUT overwriting the previously served index.html."""


SCRAPE_ATTEMPTS = 3
SCRAPE_BACKOFF = 5  # seconds; grows linearly per attempt


def _scrape_batch(handles, max_items):
    """Scrape one batch with retry+backoff. Returns the item list on success
    (possibly empty). Raises ScrapeError if all attempts fail — Apify's
    run-sync endpoint intermittently 429/500/times-out on a live cron, and an
    unguarded urlopen there would abort the whole daily run."""
    if not handles:
        return []
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    body = {"twitterHandles": handles, "maxItems": max_items, "sort": "Latest", "tweetLanguage": "en"}
    last_err = None
    for attempt in range(1, SCRAPE_ATTEMPTS + 1):
        try:
            items = http_json(url, data=body, timeout=300)
            return [t for t in items if isinstance(t, dict) and isinstance(t.get("author"), dict)]
        except Exception as e:
            last_err = e
            if attempt < SCRAPE_ATTEMPTS:
                wait = SCRAPE_BACKOFF * attempt
                log(f"  Apify batch attempt {attempt}/{SCRAPE_ATTEMPTS} failed ({e}); retry in {wait}s")
                time.sleep(wait)
    raise ScrapeError(f"batch of {len(handles)} handles failed after "
                      f"{SCRAPE_ATTEMPTS} attempts: {last_err}")


def scrape_apify():
    """Run the configured batches, tolerating a single failed batch by
    continuing with whatever the other returned. Raises ScrapeError only when
    EVERY attempted batch failed (so we never overwrite index.html with empty)."""
    batches = []  # (label, handles, max_items)
    if SMOKE_HANDLES:
        log(f"SMOKE mode: scraping {len(SMOKE_HANDLES)} handles, maxItems={SMOKE_FETCH}")
        # Single batch keeps live-test cost tiny; dedupe below still applies.
        batches.append(("smoke", SMOKE_HANDLES, SMOKE_FETCH))
    else:
        t12 = [h for h, t in HANDLES if t in (1, 2)]
        t3 = [h for h, t in HANDLES if t == 3]
        log(f"Scraping Apify: {len(t12)} T1/T2 handles + dedicated {len(t3)} T3 pass, last {HOURS_LOOKBACK}h")
        batches.append(("T1/T2", t12, T12_FETCH))
        batches.append(("T3", t3, T3_FETCH))

    results, attempted, failed = [], 0, 0
    for label, handles, max_items in batches:
        if not handles:
            continue
        attempted += 1
        try:
            results.append(_scrape_batch(handles, max_items))
        except ScrapeError as e:
            failed += 1
            log(f"  Batch '{label}' abandoned after retries: {e}")
            results.append([])

    if attempted and failed == attempted:
        # Every non-empty batch failed -> no data. Signal fatal-but-safe.
        raise ScrapeError(f"all {attempted} scrape batch(es) failed")

    seen, out = set(), []
    for t in [item for batch in results for item in batch]:
        tid = t.get("id")
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        out.append(t)
    counts = " + ".join(str(len(b)) for b in results) or "0"
    log(f"Apify returned {counts} = {len(out)} unique"
        + (f" ({failed}/{attempted} batches failed, continuing partial)" if failed else ""))
    return out


# ============================================================================
# 2. FILTER
# ============================================================================

def _is_reply(t):
    """Real Apify output: isReply is a python bool (not the string 'True')."""
    v = t.get("isReply")
    return v is True or (isinstance(v, str) and v.lower() == "true")


def _is_retweet(t):
    v = t.get("isRetweet")
    return v is True or (isinstance(v, str) and v.lower() == "true")


def _handle(t):
    """Lowercased author handle, or '' when the author dict is sparse/missing
    (apidojo sometimes omits userName). Never raises KeyError/AttributeError."""
    return ((t.get("author") or {}).get("userName") or "").lower()


def passes_filter(t, now):
    try:
        dt = datetime.strptime(t["createdAt"], "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        return False, "bad date"
    hours_old = (now - dt).total_seconds() / 3600
    if hours_old > HOURS_LOOKBACK:
        return False, "stale"
    if _is_reply(t) or _is_retweet(t):
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
    handle = (t.get("author", {}).get("userName") or "").lower()
    if not handle:
        # apidojo occasionally returns sparse author objects with no userName.
        # Downstream ranking/drafting key off the handle, so a blank one would
        # crash rank_and_allocate (KeyError 'userName') and abort the whole run.
        return False, "no author handle"
    tier = TIER_LOOKUP.get(handle, 3)
    if engagement < ENG_FLOOR.get(tier, 10):
        return False, f"engagement below T{tier} floor"
    lower = text.lower()
    for kw in MUTE_KEYWORDS:
        if kw in lower:
            return False, f"mute-keyword: {kw}"
    return True, None


# ============================================================================
# 2b. TRIAGE (cheap model) — fit / topics / human_ok per tweet
# ============================================================================

TRIAGE_SYSTEM = """You triage tweets for Rahul: a bootstrapped Indian edtech founder who writes programs, runs performance marketing (Meta/Google on Indian budgets), understands buyer psychology, and builds products with AI tools. For each tweet decide whether Rahul could add something SUBSTANTIVE (not generic agreement) as a reply.

Return STRICT JSON only, no prose, no code fences: an array with one object per input tweet:
[{"id": "<tweet id>", "fit": <int 0-10>, "topics": ["tag", ...], "human_ok": <true|false>}]

fit = how much genuine, specific value Rahul can add (10 = squarely his lane with a real angle; 0 = nothing to add / outside his world).
topics = short lowercase tags from THIS vocabulary where they apply: %s . Use only tags that fit; [] is fine.
human_ok = true only if the tweet is harmless and off-niche (life event, travel, book, non-tribal sports) and a light friendly human reply would be welcome. false for anything political/religious/divisive."""


def _triage_vocab(receipts):
    vocab = set()
    for r in receipts:
        for tag in r.get("topics", []):
            vocab.add(str(tag).lower())
    # a few generic anchors so triage isn't starved when receipts are sparse
    vocab.update(["founder", "startup", "product", "content", "audience", "off-niche"])
    return sorted(vocab)


def _triage_user_payload(batch):
    lines = []
    for t in batch:
        txt = (t.get("fullText") or t.get("text") or "").replace("\n", " ").strip()
        lines.append({"id": str(t.get("id")), "text": txt[:400]})
    return "Triage these tweets. Return the JSON array only.\n" + json.dumps(lines, ensure_ascii=False)


def triage_batch(batch, receipts):
    """Return {tweet_id: {fit, topics, human_ok}}. On failure -> fit=5 default for the batch."""
    system = TRIAGE_SYSTEM % ", ".join(_triage_vocab(receipts))
    user = _triage_user_payload(batch)
    _TOK["triage_in"] += _est_tokens(system) + _est_tokens(user)
    raw = None
    for model in TRIAGE_MODEL_CHAIN:
        try:
            # OpenAI json_mode requires an object; ask for {"items":[...]}
            raw = openai_chat(model, system,
                              user + '\nReturn as {"items": [...]}.',
                              max_tokens=1500, temperature=0.2, json_mode=True, timeout=90)
            break
        except Exception as e:
            log(f"  triage {model} failed ({e}); trying next")
    if raw is None:
        log(f"  triage failed on all models; defaulting fit=5 for batch of {len(batch)}")
        return {str(t.get("id")): {"fit": 5, "topics": [], "human_ok": False} for t in batch}
    _TOK["triage_out"] += _est_tokens(raw)
    try:
        parsed = _strip_json(raw)
        if isinstance(parsed, dict):
            parsed = parsed.get("items") or parsed.get("results") or []
        out = {}
        for row in parsed:
            rid = str(row.get("id"))
            fit = row.get("fit", 5)
            try:
                fit = max(0, min(10, int(fit)))
            except Exception:
                fit = 5
            out[rid] = {
                "fit": fit,
                "topics": [str(x).lower() for x in (row.get("topics") or [])][:6],
                "human_ok": bool(row.get("human_ok")),
            }
        # any tweet the model omitted -> default 5
        for t in batch:
            out.setdefault(str(t.get("id")), {"fit": 5, "topics": [], "human_ok": False})
        return out
    except Exception as e:
        log(f"  triage parse failed ({e}); defaulting fit=5 for batch")
        return {str(t.get("id")): {"fit": 5, "topics": [], "human_ok": False} for t in batch}


def triage_all(tweets, receipts, batch_size=30):
    result = {}
    for i in range(0, len(tweets), batch_size):
        batch = tweets[i:i + batch_size]
        log(f"  Triage batch {i // batch_size + 1}: {len(batch)} tweets")
        result.update(triage_batch(batch, receipts))
    return result


# ============================================================================
# 3. SELECTION — velocity * early-bonus * fit, tier budgets, author diversity,
#    on-niche vs off-niche(human) pools with a hard human cap.
# ============================================================================

def _velocity(t, now):
    try:
        dt = datetime.strptime(t["createdAt"], "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        return 0.0
    hours = max(0.5, (now - dt).total_seconds() / 3600)
    eng = int(t.get("likeCount", 0)) + 5 * int(t.get("replyCount", 0)) + 3 * int(t.get("retweetCount", 0))
    return eng / hours


def _is_early(t, now):
    """<3h old AND replyCount < 25 at scrape time."""
    try:
        dt = datetime.strptime(t["createdAt"], "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        return False
    hours = (now - dt).total_seconds() / 3600
    return hours < 3 and int(t.get("replyCount", 0)) < 25


def compute_priority(t, tri, now):
    fit = tri.get("fit", 5)
    early = _is_early(t, now)
    vel = _velocity(t, now)
    # fit weighting: linear in fit/10 but never fully zero (0.15 floor keeps some
    # signal for triage-borderline items), early gives a 1.5x nudge.
    fit_w = 0.15 + 0.85 * (fit / 10.0)
    return vel * (1.0 + 0.5 * (1 if early else 0)) * fit_w


def rank_and_allocate(tweets, triage, now):
    """Split into on-niche and off-niche(human) pools; apply tier budgets +
    author diversity to the on-niche pool; append a hard-capped human pool."""
    for t in tweets:
        t["_tri"] = triage.get(str(t.get("id")), {"fit": 5, "topics": [], "human_ok": False})
        t["_priority"] = compute_priority(t, t["_tri"], now)
        t["_early"] = _is_early(t, now)

    on_niche = [t for t in tweets if t["_tri"].get("fit", 5) >= 5]
    off_niche = [t for t in tweets if t["_tri"].get("fit", 5) < 5 and t["_tri"].get("human_ok")]

    by_tier = {1: [], 2: [], 3: []}
    for t in on_niche:
        h = _handle(t)
        tier = TIER_LOOKUP.get(h)
        if tier:
            by_tier[tier].append(t)
    for tier in (1, 2, 3):
        by_tier[tier].sort(key=lambda x: -x["_priority"])

    selected = []
    seen_authors = set()
    for tier in (1, 2, 3):
        budget = TIER_BUDGET[tier] + 3  # +3 buffer for variants that still SKIP
        for t in by_tier[tier]:
            h = _handle(t)
            if h in seen_authors:
                continue
            selected.append(t)
            seen_authors.add(h)
            budget -= 1
            if budget <= 0:
                break

    # off-niche human pool: highest-priority first, 1/author, hard day cap
    off_niche.sort(key=lambda x: -x["_priority"])
    human_added = 0
    for t in off_niche:
        if human_added >= HUMAN_REPLY_DAY_CAP:
            break
        h = _handle(t)
        if h in seen_authors:
            continue
        t["_human_only"] = True  # signal to drafter: off-niche, human reply only
        selected.append(t)
        seen_authors.add(h)
        human_added += 1

    log(f"Selection: {len(selected)} total "
        f"({len(selected) - human_added} on-niche + {human_added} off-niche human)")
    return selected


# ============================================================================
# 3b. RECEIPT SELECTION — pick 6-8 most topic-relevant, honoring cooldown + run set
# ============================================================================

def rank_receipts(receipts, topics, used_recent, used_this_run, limit=8):
    """Rank by overlap of receipt.topics with the tweet's triage topics.
    Exclude receipts used in the last 7 days (state) or already used this run.

    Only receipts with NON-ZERO topic overlap are returned. A 0-overlap fallback
    tail was the root of receipt-fixation: on tweets whose triage topics match no
    receipt, the tail is identical every time (same sort order, always led by the
    first receipt in the file), so the model repeatedly saw the same receipt first
    and shoehorned it into unrelated tweets. When nothing overlaps we hand the model
    an empty list; _draft_user_payload then tells it to cite no receipt and use
    SHARPENER / HONEST_QUESTION / HUMAN_REPLY instead. Better a receipt-free reply
    than the same off-topic receipt on every off-niche tweet."""
    topic_set = set(topics or [])
    scored = []
    for r in receipts:
        rid = r["id"]
        if rid in used_recent or rid in used_this_run:
            continue
        overlap = len(topic_set & set(r.get("topics", [])))
        if overlap == 0:
            continue  # never surface a receipt that shares no topic with the tweet
        scored.append((overlap, rid, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, _, r in scored[:limit]]


# ============================================================================
# 4. DRAFTING (strong model) with fallback chain
# ============================================================================

def _draft_user_payload(tweet, receipts_for_call, human_only):
    author = tweet.get("author") or {}
    handle = author.get("userName", "")
    name = author.get("name", "")
    text = tweet.get("fullText") or tweet.get("text", "")
    likes = tweet.get("likeCount", 0)
    replies = tweet.get("replyCount", 0)
    tri = tweet.get("_tri", {})
    if receipts_for_call:
        rlines = "\n".join(
            f'- id={r["id"]} [{r.get("kind","")}] ({", ".join(r.get("topics", []))}): {r.get("text","")}'
            for r in receipts_for_call
        )
    else:
        rlines = "(none relevant — do not cite any receipt; use SHARPENER / HONEST_QUESTION / HUMAN_REPLY)"
    niche_note = (
        "This tweet is OFF-NICHE for Rahul. Return ONE HUMAN_REPLY variant only (warm, light, one line)."
        if human_only else
        "This tweet is ON-NICHE. Return 2-4 varied variants; at most one HUMAN_REPLY."
    )
    return (
        f"Tweet to reply to:\n"
        f"Author: @{handle} ({name})\n"
        f"Likes: {likes}, Replies: {replies}\n"
        f"Triage fit: {tri.get('fit')}, topics: {tri.get('topics')}\n"
        f'Text: """\n{text}\n"""\n\n'
        f"{niche_note}\n\n"
        f"RELEVANT RECEIPTS (cite by id, or null):\n{rlines}\n\n"
        f"Output ONLY the JSON object specified. No code fences."
    )


def draft_reply(tweet, receipts_for_call, human_only, feedback=None):
    """Draft variants. Fallback chain: gpt-5.5 -> gpt-5.4 -> gpt-4.1.
    Returns (parsed_dict, model_name)."""
    system = MASTER_PROMPT
    user = _draft_user_payload(tweet, receipts_for_call, human_only)
    if feedback:
        user += f"\n\nYour previous attempt had these problems, fix them:\n{feedback}"
    _TOK["draft_in"] += _est_tokens(system) + _est_tokens(user)

    for model in DRAFT_MODEL_CHAIN:
        try:
            raw = openai_chat(model, system, user, max_tokens=900, temperature=0.8,
                              json_mode=True, timeout=90)
            _TOK["draft_out"] += _est_tokens(raw)
            return _strip_json(raw), model
        except Exception as e:
            log(f"  draft {model} failed on @{_handle(tweet)}: {e}")

    return {"decision": "SKIP", "skip_reason": "all draft models failed", "variants": []}, "none"


# ============================================================================
# 4b. DRAFT VALIDATOR
# ============================================================================

_NUM_RE = re.compile(r"\d[\d,]*")

# A bare number only reads as a claimed *metric* when it sits next to a unit or a
# metric noun (12%, 3x, 50k, ₹100, "200 users", "38 learners"). Pure standalone
# integers read as years, round counts, or ordinals ("ran 500 experiments",
# "back in 2019") — honest, on-voice constructions the 'specific over general'
# rule actively wants — so those are NOT treated as unsourced stats.
# Units that can immediately FOLLOW a number and turn it into a metric (12%, 3x, 50k).
_STAT_TRAIL_UNIT_RE = re.compile(r"[%x×kKmMbB]")
# Symbols that can immediately PRECEDE a number and turn it into a metric (₹100, $50).
# Kept separate so a letter ending the previous word (e.g. the 'k' in "Took 200")
# is never misread as a unit.
_STAT_LEAD_UNIT_RE = re.compile(r"[₹$]")
# Metric nouns only — audience/volume/money words that make a number a claimed stat.
# Deliberately EXCLUDES duration words (months/weeks/days/years): "36 months
# bootstrapped", "after 18 months" are honest lived timelines, the same honest-count
# class as "ran 500 experiments", not fabricated metrics.
_STAT_WORD_RE = re.compile(
    r"\b(percent|per\s*cent|users?|learners?|customers?|students?|subscribers?|"
    r"followers?|signups?|leads?|sales?|revenue|rupees?|crores?|lakhs?|dollars?)\b",
    re.IGNORECASE,
)

def _digits_in(s):
    """Return the set of digit-runs (commas stripped) present in a string."""
    return {m.replace(",", "") for m in _NUM_RE.findall(s or "")}


def _looks_like_year(num):
    """A 4-digit value in a plausible year range reads as a year, not a stat."""
    return len(num) == 4 and 1900 <= int(num) <= 2099


def _is_claimed_stat(match, text):
    """True if the numeric match at this position reads as a claimed metric —
    i.e. it is adjacent to a unit char (%, x, k, M, ₹, $) or a metric noun. The
    small window on each side keeps '38%' and '200 users' in scope while leaving
    standalone counts and years out."""
    start, end = match.start(), match.end()
    # unit immediately trailing the digits (no space): "12%", "3x", "50k"
    if _STAT_TRAIL_UNIT_RE.match(text[end:end + 1] or ""):
        return True
    # a metric noun within a short window right after the number ("200 users",
    # "1200 learners" -> the window catches the noun)
    if _STAT_WORD_RE.search(text[end:end + 12]):
        return True
    # a currency symbol immediately before the digits (₹100, $50)
    if start > 0 and _STAT_LEAD_UNIT_RE.match(text[start - 1]):
        return True
    return False


def validate_variant(v, tweet, receipts_for_call):
    """Return list of violation strings (empty = valid)."""
    problems = []
    text = (v.get("text") or "").strip()
    if not text:
        return ["empty text"]
    low = text.lower()
    if "—" in text:
        problems.append("contains em dash")
    for p in BANNED_PHRASES:
        if p in low:
            problems.append(f"banned phrase: '{p}'")
    if len(text) > 280:
        problems.append(f"too long ({len(text)} chars)")
    if "levelup" in low or "level up" in low:
        problems.append("names brand")
    # No fabricated STATS: any claimed metric in the reply must appear in the tweet
    # text or a provided receipt. A number only counts as a claimed metric when it is
    # adjacent to a unit/metric noun (12%, 50k, ₹100, "200 users"); bare standalone
    # integers and 4-digit years are honest constructions ("ran 500 experiments",
    # "back in 2019") and are NOT flagged — that fought the 'specific over general'
    # voice rule and cost variant yield on perfectly good replies.
    allowed = _digits_in(tweet.get("fullText") or tweet.get("text", ""))
    for r in receipts_for_call:
        allowed |= _digits_in(r.get("text", ""))
    for match in _NUM_RE.finditer(text):
        num = match.group(0).replace(",", "")
        if num in allowed:
            continue
        if _looks_like_year(num):
            continue
        if _is_claimed_stat(match, text):
            problems.append(f"unsourced number: {num}")
    return problems


def validate_and_fix(parsed, tweet, receipts_for_call, human_only):
    """Validate each variant; if any fail, one retry with feedback; drop still-failing.
    Also enforces the mode-mix rules. Returns cleaned parsed dict + the receipt ids used."""
    valid_ids = {r["id"] for r in receipts_for_call}

    def clean(p, seen_receipts=None):
        # seen_receipts: receipt ids already cited by a KEPT variant in this draft.
        # A given receipt may be cited at most once per tweet; on a repeat we drop the
        # linkage (receipt_used -> None) rather than the whole variant, so the reply
        # still ships but no longer double-counts the same receipt (the kunalb11 CRED
        # tweet previously cited price-signal-trust in two of four variants).
        if seen_receipts is None:
            seen_receipts = set()
        variants = p.get("variants") or []
        kept, dropped_feedback = [], []
        human_seen = False
        for v in variants:
            mode = (v.get("mode") or "").upper()
            probs = validate_variant(v, tweet, receipts_for_call)
            # Off-niche (human_only) items must yield HUMAN_REPLY only. Enforce in
            # code, not just the prompt: drop substantive modes and keep at most one
            # HUMAN_REPLY, so a model that ignores the instruction can't reintroduce
            # mode collapse via founder-flavored replies on off-niche viral tweets.
            if human_only:
                if mode != "HUMAN_REPLY":
                    probs.append("non-HUMAN_REPLY variant on off-niche tweet")
                elif human_seen:
                    probs.append("second HUMAN_REPLY on off-niche tweet")
                human_seen = True
            # at most one HUMAN_REPLY on on-niche items
            elif mode == "HUMAN_REPLY":
                if human_seen:
                    probs.append("second HUMAN_REPLY on on-niche tweet")
                human_seen = True
            if probs:
                dropped_feedback.append(f'- variant "{v.get("text","")[:60]}...": {"; ".join(probs)}')
                continue
            v["mode"] = mode
            v.setdefault("receipt_used", v.get("receipt_used"))
            ru = v.get("receipt_used")
            if str(ru).lower() in ("null", "none", ""):
                v["receipt_used"] = None
            elif ru not in valid_ids:
                # The model cited an id that isn't in the receipts handed to this
                # call (hallucinated or lightly mangled). Never let it reach the
                # queue item or state.receipts_used — drop the linkage to None.
                log(f"    dropping unknown receipt_used '{ru}' (not in {sorted(valid_ids)})")
                v["receipt_used"] = None
            elif ru in seen_receipts:
                # Same receipt already cited by an earlier kept variant in this draft.
                # Keep the variant text but drop the duplicate linkage so one receipt
                # can't be double-counted across variants of a single tweet.
                log(f"    de-duping repeated receipt_used '{ru}' within draft -> None")
                v["receipt_used"] = None
            else:
                seen_receipts.add(ru)
            kept.append(v)
        return kept, dropped_feedback

    seen_receipts = set()
    kept, feedback = clean(parsed, seen_receipts)
    if feedback and parsed.get("decision") != "SKIP":
        # one retry, feeding the violations back. Pass the same seen_receipts set so
        # retry variants can't reintroduce a receipt already cited by a kept variant.
        retry, _model = draft_reply(tweet, receipts_for_call, human_only,
                                    feedback="\n".join(feedback))
        rkept, _ = clean(retry, seen_receipts)
        # merge: keep original valid + any new valid, dedupe by mode
        seen_modes = {v["mode"] for v in kept}
        for v in rkept:
            if v["mode"] not in seen_modes:
                kept.append(v)
                seen_modes.add(v["mode"])

    parsed["variants"] = kept
    if not kept and parsed.get("decision") != "SKIP":
        parsed["decision"] = "SKIP"
        parsed["skip_reason"] = parsed.get("skip_reason") or "all variants failed validation"

    used = [v["receipt_used"] for v in kept if v.get("receipt_used")]
    return parsed, used


# ============================================================================
# 5. HTML GENERATION
# ============================================================================

def build_data_object(drafts, now):
    out = []
    for d in drafts:
        t = d["_tweet"]
        tri = t.get("_tri", {})
        try:
            created_dt = datetime.strptime(t["createdAt"], "%a %b %d %H:%M:%S %z %Y")
            created_iso = created_dt.isoformat()
        except Exception:
            created_iso = None
        variants = d.get("variants", [])
        receipt_used = next((v.get("receipt_used") for v in variants if v.get("receipt_used")), None)
        out.append({
            "tweetId": t["id"],
            "tier": TIER_LOOKUP.get(_handle(t), 3),
            "decision": d.get("decision", "SKIP"),
            "handle": (t.get("author") or {}).get("userName", ""),
            "name": (t.get("author") or {}).get("name", ""),
            "followers": int((t.get("author") or {}).get("followers", 0) or 0),
            "createdAt": created_iso,
            "likes": int(t.get("likeCount", 0)),
            "replies": int(t.get("replyCount", 0)),
            "retweets": int(t.get("retweetCount", 0)),
            "text": t.get("fullText") or t.get("text", ""),
            "url": t.get("url"),
            "why": d.get("why_this_matters"),
            "skip_reason": d.get("skip_reason"),
            # v2 fields
            "fit": int(tri.get("fit", 5)),
            "early": bool(t.get("_early")),
            "priority": round(float(t.get("_priority", 0.0)), 3),
            "receipt_used": receipt_used,
            "variants": variants,
        })
    return out


def render_html(data_obj, generated_at, models=None):
    data_json = json.dumps({
        "generatedAt": generated_at.isoformat(),
        "accounts": len(HANDLES),
        "models": models or {},  # which model served each ready draft (UI meta line)
        "tweets": data_obj,
    }, ensure_ascii=False)
    # HTML-safe the JSON before embedding it in an inline <script> block.
    # json.dumps does NOT escape <, >, & or /, so a tweet/variant containing
    # '</script>' would terminate the script tag early (blank queue) and let
    # attacker-controlled tweet text execute as HTML in the PWA origin (stored XSS).
    # Escaping these to \uXXXX is inert inside a JS string literal but no longer
    # closes the tag or opens a new element. u2028/u2029 are also escaped as they
    # are line terminators in JS string literals.
    data_json = (data_json.replace("<", "\\u003c")
                          .replace(">", "\\u003e")
                          .replace("&", "\\u0026")
                          .replace("\u2028", "\\u2028")
                          .replace("\u2029", "\\u2029"))
    template = Path(__file__).parent / "index.template.html"
    if template.exists():
        return template.read_text().replace("__DATA_INJECTION__", data_json)
    raise RuntimeError("index.template.html missing")


# ============================================================================
# 6. LEARNING v2 — normalize picks by what was offered; append offered line.
# ============================================================================

def _read_learnings_lines():
    path = Path(__file__).parent / "learnings.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def load_learnings():
    """Compute per-tier mode bias, normalized by offered share.
    Emit a bias line for a mode only when pick-share >= 1.5x offered-share with n>=5 picks.
    Also add an outcomes line from metrics.json when available."""
    from collections import Counter, defaultdict
    rows = _read_learnings_lines()

    picks = {1: Counter(), 2: Counter(), 3: Counter()}
    pick_n = {1: 0, 2: 0, 3: 0}
    offered_total = Counter()  # MODE -> count offered across all runs (global)
    offered_tier = {1: Counter(), 2: Counter(), 3: Counter()}  # per-tier (new lines only)
    for e in rows:
        act = e.get("action")
        if act in ("posted", "reply_click"):
            tier = e.get("tier")
            mode = (e.get("mode") or "").upper()
            if tier in picks and mode:
                picks[tier][mode] += 1
                pick_n[tier] += 1
        elif act == "offered":
            for mode, c in (e.get("counts") or {}).items():
                offered_total[mode.upper()] += int(c or 0)
            for tier_s, modes in (e.get("by_tier") or {}).items():
                try:
                    tier_i = int(tier_s)
                except (TypeError, ValueError):
                    continue
                if tier_i in offered_tier:
                    for mode, c in (modes or {}).items():
                        offered_tier[tier_i][mode.upper()] += int(c or 0)

    total_pick = sum(pick_n.values())
    total_off = sum(offered_total.values())
    lines = []
    if total_pick >= 5 and total_off > 0:
        biased = []
        for tier in (1, 2, 3):
            if pick_n[tier] < 5:
                continue
            # Per-tier offered share when by_tier data exists for this tier
            # (new-format lines); global share as the fallback for old lines.
            tier_off_total = sum(offered_tier[tier].values())
            for mode, c in picks[tier].most_common():
                pick_share = c / pick_n[tier]
                if tier_off_total:
                    off_share = offered_tier[tier].get(mode, 0) / tier_off_total
                else:
                    off_share = (offered_total.get(mode, 0) / total_off) if total_off else 0
                if off_share > 0 and pick_share >= 1.5 * off_share and c >= 5:
                    biased.append(f"- Tier {tier}: {mode} (Rahul picks it {pick_share:.0%} vs {off_share:.0%} offered)")
        if biased:
            lines.append("# RAHUL'S OBSERVED PREFERENCES (pick-rate vs offered-rate, normalized)")
            lines.append("When two modes both fit, lean toward these:")
            lines.extend(biased)

    # outcomes line from metrics.json
    try:
        m = json.loads((Path(__file__).parent / "metrics.json").read_text(encoding="utf-8"))
        tp = m.get("tier_perf") or {}
        if tp:
            parts = [f"T{k}: {v.get('replies',0)} replies, avg {v.get('avg_likes',0)} likes" for k, v in sorted(tp.items())]
            lines.append("# OUTCOMES SO FAR: " + "; ".join(parts))
    except Exception:
        pass

    return "\n".join(lines)


def append_offered_line(offered_counts, ready_n, now, dry=False, by_tier=None):
    """Append the per-run 'offered' summary line to learnings.jsonl (or a temp copy in DRY_RUN).

    `by_tier` ({tier: {MODE: count}}) gives load_learnings per-tier denominators;
    old lines without it fall back to the global counts."""
    payload = {
        "action": "offered",
        "ts": now.isoformat(),
        "counts": {
            "RECEIPT": offered_counts.get("RECEIPT", 0),
            "SHARPENER": offered_counts.get("SHARPENER", 0),
            "HONEST_QUESTION": offered_counts.get("HONEST_QUESTION", 0),
            "HUMAN_REPLY": offered_counts.get("HUMAN_REPLY", 0),
        },
        "ready": ready_n,
    }
    if by_tier:
        payload["by_tier"] = {
            str(t): {m: int(c) for m, c in modes.items()}
            for t, modes in sorted(by_tier.items()) if modes
        }
    line = json.dumps(payload, ensure_ascii=False)
    if dry:
        tmp = Path(__file__).parent / "dry-learnings.jsonl"
        # copy existing then append, proving the append code path without touching the real file
        existing = ""
        real = Path(__file__).parent / "learnings.jsonl"
        if real.exists():
            existing = real.read_text(encoding="utf-8")
        tmp.write_text(existing + line + "\n", encoding="utf-8")
        log(f"[DRY_RUN] offered line appended to {tmp.name}: {line}")
    else:
        path = Path(__file__).parent / "learnings.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        log(f"offered line appended: {line}")


# ============================================================================
# 7. SELF TRACKING (follower growth + reply outcomes) -> metrics.json
# ============================================================================

def _au_followers(au):
    for k in ("followers", "followersCount", "followers_count"):
        if au.get(k) is not None:
            try:
                return int(au[k])
            except Exception:
                pass
    return None


def track_self():
    """Scrape Rahul's own recent timeline: capture follower count and replies he posted
    TO OTHERS (with engagement) for outcome tracking. Never breaks the core refresh.

    Field mapping verified against a LIVE self scrape captured 2026-07-02
    (apidojo/tweet-scraper actor 61RPP7dywgiy0JPD0); the captured item is saved
    at apify-self-reply-sample.json as backing evidence:
      - isReply: python bool (True/False), NOT the string 'True'
      - reply target field: inReplyToUsername (lowercase 's'); present only on replies
        (also seen: inReplyToUserId, inReplyToId=parent tweet id)
    A reply-to-OTHERS is: isReply True AND inReplyToUsername != SELF_HANDLE.
    """
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    items = http_json(url, data={"twitterHandles": [SELF_HANDLE], "maxItems": 40, "sort": "Latest"}, timeout=300)
    followers = None
    replies = []
    for t in items:
        if not isinstance(t, dict):
            continue
        au = t.get("author") or {}
        if (au.get("userName") or "").lower() == SELF_HANDLE:
            f = _au_followers(au)
            if f is not None:
                followers = f
        # verified mapping: prefer inReplyToUsername; fall back to legacy spellings
        target = (t.get("inReplyToUsername") or t.get("inReplyToUserName")
                  or t.get("in_reply_to_screen_name"))
        text = t.get("fullText") or t.get("text") or ""
        if _is_reply(t) and target and target.lower() != SELF_HANDLE and not text.startswith("RT @"):
            tier = TIER_LOOKUP.get(target.lower())
            replies.append({
                "id": str(t.get("id") or ""),
                "ts": t.get("createdAt"),
                "target": target,
                "tier": tier,
                "text": text[:120],
                "likes": int(t.get("likeCount", 0) or 0),
                "replies": int(t.get("replyCount", 0) or 0),
                "retweets": int(t.get("retweetCount", 0) or 0),
                "url": t.get("url"),
            })
    return followers, replies


def update_metrics(followers, replies, now):
    path = Path(__file__).parent / "metrics.json"
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        m = {}
    m.setdefault("followers", [])
    m.setdefault("replies", {})
    today = now.date().isoformat()
    if followers is not None:
        m["followers"] = [x for x in m["followers"] if x.get("date") != today]
        m["followers"].append({"date": today, "followers": followers})
        m["followers"] = sorted(m["followers"], key=lambda x: x["date"])[-180:]
    for r in replies:
        if r.get("id"):
            m["replies"][r["id"]] = r  # upsert -> engagement refreshes each run
    keep = sorted(m["replies"].keys(), key=lambda k: int(k) if k.isdigit() else 0, reverse=True)[:50]
    m["replies"] = {k: m["replies"][k] for k in keep}
    from collections import defaultdict
    agg = defaultdict(lambda: {"n": 0, "likes": 0})
    for v in m["replies"].values():
        if v.get("tier"):
            agg[v["tier"]]["n"] += 1
            agg[v["tier"]]["likes"] += v.get("likes", 0)
    m["tier_perf"] = {str(t): {"replies": a["n"], "avg_likes": round(a["likes"] / a["n"], 1)} for t, a in agg.items() if a["n"]}
    m["updated"] = now.isoformat()
    if not DRY_RUN:
        path.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m


# ============================================================================
# STATE
# ============================================================================

def load_state():
    path = Path(__file__).parent / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ============================================================================
# MAIN
# ============================================================================

def main():
    now = datetime.now(timezone.utc)
    receipts = load_receipts()
    state = load_state()
    receipts_used_state = state.get("receipts_used", {})  # {id: lastUsedISOdate}
    # receipts within cooldown window
    cutoff = now - timedelta(days=RECEIPT_COOLDOWN_DAYS)
    used_recent = set()
    for rid, iso in receipts_used_state.items():
        try:
            if datetime.fromisoformat(iso.replace("Z", "+00:00")) >= cutoff:
                used_recent.add(rid)
        except Exception:
            used_recent.add(rid)  # unparseable -> treat as recently used, safe
    log(f"Loaded {len(receipts)} receipts; {len(used_recent)} within {RECEIPT_COOLDOWN_DAYS}d cooldown")

    try:
        raw = scrape_apify()
    except ScrapeError as e:
        # No usable scrape data. Leave the previously served index.html untouched
        # (stale beats blank) and exit non-zero so the cron surfaces the failure.
        log(f"FATAL: scrape produced no data, keeping existing index.html: {e}")
        sys.exit(1)

    fresh = []
    for t in raw:
        ok, reason = passes_filter(t, now)
        if ok:
            fresh.append(t)
    log(f"After filter: {len(fresh)} (from {len(raw)})")

    triage = triage_all(fresh, receipts) if fresh else {}
    selected = rank_and_allocate(fresh, triage, now)
    log(f"After rank+allocate: {len(selected)}")

    used_this_run = set()          # receipt ids consumed this run (in-memory)
    receipts_used_today = dict(receipts_used_state)  # to persist
    offered_counts = {}            # MODE -> count across all ready variants (global)
    offered_by_tier = {}           # tier -> {MODE -> count} (per-tier learning denominators)
    drafts = []

    learned = load_learnings()
    if learned:
        log("Applying normalized learned preferences")

    for i, t in enumerate(selected):
        human_only = bool(t.get("_human_only"))
        topics = t.get("_tri", {}).get("topics", [])
        rfc = rank_receipts(receipts, topics, used_recent, used_this_run, limit=8)
        log(f"  Drafting {i+1}/{len(selected)}: @{_handle(t)} "
            f"(fit={t['_tri'].get('fit')}, human_only={human_only}, receipts={len(rfc)})")
        parsed, model = draft_reply(t, rfc, human_only)
        # inject learned prefs as a soft steer via feedback path is overkill; the
        # normalized prefs are advisory and already reflected in receipt diversity.
        parsed, used = validate_and_fix(parsed, t, rfc, human_only)
        # mark receipts used (memory + to-persist state)
        for rid in used:
            used_this_run.add(rid)
            receipts_used_today[rid] = now.date().isoformat()
        # tally offered modes (global + per-tier, so load_learnings can normalize
        # per-tier picks against per-tier offered shares instead of the global mix)
        tweet_tier = TIER_LOOKUP.get(_handle(t), 3)
        for v in parsed.get("variants", []):
            m = (v.get("mode") or "").upper()
            if m:
                offered_counts[m] = offered_counts.get(m, 0) + 1
                tier_bucket = offered_by_tier.setdefault(tweet_tier, {})
                tier_bucket[m] = tier_bucket.get(m, 0) + 1
        parsed["_tweet"] = t
        parsed["_model"] = model
        drafts.append(parsed)

    ready = [d for d in drafts if d.get("decision") != "SKIP"]
    skipped = [d for d in drafts if d.get("decision") == "SKIP"]

    # Floor guard: a successful-but-empty result (scrape returned nothing, or
    # everything got filtered / triaged / validated away) must not overwrite the
    # live index.html with a blank queue. Mirror the ScrapeError branch — leave the
    # previously served queue in place and exit non-zero so the cron surfaces it.
    # DRY_RUN still writes (so previews/QA of an empty run work).
    if not DRY_RUN and len(ready) < MIN_READY_FLOOR:
        log(f"FATAL: only {len(ready)} ready item(s) (< floor {MIN_READY_FLOOR}); "
            f"keeping existing index.html and state.json (stale beats blank)")
        # Deliberately NO append_offered_line here: the queue was never published,
        # so these variants were never shown to Rahul. Logging them as 'offered'
        # would poison the offered-share denominators in load_learnings().
        sys.exit(1)

    by_tier = {1: 0, 2: 0, 3: 0}
    for d in ready:
        tier = TIER_LOOKUP.get(_handle(d["_tweet"]), 3)
        by_tier[tier] += 1

    # Which model served each ready draft (surfaced in state.json + UI meta line)
    model_counts = {}
    for d in ready:
        mname = d.get("_model", "?")
        model_counts[mname] = model_counts.get(mname, 0) + 1

    data_obj = build_data_object(drafts, now)
    # Keep: sort by velocity for injection (UI re-sorts by priority).
    data_obj.sort(key=lambda d: -_velocity_from_data(d, now))
    top_hot = [d for d in data_obj if d.get("decision") != "SKIP"][:5]

    html = render_html(data_obj, now, models=model_counts)
    if DRY_RUN:
        out_path = Path(DRY_RUN_OUT)
    else:
        out_path = Path(__file__).parent / "index.html"
    out_path.write_text(html, encoding="utf-8")
    log(f"Wrote {out_path} ({len(html):,} bytes)")

    # append the offered summary line
    append_offered_line(offered_counts, len(ready), now, dry=DRY_RUN, by_tier=offered_by_tier)

    # state
    new_state = {
        "lastRefresh": now.isoformat(),
        "ready": len(ready),
        "skipped": len(skipped),
        "by_tier": by_tier,
        "hot_5": [{"handle": d["handle"], "text": d["text"][:200]} for d in top_hot],
        "receipts_used": receipts_used_today,
        "offered_today": offered_counts,
        "models": model_counts,
    }
    if not DRY_RUN:
        Path(__file__).parent.joinpath("state.json").write_text(json.dumps(new_state, indent=2))

    # self tracking (never break the core refresh)
    followers = None
    my_replies = []
    try:
        followers, my_replies = track_self()
        update_metrics(followers, my_replies, now)
        log(f"Self-tracking: followers={followers}, replies tracked={len(my_replies)}")
    except Exception as e:
        log(f"Self-tracking skipped: {e}")

    if DRY_RUN:
        _print_dry_summary(drafts, ready, skipped, by_tier, offered_counts,
                           used_this_run, followers, len(my_replies))
    log("DONE")


def _velocity_from_data(d, now):
    if not d.get("createdAt"):
        return 0
    try:
        dt = datetime.fromisoformat(d["createdAt"])
    except Exception:
        return 0
    hours = max(0.5, (now - dt).total_seconds() / 3600)
    return ((d["likes"] or 0) + 5 * (d["replies"] or 0) + 3 * (d["retweets"] or 0)) / hours


def _print_dry_summary(drafts, ready, skipped, by_tier, offered_counts,
                       used_this_run, followers, n_self_replies):
    from collections import Counter
    modes = Counter()
    fits = Counter()
    models = Counter()
    total_variants = 0
    human_variants = 0
    for d in ready:
        models[d.get("_model", "?")] += 1
        fits[d["_tweet"]["_tri"].get("fit", 5)] += 1
        for v in d.get("variants", []):
            total_variants += 1
            m = (v.get("mode") or "").upper()
            modes[m] += 1
            if m == "HUMAN_REPLY":
                human_variants += 1
    human_pct = (100.0 * human_variants / total_variants) if total_variants else 0
    ti = _TOK["triage_in"]; to = _TOK["triage_out"]
    di = _TOK["draft_in"]; do = _TOK["draft_out"]
    # rough blended cost estimate (per-run). Sonnet drafting dominates.
    # Sonnet 4.6: ~$3/M in, $15/M out. Haiku 4.5 triage: ~$1/M in, $5/M out.
    cost = (di * 3 + do * 15 + ti * 1 + to * 5) / 1_000_000
    print("\n" + "=" * 60)
    print("DRY_RUN SUMMARY")
    print("=" * 60)
    print(f"ready={len(ready)} skipped={len(skipped)} by_tier={by_tier}")
    print(f"variants total={total_variants}  mode dist={dict(modes)}")
    print(f"HUMAN_REPLY variant share = {human_pct:.1f}%  (target <=30%)")
    print(f"fit distribution (ready items) = {dict(sorted(fits.items()))}")
    print(f"models served = {dict(models)}")
    print(f"receipts used this run ({len(used_this_run)}): {sorted(used_this_run)}")
    print(f"offered_counts = {offered_counts}")
    print(f"self: followers={followers} replies_tracked={n_self_replies}")
    print(f"tokens: triage_in={ti} triage_out={to} draft_in={di} draft_out={do}")
    print(f"est. LLM cost this run ~= ${cost:.3f}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
