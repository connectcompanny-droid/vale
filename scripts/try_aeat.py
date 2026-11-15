"""Record the real pages of the Agencia Tributaria appointment site (identity appointment for the
digital certificate), so Vale's robot can be written against what the site actually shows.

    .venv\\Scripts\\python.exe scripts\\try_aeat.py

How it works
- A normal Chrome window opens on the official appointment page.
- YOU go through the steps by hand, as a client would, choosing the certificate accreditation
  service and an office in Granada. Stop BEFORE the final confirmation: nothing is booked.
- After each page, come back to this window and press Enter. The script saves the page's form
  structure (ids, names, labels, button texts, option texts) to debug/aeat-NN.txt.

Privacy: values typed into text fields are never saved, and no screenshots are taken, so your NIE
and name do not end up in the files. Only the structure of each page is recorded.

If the site shows a captcha or a block page, stop and tell Claude: Vale never gets around those.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import Page, async_playwright  # noqa: E402

START_URL = "https://www2.agenciatributaria.gob.es/wlpl/TOCP-MUTE/internet/identificacion"
OUT = Path("debug")

# Runs in the page: structure only. Text inputs report their type and labels, never their value.
DUMP_JS = r"""
() => {
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim().slice(0, 120);
  const labelOf = (el) => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    const p = el.closest("label"); if (p) return clean(p.innerText);
    return clean(el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("title") || "");
  };
  const rows = [];
  for (const el of document.querySelectorAll("input, select, textarea, button, a[role=button], [role=radio], [role=option], [role=combobox]")) {
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute("type") || "";
    const row = { tag, type, id: el.id || "", name: el.getAttribute("name") || "", label: labelOf(el),
                  visible: !!(el.offsetWidth || el.offsetHeight) };
    if (tag === "button" || tag === "a" || type === "submit" || type === "button") row.text = clean(el.innerText || el.value);
    if (type === "radio" || type === "checkbox") row.option = clean(el.value);
    if (tag === "select") row.options = [...el.options].slice(0, 60).map((o) => clean(o.text));
    rows.push(row);
  }
  const headings = [...document.querySelectorAll("h1, h2, h3, legend")].map((h) => clean(h.innerText)).filter(Boolean).slice(0, 20);
  const lists = [...document.querySelectorAll("li, td")].map((x) => clean(x.innerText)).filter((t) => t && t.length < 90).slice(0, 80);
  const text = clean(document.body.innerText).toLowerCase();
  return { title: document.title, headings, rows, lists,
           captcha: !!document.querySelector("iframe[src*='captcha'], [class*='captcha' i], [id*='captcha' i]"),
           blocked: /request rejected|intrusion prevention|access denied|acceso denegado/.test(text) };
}
"""


async def record(page: Page, n: int) -> dict:
    lines = [f"step {n}", f"url: {page.url.split('?')[0]}", f"frames: {len(page.frames)}"]
    summary = {"captcha": False, "blocked": False}
    for i, frame in enumerate(page.frames):
        try:
            d = await frame.evaluate(DUMP_JS)
        except Exception as e:
            lines.append(f"--- frame {i}: could not read ({e})")
            continue
        summary["captcha"] |= d["captcha"]
        summary["blocked"] |= d["blocked"]
        lines.append(f"--- frame {i}: {frame.url.split('?')[0]}  title={d['title']!r}  captcha={d['captcha']}  blocked={d['blocked']}")
        lines += [f"heading: {h}" for h in d["headings"]]
        for r in d["rows"]:
            extra = "".join(f" {k}={r[k]!r}" for k in ("text", "option", "options") if r.get(k))
            lines.append(f"{r['tag']:8} type={r['type']:9} id={r['id']!r:28} name={r['name']!r:24} label={r['label']!r}{extra}{'' if r['visible'] else '  (hidden)'}")
        if d["lists"]:
            lines.append("list/table texts: " + " | ".join(d["lists"]))
    OUT.mkdir(exist_ok=True)
    (OUT / f"aeat-{n:02d}.txt").write_text("\n".join(lines), encoding="utf-8")
    return summary


async def main() -> None:
    print(__doc__)
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=False, channel="chrome")
        except Exception:
            browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(locale="es-ES", timezone_id="Europe/Madrid", viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        await page.goto(START_URL)
        n = 0
        while True:
            n += 1
            answer = await asyncio.to_thread(input, f"\nPage {n}: when it is fully loaded, press Enter to record it (or type 'fim' to finish): ")
            if answer.strip().lower() in ("fim", "fin", "end", "q"):
                break
            page = ctx.pages[-1]  # the site may open a new tab
            s = await record(page, n)
            print(f"  saved debug/aeat-{n:02d}.txt")
            if s["blocked"]:
                print("  STOP  the site refused this session. Do not retry now; tell Claude.")
                break
            if s["captcha"]:
                print("  NOTE  this page has a captcha. Vale will not solve it: this step would stay with the client.")
            print("  Now do the next step in the Chrome window (do NOT press the final confirm button).")
        await browser.close()
    print("\nDone. Tell Claude it is finished; nothing was booked and no personal values were saved.")


if __name__ == "__main__":
    asyncio.run(main())
