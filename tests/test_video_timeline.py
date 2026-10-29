import pytest
from pydantic import ValidationError

from buwubot.learning import LearningSegment


def timeline(**changes):
    return dict(url='https://www.bilibili.com/video/BV1234567890/',
        video_id='BV1234567890', title='actual', uploader='author', part=1,
        start_seconds=60, end_seconds=62.5, duration=62.5, captions=[],
        coverage='timeline_frames', visual_fps=1,
        frames=[{'timestamp':60, 'frame_times':[60,61,62], 'asset_hash':'a'*64}],
        audio_status='transcribed', speech=[{'start':60.2,'end':62.3,'text':'真实音轨文字'}],
        **changes)


def test_timeline_keeps_ordered_visual_and_speech_timestamps_and_old_material():
    item = LearningSegment.model_validate(timeline())
    assert item.frames[0].frame_times == [60,61,62]
    assert item.speech[0].text == '真实音轨文字'
    old = timeline()
    for field in ('visual_fps','speech','audio_status'):
        old.pop(field)
    old.update(coverage='sampled_frames', frames=[{'timestamp':61,'asset_hash':'a'*64}])
    assert LearningSegment.model_validate(old).audio_status == 'not_read'


@pytest.mark.parametrize('patch', [
    {'frames':[{'timestamp':60,'frame_times':[60,62],'asset_hash':'a'*64}]},
    {'frames':[{'timestamp':60,'frame_times':[60,62,61],'asset_hash':'a'*64}]},
    {'frames':[{'timestamp':60,'frame_times':[60,61,62.5],'asset_hash':'a'*64}]},
    {'speech':[{'start':59,'end':62,'text':'越界'}]},
    {'speech':[{'start':60,'end':63,'text':'越界'}]},
    {'speech':[{'start':61,'end':60,'text':'倒置'}]},
    {'speech':[{'start':60,'end':61,'text':''}]},
    {'speech':[]}, {'visual_fps':float('nan')},
    {'audio_status':'no_speech_detected'},
])
def test_timeline_rejects_missing_visual_coverage_and_invalid_speech(patch):
    value = timeline()
    value.update(patch)
    with pytest.raises(ValidationError):
        LearningSegment.model_validate(value)


def test_empty_transcription_is_uncertain_not_a_claim_of_silence():
    value = timeline()
    value.update(audio_status='no_speech_detected', speech=[])
    item = LearningSegment.model_validate(value)
    assert item.audio_status == 'no_speech_detected' and not item.speech


async def test_sequential_reader_decodes_entire_fractional_interval_and_offsets_audio(tmp_path, monkeypatch):
    import asyncio
    import shutil

    from buwubot.video_media import TimelineSource, run_media
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('FFmpeg not installed')
    video = tmp_path / 'synthetic.mp4'
    await run_media(ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i',
        'testsrc2=size=320x180:rate=25:duration=4.5','-f','lavfi','-i',
        'sine=frequency=440:duration=4.5','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',
        '-c:a','aac',str(video))
    class Media:
        def save(self,content):
            assert content
            return {'hash':'a'*64}
    folder = tmp_path / 'operation'
    folder.mkdir()
    source = TimelineSource(folder,media=Media(),asr_python='',asr_model_dir='',worker='')
    calls = []
    transcribing = asyncio.Event()
    interrupted = True
    async def playback(*args):
        return ('BV1234567890','https://www.bilibili.com/video/BV1234567890/',
                {'title':'actual','owner':{'name':'author'}},4.5,'unused')
    async def download(*args):
        return video
    async def speech(path):
        calls.append(path)
        if interrupted:
            transcribing.set()
            await asyncio.Event().wait()
        return {'audio_status':'transcribed','speech':[{'start':.2,'end':1.5,'text':'actual speech'}],
                'language':'en','language_probability':.2}
    monkeypatch.setattr(source,'_playback',playback)
    monkeypatch.setattr(source,'_download',download)
    monkeypatch.setattr(source,'_transcribe',speech)
    async def captions(*args):
        from buwubot.learning import SourceUnavailable
        raise SourceUnavailable('no public caption track')
    monkeypatch.setattr(source,'_caption_segment',captions)
    try:
        task = asyncio.create_task(source.read_segment('https://www.bilibili.com/video/BV1234567890/',1,2,60))
        await asyncio.wait_for(transcribing.wait(),5)
        checkpoint = {p.name:p.stat().st_mtime_ns for p in folder.glob('*.jpg')}
        assert checkpoint
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        interrupted = False
        result = await source.read_segment('https://www.bilibili.com/video/BV1234567890/',1,2,60)
        assert checkpoint == {p.name:p.stat().st_mtime_ns for p in folder.glob('*.jpg')}
        assert result.end_seconds == result.duration == 4.5
        assert result.coverage == 'timeline_frames'
        assert [t for f in result.frames for t in f.frame_times] == [2,3,4]
        assert result.speech[0].start == 2.2 and result.speech[0].end == 3.5
        assert result.audio_language == 'en' and result.audio_language_probability == .2
        assert calls and calls[0].is_file()
    finally:
        await source.close()


