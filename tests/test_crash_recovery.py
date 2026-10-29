import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from buwubot.config import Settings
from buwubot.coordinator import Coordinator
from buwubot.memory import MemoryProposal, MemoryService
from buwubot.store import Store


@pytest.mark.parametrize("boundary",["before-commit","after-commit","send-unknown","retry-wait"])
async def test_real_process_crash_preserves_atomicity_and_avoids_duplicate_send(tmp_path,boundary):
    result=await asyncio.to_thread(subprocess.run,[sys.executable,str(Path(__file__).with_name("crash_worker.py")),
                                  str(tmp_path),boundary],timeout=10,capture_output=True,check=False)
    assert result.returncode==23,result.stderr.decode(errors="replace")
    store=Store(tmp_path/"bot.db")
    cfg=Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手'], group_id="123",self_id="11")
    memory=MemoryService(store,cfg)

    class QQFixture:
        async def history(self,*args):
            file=tmp_path/"simulated-qq.json"
            return [json.loads(file.read_text(encoding="utf-8"))] if file.exists() else []

        async def send(self,*args):
            pytest.fail("recovery must not send an already emitted message again")

    coordinator=Coordinator(store,QQFixture(),cfg)
    assert store.rows("PRAGMA integrity_check")[0][0]=="ok"
    assert len(store.rows("SELECT * FROM memories"))==(1 if boundary=="after-commit" else 0)
    assert len(store.rows("SELECT * FROM index_outbox"))==(1 if boundary=="after-commit" else 0)
    await coordinator.recover()
    jid=store.rows("SELECT id FROM jobs")[0][0]
    job=coordinator.queue.get(jid)
    if boundary=="send-unknown":
        assert job["state"]=="succeeded"
        assert store.rows("SELECT status FROM outgoing")[0][0]=="sent"
    elif boundary=="retry-wait":
        assert job["state"]=="retry_wait" and job["next_attempt"]==220
    else:
        assert job["state"]=="pending"
        p=MemoryProposal.model_validate(job["payload"]["proposal"])
        await coordinator.commit(lambda:memory.commit(p))
        assert len(store.rows("SELECT * FROM memories"))==1
        assert len(store.rows("SELECT * FROM index_outbox"))==1
        assert store.rows("SELECT revision FROM memories")[0][0]==1
    store.close()
