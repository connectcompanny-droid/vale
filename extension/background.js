// Talks to Vale on this computer. Content scripts cannot call it directly (CORS), so they ask here.
const DEFAULT_BASE = "http://127.0.0.1:8000";

async function settings() {
  const s = await chrome.storage.local.get(["token", "base"]);
  return { token: s.token || "", base: (s.base || DEFAULT_BASE).replace(/\/$/, "") };
}

async function call(path, method = "GET", body) {
  const { token, base } = await settings();
  if (!token) throw new Error("Falta el token: abre el icono de Vale Copiloto y pégalo.");
  const r = await fetch(base + path, {
    method,
    headers: { "content-type": "application/json", "x-vale-token": token },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch {}
    throw new Error(`Vale respondió ${r.status} ${detail}`.trim());
  }
  return r.json();
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  call(msg.path, msg.method, msg.body).then((data) => reply({ ok: true, data }), (e) => reply({ ok: false, error: e.message }));
  return true; // async reply
});

// Every minute: if a client confirmed an offer, tell the operator even if no tab is open.
let seen = new Set();
async function watch() {
  try {
    const { items } = await call("/admin/copilot/queue");
    const confirmed = items.filter((i) => i.state === "booking");
    chrome.action.setBadgeText({ text: confirmed.length ? String(confirmed.length) : "" });
    chrome.action.setBadgeBackgroundColor({ color: "#3DDC97" });
    for (const i of confirmed) {
      if (seen.has(i.id)) continue;
      seen.add(i.id);
      chrome.notifications.create(`vale-${i.id}`, {
        type: "basic", iconUrl: "icon.png", priority: 2, requireInteraction: true,
        title: "Cliente confirmó: reservar ya",
        message: `${i.tramite} · ${i.offer ? i.offer.office + " · " + i.offer.starts_at : ""}`,
      });
    }
  } catch { /* Vale apagado o sin token: nada que avisar */ }
}
chrome.alarms.create("vale-watch", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((a) => a.name === "vale-watch" && watch());
chrome.runtime.onStartup.addListener(watch);
chrome.runtime.onInstalled.addListener(watch);
