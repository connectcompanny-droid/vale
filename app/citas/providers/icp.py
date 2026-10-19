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

# Confirmed on Granada (Oct 2026): province select, tramiteGrupo[0..1] lists, and the data form
# (rdbTipoDocNie/Dni/Pas, txtIdCitado, txtDesCitado, btnEnviar). Nationality is not asked for
# every trámite. Steps after the data form still need validation.
# The site runs an anti-bot layer (TSPD frame). Vale must not try to evade it: if it blocks,
# the request goes to needs_human and checks back off.
SEL = {
    # Several candidates per element (CSS "a, b, c"): the first match wins. Narrow these down
    # to the real ids once confirmed with scripts/try_icp.py.
    "province": "select#form, select[name='form'], select[id*='rovinc' i], select[name*='rovinc' i], form select",
    "accept": "#btnAceptar, input[value='Aceptar'], button:has-text('Aceptar')",
    "tramite": "select[id^='tramiteGrupo'], select[name^='tramiteGrupo'], select[id*='tramite' i]",
    "enter": "#btnEntrar, input[value='Entrar'], button:has-text('Entrar')",
    "doc_passport": "#rdbTipoDocPas, input[type='radio'][value*='PASAPORTE' i]",
    "doc_nie": "#rdbTipoDocNie, input[type='radio'][value*='N.I.E' i], input[type='radio'][value*='NIE' i]",
    "doc_number": "#txtIdCitado, input[name='txtIdCitado']",
    "name": "#txtDesCitado, input[name='txtDesCitado']",
    "country": "#txtPaisNac, select[name='txtPaisNac']",
    "send": "#btnEnviar, input[value*='Enviar' i], input[value*='Solicitar' i], button:has-text('Solicitar')",
    # Buttons that only move through intermediate pages (info pages after the data form).
    # Never includes booking/confirm buttons.
    "advance": "#btnSubmit, #btnEnviar, input[value*='Solicitar' i], button:has-text('Solicitar')",
    "office": "#idSede, select[name='idSede']",
    "next": "#btnSiguiente, input[value='Siguiente'], button:has-text('Siguiente')",
    "phone": "#txtTelefonoCitado, input[name='txtTelefonoCitado']",
    "email1": "#emailUNO, input[name='emailUNO']",
    "email2": "#emailDOS, input[name='emailDOS']",
    "slot_radio": "input[type='radio'][name^='rdbCita']",
    "confirm_check": "#chkTotal",
    "send_email_check": "#enviarCorreo",
    "confirm": "#btnConfirmar, input[value*='Confirmar' i], button:has-text('Confirmar')",
    "captcha": "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], #captcha",
}
TEXT = {
    "no_slots": "no hay citas disponibles",
    "reference": "justificante",
}


TLS_ERRORS = ("ERR_CERT_", "ERR_SSL_", "SSL_ERROR", "SEC_ERROR_")


