// Sends a Web Push to all subscribed devices after the daily refresh.
// Run by the GitHub Action. Needs VAPID_* + PUSH_SUBS_TOKEN secrets.
const webpush = require("web-push");
const fs = require("fs");
const PROXY = "https://x-refresh-proxy.levelupedtech.workers.dev";
const TOKEN = process.env.PUSH_SUBS_TOKEN;

(async () => {
  if (!process.env.VAPID_PRIVATE_KEY) { console.log("no VAPID key, skip"); return; }
  webpush.setVapidDetails(process.env.VAPID_SUBJECT, process.env.VAPID_PUBLIC_KEY, process.env.VAPID_PRIVATE_KEY);
  let state = { ready: 0, by_tier: {} };
  try { state = JSON.parse(fs.readFileSync("state.json", "utf8")); } catch (e) {}
  let subs = [];
  try {
    const r = await fetch(`${PROXY}/subs?t=${TOKEN}`);
    subs = (await r.json()).subs || [];
  } catch (e) { console.log("subs fetch failed:", e.message); return; }
  if (!subs.length) { console.log("no subscriptions"); return; }
  const bt = state.by_tier || {};
  const payload = JSON.stringify({
    title: "X reply queue ready",
    body: `${state.ready} replies ready · T1 ${bt["1"]||0} · T2 ${bt["2"]||0} · T3 ${bt["3"]||0}`,
    url: "https://levelupadmin.github.io/x-reply-queue/",
  });
  for (const s of subs) {
    try { await webpush.sendNotification(s, payload); console.log("sent", s.endpoint.slice(0, 40)); }
    catch (e) {
      console.log("fail", e.statusCode);
      if (e.statusCode === 404 || e.statusCode === 410) {
        try { await fetch(`${PROXY}/unsub?t=${TOKEN}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint: s.endpoint }) }); } catch (_) {}
      }
    }
  }
})();
