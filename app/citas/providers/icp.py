"""Provider for the Spanish government's cita previa system (ICP / "icpplus").

IMPORTANT, before enabling in production:
- Every selector in SEL and every text in TEXT must be validated against the live site
  for each launch province; the site changes and differs by province.
- Vale never solves CAPTCHAs. When one appears the request goes to `needs_human` and the
  user finishes that step themselves.
- Be polite: one browser per check, human-like pacing, a minimum interval per request
  (MONITOR_MIN_INTERVAL_MINUTES) and a global concurrency cap.
- Book only for the requesting user, with their own data, after their explicit approval.
"""

import asyncio
import logging
import random
import uuid

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.citas.catalog import PROVINCES, TRAMITES, label_matches
from app.citas.providers.base import BookResult, CheckResult, CitaProvider, Slot
from app.config import get_settings

log = logging.getLogger(__name__)

BASE_URL = "https://icp.administracionelectronica.gob.es/icpplus/index.html"

# --- VERIFY against the live site ---
SEL = {
    "province": "select#form",
    "accept": "#btnAceptar",
    "tramite": "select[id^='tramiteGrupo']",
    "enter": "#btnEntrar",
    "doc_passport": "#rdbTipoDocPas",
    "doc_nie": "#rdbTipoDocNie",
    "doc_number": "#txtIdCitado",
    "name": "#txtDesCitado",
    "country": "#txtPaisNac",
    "send": "#btnEnviar",
    "office": "#idSede",
    "next": "#btnSiguiente",
    "phone": "#txtTelefonoCitado",
    "email1": "#emailUNO",
    "email2": "#emailDOS",
    "slot_radio": "input[type='radio'][name^='rdbCita']",
    "confirm_check": "#chkTotal",
    "send_email_check": "#enviarCorreo",
    "confirm": "#btnConfirmar",
    "captcha": "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], #captcha",
}
TEXT = {
    "no_slots": "no hay citas disponibles",
    "reference": "justificante",
}


def _nie_like(value: str) -> bool:
    v = value.upper().replace("-", "")
    return len(v) == 9 and v[0] in "XYZ" and v[1:8].isdigit()


async def _pause(lo: float = 0.8, hi: float = 2.2) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


