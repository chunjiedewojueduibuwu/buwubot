import pytest

from buwubot.video_links import normalize_video_url, video_references

URL = 'https://www.bilibili.com/video/BV1234567890/'


@pytest.mark.parametrize('value,expected', [
    ('BV1234567890', URL),
    ('m.bilibili.com/video/BV1234567890?p=2&share_source=qq', URL+'?p=2'),
    ('http://www.bilibili.com/video/BV1234567890/?p=3', URL+'?p=3'),
    ('//bilibili.com/zh-Hans/video/BV1234567890/', URL),
    ('https:\\/\\/m.bilibili.com\\/video\\/BV1234567890?p=2&amp;share_source=qq', URL+'?p=2'),
    ('（https://www.bilibili.com/video/BV1234567890/）。', URL),
    ('b23.tv/AbC123?share_source=qq', 'https://b23.tv/AbC123'),
    ('https%3A%2F%2Fm.bilibili.com%2Fvideo%2FBV1234567890%3Fp%3D2', URL+'?p=2'),
])
def test_video_share_forms_preserve_part_and_normalize_without_network(value, expected):
    assert normalize_video_url(value) == expected


@pytest.mark.parametrize('value', [
    'https://bilibili.com.evil.example/video/BV1234567890/',
    'https://bilibili.com@evil.example/video/BV1234567890/',
    'https://user:password@www.bilibili.com/video/BV1234567890/',
    'https://@www.bilibili.com/video/BV1234567890/',
    'https://www.bilibili.com:9876/video/BV1234567890/',
    'https://api.bilibili.com/x/v2/reply?oid=1',
    URL+'#comments', URL+'?p=0', URL+'?p=1001', URL+'?p=2&p=3',
    'file:///BV1234567890', 'https://b23.tv/a/extra',
])
def test_unsafe_or_ambiguous_video_references_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_video_url(value)


def test_extract_share_text_deduplicates_and_never_mines_bv_from_other_hosts():
    text = ('看这个：https://evil.example/video/BV9999999999/ '
            'm.bilibili.com/video/BV1234567890?p=2&amp;share_source=qq，'
            '还有（BV1234567890） https://evil.example/?next=BV8888888888 '
            'http://b23.tv/AbC123。')
    assert video_references(text) == [URL+'?p=2', URL, 'https://b23.tv/AbC123']
    assert video_references(' '.join([URL]*30)) == [URL]


def test_native_json_share_decodes_unicode_query_separator():
    card = r'{"meta":{"url":"https:\/\/m.bilibili.com\/video\/BV1234567890?p=2\u0026share_source=qq"}}'
    assert video_references(card) == [URL+'?p=2']


@pytest.mark.parametrize('text', [
    'https://evil.example/?next=%22BV1234567890%22',
    'https://evil.example/?next=%20BV1234567890',
    'https://evil.example/?next=&quot;BV1234567890&quot;',
    'evil.example/?next=BV1234567890',
    'evil.example?next=BV1234567890',
    '127.0.0.1/?next=BV1234567890',
    'next=BV1234567890',
    'evil.example/?next="BV1234567890"',
])
def test_encoded_bv_inside_untrusted_url_is_not_a_video_reference(text):
    assert video_references(text) == []


def test_json_url_value_is_not_split_at_escaped_quotes():
    import json
    assert video_references(json.dumps({'url':'https://evil.example/?next="BV1234567890"'})) == []
    assert video_references(json.dumps({'url':'evil.example/?next=BV1234567890'})) == []
    assert video_references(json.dumps({'url':'evil.example/?next="BV1234567890"'})) == []
    assert video_references(json.dumps({'url':'127.0.0.1/?next="BV1234567890"'})) == []
