# Adding a receipt from your phone

1. GitHub mobile → this repo → `receipts.json` → ✏️ Edit.
2. Add an entry to the `"receipts"` array (keep valid JSON — mind commas).
3. Commit to `main`. The next scheduled refresh picks it up automatically.

Copy-paste example (edit every field):

```json
{"id": "short-brand-neutral-slug", "kind": "stat", "topics": ["pricing", "india", "trust"],
 "text": "The claim in Rahul's first-person voice. Only things literally true.",
 "use_when": "topics/takes where this receipt fits"}
```

Rules: never your own brand/company name in `id`/`text` (third-party tools like Lovable are fine); no numbers you can't defend. `_meta.confirm_before_ship` lists receipts awaiting your factual sign-off.
