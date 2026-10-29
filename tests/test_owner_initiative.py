import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_engine import build

from buwubot.models import Completion


def prepared(tmp_path):
    now = [datetime(2026,10,24,10,tzinfo=ZoneInfo('Asia/Shanghai'))]
    store, engine, qq, _ = build(tmp_path,lambda:now[0])
    cfg = engine.settings.model_copy(update={'owner_id':'22','owner_initiative_enabled':True,
                                            'owner_initiative_interval_minutes':60})
    store.save_settings(cfg,store.settings()[1])
    engine.settings = engine.memory.settings = engine.coordinator.settings = cfg
    def incoming(mid=1):
        return engine.memory.archive.accept({'post_type':'message','message_type':'private',
            'self_id':11,'user_id':22,'message_id':mid,'time':now[0].timestamp()-600,
            'sender':{'nickname':'owner'},'message':[{'type':'text','data':{'text':'hello'}}]},cfg)
    incoming()
    return store, engine, qq, now, incoming


def due(engine, now):
    engine.enqueue_owner_initiative()
    now[0] += timedelta(hours=1,seconds=1)
    engine.enqueue_owner_initiative()
    rows = engine.store.rows("SELECT id FROM jobs WHERE kind='initiative'")
    assert len(rows) == 1
    return engine.queue.get(rows[0]['id'])


@pytest.mark.parametrize('changes',[{'owner_initiative_enabled':False},{'owner_private_enabled':False},
    {'qq_enabled':False},{'owner_id':'11'},{'runtime_enabled':False}])
def test_initiative_requires_explicit_owner_private_permission(tmp_path,changes):
    store, engine, _, now, _ = prepared(tmp_path)
    engine.settings = engine.settings.model_copy(update=changes)
    try:
        engine.enqueue_owner_initiative()
        now[0] += timedelta(hours=2)
        engine.enqueue_owner_initiative()
        assert not store.rows("SELECT id FROM jobs WHERE kind='initiative'")
    finally:
        store.close()


async def test_owner_initiative_sends_once_through_coordinator_and_does_not_nudge_unanswered(tmp_path):
    store, engine, qq, now, _ = prepared(tmp_path)
    marker=[{'id':'growth-private','kind':'habit','content':'助手喜欢分享实际读到的发现。','revision':1}]
    engine.memory.growth_context=lambda **kwargs:marker
    class Models:
        async def call(self,role,payload,**kwargs):
            assert role == 'chat'
            data = json.loads(payload[-1]['content'])
            assert data['persona_growth']==marker
            assert all(row['group_id'] == 'private:22' for row in data['context'])
            return Completion('',engine.settings.primary_model,{'text':'想和你聊会儿'})
    engine.models = Models()
    try:
        job = due(engine,now)
        await engine.process(job)
        assert len(qq.sent) == 1 and qq.sent[0][0] == 'private:22'
        assert qq.sent[0][2] is None
        assert store.rows("SELECT id FROM jobs WHERE kind='memory'")
        for row in store.rows("SELECT id FROM jobs WHERE kind='memory'"):
            engine.queue.finish(row['id'])
        now[0] += timedelta(days=1)
        engine.enqueue_owner_initiative()
        assert len(store.rows("SELECT id FROM jobs WHERE kind='initiative'")) == 1
        await engine.process(engine.queue.get(job['id']))
        assert len(qq.sent) == 1
    finally:
        store.close()


async def test_new_owner_input_during_generation_cancels_optional_message(tmp_path):
    store, engine, qq, now, incoming = prepared(tmp_path)
    class Models:
        async def call(self,*args,**kwargs):
            incoming(2)
            return Completion('',engine.settings.primary_model,{'text':'stale initiative'})
    engine.models = Models()
    try:
        job = due(engine,now)
        await engine.process(job)
        assert not qq.sent
        assert engine.queue.get(job['id'])['state'] == 'skipped_expired'
    finally:
        store.close()


async def test_silence_is_successful_decision_without_send(tmp_path):
    store, engine, qq, now, _ = prepared(tmp_path)
    class Models:
        async def call(self,*args,**kwargs):
            return Completion('',engine.settings.primary_model,{'text':None})
    engine.models = Models()
    try:
        job = due(engine,now)
        await engine.process(job)
        assert not qq.sent and engine.queue.get(job['id'])['state'] == 'succeeded'
        engine.enqueue_owner_initiative()
        assert len(store.rows("SELECT id FROM jobs WHERE kind='initiative'")) == 1
    finally:
        store.close()


