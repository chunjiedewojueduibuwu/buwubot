"""Disposable process used only by crash-boundary tests."""

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from buwubot.config import Settings
from buwubot.coordinator import Coordinator
from buwubot.memory import MemoryProposal, MemoryService
from buwubot.store import Store


async def run():
    directory,boundary=Path(sys.argv[1]),sys.argv[2]
    store=Store(directory/"bot.db")
    cfg=Settings(runtime_enabled=True, qq_enabled=True, group_id="123",self_id="11")
    memory=MemoryService(store,cfg)
    memory.archive.accept({"post_type":"message","message_type":"group","group_id":123,
        "self_id":11,"user_id":22,"message_id":1,"time":100,"message":[]},cfg)
    proposal=MemoryProposal(group_id="123",subject_id="22",content="甲喜欢茶",source_message_ids=["123:id:1"])

    class QQFixture:
        async def send(self,group,segments,reply_id=None):
            with (directory/"simulated-qq.json").open("w",encoding="utf-8") as output:
                json.dump({"user_id":11,"time":100,"message_id":88,"message":[
                    {"type":"reply","data":{"id":reply_id}},*segments]},output)
                output.flush()
                os.fsync(output.fileno())
            os._exit(23)

    coordinator=Coordinator(store,QQFixture(),cfg)
    queue=coordinator.queue
    jid=queue.enqueue("reply" if boundary=="send-unknown" else "memory",
        {"group_id":"123","proposal":proposal.model_dump(),"reply_id":"1"},
        dedupe_key="crash-boundary",priority=0,source_time=100)
    queue.claim(100)
    if boundary=="retry-wait":
        queue.defer(jid,"temporary model outage",now=100,retry_after=120)
    elif boundary=="send-unknown":
        await coordinator.send(jid,[{"type":"text","data":{"text":"你好"}}],now=100)
    else:
        if boundary=="before-commit":
            original=store.transaction

            @contextmanager
            def interrupted_transaction():
                with original() as db:
                    yield db
                    os._exit(23)

            store.transaction=interrupted_transaction
        await coordinator.commit(lambda:memory.commit(proposal))
    os._exit(23)


if __name__=="__main__":
    asyncio.run(run())
