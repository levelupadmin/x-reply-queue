# Receipts: verified-only personal memory

The active pool (`receipts.json`) is **empty by design** since 2026-07-03. The engine
drafts perspective-first replies with zero personal claims. The 20 old LLM-authored,
never-verified receipts sit in `receipts.quarantine.json` and are never used.

## Add a TRUE receipt from your phone (only you should do this)
1. Open github.com/levelupadmin/x-reply-queue on your phone, edit `receipts.json`.
2. Add an entry inside `"receipts": [...]` and commit; the next morning run uses it:
```json
{"id": "my-real-story", "kind": "story|stat|observation|opinion",
 "topics": ["marketing", "pricing"],
 "text": "The receipt exactly as you would say it, in first person.",
 "use_when": "one line on when it fits"}
```
3. Only add things that are literally true. The model will state them as YOUR life.
4. To resurrect a quarantined receipt you know is true, copy it across from
   `receipts.quarantine.json` unchanged.
