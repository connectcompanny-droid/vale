"""Manual check of the cita previa site with a VISIBLE browser. Never books anything.

    python scripts/try_icp.py                  # menu only: province + trámite, stops before personal data
    python scripts/try_icp.py --check          # also asks for your data and goes up to the availability page
    python scripts/try_icp.py --province madrid --tramite huellas

Each step prints OK/FAIL with the selector it used and saves a screenshot to ./debug/.
Personal data typed here stays in memory only: it is not saved anywhere.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import Page, async_playwright  # noqa: E402

from app.citas.catalog import PROVINCES, TRAMITES, label_matches  # noqa: E402
from app.citas.providers.icp import BASE_URL, SEL, TEXT, _nie_like  # noqa: E402

DEBUG = Path("debug")
step_no = 0


async def snap(page: Page, name: str) -> None:
    global step_no
    step_no += 1
    DEBUG.mkdir(exist_ok=True)
    await page.screenshot(path=DEBUG / f"{step_no:02d}-{name}.png", full_page=True)


def ok(msg: str) -> None:
    print(f"  OK    {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL  {msg}")


async def root(page: Page, timeout_s: float = 20.0):
    """The frame that holds the form. The ICP page may render its form late or inside an iframe."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        for frame in page.frames:
            try:
                if await frame.locator("select, input[type='submit'], input[type='button'], button").count():
                    return frame
            except Exception:
                pass
        if loop.time() > deadline:
            return page.main_frame
        await asyncio.sleep(0.5)


async def dump_elements(page: Page) -> None:
    """Print the real ids/names of every form control in every frame, so the selectors can be fixed."""
    js = """() => [...document.querySelectorAll('select, input, button, a.btn, iframe')].map(e => ({
        tag: e.tagName.toLowerCase(), type: e.type || '', id: e.id || '', name: e.getAttribute('name') || '',
        value: (e.value || '').slice(0, 40), text: (e.innerText || '').trim().slice(0, 40),
        src: (e.src || '').slice(0, 80), visible: !!(e.offsetWidth || e.offsetHeight) }))"""
    lines = [f"frames on page: {len(page.frames)}"]
    for i, frame in enumerate(page.frames):
        lines.append(f"--- frame {i}: {frame.url[:100]}")
        try:
            for e in await frame.evaluate(js):
                lines.append(f"{e['tag']:7} type={e['type']:9} id={e['id']!r:28} name={e['name']!r:24} value={e['value']!r:22} text={e['text']!r} {'src=' + e['src'] if e['src'] else ''} {'' if e['visible'] else '(hidden)'}")
        except Exception as ex:
            lines.append(f"(could not read frame: {type(ex).__name__})")
    try:
        html = await page.content()
        lines.append(f"--- main html length: {len(html)}; first 600 chars:")
        lines.append(html[:600])
    except Exception:
        pass
    DEBUG.mkdir(exist_ok=True)
    out = DEBUG / f"{step_no:02d}-elements.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"        Diagnostics (also saved to {out}):")
    for ln in lines[:60]:
        if "(hidden)" not in ln:
            print(f"          {ln}")


async def pick(page, selector: str, predicate, what: str) -> bool:
    selects = await page.locator(selector).all()
    if not selects:
        fail(f"{what}: selector {selector!r} not found on the page")
        await dump_elements(page.page)
        return False
    listing = []
    for i, select in enumerate(selects):
        label = await select.get_attribute("id") or await select.get_attribute("name") or f"list {i + 1}"
        for opt in await select.locator("option").all():
            text = (await opt.inner_text()).strip()
            listing.append((label, text))
            if predicate(text):
                await select.select_option(value=await opt.get_attribute("value"))
                ok(f"{what}: chose {text!r} (in {label})")
                return True
    fail(f"{what}: no matching option. Options on the page, by list:")
    for label, text in listing:
        print(f"          [{label}] {text}")
    return False


async def click(page: Page, key: str) -> bool:
    sel = SEL[key]
    btn = page.locator(sel).first
    if not await btn.count():
        fail(f"button {key}: selector {sel!r} not found")
        await dump_elements(page.page)
        return False
    await btn.click()
    await page.wait_for_load_state("domcontentloaded")
    ok(f"clicked {key}")
    return True


