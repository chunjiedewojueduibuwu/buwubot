import asyncio
import logging

import httpx
import pytest

from buwubot.config import Settings
from buwubot.models import ModelClient


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