class ICPProvider(CitaProvider):
    public_url = BASE_URL

    def __init__(self):
        self._pw = None
        self._browser: Browser | None = None
        self._held: dict[str, tuple[BrowserContext, Page]] = {}

    async def _ensure_browser(self) -> Browser:
        if self._browser is None:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=get_settings().headless)
        return self._browser

    async def _new_page(self) -> tuple[BrowserContext, Page]:
        browser = await self._ensure_browser()
        ctx = await browser.new_context(locale="es-ES", timezone_id="Europe/Madrid", viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        page.set_default_timeout(30_000)
        return ctx, page

    async def _captcha(self, page: Page) -> bool:
        return await page.locator(SEL["captcha"]).count() > 0

    async def _select_by_text(self, page: Page, selector: str, predicate) -> bool:
        options = await page.locator(f"{selector} option").all()
        for opt in options:
            text = (await opt.inner_text()).strip()
            if predicate(text):
                await page.select_option(selector, value=await opt.get_attribute("value"))
                return True
        return False

    async def _reach_slots(self, page: Page, province: str, tramite_code: str, profile: dict, phone: str = "") -> CheckResult | None:
        """Walk the form up to the slot list. Returns a CheckResult to stop early, or None when slots are on screen."""
        tramite = TRAMITES[tramite_code]
        await page.goto(BASE_URL)
        await _pause()
        if not await self._select_by_text(page, SEL["province"], lambda t: PROVINCES[province].lower() in t.lower()):
            return CheckResult("error", reason="province_option_not_found")
        await page.click(SEL["accept"])
        await _pause()
        if not await self._select_by_text(page, SEL["tramite"], lambda t: label_matches(tramite, t)):
            return CheckResult("error", reason="tramite_option_not_found")
        await page.click(SEL["accept"])
        await _pause()
        await page.click(SEL["enter"])
        await _pause()

        number = str(profile.get("nie") or profile.get("document_number") or "")
        await page.check(SEL["doc_nie"] if _nie_like(number) else SEL["doc_passport"])
        await page.fill(SEL["doc_number"], number)
        await page.fill(SEL["name"], str(profile.get("full_name", "")).upper())
        if await page.locator(SEL["country"]).count():
            await self._select_by_text(page, SEL["country"], lambda t: str(profile.get("nationality", "")).upper() in t.upper())
        if await self._captcha(page):
            return CheckResult("needs_human", reason="captcha")
        await page.click(SEL["send"])
        await _pause()
        await page.click(SEL["send"])  # "Solicitar cita"
        await _pause(1.5, 3.0)

        body = (await page.inner_text("body")).lower()
        if TEXT["no_slots"] in body:
            return CheckResult("no_slots")
        if await self._captcha(page):
            return CheckResult("needs_human", reason="captcha")

        if await page.locator(SEL["office"]).count():
            # First office with availability; a later version can honour user preferences.
            await page.select_option(SEL["office"], index=1)
            await page.click(SEL["next"])
            await _pause()
        if await page.locator(SEL["phone"]).count():
            await page.fill(SEL["phone"], phone[-9:] if phone else "")
            email = str(profile.get("email", ""))
            if email and await page.locator(SEL["email1"]).count():
                await page.fill(SEL["email1"], email)
                await page.fill(SEL["email2"], email)
            await page.click(SEL["next"])
            await _pause()
        return None

    async def _read_slots(self, page: Page) -> list[Slot]:
        slots = []
        for radio in await page.locator(SEL["slot_radio"]).all():
            rid = await radio.get_attribute("id") or ""
            label = page.locator(f"label[for='{rid}']") if rid else None
            text = (await label.inner_text()).strip() if label and await label.count() else ""
            slots.append(Slot(office=text.split("\n")[0][:200] or "Oficina de extranjería", starts_at=text[-40:] or "", raw={"radio_id": rid, "text": text}))
        return slots

    async def check(self, request_id: int, province: str, tramite_code: str, profile: dict) -> CheckResult:
        ctx, page = await self._new_page()
        keep = False
        try:
            early = await self._reach_slots(page, province, tramite_code, profile, profile.get("phone", ""))
            if early:
                return early
            slots = await self._read_slots(page)
            if not slots:
                return CheckResult("no_slots")
            # Keep the session open so the user's approval books exactly this slot.
            key = uuid.uuid4().hex
            self._held[key] = (ctx, page)
            keep = True
            return CheckResult("slots", slots=slots, hold_key=key)
        except Exception as e:  # network, layout change, timeout
            log.warning("check failed for request %s: %s", request_id, e)
            return CheckResult("error", reason=type(e).__name__)
        finally:
            if not keep:
                await ctx.close()

    async def book(self, request_id: int, slot: Slot, profile: dict, phone: str, hold_key: str | None) -> BookResult:
        held = self._held.pop(hold_key, None) if hold_key else None
        if not held:
            return BookResult("slot_gone", reason="session_expired")
        ctx, page = held
        try:
            rid = slot.raw.get("radio_id")
            if not rid or not await page.locator(f"#{rid}").count():
                return BookResult("slot_gone")
            await page.check(f"#{rid}")
            await page.click(SEL["next"])
            await _pause()
            if await self._captcha(page):
                return BookResult("needs_human", reason="captcha")
            if await page.locator(SEL["confirm_check"]).count():
                await page.check(SEL["confirm_check"])
            if await page.locator(SEL["send_email_check"]).count():
                await page.check(SEL["send_email_check"])
            await page.click(SEL["confirm"])
            await _pause(1.5, 3.0)
            body = await page.inner_text("body")
            if TEXT["reference"] not in body.lower():
                return BookResult("needs_human", reason="unexpected_confirmation_page")
            ref_line = next((ln for ln in body.splitlines() if TEXT["reference"] in ln.lower()), "")
            return BookResult("booked", reference=ref_line.strip()[:120] or None)
        except Exception as e:
            log.warning("booking failed for request %s: %s", request_id, e)
            return BookResult("error", reason=type(e).__name__)
        finally:
            await ctx.close()

    async def release(self, hold_key: str) -> None:
        held = self._held.pop(hold_key, None)
        if held:
            await held[0].close()

    async def close(self) -> None:
        for key in list(self._held):
            await self.release(key)
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