async def test_media_command_cancellation_reaps_only_its_own_process(tmp_path):
    import asyncio
    import sys

    import psutil

    from buwubot.video_media import run_media
    pidfile = tmp_path/'child.pid'
    script = 'import os,time,pathlib;pathlib.Path(__import__("sys").argv[1]).write_text(str(os.getpid()));time.sleep(60)'
    task = asyncio.create_task(run_media(sys.executable,'-c',script,pidfile))
    try:
        async with asyncio.timeout(5):
            while not pidfile.is_file():
                await asyncio.sleep(.02)
        pid = int(pidfile.read_text())
        assert psutil.pid_exists(pid)
        child = psutil.Process(pid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Windows can retain a dead PID while pipe handles are being closed.
        await asyncio.to_thread(child.wait,timeout=2)
        assert not child.is_running()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)


async def test_media_download_resumes_partial_stream_and_refuses_mismatched_ranges(tmp_path):
    import hashlib

    import httpx

    from buwubot.learning import SourceUnavailable
    from buwubot.video_media import TimelineSource
    canonical = 'https://www.bilibili.com/video/BV1234567890/'
    folder = tmp_path/'operation'
    folder.mkdir()
    cache = tmp_path/'streams'/hashlib.sha256(f'{canonical}:1'.encode()).hexdigest()
    cache.mkdir(parents=True)
    (cache/'source.part').write_bytes(b'first')
    calls = []
    async def handle(request):
        calls.append(request.headers['range'])
        return httpx.Response(206,headers={'Content-Range':'bytes 5-8/9'},content=b'last')
    source = TimelineSource(folder,media=None,asr_python='',asr_model_dir='',worker='')
    await source.http.aclose()
    source.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        path = await source._download('https://cdn.bilivideo.com/video.mp4',canonical,1)
        assert path.read_bytes() == b'firstlast' and calls == ['bytes=5-']
        path.unlink()
        (cache/'source.part').write_bytes(b'wrong-offset')
        with pytest.raises(SourceUnavailable):
            await source._download('https://cdn.bilivideo.com/video.mp4',canonical,1)
        assert not path.exists()
    finally:
        await source.close()


async def test_next_interval_reuses_verified_playback_metadata_without_new_playback_request(tmp_path):
    import hashlib
    import json

    import httpx

    from buwubot.video_media import TimelineSource
    canonical = 'https://www.bilibili.com/video/BV1234567890/'
    folder = tmp_path/'next-operation'
    folder.mkdir()
    cache = tmp_path/'streams'/hashlib.sha256(f'{canonical}:1'.encode()).hexdigest()
    cache.mkdir(parents=True)
    (cache/'source.mp4').write_bytes(b'verified download')
    (cache/'metadata.json').write_text(json.dumps({'video_id':'BV1234567890','canonical':canonical,
        'part':1,'title':'actual title','uploader':'author','duration':78,
        'stream':'https://cdn.bilivideo.com/video.mp4'}))
    async def handle(request):
        pytest.fail('a new playback request is unnecessary for an already downloaded video')
    source = TimelineSource(folder,media=None,asr_python='',asr_model_dir='',worker='')
    await source.http.aclose()
    source.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        identity,canonical,metadata,duration,_stream = await source._playback(canonical,1,24)
        assert identity == 'BV1234567890' and duration == 78 and metadata['title'] == 'actual title'
    finally:
        await source.close()


def test_detailed_video_sheets_fit_budget_without_dropping_timeline_frames(tmp_path):
    import hashlib
    import random
    from io import BytesIO
    from types import SimpleNamespace

    from PIL import Image

    from buwubot.video_media import TimelineSource

    randomizer = random.Random(7)
    paths = []
    for i in range(12):
        path = tmp_path/f'{i}.png'
        Image.frombytes('RGB',(384,216),randomizer.randbytes(384*216*3)).save(path)
        paths.append(path)
    assets = {}
    def save(content):
        assert 0 < len(content) <= 32000
        digest = hashlib.sha256(content).hexdigest()
        assets[digest] = content
        return {'hash':digest}
    frames = TimelineSource._sheets(SimpleNamespace(media=SimpleNamespace(save=save)),paths,24)
    assert [timestamp for frame in frames for timestamp in frame.frame_times] == list(range(24,36))
    assert len(frames)==2 and len(assets)==2
    for content in assets.values():
        with Image.open(BytesIO(content)) as sheet:
            assert 576 <= sheet.width <= 1152
            assert any(high>low for low,high in sheet.getextrema())
