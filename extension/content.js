// Vale Copiloto: a small panel on the cita previa site. Every action starts with the operator's
// click on this panel; the copilot only fills fields and reads the page. It never presses the
// site's own buttons (Aceptar, Entrar, Enviar, Confirmar): the operator does.
(() => {
  if (window.__valeCopilot) return;
  window.__valeCopilot = true;

  const SEL = {
    province: "select#form, select[name='form']",
    tramite: "select[id^='tramiteGrupo'], select[name^='tramiteGrupo']",
    docNie: "#rdbTipoDocNie",
    docPas: "#rdbTipoDocPas",
    number: "#txtIdCitado",
    name: "#txtDesCitado",
    country: "#txtPaisNac",
    phone: "#txtTelefonoCitado",
    email1: "#emailUNO",
    email2: "#emailDOS",
    office: "#idSede",
    slots: "input[type='radio'][name^='rdbCita']",
    buttons: "#btnAceptar, #btnEntrar, #btnEnviar, #btnSiguiente, #btnConfirmar, #btnSubmit, button[type=submit], input[type=submit]",
  };
  // Which appointment system this page belongs to. "icp" = extranjería, "aeat" = Agencia Tributaria
  // (identity appointment for the digital certificate). On AEAT the form ids are not known in
  // advance, so fields are found by their labels, and every value also has a copy button.
  const SITE = /agenciatributaria\.gob\.es$/.test(location.hostname) ? "aeat" : "icp";
  const SITE_NAME = { icp: "Extranjería", aeat: "Agencia Tributaria · certificado digital" }[SITE];
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const norm = (t) => (t || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toUpperCase().replace(/[^A-Z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();

  const api = (path, method = "GET", body) =>
    new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ path, method, body }, (res) => {
        if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
        res && res.ok ? resolve(res.data) : reject(new Error(res ? res.error : "Sin respuesta de Vale"));
      });
    });

  // Only the frame that holds the form shows the panel.
  const hasForm = () => SITE === "aeat" ? !!$("form, input, select") : !!$(`${SEL.province}, ${SEL.tramite}, ${SEL.number}, ${SEL.office}, ${SEL.slots}, ${SEL.phone}`) || /justificante/i.test(document.body.innerText || "");
  if (!hasForm()) return;

  // ---------- filling (fires the events the site listens to) ----------
  function setValue(el, value) {
    if (!el || value == null) return false;
    const proto = el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.classList.add("vale-filled");
    return true;
  }
  function selectByText(select, test) {
    if (!select) return false;
    const opt = [...select.options].find((o) => test(norm(o.text)));
    return opt ? setValue(select, opt.value) : false;
  }
  function check(el) {
    if (!el) return false;
    el.checked = true;
    el.dispatchEvent(new Event("click", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }
  const matches = (item, text) => item.match.some((alt) => norm(alt).split(" ").every((w) => text.includes(w)));

  function highlightNext() {
    $$(SEL.buttons).forEach((b) => b.classList.remove("vale-next"));
    const b = $$(SEL.buttons).find((x) => x.offsetParent !== null);
    if (b) b.classList.add("vale-next");
  }

  function labelText(el) {
    const bits = [el.name, el.id, el.getAttribute("placeholder"), el.getAttribute("aria-label"), el.getAttribute("title")];
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) bits.push(l.innerText); }
    const wrap = el.closest("label"); if (wrap) bits.push(wrap.innerText);
    const prev = el.closest("td, div, p")?.previousElementSibling; if (prev) bits.push(prev.innerText);
    return norm(bits.filter(Boolean).join(" "));
  }
  // Field kinds by the words around them (Spanish forms).
  const KINDS = [
    ["number", /\b(NIF|NIE|DNI|DOCUMENTO|IDENTIFICACION)\b/],
    ["surname1", /\b(PRIMER APELLIDO|APELLIDO 1|APELLIDO1)\b/],
    ["name", /\b(NOMBRE|APELLIDOS Y NOMBRE|RAZON SOCIAL)\b/],
    ["phone", /\b(TELEFONO|MOVIL|MOBILE)\b/],
    ["email", /\b(CORREO|EMAIL|E MAIL)\b/],
  ];
  function fillByLabels(item) {
    const c = item.client, done = new Set();
    const values = { number: c.nie || c.number, surname1: c.name.split(" ").slice(-1)[0], name: c.name, phone: c.phone, email: c.email };
    for (const el of $$("input[type=text], input[type=tel], input[type=email], input:not([type])")) {
      if (el.offsetParent === null || el.value) continue;
      const lab = labelText(el);
      const kind = (KINDS.find(([, re]) => re.test(lab)) || [])[0];
      if (kind && values[kind] && setValue(el, values[kind])) done.add({ number: "NIE", surname1: "apellido", name: "nombre", phone: "teléfono", email: "email" }[kind]);
    }
    // Service lists: pick the option about the electronic certificate, and a Granada office.
    for (const sel of $$("select")) {
      const lab = labelText(sel);
      if (/SERVICIO|TRAMITE|GESTION|MATERIA|ASUNTO/.test(lab) && selectByText(sel, (t) => /CERTIFICADO (ELECTRONICO|DIGITAL)|ACREDITACION/.test(t))) done.add("servicio");
      else if (/PROVINCIA/.test(lab) && selectByText(sel, (t) => t.includes(norm(item.province)))) done.add("provincia");
      else if (/OFICINA|DELEGACION|ADMINISTRACION|LOCALIDAD|MUNICIPIO/.test(lab) && selectByText(sel, (t) => t.includes(norm(item.province)))) done.add("oficina");
    }
    for (const r of $$("input[type=radio], input[type=checkbox]")) {
      const lab = labelText(r);
      if (/CERTIFICADO (ELECTRONICO|DIGITAL)|ACREDITACION.*CERTIFICADO/.test(lab) && check(r)) done.add("servicio");
    }
    return [...done];
  }

  function fillStep(item) {
    if (SITE === "aeat") { const d = fillByLabels(item); highlightNext(); return d; }
    const done = [];
    const c = item.client;
    if ($(SEL.province) && selectByText($(SEL.province), (t) => t.includes(norm(item.province)))) done.push("provincia");
    for (const s of $$(SEL.tramite)) if (selectByText(s, (t) => matches(item, t))) { done.push("trámite"); break; }
    if ($(SEL.number)) {
      check($(c.doc_type === "nie" ? SEL.docNie : SEL.docPas));
      setValue($(SEL.number), c.number) && done.push("documento");
      setValue($(SEL.name), c.name) && done.push("nombre");
      if ($(SEL.country)) selectByText($(SEL.country), (t) => t.includes(norm(c.nationality))) && done.push("país");
    }
    if ($(SEL.phone)) {
      setValue($(SEL.phone), c.phone) && done.push("teléfono");
      if (c.email) { setValue($(SEL.email1), c.email); setValue($(SEL.email2), c.email); done.push("email"); }
    }
    highlightNext();
    return done;
  }

  // ---------- reading the page ----------
  function readSlots() {
    if (SITE === "aeat") {
      const cands = $$("input[type=radio], button, a").filter((el) => el.offsetParent !== null && !el.closest("#vale-copilot"));
      return cands.map((el) => {
        const row = el.closest("tr, li, label") || el.parentElement;
        const text = ((el.tagName === "INPUT" ? row?.innerText : el.innerText) || "").replace(/\s+/g, " ").trim();
        const date = text.match(/\d{1,2}[/.-]\d{1,2}[/.-]\d{4}/), time = text.match(/\b\d{1,2}:\d{2}\b/);
        return date || time ? { radio: el, office: (document.body.innerText.match(/(DELEGACI[OÓ]N|ADMINISTRACI[OÓ]N)[^\n]{0,60}/i) || [""])[0].trim(),
          starts_at: [date && date[0], time && time[0]].filter(Boolean).join(" "), text } : null;
      }).filter(Boolean).slice(0, 12);
    }
    const office = $(SEL.office) ? $(SEL.office).selectedOptions[0]?.text.trim() : "";
    return $$(SEL.slots).map((r) => {
      const row = r.closest("tr, li, label, div") || r.parentElement;
      const text = (row?.innerText || "").replace(/\s+/g, " ").trim();
      const date = text.match(/\d{1,2}[/.-]\d{1,2}[/.-]\d{4}/);
      const time = text.match(/\b\d{1,2}:\d{2}\b/);
      return { radio: r, office: office || (text.match(/(CNP|COMISAR[IÍ]A|OFICINA)[^,\n]*/i) || [""])[0], starts_at: [date && date[0], time && time[0]].filter(Boolean).join(" ") || text.slice(0, 40), text };
    });
  }
  function readReference() {
    const text = (document.body.innerText || "").replace(/\s+/g, " ");
    // The receipt code: after the word "justificante", an upper-case code that contains a digit.
    const m = text.match(/(?:[Jj]ustificante|[Ll]ocalizador|[Nn][úu]mero de cita|[Cc][óo]digo de cita)[\s\S]{0,60}?\b([A-Z0-9]*\d[A-Z0-9]*)\b/);
    if (m && m[1].length < 6) return "";
    return m ? m[1] : "";
  }

  // ---------- panel ----------
  const panel = document.createElement("div");
  panel.id = "vale-copilot";
  panel.innerHTML = `<header><b>vale</b> copiloto <button data-a="min" title="Minimizar">–</button></header><div class="body"></div>`;
  document.documentElement.append(panel);
  const body = panel.querySelector(".body");
  panel.querySelector('[data-a="min"]').onclick = () => panel.classList.toggle("min");

  let queue = [];
  let current = null;
  const say = (text, cls = "") => { const p = document.createElement("p"); p.className = cls; p.textContent = text; body.append(p); return p; };
  const btn = (label, fn, cls = "") => { const b = document.createElement("button"); b.type = "button"; b.textContent = label; b.className = cls; b.onclick = fn; body.append(b); return b; };

  async function refresh() {
    try {
      queue = (await api("/admin/copilot/queue")).items.filter((i) => (i.site || "icp") === SITE);
    } catch (e) {
      body.textContent = ""; say(e.message, "err"); return;
    }
    if (current) current = queue.find((i) => i.id === current.id) || null;
    if (!current) current = queue.find((i) => i.state === "booking") || queue.find((i) => i.state === "monitoring") || queue[0] || null;
    render();
  }

  function render() {
    body.textContent = "";
    say(SITE_NAME, "site");
    if (!current) { say("No hay clientes en la cola para esta web."); return; }
    const urgent = queue.filter((i) => i.state === "booking");
    if (urgent.length && current.state !== "booking") say(`⚠ ${urgent.length} cliente(s) confirmaron: resérvalos primero.`, "warn");

    const pick = document.createElement("select");
    queue.forEach((i) => {
      const o = new Option(`#${i.id} · ${i.tramite} · ${i.state === "booking" ? "RESERVAR YA" : i.state === "offer_pending" ? "oferta enviada" : "esperando"}`, i.id);
      o.selected = i.id === current.id; pick.add(o);
    });
    pick.onchange = () => { current = queue.find((i) => i.id === Number(pick.value)); render(); };
    body.append(pick);

    const c = current.client;
    const chips = document.createElement("div");
    chips.className = "chips";
    for (const [label, value] of [["NIE/Doc.", c.nie || c.number], ["Nombre", c.name], ["País", c.nationality], ["Tel.", c.phone], ["Email", c.email]]) {
      if (!value) continue;
      const b = document.createElement("button");
      b.type = "button"; b.title = "Copiar"; b.textContent = `${label}: ${value}`;
      b.onclick = () => navigator.clipboard.writeText(value).then(() => { b.classList.add("copied"); setTimeout(() => b.classList.remove("copied"), 900); });
      chips.append(b);
    }
    body.append(chips);
    if (current.state === "booking") say(`Confirmado por el cliente: ${current.offer.office} · ${current.offer.starts_at}. Elige ese hueco y confirma en la web.`, "warn");

    btn("Rellenar este paso", () => {
      const done = fillStep(current);
      say(done.length ? `Rellenado: ${done.join(", ")}. Pulsa el botón marcado de la web.` : "En esta pantalla no hay nada que rellenar.", done.length ? "ok" : "");
    }, "main");

    const slots = readSlots();
    if (slots.length && current.state === "monitoring") {
      say(`Huecos en pantalla: ${slots.length}. Elige uno para proponérselo al cliente:`);
      slots.slice(0, 6).forEach((sl) => btn(`Proponer: ${sl.starts_at}`, async (ev) => {
        const office = prompt("Oficina", sl.office) || sl.office;
        const when = prompt("Fecha y hora (DD/MM/AAAA HH:MM)", sl.starts_at) || sl.starts_at;
        ev.target.disabled = true;
        try { await api(`/admin/copilot/${current.id}/offer`, "POST", { office, starts_at: when }); say("Enviado al cliente. Te aviso cuando confirme.", "ok"); refresh(); }
        catch (e) { say(e.message, "err"); ev.target.disabled = false; }
      }));
    }
    if (current.state === "monitoring") {
      btn("Proponer un hueco a mano", async (ev) => {
        const office = prompt("Oficina", ""); if (!office) return;
        const when = prompt("Fecha y hora (DD/MM/AAAA HH:MM)", ""); if (!when) return;
        ev.target.disabled = true;
        try { await api(`/admin/copilot/${current.id}/offer`, "POST", { office, starts_at: when }); say("Enviado al cliente.", "ok"); refresh(); }
        catch (e) { say(e.message, "err"); ev.target.disabled = false; }
      }, "ghost");
    }
    if (current.state === "booking" && slots.length) {
      const wanted = norm(current.offer.starts_at);
      const hit = slots.find((sl) => norm(sl.starts_at) === wanted || norm(sl.text).includes(wanted));
      btn(hit ? "Marcar el hueco confirmado" : "No encuentro el hueco confirmado", () => {
        if (hit) { check(hit.radio); hit.radio.scrollIntoView({ block: "center" }); highlightNext(); say("Marcado. Revisa y pulsa Confirmar en la web.", "ok"); }
        else say("Si ya no está, pulsa «Ya no estaba».", "warn");
      });
    }

    const ref = readReference();
    if (current.state === "booking") {
      const input = document.createElement("input");
      input.placeholder = "Código del justificante"; input.value = ref; body.append(input);
      btn("Reservado ✓ (avisar al cliente)", async (ev) => {
        if (!input.value.trim()) { say("Copia el código del justificante.", "warn"); return; }
        ev.target.disabled = true;
        try { await api(`/admin/copilot/${current.id}/booked`, "POST", { reference: input.value.trim() }); say("Hecho: el cliente ya tiene su justificante y el recordatorio.", "ok"); current = null; refresh(); }
        catch (e) { say(e.message, "err"); ev.target.disabled = false; }
      }, "main");
    }
    if (current.state === "booking" || current.state === "offer_pending") {
      btn("Ya no estaba / retirar", async () => {
        try { await api(`/admin/copilot/${current.id}/gone`, "POST"); say("Avisado al cliente; vuelve a la cola.", "ok"); refresh(); } catch (e) { say(e.message, "err"); }
      }, "ghost");
    }
    if (current.state === "monitoring" && /no hay citas|no existen citas|no hay huecos|sin disponibilidad|no hay disponibilidad/i.test(document.body.innerText || "")) {
      btn(`Sin huecos: anotar para todos los de «${current.tramite}»`, async () => {
        try { const r = await api("/admin/copilot/checked", "POST", { tramite_code: current.tramite_code }); say(`Anotado (${r.marked}). Vale aprende de estas horas.`, "ok"); } catch (e) { say(e.message, "err"); }
      }, "ghost");
    }
  }

  refresh();
  setInterval(refresh, 30000);
})();