async def captcha(page: Page) -> bool:
    if await page.locator(SEL["captcha"]).count():
        print("  STOP  captcha on screen. Vale never solves captchas; in production this goes to the user.")
        return True
    return False


async def run(province: str, tramite_code: str, check: bool) -> None:
    tramite = TRAMITES[tramite_code]
    print(f"\nProvince: {PROVINCES[province]} | Trámite: {tramite.label['pt']} | Mode: {'availability check' if check else 'menu only'}\n")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=500)
        ctx = await browser.new_context(locale="es-ES", timezone_id="Europe/Madrid", viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        page.set_default_timeout(30_000)
        try:
            print("1) Home page")
            await page.goto(BASE_URL)
            await page.wait_for_load_state("networkidle")
            await snap(page, "home")
            if not await pick(await root(page), SEL["province"], lambda t: PROVINCES[province].lower() in t.lower(), "province"):
                return
            if not await click(await root(page), "accept"):
                return
            await snap(page, "province-chosen")

            print("2) Trámite")
            if not await pick(await root(page), SEL["tramite"], lambda t: label_matches(tramite, t), "trámite"):
                return
            if not await click(await root(page), "accept"):
                return
            await snap(page, "tramite-chosen")

            print("3) Information page")
            if not await click(await root(page), "enter"):
                return
            await snap(page, "data-form")
            missing = False
            for key in ("doc_passport", "doc_nie", "doc_number", "name", "send"):
                present = await (await root(page)).locator(SEL[key]).count() > 0
                missing |= not present
                (ok if present else fail)(f"field {key}: {'found' if present else 'NOT found'}")
            has_country = await (await root(page)).locator(SEL["country"]).count() > 0
            ok(f"field country: {'found' if has_country else 'not asked for this trámite (optional)'}")
            if missing:
                await dump_elements(page)
            if await captcha(await root(page)):
                return
            if not check:
                print("\nMenu-only mode finished. Nothing was submitted.")
                return

            print("4) Your data (kept in memory only)")
            number = input("     Passport or NIE number: ").strip()
            name = input("     Full name as on the document: ").strip().upper()
            country = input("     Nationality in Spanish (e.g. BRASIL): ").strip().upper()
            await (await root(page)).locator(SEL["doc_nie"] if _nie_like(number) else SEL["doc_passport"]).first.check()
            await (await root(page)).locator(SEL["doc_number"]).first.fill(number)
            await (await root(page)).locator(SEL["name"]).first.fill(name)
            if await (await root(page)).locator(SEL["country"]).count():
                await pick(await root(page), SEL["country"], lambda t: country in t.upper(), "nationality")
            if await captcha(await root(page)):
                return
            await click(await root(page), "send")
            await snap(page, "after-data")
            await click(await root(page), "send")  # "Solicitar cita"
            await page.wait_for_timeout(2500)
            await snap(page, "availability")

            print("5) Result")
            body = (await (await root(page)).inner_text("body")).lower()
            if TEXT["no_slots"] in body:
                ok("site says there are no slots right now (detected correctly)")
            elif await captcha(await root(page)):
                pass
            elif await (await root(page)).locator(SEL["office"]).count():
                ok("office selection page reached: there may be availability")
            else:
                fail("unknown page. Check the last screenshot in ./debug and send it to Claude.")
                await dump_elements(page)
            print("\nStopped before choosing an office or booking. Nothing was booked.")
        except Exception as e:
            fail(f"{type(e).__name__}: {e}")
            await snap(page, "error")
        finally:
            input("\nPress Enter to close the browser...")
            await browser.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--province", default="granada", choices=list(PROVINCES))
    ap.add_argument("--tramite", default="nie", choices=list(TRAMITES))
    ap.add_argument("--check", action="store_true", help="enter your data and go up to the availability page")
    a = ap.parse_args()
    asyncio.run(run(a.province, a.tramite, a.check))


if __name__ == "__main__":
    main()
