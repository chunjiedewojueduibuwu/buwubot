from copy import deepcopy

from buwubot.conversations import message_context


def test_source_context_uses_message_day_and_never_mutates_archive():
    source = {'id': 'source', 'time': 1792924583, 'text': '这位是阿简', 'forwarded': {
        'nodes': [{'time': 1792924583, 'author_id': '22', 'text': '原图', 'images': []}]}}
    before = deepcopy(source)
    context = message_context(source)
    assert context['local_time'] == '2026-10-25T18:36:23+08:00'
    assert context['forwarded']['nodes'][0]['local_time'] == context['local_time']
    assert source == before


def test_unknown_source_time_does_not_become_current_time():
    source = {'id': 'source', 'time': None, 'text': '时间未知'}
    assert message_context(source)['local_time'] is None
    assert 'local_time' not in source