def classify_error(e: Exception) -> str:
    """Short reason for logs/audit. Certificate problems get their own class so the monitor can
    pause instead of retrying: an untrusted certificate may mean the connection is intercepted."""
    msg = str(e)
    if any(code in msg for code in TLS_ERRORS):
        return "tls_untrusted"
    if "Timeout" in type(e).__name__ or "Timeout" in msg:
        return "timeout"
    if "ERR_" in msg:
        return "network"
    return type(e).__name__


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
            s = get_settings()
            # Real Chrome when available: up-to-date root store. TLS errors are NEVER ignored:
            # personal data goes through this connection.
            try:
                self._browser = await self._pw.chromium.launch(headless=s.headless, channel=s.browser_channel or None)
            except Exception as e:
                if not s.browser_channel:
                    raise
                log.warning("browser channel %r unavailable (%s); using bundled Chromium", s.browser_channel, e)
                self._browser = await self._pw.chromium.launch(headless=s.headless)
        return self._browser

    async def _new_page(self) -> tuple[BrowserContext, Page]:
        browser = await self._ensure_browser()
        ctx = await browser.new_context(locale="es-ES", timezone_id="Europe/Madrid", viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        page.set_default_timeout(30_000)
        return ctx, page

    async def _f(self, page: Page, timeout_s: float = 20.0):
        """Frame holding the form: the ICP page may render late or inside an iframe."""
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

    async def _captcha(self, page: Page) -> bool:
        return any([await fr.locator(SEL["captcha"]).count() > 0 for fr in page.frames])

    async def _select_by_text(self, page, selector: str, predicate) -> bool:
        """Pick the first option matching `predicate` across ALL selects matching `selector`
        (the trámite page has one list per group: extranjería offices, police, ...)."""
        for select in await page.locator(selector).all():
            for opt in await select.locator("option").all():
                text = (await opt.inner_text()).strip()
                if predicate(text):
                    await select.select_option(value=await opt.get_attribute("value"))
                    return True
        return False

    async def _f(self, page: Page, timeout_s: float = 20.0):
        """Frame holding the form: the ICP page may render late or inside an iframe."""
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

    async def _captcha(self, page: Page) -> bool:
        return any([await fr.locator(SEL["captcha"]).count() > 0 for fr in page.frames])

    async def _select_by_text(self, page: Page, selector: str, predicate) -> bool:
        select = page.locator(selector).first
        if not await select.count():
            return False
        for opt in await select.locator("option").all():
            text = (await opt.inner_text()).strip()
            if predicate(text):
                await select.select_option(value=await opt.get_attribute("value"))
                return True
        return False

    async def _reach_slots(self, page: Page, province: str, tramite_code: str, profile: dict, phone: str = "") -> CheckResult | None:
        """Walk the form up to the slot list. Returns a CheckResult to stop early, or None when slots are on screen."""
        tramite = TRAMITES[tramite_code]
        await page.goto(BASE_URL)
        await page.wait_for_load_state("networkidle")
        await _pause()
        if not await self._select_by_text(await self._f(page), SEL["province"], lambda t: PROVINCES[province].lower() in t.lower()):
            return CheckResult("error", reason="province_option_not_found")
        await (await self._f(page)).locator(SEL["accept"]).first.click()
        await _pause()
        if not await self._select_by_text(await self._f(page), SEL["tramite"], lambda t: label_matches(tramite, t)):
            return CheckResult("error", reason="tramite_option_not_found")
        await (await self._f(page)).locator(SEL["accept"]).first.click()
        await _pause()
        await (await self._f(page)).locator(SEL["enter"]).first.click()
        await _pause()

        number = str(profile.get("nie") or profile.get("document_number") or "")
        await (await self._f(page)).locator(SEL["doc_nie"] if _nie_like(number) else SEL["doc_passport"]).first.check()
        await (await self._f(page)).locator(SEL["doc_number"]).first.fill(number)
        await (await self._f(page)).locator(SEL["name"]).first.fill(str(profile.get("full_name", "")).upper())
        if await (await self._f(page)).locator(SEL["country"]).count():
            await self._select_by_text(await self._f(page), SEL["country"], lambda t: str(profile.get("nationality", "")).upper() in t.upper())
        if await self._captcha(page):
            return CheckResult("needs_human", reason="captcha")
        await (await self._f(page)).locator(SEL["send"]).first.click()
        await _pause()
        # Walk through intermediate pages ("infogenerica", "Solicitar cita") until a result shows.
        for _ in range(4):
            frame = await self._f(page)
            body = (await frame.inner_text("body")).lower()
            if TEXT["no_slots"] in body:
                return CheckResult("no_slots")
            if await self._captcha(page):
                return CheckResult("needs_human", reason="captcha")
            if await frame.locator(SEL["office"]).count() or await frame.locator(SEL["slot_radio"]).count() or await frame.locator(SEL["phone"]).count():
                break
            nxt = frame.locator(SEL["advance"]).first
            if not await nxt.count():
                return CheckResult("error", reason="stuck_after_data_form")
            await nxt.click()
            await _pause(1.5, 3.0)

        if await (await self._f(page)).locator(SEL["office"]).count():
            # First office with availability; a later version can honour user preferences.
            await (await self._f(page)).locator(SEL["office"]).first.select_option(index=1)
            await (await self._f(page)).locator(SEL["next"]).first.click()
            await _pause()
        if await (await self._f(page)).locator(SEL["phone"]).count():
            await (await self._f(page)).locator(SEL["phone"]).first.fill(phone[-9:] if phone else "")
            email = str(profile.get("email", ""))
            if email and await (await self._f(page)).locator(SEL["email1"]).count():
                await (await self._f(page)).locator(SEL["email1"]).first.fill(email)
                await (await self._f(page)).locator(SEL["email2"]).first.fill(email)
            await (await self._f(page)).locator(SEL["next"]).first.click()
            await _pause()
        return None

    async def _read_slots(self, page: Page) -> list[Slot]:
        slots = []
        for radio in await (await self._f(page)).locator(SEL["slot_radio"]).all():
            rid = await radio.get_attribute("id") or ""
            label = (await self._f(page)).locator(f"label[for='{rid}']") if rid else None
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
            return CheckResult("error", reason=classify_error(e))
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
            if not rid or not await (await self._f(page)).locator(f"#{rid}").count():
                return BookResult("slot_gone")
            await (await self._f(page)).locator(f"#{rid}").check()
            await (await self._f(page)).locator(SEL["next"]).first.click()
            await _pause()
            if await self._captcha(page):
                return BookResult("needs_human", reason="captcha")
            if await (await self._f(page)).locator(SEL["confirm_check"]).count():
                await (await self._f(page)).locator(SEL["confirm_check"]).first.check()
            if await (await self._f(page)).locator(SEL["send_email_check"]).count():
                await (await self._f(page)).locator(SEL["send_email_check"]).first.check()
            await (await self._f(page)).locator(SEL["confirm"]).first.click()
            await _pause(1.5, 3.0)
            body = await (await self._f(page)).inner_text("body")
            if TEXT["reference"] not in body.lower():
                return BookResult("needs_human", reason="unexpected_confirmation_page")
            ref_line = next((ln for ln in body.splitlines() if TEXT["reference"] in ln.lower()), "")
            return BookResult("booked", reference=ref_line.strip()[:120] or None)
        except Exception as e:
            log.warning("booking failed for request %s: %s", request_id, e)
            return BookResult("error", reason=classify_error(e))
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
