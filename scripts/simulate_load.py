"""Local load simulation of the conversation pipeline, without WhatsApp, Claude or the site.

    python scripts/simulate_load.py --clients 1000 --concurrency 50

Each simulated client goes through consent, typed data, confirmation and a cita request. External
calls are replaced by fakes with configurable latency, so the numbers show Vale's own overhead
(database, routing, locks) and how latency of the LLM translates into required concurrency.
"""

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QUEUE_MODE", "inline")
if not os.environ.get("FERNET_KEY"):
    from cryptography.fernet import Fernet

    os.environ["FERNET_KEY"] = Fernet.generate_key().decode()

from app import db, router  # noqa: E402
from app.channels import Channel, IncomingMessage, set_channel  # noqa: E402
from app.citas import service  # noqa: E402


class NullChannel(Channel):
    sent = 0

    def parse_webhook(self, payload):
        return None

    async def send_text(self, phone, text):
        NullChannel.sent += 1

    async def send_buttons(self, phone, text, buttons):
        NullChannel.sent += 1

    async def send_audio(self, phone, audio, mime):
        NullChannel.sent += 1

    async def download_media(self, message):
        return b"", "image/jpeg"


class SlowAgent:
    def __init__(self, latency):
        self.latency = latency

    async def reply(self, session, user, text, on_delete, on_data=None):
        await asyncio.sleep(self.latency)  # stands in for a Claude call
        if text.startswith("DATA ") and on_data:
            await on_data(dict(kv.split("=", 1) for kv in text[5:].split(";")))
            return ""
        if text == "REQUEST":
            await service.create_request(session, user, "granada", "nie")
        return "ok"


async def client(i: int, sem: asyncio.Semaphore, lat: list[float]) -> None:
    phone = f"3469{i:07d}"
    steps = [dict(text="hola"), dict(button_id="consent:yes"),
             dict(text=f"DATA full_name=CLIENT {i};document_number=P{i:07d};nationality=BRASIL"),
             dict(button_id="data:latest:ok"), dict(text="REQUEST")]
    for n, step in enumerate(steps):
        async with sem:
            t0 = time.perf_counter()
            await router.handle(IncomingMessage(phone=phone, message_id=f"{i}-{n}", **step))
            lat.append(time.perf_counter() - t0)


async def main(clients: int, concurrency: int, llm_latency: float, db_url: str | None) -> None:
    url = db_url or f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/load.db"
    db.init_engine(url)
    await db.create_all()
    set_channel(NullChannel())
    router.set_agent(SlowAgent(llm_latency))
    sem, lat = asyncio.Semaphore(concurrency), []
    t0 = time.perf_counter()
    await asyncio.gather(*(client(i, sem, lat) for i in range(clients)))
    total = time.perf_counter() - t0
    lat.sort()
    msgs = len(lat)
    print(f"clients={clients} messages={msgs} concurrency={concurrency} fake_llm_latency={llm_latency}s db={url.split(':')[0]}")
    print(f"total {total:.1f}s  throughput {msgs / total:.1f} msg/s")
    print(f"latency p50 {lat[msgs // 2] * 1000:.0f} ms  p95 {lat[int(msgs * .95)] * 1000:.0f} ms  max {lat[-1] * 1000:.0f} ms")
    print(f"=> one worker at this concurrency handles ~{int(msgs / total * 86400):,} messages/day")
    await db.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, default=500)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--llm-latency", type=float, default=0.0)
    ap.add_argument("--db", default=None, help="database URL (default: temporary SQLite)")
    a = ap.parse_args()
    asyncio.run(main(a.clients, a.concurrency, a.llm_latency, a.db))
