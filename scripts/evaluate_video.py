"""Resumable isolated video acceptance; no real QQ transport or balance endpoint."""

import argparse
import asyncio
import io
import json
import sqlite3
import time
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from buwubot.config import Settings
from buwubot.coordinator import Coordinator, WorkPaused
from buwubot.engine import BotEngine
from buwubot.memory import MemoryService
from buwubot.models import ModelClient, ModelUnavailable
from buwubot.skill_plugins import SkillPlugins
from buwubot.stickers import MediaStore, StickerService
from buwubot.store import Store
from buwubot.tools import ToolRegistry


class IsolatedTransport:
    connected = True

    async def send(self, group, segments, reply):
        return 'isolated-' + str(time.time_ns())

    async def history(self, *args, **kwargs):
        return []


async def evaluate(args):
    root = Path(__file__).resolve().parents[1]
    folder = args.data_dir.resolve()
    assert folder.is_relative_to((root / '.tools').resolve()), 'evaluation data must stay in .tools'
    assert folder != (root / '.tools').resolve(), 'use a dedicated evaluation directory'
    folder.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect((root / 'data/bot.db').as_uri() + '?mode=ro', uri=True)) as db:
        current = Settings.model_validate_json(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
    # Persist only non-secret synthetic settings. Actual API key stays in the client in memory.
    cfg = Settings(group_id='123', self_id='11', owner_id='22', persona=current.persona,
        persona_name=current.persona_name, persona_id=current.persona_id,
        workbuddy_url=current.workbuddy_url, workbuddy_key='isolated-placeholder',
        primary_model=current.primary_model, auxiliary_model=current.auxiliary_model,
        reviewer_model=current.reviewer_model, routine_enabled=False)
    store = Store(folder / 'bot.db')
    client = None
    try:
        saved, revision = store.settings()
        if saved.storage() != cfg.storage():
            store.save_settings(cfg, revision)
        qq = IsolatedTransport()
        coordinator = Coordinator(store, qq, cfg)
        memory = MemoryService(store, cfg)
        media = MediaStore(store, folder / 'media')
        tools = ToolRegistry()
        engine = BotEngine(store, coordinator, memory, media, StickerService(store, cfg, media), tools, folder)
        engine.qq = qq
        manager = SkillPlugins(store, tools, folder / 'skills', media=media)
        item = next((item for item in manager.list() if item['id']=='bilibili_video'), None)
        if item is None:
            package = io.BytesIO()
            with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name in ('plugin.json', 'main.py'):
                    archive.write(root / 'skill_packages/bilibili_video' / name, name)
            item = await coordinator.commit(lambda:manager.install(package.getvalue()))
        if not item['enabled']:
            await coordinator.commit(lambda:manager.set_enabled('bilibili_video', True,
                                                                expected_revision=item['revision']))
        if not store.rows("SELECT id FROM jobs WHERE dedupe_key='video-acceptance'"):
            now = datetime.now(ZoneInfo('Asia/Shanghai'))
            with store.transaction():
                message = memory.archive.accept({'post_type':'message', 'message_type':'group',
                    'self_id':11, 'group_id':123, 'user_id':22, 'message_id':1,
                    'time':now.timestamp(), 'sender':{'nickname':'isolated evaluator'},
                    'message':[{'type':'text', 'data':{'text':'Summarize this video: ' + args.url}}]}, cfg)
                engine.queue.enqueue('reply', {'group_id':'123', 'persona_id':cfg.persona_id,
                    'messages':[vars(message)], 'source_message_ids':[message.id],
                    'reply_source_id':message.id, 'reply_id':message.message_id},
                    dedupe_key='video-acceptance', priority=0, source_time=message.time)
                store.set_state('video_acceptance_url', args.url)
        assert store.get_state('video_acceptance_url') == args.url, 'use a new data directory for another video'
        if not args.initialize_only:
            client = ModelClient(cfg.model_copy(update={'workbuddy_key':current.workbuddy_key}))
            engine.models = memory.models = client
            await coordinator.recover()
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                job = engine.queue.claim(time.time(), kinds=['reply','tool'])
                if job:
                    try:
                        await engine.process(job)
                    except Exception as exc:  # noqa: BLE001 -- retain diagnostic failures without a retry loop for unknown bugs
                        engine.queue.defer(job['id'], type(exc).__name__, now=time.time(),
                            needs_attention=getattr(exc, 'needs_attention',
                                not isinstance(exc, (ModelUnavailable, WorkPaused, OSError))),
                            retry_after=getattr(exc, 'retry_after', 0))
                pending = store.rows("SELECT id FROM jobs WHERE kind IN ('reply','tool') "
                    "AND state IN ('pending','running','retry_wait')")
                if not pending:
                    break
                await asyncio.sleep(1)
        jobs = [dict(row) for row in store.rows("SELECT kind,state,next_attempt FROM jobs "
                                              "WHERE kind IN ('reply','tool')")]
        report = {'scope':'persistent isolated acceptance; fake transport only; no formal writes',
            'url':args.url, 'jobs':jobs, 'outgoing':[dict(row) for row in store.rows(
                'SELECT status,segments FROM outgoing')], 'tool_progress':[
                {'steps':len(engine.queue.get(row['id'])['payload'].get('sequence_seen', [])),
                 'sources':engine.queue.get(row['id'])['payload'].get('sequence_sources', []),
                 'notes':engine.queue.get(row['id'])['payload'].get('sequence_notes', ''),
                 'result_state':engine.queue.get(row['id'])['payload'].get('tool_result', {}).get('state')}
                for row in store.rows("SELECT id FROM jobs WHERE kind='tool'")],
            'initialize_only':args.initialize_only}
        report['outcome'] = ('initialized' if args.initialize_only else 'delivery_completed'
            if any(row['status']=='sent' for row in report['outgoing']) and all(
                row['state']=='succeeded' for row in jobs) else 'checkpoint_pending'
            if any(row['state'] in {'pending','running','retry_wait'} for row in jobs)
            else 'attention_or_terminal_without_delivery')
        report['semantic_review'] = 'required; process completion alone is not acceptance'
        (folder / 'checkpoint.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'initialize_only':args.initialize_only, 'jobs':jobs,
                          'completed_steps':[row['steps'] for row in report['tool_progress']]}))
    finally:
        if client:
            await client.close()
        store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('.tools/video-acceptance-data'))
    parser.add_argument('--url', default='https://www.bilibili.com/video/BV1htnt6nENM/')
    parser.add_argument('--seconds', type=int, default=300)
    parser.add_argument('--initialize-only', action='store_true')
    options = parser.parse_args()
    if not 1 <= options.seconds <= 600:
        parser.error('--seconds must be between 1 and 600')
    asyncio.run(evaluate(options))
