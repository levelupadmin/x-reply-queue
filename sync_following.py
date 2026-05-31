#!/usr/bin/env python3
"""
Weekly following-sync for the X reply queue.

Scrapes who @rahul_reddy follows (Apify apidojo/twitter-user-scraper, getFollowing),
classifies any NEW follows for thesis-fit + tier (GPT-4.1-mini), and updates
handles.json:
  - ADD new on-thesis follows as synced rows with source="following"
  - REMOVE synced rows with source="following" that are no longer followed
  - NEVER touch "core" (locked) or synced rows with source="discovery" (sticky)

Safety: if the follow scrape returns suspiciously few accounts (likely a failed
scrape), only additions are skipped too and NOTHING is removed.

Env: APIFY_TOKEN, OPENAI_API_KEY
Test locally: set SYNC_INPUT_FILE=/path/to/following.json to skip the live scrape.
"""
import json, os, datetime, urllib.request
from pathlib import Path

APIFY_TOKEN   = os.environ.get("APIFY_TOKEN", "")
OPENAI_API_KEY= os.environ.get("OPENAI_API_KEY", "")
X_HANDLE      = os.environ.get("X_HANDLE", "rahul_reddy")
APIDOJO_USER_SCRAPER = "V38PZzpEgOfeeWvZY"
MODEL = "gpt-4.1-mini"
MIN_SAFE_FOLLOWS = 50   # below this, treat scrape as failed -> no removals/adds

ROOT = Path(__file__).parent
HANDLES_PATH = ROOT / "handles.json"

CLASSIFY_SYS = """You curate X/Twitter accounts for Rahul, a Bangalore-based edtech FOUNDER who replies to others' tweets to grow his audience of founders/operators/creators. He runs writing programs + creator masterclasses + skill cohorts in India.

For each account decide include (true/false) and a tier.

INCLUDE if Rahul could add value replying AND the audience overlaps his ICP:
- founders, startup operators, indie hackers, SaaS/product builders
- designers, writers, authors, creators, filmmakers/directors (his masterclass world)
- marketers, growth, performance-ads people
- investors/VCs/angels, edtech, AI builders/researchers
- India-relevant business/creator voices (strong plus)

EXCLUDE: politicians or political commentary, pure news/media orgs, sports/cricket fans, movie/celebrity FAN accounts, generic meme accounts, NSFW, brands/bots, inactive or empty-bio tiny accounts, non-English-primary regional accounts.

TIER: 1 = anchor (marquee, ~>=300k followers or iconic). 2 = adjacent operator (~30k-300k, on-thesis). 3 = rising voice (<30k, on-thesis, active).
Return STRICT JSON: {"results":[{"handle":"...","include":true,"tier":2}]}. Echo handles exactly; tier may be null if not included."""

def log(m): print(f"[sync] {m}", flush=True)

def http_json(url, data=None, headers=None, timeout=120):
    headers = headers or {}; headers.setdefault("User-Agent","x-reply-sync/1.0")
    body=None
    if data is not None:
        body=json.dumps(data).encode(); headers["Content-Type"]="application/json"
    req=urllib.request.Request(url,data=body,headers=headers)
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return json.loads(r.read())

def scrape_following():
    f=os.environ.get("SYNC_INPUT_FILE")
    if f:
        log(f"using local SYNC_INPUT_FILE={f}")
        recs=json.load(open(f))
    else:
        url=f"https://api.apify.com/v2/acts/{APIDOJO_USER_SCRAPER}/run-sync-get-dataset-items?token={APIFY_TOKEN}"
        recs=http_json(url, data={"twitterHandles":[X_HANDLE],"maxItems":600,"getFollowing":True,"getAbout":True}, timeout=300)
    out={}
    for x in recs:
        if not isinstance(x,dict): continue
        un=(x.get("userName") or x.get("username") or "").strip()
        if not un or un.lower()==X_HANDLE.lower(): continue
        if un.lower()=="none" or (x.get("description")=="mock data"): continue
        out[un.lower()]={"handle":un,
            "followers":int(x.get("followers") or x.get("followersCount") or 0),
            "bio":(x.get("description") or "")[:200]}
    return out

def classify(cands):
    """cands: list of {handle,followers,bio} -> dict lower-handle -> {include,tier}"""
    res={}
    def chunks(l,n):
        for i in range(0,len(l),n): yield l[i:i+n]
    for batch in chunks(cands,25):
        body={"model":MODEL,"temperature":0,"max_tokens":2500,
              "response_format":{"type":"json_object"},
              "messages":[{"role":"system","content":CLASSIFY_SYS},
                          {"role":"user","content":"Classify:\n"+json.dumps(batch,ensure_ascii=False)}]}
        try:
            j=http_json("https://api.openai.com/v1/chat/completions",data=body,
                        headers={"Authorization":f"Bearer {OPENAI_API_KEY}"},timeout=90)
            for r in json.loads(j["choices"][0]["message"]["content"]).get("results",[]):
                h=(r.get("handle") or "").lower()
                if h: res[h]=r
        except Exception as e:
            log(f"classify batch error: {e}")
    return res

def main():
    data=json.loads(HANDLES_PATH.read_text(encoding="utf-8"))
    core={c["handle"].lower() for c in data.get("core",[])}
    synced=data.get("synced",[])
    synced_by={s["handle"].lower():s for s in synced}
    evaluated=set(data.get("_evaluated", []))

    follows=scrape_following()
    log(f"scraped {len(follows)} followed accounts for @{X_HANDLE}")
    if len(follows) < MIN_SAFE_FOLLOWS:
        log(f"SAFETY: only {len(follows)} follows returned (< {MIN_SAFE_FOLLOWS}). Aborting with NO changes.")
        return

    # ADD: followed, on-thesis, not already in core/synced
    new=[v for lo,v in follows.items() if lo not in core and lo not in synced_by and lo not in evaluated]
    log(f"{len(new)} new follows to classify")
    added=0
    for v in new:
        evaluated.add(v["handle"].lower())   # judged this run; don't re-spend next week
    if new:
        verdict=classify(new)
        today=datetime.date.today().isoformat()
        for v in new:
            r=verdict.get(v["handle"].lower())
            if r and r.get("include"):
                t=r.get("tier") or 3; t=t if t in (1,2,3) else 3
                synced.append({"handle":v["handle"],"tier":t,"source":"following",
                               "category":r.get("category",""),"added":today})
                added+=1

    # REMOVE: source="following" rows no longer followed (core & discovery untouched)
    before=len(synced)
    kept=[]
    removed=[]
    for s in synced:
        lo=s["handle"].lower()
        if s.get("source")=="following" and lo not in follows:
            removed.append(s["handle"]); continue
        kept.append(s)
    synced=kept

    # recompute meta + write
    from collections import Counter
    data["synced"]=synced
    data["_evaluated"]=sorted(evaluated)
    data["_meta"]["updated"]=datetime.date.today().isoformat()
    st=Counter(s["tier"] for s in synced); srcc=Counter(s["source"] for s in synced)
    data["_meta"]["counts"]={"core":len(data.get("core",[])),"synced":len(synced),
        "total":len(data.get("core",[]))+len(synced),
        "synced_by_tier":dict(sorted(st.items())),"synced_by_source":dict(srcc)}
    HANDLES_PATH.write_text(json.dumps(data,ensure_ascii=False,indent=1),encoding="utf-8")
    log(f"added {added}, removed {len(removed)} ({removed[:8]}{'...' if len(removed)>8 else ''})")
    log(f"synced now {len(synced)}, total {data['_meta']['counts']['total']}")

if __name__=="__main__":
    main()
