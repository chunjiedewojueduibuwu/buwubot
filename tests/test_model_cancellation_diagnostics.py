import asyncio
import logging

import httpx
import pytest

from buwubot.config import Settings
from buwubot.models import ModelClient, ModelUnavailable


async def test_cancelled_call_records_role_without_retrying_or_logging_secrets(caplog):
    entered = asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()

    caplog.set_level(logging.INFO, logger='buwubot.models')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelClient(Settings(workbuddy_key='credential-fixture'), http=http)
        task = asyncio.create_task(client.call('auxiliary', [{'role': 'user', 'content': 'private-fixture'}]))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == 1
    assert 'event=model_request_cancelled role=auxiliary' in caplog.text
    assert 'credential-fixture' not in caplog.text and 'private-fixture' not in caplog.text


async def test_response_deadline_bounds_stalled_calls_even_with_transport_heartbeats():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        # A mock transport, like a perpetually heartbeating upstream, never raises a read timeout.
        await asyncio.Event().wait()

    settings = Settings().model_copy(update={'model_timeout_seconds': 0.02})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelClient(settings, http=http)
        with pytest.raises(ModelUnavailable, match='deadline') as failed:
            await asyncio.wait_for(client.call('chat', []), 0.5)
    assert calls == 2
    assert not failed.value.needs_attention
    assert failed.value.shared
