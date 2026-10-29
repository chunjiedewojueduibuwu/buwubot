import sqlite3

import pytest
from test_memory import proposal, setup_memory

from buwubot.memory import MemoryProposal, MemoryService, ReviewRequired
from buwubot.store import Conflict, Store

DAY = 86400


def distilled(memory, parent, tier='secondary', **changes):
    data = proposal().model_dump() | {
        'tier': tier, 'content': '甲长期偏爱茶', 'snapshot_epoch': memory.epoch(),
        'distilled_from': [{'memory_id': parent, 'expected_revision': memory.get(parent)['revision']}]}
    return MemoryProposal.model_validate(data | changes)


@pytest.mark.parametrize('tier,days', [('secondary', 14), ('important', 30)])
async def test_distillation_preserves_recent_level_fact_and_receipt(tmp_path, monkeypatch, tier, days):
    store, memory, coordinator = setup_memory(tmp_path)
    monkeypatch.setattr('buwubot.memory.time.time', lambda: 100)
    parent = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent'})))
    original = memory.get(parent)
    now = 100 + 7 * DAY
    monkeypatch.setattr('buwubot.memory.time.time', lambda: now)
    p = distilled(memory, parent, tier)
    try:
        target = await coordinator.commit(lambda: memory.commit(p, reviewed=True))
        assert memory.get(parent) == original
        view = memory.with_reading_policy(memory.get(parent), now=now)
        assert view['memory_remaining'] == pytest.approx(.5)
        assert view['memory_half_life_minutes'] == days * 1440
        assert memory.with_reading_policy(memory.get(parent), now=now + days * DAY)['memory_remaining'] == pytest.approx(.25)
        assert memory.get(target)['tier'] == tier
        links = [tuple(row) for row in store.rows('SELECT * FROM memory_distillations')]
        state = memory.lifecycle.state(original)
        assert await coordinator.commit(lambda: memory.commit(p, reviewed=True)) == target
        assert [tuple(row) for row in store.rows('SELECT * FROM memory_distillations')] == links
        assert memory.lifecycle.state(original) == state
    finally:
        store.close()


async def test_later_secondary_distillation_cannot_shorten_important_detail_window(tmp_path, monkeypatch):
    store, memory, coordinator = setup_memory(tmp_path)
    monkeypatch.setattr('buwubot.memory.time.time', lambda: 100)
    parent = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent'})))
    try:
        await coordinator.commit(lambda: memory.commit(distilled(memory, parent, 'important'), reviewed=True))
        monkeypatch.setattr('buwubot.memory.time.time', lambda: 100 + 30 * DAY)
        await coordinator.commit(lambda: memory.commit(distilled(memory, parent, content='甲不喜欢咖啡'), reviewed=True))
        view = memory.with_reading_policy(memory.get(parent), now=100 + 30 * DAY)
        assert view['memory_remaining'] == pytest.approx(.5)
        assert view['memory_half_life_minutes'] == 30 * 1440
    finally:
        store.close()


@pytest.mark.parametrize('fault', ['revision', 'identity', 'source', 'manual', 'tier', 'self', 'persona', 'group', 'recall'])
async def test_invalid_distillation_never_creates_target_or_changes_parent(tmp_path, fault):
    store, memory, coordinator = setup_memory(tmp_path)
    parent = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent'})))
    p = distilled(memory, parent)
    if fault == 'revision':
        await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={
            'memory_id': parent, 'expected_revision': 1, 'tier': 'recent', 'content': '更新过的原文'}), reviewed=True))
    elif fault == 'identity':
        p = p.model_copy(update={'subject_id': ''})
    elif fault == 'source':
        source = memory.archive.get('123:id:1')
        memory.archive.accept(source.__class__(**(vars(source) | {'id': '123:id:2', 'message_id': '2', 'time': 200,
            'text': '不同来源', 'segments': [{'type': 'text', 'data': {'text': '不同来源'}}]})), memory.settings)
        p = p.model_copy(update={'source_message_ids': ['123:id:2']})
    elif fault == 'manual':
        await coordinator.commit(lambda: memory.edit(parent, content='人工维护的原文', expected_revision=1))
        p = distilled(memory, parent)
    elif fault == 'tier':
        p = p.model_copy(update={'tier': 'recent'})
    elif fault == 'self':
        p = p.model_copy(update={'memory_id': parent, 'expected_revision': 1})
    elif fault in {'persona', 'group'}:
        column, value = ('persona_id', 'other') if fault == 'persona' else ('group_id', '999')
        store.connection.execute(f'UPDATE memories SET {column}=? WHERE id=?', (value, parent))
    elif fault == 'recall':
        store.connection.execute('UPDATE messages SET recalled=1')
    before = memory.get(parent)
    state = memory.lifecycle.state(before)
    try:
        with pytest.raises((ValueError, Conflict)):
            await coordinator.commit(lambda: memory.commit(p, reviewed=True))
        assert memory.get(parent) == before
        assert memory.lifecycle.state(before) == state
        assert len(store.rows('SELECT * FROM memories')) == 1
        assert not store.rows('SELECT * FROM memory_distillations')
    finally:
        store.close()


async def test_multi_source_distillation_is_atomic_and_persistent(tmp_path, monkeypatch):
    store, memory, coordinator = setup_memory(tmp_path)
    monkeypatch.setattr('buwubot.memory.time.time', lambda: 100)
    first = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent'})))
    second = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={
        'tier': 'recent', 'content': '甲还在聊天中再次提到喝茶'})))
    p = distilled(memory, first)
    data = p.model_dump()
    data['distilled_from'].append({'memory_id': second, 'expected_revision': 99})
    bad = MemoryProposal.model_validate(data)
    before = memory.lifecycle.state(memory.get(first))
    try:
        with pytest.raises(Conflict):
            await coordinator.commit(lambda: memory.commit(bad, reviewed=True))
        assert memory.lifecycle.state(memory.get(first)) == before
        assert len(store.rows('SELECT * FROM memories')) == 2
        assert not store.rows('SELECT * FROM memory_distillations')
        data['distilled_from'][1]['expected_revision'] = 1
        target = await coordinator.commit(lambda: memory.commit(MemoryProposal.model_validate(data), reviewed=True))
        cfg = memory.settings
    finally:
        store.close()
    store = Store(tmp_path / 'bot.db')
    memory = MemoryService(store, cfg)
    try:
        assert len(store.rows('SELECT * FROM memory_distillations WHERE target_id=?', (target,))) == 2
        for mid in (first, second):
            assert memory.with_reading_policy(memory.get(mid), now=100 + 14 * DAY)['memory_remaining'] == pytest.approx(.5)
    finally:
        store.close()


async def test_secondary_distillation_requires_review_and_rolls_back_with_index_failure(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    parent = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent'})))
    p = distilled(memory, parent)
    state = memory.lifecycle.state(memory.get(parent))
    try:
        with pytest.raises(ReviewRequired):
            await coordinator.commit(lambda: memory.commit(p))
        store.connection.execute("CREATE TRIGGER fail_index BEFORE INSERT ON index_outbox "
                                 "BEGIN SELECT RAISE(ABORT,'fixture index failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            await coordinator.commit(lambda: memory.commit(p, reviewed=True))
        assert len(store.rows('SELECT * FROM memories')) == 1
        assert memory.lifecycle.state(memory.get(parent)) == state
        assert not store.rows('SELECT * FROM memory_distillations')
        assert not memory.was_applied(p)
    finally:
        store.close()
