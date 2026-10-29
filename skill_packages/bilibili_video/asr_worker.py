"""Optional isolated CPU speech extractor; local weights only, no model downloads."""

import json
import math
import sys
import wave
from pathlib import Path


def transcribe(audio, model_dir):
    audio, model_dir = Path(audio), Path(model_dir)
    weights = model_dir / 'model.bin'
    if not weights.is_file() or not 0 < weights.stat().st_size <= 100_000_000:
        raise ValueError('a prepared local model below 100MB is required')
    with wave.open(str(audio), 'rb') as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1,2,16000):
            raise ValueError('expected mono 16kHz PCM audio')
        duration = stream.getnframes() / 16000
        if not 0 < duration <= 30:
            raise ValueError('audio interval exceeds worker limit')
        pcm = stream.readframes(stream.getnframes())
        if len(pcm) != round(duration * 16000) * 2:
            raise ValueError('incomplete PCM audio')
    import numpy as np
    from faster_whisper import WhisperModel
    model = WhisperModel(str(model_dir), device='cpu', compute_type='int8', cpu_threads=1,
                         num_workers=1, local_files_only=True)
    # FFmpeg already decoded PCM. Avoid a second decode and PyAV version skew.
    samples = np.frombuffer(pcm,dtype='<i2').astype(np.float32) / 32768.0
    # The task needs speech for this interval, not generated timestamp tokens
    # from Whisper's padded 30-second window. Keep actual-audio bounds strict.
    segments, info = model.transcribe(samples, language=None, beam_size=1,
        vad_filter=True, condition_on_previous_text=False, without_timestamps=True)
    speech = []
    for segment in segments:
        start, end, text = float(segment.start), float(segment.end), segment.text.strip()
        if (not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start < duration or not start < end <= duration + .25
                or len(text) > 8000 or len(speech) >= 2000):
            raise ValueError('invalid transcription interval')
        if text:
            speech.append({'start':start,'end':min(end,duration),'text':text})
    return {'audio_status':'transcribed' if speech else 'no_speech_detected', 'speech':speech,
            'language':info.language, 'language_probability':info.language_probability}


if __name__ == '__main__':
    try:
        result = transcribe(sys.argv[1],sys.argv[2])
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    except (ValueError, OSError, ImportError, RuntimeError, TypeError):
        # No paths, raw decoder errors or credentials go back to the language model.
        print(json.dumps({'error':'本地语音转写暂不可用，保留片段等待恢复'},ensure_ascii=False))
        sys.exit(1)
