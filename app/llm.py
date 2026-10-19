"""One Anthropic client and one concurrency limit per process, shared by the agent and the
document reader, so a burst of messages can't exceed the API rate limit."""

import asyncio

from anthropic import AsyncAnthropic

from app.config import get_settings

_client: AsyncAnthropic | None = None
_sem: asyncio.Semaphore | None = None


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=get_settings().anthropic_api_key, max_retries=4)
    return _client


def limiter() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(get_settings().llm_concurrency)
    return _sem
