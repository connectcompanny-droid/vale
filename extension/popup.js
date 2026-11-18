const $ = (id) => document.getElementById(id);
chrome.storage.local.get(["token"]).then((s) => { $("token").value = s.token || ""; load(); });
$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ token: $("token").value.trim() });
  load();
});
$("open2").addEventListener("click", () => chrome.tabs.create({ url: "https://www2.agenciatributaria.gob.es/wlpl/TOCP-MUTE/internet/identificacion" }));
$("open").addEventListener("click", () => chrome.tabs.create({ url: "https://icp.administracionelectronica.gob.es/icpplus/index.html" }));

function load() {
  const q = $("q");
  q.textContent = "Cargando…";
  chrome.runtime.sendMessage({ path: "/admin/copilot/queue" }, (res) => {
    if (!res || !res.ok) { q.textContent = res ? res.error : "Sin respuesta de Vale"; return; }
    const items = res.data.items;
    const by = (st) => items.filter((i) => i.state === st);
    q.innerHTML = "";
    const add = (text, hot) => { const li = document.createElement("li"); li.textContent = text; if (hot) li.className = "hot"; q.append(li); };
    const ul = document.createElement("ul"); q.append(ul);
    by("booking").forEach((i) => add(`RESERVAR YA · ${i.tramite} · ${i.offer.office} ${i.offer.starts_at}`, true));
    if (by("offer_pending").length) add(`${by("offer_pending").length} oferta(s) esperando el sí del cliente`);
    const waiting = {};
    by("monitoring").forEach((i) => { waiting[i.tramite] = (waiting[i.tramite] || 0) + 1; });
    Object.entries(waiting).forEach(([t, n]) => add(`${n} esperando hueco · ${t}`));
    if (!items.length) add("No hay nada pendiente.");
    q.querySelectorAll("li").forEach((li) => ul.append(li));
    chrome.runtime.sendMessage({ path: "/admin/copilot/hints" }, (h) => {
      if (!h || !h.ok) return;
      const p = document.createElement("p");
      p.style.fontSize = "12px";
      p.textContent = h.data.enough_data
        ? "Horas con más huecos encontrados: " + h.data.best_hours.map((b) => `${b.hour}h (${b.found})`).join(", ")
        : h.data.note;
      q.append(p);
    });
  });
}
