import pytest
from test_memory import proposal, setup_memory

from buwubot.memory import MemoryProposal, MemoryService
from buwubot.store import Store

DAY = 86400


def mention(memory, mid, when, *, text='再强调一次，我确实喜欢喝茶', quote_only=False):
    return memory.archive.accept({'time': when, 'post_type': 'message', 'message_type': 'group',
        'group_id': 123, 'user_id': 22, 'self_id': 11, 'message_id': mid,
        'message': [{'type': 'reply', 'data': {'id': '1'}}] if quote_only else
                   [{'type': 'text', 'data': {'text': text}}]}, memory.settings)


def update(memory, mid, source, *, reinforce=True):
    item = memory.get(mid)
    return MemoryProposal.model_validate(proposal().model_dump() | {
        'memory_id': mid, 'expected_revision': item['revision'], 'tier': item['tier'],
        'content': item['content'], 'event_time': item['event_time'], 'snapshot_epoch': memory.epoch(),
        'source_message_ids': [source.id],
        'reinforcement_source_ids': [source.id] if reinforce else []})


async def test_new_evidence_strengthens_once_and_preserves_event_time_after_restart(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent', 'event_time': 100})))
    now = 100 + 7 * DAY
    source = mention(memory, 2, now)
    candidate = update(memory, mid, source)
    try:
        assert memory.with_reading_policy(memory.get(mid), now=now)['memory_remaining'] == pytest.approx(.5)
        await coordinator.commit(lambda: memory.commit(candidate, reviewed=True))
        view = memory.with_reading_policy(memory.get(mid), now=now)
        assert view['memory_remaining'] == pytest.approx(.575)
        assert view['decay_basis_time'] == now and memory.get(mid)['event_time'] == 100
        assert await coordinator.commit(lambda: memory.commit(candidate, reviewed=True)) == mid
        assert memory.get(mid)['revision'] == 2
        cfg = memory.settings
    finally:
        store.close()
    store = Store(tmp_path / 'bot.db')
    memory = MemoryService(store, cfg)
    try:
        assert memory.with_reading_policy(memory.get(mid), now=now) == view
    finally:
        store.close()


async def test_old_alias_and_reconsidering_same_source_do_not_strengthen_twice(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent', 'event_time': 100})))
    now = 100 + 7 * DAY
    source = mention(memory, 2, now)
    await coordinator.commit(lambda: memory.commit(update(memory, mid, source), reviewed=True))
    before = memory.lifecycle.state(memory.get(mid))
    duplicate = source.__class__(**(vars(source) | {'id': '123:id:99', 'message_id': '99', 'source_provider': 'snowluma'}))
    assert memory.archive.accept(duplicate, memory.settings) is None
    try:
        candidate = update(memory, mid, duplicate).model_copy(update={'reason': '新的整理理由不代表新的经历'})
        await coordinator.commit(lambda: memory.commit(candidate, reviewed=True))
        assert memory.lifecycle.state(memory.get(mid)) == before
        source_again = update(memory, mid, source)
        await coordinator.commit(lambda: memory.commit(source_again, reviewed=True))
        assert memory.lifecycle.state(memory.get(mid)) == before
    finally:
        store.close()


@pytest.mark.parametrize('quote_only', [False, True])
async def test_background_update_or_quote_only_is_not_new_emphasis(tmp_path, quote_only):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent', 'event_time': 100})))
    now = 100 + 7 * DAY
    source = mention(memory, 2, now, quote_only=quote_only)
    try:
        await coordinator.commit(lambda: memory.commit(update(memory, mid, source, reinforce=quote_only), reviewed=True))
        assert memory.with_reading_policy(memory.get(mid), now=now)['memory_remaining'] == pytest.approx(.5)
        # Reinterpreting an already-processed source cannot turn it into a new emphasis.
        await coordinator.commit(lambda: memory.commit(update(memory, mid, source), reviewed=True))
        assert memory.with_reading_policy(memory.get(mid), now=now)['memory_remaining'] == pytest.approx(.5)
    finally:
        store.close()


async def test_delayed_processing_does_not_make_an_old_emphasis_fresh(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent', 'event_time': 100})))
    source = mention(memory, 2, 100 + 7 * DAY)
    try:
        await coordinator.commit(lambda: memory.commit(update(memory, mid, source), reviewed=True))
        view = memory.with_reading_policy(memory.get(mid), now=100 + 21 * DAY)
        assert view['memory_remaining'] == pytest.approx(.575 / 4)
        assert view['decay_basis_time'] == source.time
    finally:
        store.close()


async def test_reinforcement_requires_evidence_in_the_current_proposal(tmp_path):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={'tier': 'recent', 'event_time': 100})))
    source = mention(memory, 2, 100 + 7 * DAY)
    try:
        candidate = update(memory, mid, source).model_copy(update={'reinforcement_source_ids': ['private:44:id:3']})
        with pytest.raises(ValueError, match='reinforcement'):
            await coordinator.commit(lambda: memory.commit(candidate, reviewed=True))
        assert memory.get(mid)['revision'] == 1
    finally:
        store.close()


@pytest.mark.parametrize('same_batch', [False, True])
async def test_independent_same_second_emphasis_counts_each_source_once(tmp_path, same_batch):
    store, memory, coordinator = setup_memory(tmp_path)
    mid = await coordinator.commit(lambda: memory.commit(proposal().model_copy(update={
        'tier':'recent','event_time':100})))
    when = 100 + 7 * DAY
    sources = [mention(memory,2,when,text='再次强调喜欢茶'),
               mention(memory,3,when,text='再说一次，还是茶')]
    try:
        if same_batch:
            candidate = update(memory,mid,sources[0]).model_copy(update={
                'source_message_ids':[s.id for s in sources],
                'reinforcement_source_ids':[s.id for s in sources]})
            await coordinator.commit(lambda:memory.commit(candidate,reviewed=True))
        else:
            for source in sources:
                candidate = update(memory,mid,source)
                await coordinator.commit(lambda candidate=candidate:memory.commit(candidate,reviewed=True))
        assert memory.with_reading_policy(memory.get(mid),now=when)['memory_remaining']==pytest.approx(.63875)
        before=memory.lifecycle.state(memory.get(mid))
        await coordinator.commit(lambda:memory.commit(candidate,reviewed=True))
        assert memory.lifecycle.state(memory.get(mid))==before
        assert memory.get(mid)['event_time']==100
    finally:
        store.close()
