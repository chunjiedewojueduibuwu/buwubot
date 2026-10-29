import json
import sqlite3

import pytest
from test_memory import proposal, setup_memory

from buwubot.config import MemoryPolicies, Settings
from buwubot.memory import MemoryService
from buwubot.memory_lifecycle import remaining
from buwubot.store import Conflict, Store

DAY = 86400


@pytest.mark.parametrize('tier,days', [('recent', 7), ('secondary', 180)])
async def test_ordinary_half_life_and_reopen_do_not_change_confidence(tmp_path, tier, days):
    store, memory, coordinator = setup_memory(tmp_path)
    p = proposal().model_copy(update={'tier': tier, 'event_time': 100})
    mid = await coordinator.commit(lambda: memory.commit(p))
    now = 100 + days * DAY
    view = memory.with_reading_policy(memory.get(mid), now=now)
    assert view['memory_remaining'] == pytest.approx(.5)
    assert view['confidence'] == p.confidence
    before = [tuple(r) for r in store.rows('SELECT * FROM memory_lifecycle')]
    assert memory.with_reading_policy(memory.get(mid), now=now) == view
    assert [tuple(r) for r in store.rows('SELECT * FROM memory_lifecycle')] == before
    cfg = memory.settings
    store.close()
    store = Store(tmp_path / 'bot.db')
    memory = MemoryService(store, cfg)
    try:
        assert memory.with_reading_policy(memory.get(mid), now=now) == view
    finally:
        store.close()


def test_legacy_policies_do_not_silently_enable_automatic_deletion():
    policies = MemoryPolicies.model_validate({'recent': {'strategy': 'recency', 'half_life_minutes': 1440},
                                             'secondary': {'strategy': 'relevance'}})
    assert policies.recent.forget_threshold == policies.secondary.forget_threshold == 0
    fresh = Settings(runtime_enabled=True, qq_enabled=True, owner_private_enabled=True, owner_id='10001', persona_name='助手', aliases=['助手']).memory_policies
    assert fresh.recent.forget_threshold == fresh.secondary.forget_threshold == .01
    assert fresh.important.forget_threshold == 0 and fresh.important.half_life_minutes is None


async def test_ordinary_expiry_uses_coordinator_scope_and_source_barrier(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    p = proposal().model_copy(update={'tier': 'recent', 'event_time': 100})
    mid = await coordinator.commit(lambda: memory.commit(p))
    now = 100 + 50 * DAY
    with pytest.raises(RuntimeError, match='coordinator'):
        memory.expire_ordinary(now=now)
    assert memory.with_reading_policy(memory.get(mid), now=now)['memory_remaining'] == 0
    assert await coordinator.commit(lambda: memory.expire_ordinary(now=now)) == 1
    assert memory.get(mid) is None
    assert memory.search_text('茶', group_id='123') == []
    assert store.rows('SELECT operation FROM index_outbox WHERE memory_id=? ORDER BY id DESC', (mid,))[0][0] == 'delete'
    with pytest.raises(Conflict):
        await coordinator.commit(lambda: memory.commit(p.model_copy(update={'content': '重述旧茶偏好'})))
    assert await coordinator.commit(lambda: memory.expire_ordinary(now=now)) == 0
    store.close()


async def test_manual_content_edit_preserves_decay_anchor_and_prevents_auto_deletion(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent', 'event_time': 100})))
    await coordinator.commit(lambda: memory.edit(mid, content='用户确认的短期事项', expected_revision=1))
    item = memory.get(mid)
    view = memory.with_reading_policy(item, now=100 + 50 * DAY)
    assert view['decay_basis_time'] == 100 and view['memory_protected'] is True
    assert await coordinator.commit(lambda: memory.expire_ordinary(now=100 + 500 * DAY)) == 0
    assert memory.get(mid)['revision'] == 2
    store.close()


async def test_important_and_unauthorized_or_other_persona_records_are_not_expired(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    important = proposal().model_copy(update={'tier': 'important', 'event_time': 100})
    mid = await coordinator.commit(lambda: memory.commit(important, reviewed=True))
    ordinary = proposal().model_copy(update={'tier': 'recent', 'event_time': 100, 'content': '另一个临时事项'})
    other = await coordinator.commit(lambda: memory.commit(ordinary))
    with store.transaction() as db:
        db.execute('UPDATE memories SET persona_id=? WHERE id=?', ('another-persona', other))
    now = 100 + 10000 * DAY
    assert memory.with_reading_policy(memory.get(mid), now=now)['memory_remaining'] == 1
    assert await coordinator.commit(lambda: memory.expire_ordinary(now=now)) == 0
    with store.transaction() as db:
        db.execute('UPDATE memories SET persona_id=NULL,group_id=? WHERE id=?', ('999', other))
    assert await coordinator.commit(lambda: memory.expire_ordinary(now=now)) == 0
    store.close()


async def test_existing_ordinary_without_side_state_uses_original_event_time_not_updated_at(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent', 'event_time': 100})))
    with store.transaction() as db:
        db.execute('DELETE FROM memory_lifecycle WHERE memory_id=?', (mid,))
    item = memory.get(mid) | {'updated_at': 100 + 7 * DAY}
    assert memory.with_reading_policy(item, now=100 + 7 * DAY)['memory_remaining'] == pytest.approx(.5)
    assert not store.rows('SELECT * FROM memory_lifecycle')
    assert json.dumps(memory.get(mid))  # Canonical facts remain serializable, with no read-side mutation.
    store.close()


async def test_lifecycle_state_cannot_be_written_outside_the_coordinator(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal()))
    item = memory.get(mid)
    before = [tuple(row) for row in store.rows('SELECT * FROM memory_lifecycle')]
    try:
        with pytest.raises(RuntimeError, match='coordinator'):
            memory.lifecycle.sync(item, item, memory.settings.memory_policies, now=1000, manual=True)
        assert [tuple(row) for row in store.rows('SELECT * FROM memory_lifecycle')] == before
    finally:
        store.close()


def test_clock_rollback_never_amplifies_retention_and_long_elapsed_is_bounded():
    assert remaining(.6, 200, 100, 7 * 1440) == .6
    assert 0 <= remaining(1, 0, 10**20, 1) < 1e-300
    assert remaining(1, 0, 10**20, None) == 1
    with pytest.raises(ValueError):
        remaining(1, 0, float('nan'), 10)


async def test_index_failure_rolls_back_fact_and_lifecycle_together(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal()))
    old = memory.get(mid)
    state = [tuple(row) for row in store.rows('SELECT * FROM memory_lifecycle')]
    store.connection.execute("CREATE TRIGGER fail_index BEFORE INSERT ON index_outbox "
                             "BEGIN SELECT RAISE(ABORT,'fixture index failure'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await coordinator.commit(lambda: memory.edit(mid, content='修改后内容', expected_revision=1))
        assert memory.get(mid) == old
        assert [tuple(row) for row in store.rows('SELECT * FROM memory_lifecycle')] == state
    finally:
        store.close()
