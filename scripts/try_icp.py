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


async def options(page: Page, selector: str) -> list[str]:
    return [(await o.inner_text()).strip() for o in await page.locator(f"{selector} option").all()]


async def pick(page: Page, selector: str, predicate, what: str) -> bool:
    if not await page.locator(selector).count():
        fail(f"{what}: selector {selector!r} not found on the page")
        return False
    for opt in await page.locator(f"{selector} option").all():
        text = (await opt.inner_text()).strip()
        if predicate(text):
            await page.select_option(selector, value=await opt.get_attribute("value"))
            ok(f"{what}: chose {text!r}")
            return True
    fail(f"{what}: no matching option. Options on the page:")
    for t in await options(page, selector):
        print(f"          - {t}")
    return False


async def click(page: Page, key: str) -> bool:
    sel = SEL[key]
    if not await page.locator(sel).count():
        fail(f"button {key}: selector {sel!r} not found")
        return False
    await page.click(sel)
    await page.wait_for_load_state("domcontentloaded")
    ok(f"clicked {key} ({sel})")
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
            await snap(page, "home")
            if not await pick(page, SEL["province"], lambda t: PROVINCES[province].lower() in t.lower(), "province"):
                return
            if not await click(page, "accept"):
                return
            await snap(page, "province-chosen")

            print("2) Trámite")
            if not await pick(page, SEL["tramite"], lambda t: label_matches(tramite, t), "trámite"):
                return
            if not await click(page, "accept"):
                return
            await snap(page, "tramite-chosen")

            print("3) Information page")
            if not await click(page, "enter"):
                return
            await snap(page, "data-form")
            for key in ("doc_passport", "doc_nie", "doc_number", "name", "country", "send"):
                present = await page.locator(SEL[key]).count() > 0
                (ok if present else fail)(f"field {key}: {SEL[key]!r} {'found' if present else 'NOT found'}")
            if await captcha(page):
                return
            if not check:
                print("\nMenu-only mode finished. Nothing was submitted.")
                return

            print("4) Your data (kept in memory only)")
            number = input("     Passport or NIE number: ").strip()
            name = input("     Full name as on the document: ").strip().upper()
            country = input("     Nationality in Spanish (e.g. BRASIL): ").strip().upper()
            await page.check(SEL["doc_nie"] if _nie_like(number) else SEL["doc_passport"])
            await page.fill(SEL["doc_number"], number)
            await page.fill(SEL["name"], name)
            if await page.locator(SEL["country"]).count():
                await pick(page, SEL["country"], lambda t: country in t.upper(), "nationality")
            if await captcha(page):
                return
            await click(page, "send")
            await snap(page, "after-data")
            await click(page, "send")  # "Solicitar cita"
            await page.wait_for_timeout(2500)
            await snap(page, "availability")

            print("5) Result")
            body = (await page.inner_text("body")).lower()
            if TEXT["no_slots"] in body:
                ok("site says there are no slots right now (detected correctly)")
            elif await captcha(page):
                pass
            elif await page.locator(SEL["office"]).count():
                ok("office selection page reached: there may be availability")
            else:
                fail("unknown page. Check the last screenshot in ./debug and send it to Claude.")
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
