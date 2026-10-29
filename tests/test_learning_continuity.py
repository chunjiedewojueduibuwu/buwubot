import json

import pytest
from test_learning import URL, setup

from buwubot.coordinator import WorkPaused
from buwubot.learning import LearningService
from buwubot.models import Completion


async def test_resumed_learning_receives_prior_notes_after_material_retention_and_restart(tmp_path):
    class Models:
        def __init__(self):
            self.inputs = []

        async def call(self, role, payload, **kwargs):
            self.inputs.append(json.loads(payload[-1]['content']))
            return Completion('', 'cn:deepseek-v4.1-flash', {
                'summary':'Only the first sampled interval was read.', 'facts':[], 'suggestions':[]})

    models = Models()
    store, service = setup(tmp_path, models=models)
    try:
        item = service.enqueue(URL)
        await service.step(item)
        service.cleanup(now=10**12)
        assert store.rows('SELECT material FROM learning_segments')[0][0] is None
        await service.close()
        resumed = LearningService(store, service.coordinator, service.memory, models,
                                  service.source, allowed=lambda:True)
        await resumed.step(item)
        context = models.inputs[-1]['prior_notes']
        assert context['omitted_count'] == 0
        assert len(context['notes']) == 1
        note = context['notes'][0]
        assert note['summary'] == 'Only the first sampled interval was read.'
        assert note['start_seconds'] == 0 and note['end_seconds'] == 60
        assert note['coverage'] == 'captions_only'
        assert models.inputs[-1]['start_seconds'] == 60
        assert not store.rows('SELECT * FROM memories')
        await resumed.close()
    finally:
        store.close()


def seed_note(store, service, *, item=None, start=0, summary='prior note', state='committed',
              persona=None, stamp=None):
    cfg, _ = store.settings()
    item = item or service.enqueue(URL)
    segment = f'{item}:{start}:{persona or cfg.persona_id}:{stamp or cfg.persona_fingerprint}'
    with store.transaction() as db:
        db.execute('INSERT INTO learning_segments(id,item_id,start_seconds,end_seconds,coverage,'
                   'persona_stamp,state,created_at) VALUES(?,?,?,?,?,?,?,?)',
                   (segment,item,start,start+1,'sampled_frames',stamp or cfg.persona_fingerprint,state,1))
        db.execute('INSERT INTO learning_notes VALUES(?,?,?,?,?,?)',
                   (segment,summary,'[]','[]',persona or cfg.persona_id,1))
    return item


def test_prior_notes_exclude_other_source_persona_version_future_and_uncommitted(tmp_path):
    store, service = setup(tmp_path)
    try:
        cfg, _ = store.settings()
        item = seed_note(store, service, summary='allowed')
        seed_note(store, service, start=1, persona='other', summary='private')
        seed_note(store, service, start=2, stamp='old-persona', summary='old setting')
        seed_note(store, service, start=3, state='material', summary='unfinished')
        seed_note(store, service, start=4, stamp=cfg.persona_fingerprint + ':notes-v1', summary='legacy note')
        seed_note(store, service, start=5, stamp=cfg.persona_fingerprint + ':notes-v2', summary='current note')
        seed_note(store, service, start=70, summary='future')
        other = service.enqueue('https://www.bilibili.com/video/BV0000000000/')
        seed_note(store, service, item=other, summary='other video')
        context = service._prior_notes(dict(service.get(item), cursor_seconds=60), cfg)
        assert [note['summary'] for note in context['notes']] == ['allowed', 'legacy note', 'current note']
        assert context['omitted_count'] == 0
    finally:
        store.close()


@pytest.mark.parametrize('summary', ['small', '\x00' * 2000], ids=['small','escaped'])
def test_prior_notes_use_serialized_byte_budget_not_fixed_count(tmp_path, summary):
    store, service = setup(tmp_path)
    try:
        for start in range(30):
            item = seed_note(store, service, start=start, summary=summary)
        context = service._prior_notes(dict(service.get(item), cursor_seconds=60), store.settings()[0])
        assert len(json.dumps(context,ensure_ascii=False).encode()) <= 16000
        assert context['omitted_count'] + len(context['notes']) == 30
        assert context['notes'][-1]['start_seconds'] == 29
        if summary == 'small':
            assert len(context['notes']) == 30
        else:
            assert context['omitted_count'] > 0
    finally:
        store.close()


@pytest.mark.parametrize('legacy_suffix', ['', ':notes-v1'])
async def test_old_cached_draft_regenerates_and_current_contract_reuses_after_pause(tmp_path, legacy_suffix):
    allowed = True

    class Models:
        calls = 0

        async def call(self, role, payload, **kwargs):
            nonlocal allowed
            self.calls += 1
            allowed = False
            return Completion('', 'cn:deepseek-v4.1-flash', {
                'summary':'source-bound note', 'facts':[], 'suggestions':[]})

    models = Models()
    store, service = setup(tmp_path, models=models, allowed=lambda:allowed)
    try:
        item = service.enqueue(URL)
        with pytest.raises(WorkPaused):
            await service.step(item)
        with store.transaction() as db:
            db.execute('UPDATE learning_segments SET persona_stamp=?',
                       (store.settings()[0].persona_fingerprint + legacy_suffix,))
        allowed = True
        with pytest.raises(WorkPaused):
            await service.step(item)
        assert models.calls == 2
        allowed = True
        await service.step(item)
        assert models.calls == 2
        assert service.get(item)['cursor_seconds'] == 60
    finally:
        await service.close()
        store.close()


async def test_revision_receives_identical_prior_notes(tmp_path):
    class Models:
        initial = None
        revision = None

        async def call(self, role, payload, **kwargs):
            data = json.loads(payload[-1]['content'])
            draft = {'summary':'current source only', 'facts':[], 'suggestions':[]}
            if 'feedback' in data:
                self.revision = data['prior_notes']
            elif data['start_seconds'] == 60:
                self.initial = data['prior_notes']
                draft['facts'] = [{'content':'uncertain current claim', 'confidence':.2}]
            return Completion('', 'cn:deepseek-v4.1-flash', draft)

    models = Models()
    store, service = setup(tmp_path, models=models)
    try:
        item = service.enqueue(URL)
        await service.step(item)
        await service.step(item)
        assert models.initial == models.revision
        assert len(models.revision['notes']) == 1
        assert service.get(item)['state'] == 'completed'
    finally:
        await service.close()
        store.close()
