import importlib.util
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker():
    path = Path(__file__).parents[1] / 'skill_packages/bilibili_video/asr_worker.py'
    spec = importlib.util.spec_from_file_location('video_asr_worker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def audio(tmp_path):
    path = tmp_path / 'audio.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * 32000)
    model = tmp_path / 'model'
    model.mkdir()
    (model / 'model.bin').write_bytes(b'prepared local weights')
    return path, model


def fake_model(monkeypatch, segments):
    calls = []
    class Whisper:
        def __init__(self, path, **kwargs):
            calls.append((path, kwargs))
        def transcribe(self, path, **kwargs):
            calls.append((path, kwargs))
            return iter(segments), SimpleNamespace(language='zh', language_probability=.95)
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Whisper))
    return calls


def test_asr_preserves_relative_timestamps_and_uses_only_local_single_thread_cpu(worker, tmp_path, monkeypatch):
    path, model = audio(tmp_path)
    calls = fake_model(monkeypatch, [SimpleNamespace(start=.2,end=1.7,text='  实际说话  ')])
    result = worker.transcribe(path, model)
    assert result == {'audio_status':'transcribed', 'speech':[{'start':.2,'end':1.7,'text':'实际说话'}],
                      'language':'zh', 'language_probability':.95}
    assert calls[0][1] == {'device':'cpu','compute_type':'int8','cpu_threads':1,'num_workers':1,
                         'local_files_only':True}
    assert calls[1][1]['vad_filter'] is True


def test_asr_empty_result_does_not_claim_silence(worker, tmp_path, monkeypatch):
    path, model = audio(tmp_path)
    fake_model(monkeypatch, [])
    assert worker.transcribe(path,model)['audio_status'] == 'no_speech_detected'


def test_asr_uses_interval_decode_instead_of_generated_padded_timestamps(worker,tmp_path,monkeypatch):
    path, model = audio(tmp_path)
    class Whisper:
        def __init__(self, *args, **kwargs):
            pass
        def transcribe(self, samples, **kwargs):
            # Whisper timestamp tokens may extend beyond actual short-clip audio.
            end = 2 if kwargs.get('without_timestamps') else 4
            return iter([SimpleNamespace(start=0,end=end,text='speech in this interval')]), \
                SimpleNamespace(language='en',language_probability=.95)
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=Whisper))
    assert worker.transcribe(path,model)['speech'] == [
        {'start':0,'end':2,'text':'speech in this interval'}]


@pytest.mark.parametrize('segment', [
    SimpleNamespace(start=-1,end=1,text='invalid'),
    SimpleNamespace(start=1,end=3,text='invalid'),
    SimpleNamespace(start=1,end=.5,text='invalid'),
    SimpleNamespace(start=float('nan'),end=1,text='invalid'),
])
def test_invalid_asr_intervals_are_not_accepted(worker,tmp_path,monkeypatch,segment):
    path,model = audio(tmp_path)
    fake_model(monkeypatch,[segment])
    with pytest.raises(ValueError):
        worker.transcribe(path,model)


def test_missing_model_does_not_trigger_download_or_import(worker,tmp_path,monkeypatch):
    path,model = audio(tmp_path)
    (model/'model.bin').unlink()
    fake_model(monkeypatch,[])
    with pytest.raises(ValueError,match='prepared'):
        worker.transcribe(path,model)


def test_asr_bounded_end_rounding_is_clamped_to_actual_audio(worker,tmp_path,monkeypatch):
    path,model = audio(tmp_path)
    fake_model(monkeypatch,[SimpleNamespace(start=1,end=2.12,text='speech')])
    assert worker.transcribe(path,model)['speech'] == [{'start':1,'end':2,'text':'speech'}]