@pytest.mark.parametrize('change',['new_input','permission','expired'])
async def test_final_coordinator_rechecks_optional_message_before_sending(tmp_path,change):
    store, engine, qq, now, incoming = prepared(tmp_path)
    try:
        job = due(engine,now)
        if change == 'new_input':
            incoming(2)
        elif change == 'permission':
            cfg, revision = store.settings()
            store.save_settings(cfg.model_copy(update={'owner_initiative_enabled':False}),revision)
        else:
            now[0] += timedelta(hours=2)
        await engine.coordinator.send(job['id'],[{'type':'text','data':{'text':'stale'}}],
                                      now=now[0].timestamp())
        assert not qq.sent
        assert engine.queue.get(job['id'])['state'] == 'skipped_expired'
    finally:
        store.close()


def test_no_initiative_during_routine_pause_or_foreground_work(tmp_path):
    store, engine, _, now, _ = prepared(tmp_path)
    try:
        engine.enqueue_owner_initiative()
        now[0] += timedelta(hours=13)
        engine.enqueue_owner_initiative()
        assert not store.rows("SELECT id FROM jobs WHERE kind='initiative'")
        now[0] -= timedelta(hours=12)
        engine.queue.enqueue('reply',{'group_id':'123'},dedupe_key='foreground',priority=0,
                             source_time=now[0].timestamp())
        engine.enqueue_owner_initiative()
        assert not store.rows("SELECT id FROM jobs WHERE kind='initiative'")
    finally:
        store.close()


async def test_unconfirmed_send_spends_anchor_even_after_reconciliation_skips(tmp_path):
    from buwubot.qq import SendUncertain

    store, engine, qq, now, _ = prepared(tmp_path)
    async def uncertain(*args,**kwargs):
        raise SendUncertain('response lost')
    qq.send = uncertain
    try:
        job = due(engine,now)
        await engine.process(job)
        outgoing = dict(store.rows('SELECT * FROM outgoing')[0])
        assert outgoing['status'] == 'uncertain'
        await engine.coordinator._reconcile(outgoing)
        assert engine.queue.get(job['id'])['state'] == 'skipped_uncertain'
        now[0] += timedelta(days=4)
        engine.enqueue_owner_initiative()
        assert len(store.rows("SELECT id FROM jobs WHERE kind='initiative'")) == 1
    finally:
        store.close()


async def test_long_unanswered_period_allows_occasional_contact_not_permanent_silence(tmp_path):
    store, engine, qq, now, _ = prepared(tmp_path)
    try:
        job = due(engine,now)
        await engine.process(job)
        for row in store.rows("SELECT id FROM jobs WHERE kind='memory'"):
            engine.queue.finish(row['id'])
        now[0] += timedelta(days=4)
        engine.enqueue_owner_initiative()
        rows = store.rows("SELECT id FROM jobs WHERE kind='initiative' AND state='pending'")
        assert len(rows) == 1
        assert len(qq.sent) == 1
    finally:
        store.close()


async def test_older_backfill_does_not_replace_latest_owner_input(tmp_path):
    store, engine, qq, now, _ = prepared(tmp_path)
    try:
        job = due(engine,now)
        old_time = now[0].timestamp() - 7200
        engine.memory.archive.accept({'post_type':'message','message_type':'private','self_id':11,
            'user_id':22,'message_id':2,'time':old_time,'sender':{'nickname':'owner'},
            'message':[{'type':'text','data':{'text':'earlier history'}}]},engine.settings)
        await engine.process(job)
        assert len(qq.sent) == 1
    finally:
        store.close()


async def test_changed_owner_expires_old_optional_job_instead_of_blocking_new_owner(tmp_path):
    store, engine, _, now, _ = prepared(tmp_path)
    try:
        job = due(engine,now)
        cfg = engine.settings.model_copy(update={'owner_id':'33'})
        store.save_settings(cfg,store.settings()[1])
        engine.settings = engine.memory.settings = engine.coordinator.settings = cfg
        await engine.process(job)
        assert engine.queue.get(job['id'])['state'] == 'skipped_expired'
    finally:
        store.close()


async def test_foreground_arriving_during_generation_defers_saved_optional_draft(tmp_path):
    store, engine, qq, now, _ = prepared(tmp_path)
    class Models:
        async def call(self,*args,**kwargs):
            engine.queue.enqueue('reply',{'group_id':'123'},dedupe_key='new-foreground',priority=0,
                                 source_time=now[0].timestamp())
            return Completion('',engine.settings.primary_model,{'text':'wait until quiet'})
    engine.models = Models()
    try:
        job = due(engine,now)
        await engine.process(job)
        assert not qq.sent
        assert engine.queue.get(job['id'])['state'] == 'pending'
        assert engine.queue.get(job['id'])['payload']['segments'][0]['data']['text'] == 'wait until quiet'
    finally:
        store.close()
